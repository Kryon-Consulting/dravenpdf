"""Digital signatures with pyHanko: sign with a PKCS#12 key, verify signatures.

Signing appends an incremental update to the document's exact bytes (PAdES
baseline, optionally with an RFC 3161 timestamp). Anything that rewrites the file
afterwards breaks the signature, so :class:`~dravenpdf.document.pdf.PdfDocument`
hands back a signed document's original bytes untouched. An operation that writes a
new file removes the signatures first (:func:`strip_signatures`) and warns, so the
result is a plain unsigned file rather than one carrying broken signatures.

These are advanced electronic signatures. EU *qualified* signatures need certified
signing hardware and a qualified provider, which is out of scope (decision D12).
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from os import PathLike
from pathlib import Path

import pikepdf
from asn1crypto import x509 as asn1_x509
from pydantic import SecretStr
from pyhanko.pdf_utils.crypt import AuthStatus
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign import fields, signers, timestamps
from pyhanko.sign.general import SigningError as PyhankoSigningError
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko.sign.validation.status import SignatureCoverageLevel
from pyhanko_certvalidator import ValidationContext

from dravenpdf.errors import PdfPasswordError, SigningError


def _drop_traceback(record: logging.LogRecord) -> bool:
    # pyHanko logs a full traceback whenever it can't build a trust chain, which is
    # the normal case for a certificate that isn't in the trust roots. The result
    # already says "not trusted"; keep the one-line warning, drop the traceback.
    record.exc_info = None
    record.exc_text = None
    return True


logging.getLogger("pyhanko.sign.validation.generic_cms").addFilter(_drop_traceback)


_MAX_FIELD_NODES = 10_000  # bounds the walk over a (possibly cyclic) field tree


def _field_nodes(pdf: pikepdf.Pdf) -> list[tuple[pikepdf.Array, pikepdf.Dictionary, object]]:
    """Every node of the form's field tree as (containing array, node, field type).

    The field type is inherited from parents, as in the PDF spec. Signed signature
    fields are not descended into: their kids are widgets.
    """
    acroform = pdf.Root.get("/AcroForm")
    if not isinstance(acroform, pikepdf.Dictionary):
        return []
    top = acroform.get("/Fields")
    if not isinstance(top, pikepdf.Array):
        return []
    nodes: list[tuple[pikepdf.Array, pikepdf.Dictionary, object]] = []
    pending: list[tuple[pikepdf.Array, object]] = [(top, None)]
    while pending and len(nodes) < _MAX_FIELD_NODES:
        array, inherited = pending.pop()
        for node in array:
            if not isinstance(node, pikepdf.Dictionary):
                continue
            kind = node.get("/FT", inherited)
            nodes.append((array, node, kind))
            kids = node.get("/Kids")
            signed = kind == pikepdf.Name.Sig and "/V" in node
            if not signed and isinstance(kids, pikepdf.Array):
                pending.append((kids, kind))
    return nodes


def _filled_signature_fields(pdf: pikepdf.Pdf) -> list[tuple[pikepdf.Array, pikepdf.Dictionary]]:
    return [
        (array, node)
        for array, node, kind in _field_nodes(pdf)
        if kind == pikepdf.Name.Sig and "/V" in node
    ]


def has_signature_values(pdf: pikepdf.Pdf) -> bool:
    """Any signature field that has been signed (cheap check, no validation)."""
    return bool(_filled_signature_fields(pdf))


def _same(a: pikepdf.Object, b: pikepdf.Object) -> bool:
    if a.is_indirect or b.is_indirect:
        return a.is_indirect and b.is_indirect and a.objgen == b.objgen
    return bool(a == b)


def strip_signatures(pdf: pikepdf.Pdf) -> bool:
    """Remove every signed signature field from ``pdf``, in place; True if any were.

    For a file that is about to be written anew, where the signatures would be broken
    anyway. Removes the fields, their widgets (so a visible signature no longer shows
    on the page), the document's signature permissions (``/Perms``: DocMDP, UR3) and
    its validation data (``/DSS``). Other form fields, and unsigned signature fields
    waiting to be signed, stay.
    """
    filled = _filled_signature_fields(pdf)
    if not filled:
        return False
    removed: set[tuple[int, int]] = set()
    for array, field in filled:
        for index in reversed(range(len(array))):
            if _same(array[index], field):
                del array[index]
        if field.is_indirect:
            removed.add(field.objgen)
        for kid in field.get("/Kids", []):
            if isinstance(kid, pikepdf.Dictionary) and kid.is_indirect:
                removed.add(kid.objgen)
    for page in pdf.pages:
        annots = page.obj.get("/Annots")
        if not isinstance(annots, pikepdf.Array):
            continue
        kept = [a for a in annots if not _belongs_to(a, removed)]
        if len(kept) == len(annots):
            continue
        if kept:
            page.obj.Annots = pikepdf.Array(kept)
        else:
            del page.obj["/Annots"]
    # A parent field whose only kids were signatures would be left empty.
    for array, node, _ in _field_nodes(pdf):
        kids = node.get("/Kids")
        if isinstance(kids, pikepdf.Array) and len(kids) == 0:
            for index in reversed(range(len(array))):
                if _same(array[index], node):
                    del array[index]
    root = pdf.Root
    for key in ("/Perms", "/DSS"):
        if key in root:
            del root[key]
    acroform = root.AcroForm
    remaining = _field_nodes(pdf)
    if not remaining:
        del root["/AcroForm"]
    elif any(kind == pikepdf.Name.Sig for _, _, kind in remaining):
        acroform.SigFlags = 1  # signature fields exist; no longer append-only
    elif "/SigFlags" in acroform:
        del acroform["/SigFlags"]
    return True


def _belongs_to(annot: pikepdf.Object, fields: set[tuple[int, int]]) -> bool:
    if not isinstance(annot, pikepdf.Dictionary):
        return False
    if annot.is_indirect and annot.objgen in fields:
        return True
    parent = annot.get("/Parent")
    return isinstance(parent, pikepdf.Dictionary) and parent.is_indirect and parent.objgen in fields


def _plain(value: str | SecretStr | None) -> str | None:
    return value.get_secret_value() if isinstance(value, SecretStr) else value


class SigningKey:
    """A private key and certificate chain loaded from a PKCS#12 (.p12 / .pfx) file.

    The passphrase is used once, to load the key, and not kept.
    """

    def __init__(self, signer: signers.SimpleSigner) -> None:
        if signer.signing_cert is None:
            raise SigningError("the PKCS#12 file has no certificate")
        self._signer = signer
        self._certificate = signer.signing_cert

    @classmethod
    def from_pkcs12(
        cls, source: bytes | str | PathLike[str], password: str | SecretStr | None = None
    ) -> SigningKey:
        data = source if isinstance(source, bytes) else Path(source).read_bytes()
        passphrase = _plain(password)
        try:
            signer = signers.SimpleSigner.load_pkcs12_data(
                data, other_certs=[], passphrase=passphrase.encode() if passphrase else None
            )
        except (ValueError, TypeError) as exc:
            raise SigningError(
                "could not load the PKCS#12 key: wrong password or not a PKCS#12 file"
            ) from exc
        if signer is None:
            raise SigningError("the PKCS#12 file has no key")
        return cls(signer)

    @property
    def subject(self) -> str:
        return str(self._certificate.subject.human_friendly)

    @property
    def not_after(self) -> datetime:
        expires: datetime = self._certificate.not_valid_after
        return expires

    def __repr__(self) -> str:
        return f"<SigningKey {self.subject!r}>"


@dataclass(frozen=True)
class SignatureBox:
    """Where a visible signature goes: 0-based page, and a rectangle in points
    measured from the page's bottom-left corner."""

    page: int
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class SignatureInfo:
    """One embedded signature, as :meth:`PdfDocument.verify_signatures` reports it."""

    field: str
    signer: str
    """The signing certificate's subject."""
    signed_at: datetime | None
    """The time the signer's machine claimed (not proof; see ``timestamped``)."""
    intact: bool
    """The signed bytes are unchanged."""
    valid: bool
    """The cryptographic signature checks out against the certificate."""
    trusted: bool
    """The certificate chains up to one of the given trust roots."""
    covers_whole_document: bool
    """Nothing was added after signing (False if later revisions exist)."""
    timestamped: bool
    reason: str | None
    location: str | None

    @property
    def ok(self) -> bool:
        """Intact, valid and trusted.

        Says nothing about later changes: an earlier signature in a file signed twice
        is ok, though it doesn't cover the whole document. Use
        :func:`signature_problems` to judge the document as a whole.
        """
        return self.intact and self.valid and self.trusted


def signature_problems(
    signatures: Sequence[SignatureInfo], *, require_trust: bool = True
) -> list[str]:
    """Why a document's signatures don't vouch for it; empty if they do.

    The rule: at least one signature, every one intact, valid and (unless
    ``require_trust`` is False) trusted, and at least one covering the whole file, so
    nothing was appended after the last signature. The CLI and HTTP ``verify`` use it.
    """
    if not signatures:
        return ["the file has no signatures"]
    problems = []
    for info in signatures:
        if not (info.intact and info.valid):
            problems.append(f"{info.field}: the signature is broken")
        elif require_trust and not info.trusted:
            problems.append(f"{info.field}: the signer is not trusted")
    if not any(info.covers_whole_document for info in signatures):
        problems.append("the file was changed after its last signature")
    return problems


def sign(
    data: bytes,
    key: SigningKey,
    *,
    field_name: str = "Signature",
    reason: str | None = None,
    location: str | None = None,
    contact: str | None = None,
    box: SignatureBox | None = None,
    timestamp_url: str | None = None,
) -> bytes:
    """Sign ``data`` and return the signed file (the original bytes plus an update)."""
    field_name = _free_field_name(data, field_name)
    metadata = signers.PdfSignatureMetadata(
        field_name=field_name,
        reason=reason,
        location=location,
        contact_info=contact,
        subfilter=fields.SigSeedSubFilter.PADES,
    )
    spec = fields.SigFieldSpec(field_name)
    if box is not None:
        spec = fields.SigFieldSpec(
            field_name,
            on_page=box.page,
            box=(
                round(box.x),
                round(box.y),
                round(box.x + box.width),
                round(box.y + box.height),
            ),
        )
    timestamper = timestamps.HTTPTimeStamper(timestamp_url) if timestamp_url else None
    try:
        writer = IncrementalPdfFileWriter(io.BytesIO(data))
        pdf_signer = signers.PdfSigner(
            metadata, signer=key._signer, timestamper=timestamper, new_field_spec=spec
        )
        output = pdf_signer.sign_pdf(writer)
    except (PyhankoSigningError, ValueError, OSError) as exc:
        raise SigningError(f"signing failed: {str(exc).splitlines()[0][:300]}") from exc
    assert isinstance(output, io.BytesIO)
    return output.getvalue()


def _free_field_name(data: bytes, wanted: str) -> str:
    """``wanted``, or ``wanted2``, ``wanted3``... if a field already has that name."""
    reader = PdfFileReader(io.BytesIO(data))
    taken = {s.field_name for s in reader.embedded_signatures}
    if wanted not in taken:
        return wanted
    number = 2
    while f"{wanted}{number}" in taken:
        number += 1
    return f"{wanted}{number}"


def load_certificates(pem_or_der: Iterable[bytes]) -> list[asn1_x509.Certificate]:
    """Parse trust-root certificates (PEM, possibly several per blob, or DER)."""
    from asn1crypto import pem

    certificates = []
    for blob in pem_or_der:
        try:
            ders = (
                [d for _, _, d in pem.unarmor(blob, multiple=True)] if pem.detect(blob) else [blob]
            )
            for der in ders:
                certificate = asn1_x509.Certificate.load(der)
                certificate.native  # parse now, so a bad file fails here  # noqa: B018
                certificates.append(certificate)
        except (ValueError, TypeError, OSError) as exc:
            raise SigningError("a trust root is not a certificate (PEM or DER)") from exc
    return certificates


def verify(
    data: bytes,
    trust_roots: Sequence[asn1_x509.Certificate] = (),
    *,
    password: str | SecretStr | None = None,
) -> list[SignatureInfo]:
    """Check every embedded signature. Without trust roots, ``trusted`` is False.

    An encrypted file is decrypted by pyHanko's reader (with ``password``, the user or
    owner password) and checked as it is: nothing is rewritten.
    """
    try:
        reader = PdfFileReader(io.BytesIO(data))
    except Exception as exc:
        raise SigningError(f"could not read the signatures: {str(exc)[:300]}") from exc
    if reader.encrypted:
        secret = _plain(password) or ""
        try:
            auth = reader.decrypt(secret).status
        except Exception:
            auth = AuthStatus.FAILED
        if auth == AuthStatus.FAILED:
            message = "wrong password for this PDF" if secret else "this PDF needs a password"
            raise PdfPasswordError(message) from None  # no chaining: keep secrets out
    try:
        embedded = list(reader.embedded_signatures)
    except Exception as exc:
        raise SigningError(f"could not read the signatures: {str(exc)[:300]}") from exc
    results = []
    for signature in embedded:
        # Explicit (possibly empty) trust roots: never the operating system's list,
        # so "trusted" means trusted by the roots the caller chose.
        context = ValidationContext(trust_roots=list(trust_roots))
        status = validate_pdf_signature(signature, context)
        sig_dict = signature.sig_object
        results.append(
            SignatureInfo(
                field=str(signature.field_name),  # a proxy object in encrypted files
                signer=str(status.signing_cert.subject.human_friendly),
                signed_at=status.signer_reported_dt,
                intact=bool(status.intact),
                valid=bool(status.valid),
                trusted=bool(status.trusted),
                covers_whole_document=status.coverage == SignatureCoverageLevel.ENTIRE_FILE,
                timestamped=status.timestamp_validity is not None,
                reason=_text(sig_dict.get("/Reason")),
                location=_text(sig_dict.get("/Location")),
            )
        )
    return results


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def has_signatures(data: bytes) -> bool:
    try:
        return bool(PdfFileReader(io.BytesIO(data)).embedded_signatures)
    except Exception:
        return False
