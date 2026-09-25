"""PdfDocument: an in-memory PDF with a chainable, non-mutating API."""

from __future__ import annotations

import asyncio
import io
import secrets
import warnings
from collections.abc import Iterable, Iterator, Mapping, Sequence
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import pikepdf
from pydantic import SecretStr

from dravenpdf.document import forms as form_ops
from dravenpdf.document import images as image_ops
from dravenpdf.document import pages as ops
from dravenpdf.document import signing as sign_ops
from dravenpdf.document import stamp as stamp_ops
from dravenpdf.document import text as text_ops
from dravenpdf.document.forms import FieldValue, FormField
from dravenpdf.document.images import ImageFormat
from dravenpdf.document.signing import SignatureBox, SignatureInfo, SigningKey
from dravenpdf.document.stamp import Position
from dravenpdf.errors import (
    InvalidPdfError,
    PdfOperationError,
    PdfPasswordError,
    SignatureInvalidatedWarning,
    SigningError,
)
from dravenpdf.options import Margins, PaperSize, RenderOptions

if TYPE_CHECKING:
    from dravenpdf.render.report import RenderReport


class HtmlRenderer(Protocol):
    """What stamp_html needs from a renderer (AsyncRenderer fits)."""

    async def from_html(
        self, html: str, options: RenderOptions | None = None, *, base_url: str | None = None
    ) -> PdfDocument: ...


# PdfDocument.metadata keys and the docinfo entries they map to.
METADATA_KEYS: dict[str, str] = {
    "title": "/Title",
    "author": "/Author",
    "subject": "/Subject",
    "keywords": "/Keywords",
    "creator": "/Creator",
    "producer": "/Producer",
    "creation_date": "/CreationDate",
    "mod_date": "/ModDate",
}
_EDITABLE = ("title", "author", "subject", "keywords", "creator", "producer")


def _has_signature_values(pdf: pikepdf.Pdf) -> bool:
    """Any signature field that has been signed (cheap check, no validation)."""
    acroform = pdf.Root.get("/AcroForm")
    if not isinstance(acroform, pikepdf.Dictionary):
        return False
    pending = list(acroform.get("/Fields", []))
    seen = 0
    while pending and seen < 10_000:
        seen += 1
        field = pending.pop()
        if not isinstance(field, pikepdf.Dictionary):
            continue
        if field.get("/FT") == pikepdf.Name.Sig and "/V" in field:
            return True
        pending.extend(field.get("/Kids", []))
    return False


def _plain(value: str | SecretStr) -> str:
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def _as_pdf(document: PdfDocument | bytes) -> pikepdf.Pdf:
    if isinstance(document, PdfDocument):
        return document._pdf
    return PdfDocument.from_bytes(document)._pdf


class PdfDocument:
    """A PDF held in memory, backed by :class:`pikepdf.Pdf`.

    Every operation returns a **new** PdfDocument and leaves this one unchanged,
    so calls chain (``doc.rotate(90).delete([0]).save(...)``) and a document can be
    shared safely. A single PdfDocument is not safe to use from several threads
    at once.

    Page numbers: integer arguments are 0-based (negative counts from the end);
    range strings like ``"1-3,5,8-"`` are 1-based.
    """

    def __init__(self, pdf: pikepdf.Pdf) -> None:
        self._pdf = pdf
        # Applied when writing (see encrypt()); carried to derived documents.
        self._encryption: pikepdf.Encryption | None = None
        self.was_encrypted = False
        """True if this document was opened from a password-protected file."""
        # The exact bytes this document was read from, while it is unmodified: written
        # back as-is, so signatures and incremental updates survive.
        self._source: bytes | None = None
        self._signed = False
        self.render_report: RenderReport | None = None
        """Set on documents returned by a renderer: what failed while rendering.
        Documents derived from this one (rotate, merge, ...) don't carry it."""

    # ------------------------------------------------------------------ loading

    @classmethod
    def from_bytes(cls, data: bytes, *, password: str | SecretStr | None = None) -> PdfDocument:
        """Read a PDF. Password-protected files need ``password`` (user or owner).

        Opening decrypts: the document and anything derived from it are written
        unencrypted unless you call :meth:`encrypt`.
        """
        secret = password.get_secret_value() if isinstance(password, SecretStr) else password
        try:
            pdf = pikepdf.open(io.BytesIO(data), password=secret or "")
        except pikepdf.PasswordError:
            message = "wrong password for this PDF" if secret else "this PDF needs a password"
            raise PdfPasswordError(message) from None  # no chaining: keep secrets out
        except pikepdf.PdfError as exc:
            raise InvalidPdfError(f"not a readable PDF: {exc}") from exc
        doc = cls(pdf)
        doc.was_encrypted = pdf.is_encrypted
        if not pdf.is_encrypted:  # opening decrypts, so encrypted input is rewritten
            doc._source = data
            doc._signed = _has_signature_values(pdf)
        return doc

    @classmethod
    def open(
        cls, path: str | PathLike[str], *, password: str | SecretStr | None = None
    ) -> PdfDocument:
        return cls.from_bytes(Path(path).read_bytes(), password=password)

    @classmethod
    def from_images(
        cls,
        images: Sequence[bytes],
        *,
        paper: PaperSize | None = None,
        landscape: bool = False,
        margin: float = 0,
    ) -> PdfDocument:
        """One page per image. Without ``paper``, pages are the images' own size;
        with it, images are fitted inside ``margin`` (points), keeping aspect ratio."""
        return cls.from_bytes(
            image_ops.images_to_pdf(images, paper=paper, landscape=landscape, margin=margin)
        )

    @classmethod
    def merge(cls, documents: Iterable[PdfDocument | bytes]) -> PdfDocument:
        """All pages of each document, in order. Metadata, and pending encryption,
        come from the first."""
        items = list(documents)
        merged = cls(ops.merge([_as_pdf(d) for d in items]))
        if items and isinstance(items[0], PdfDocument):
            merged._encryption = items[0]._encryption
        return merged

    # ------------------------------------------------------------------ reading

    @property
    def page_count(self) -> int:
        return len(self._pdf.pages)

    def __len__(self) -> int:
        return self.page_count

    @property
    def metadata(self) -> dict[str, str]:
        """Document info (title, author, ...), only the fields that are set."""
        docinfo = self._pdf.docinfo
        result = {}
        for name, key in METADATA_KEYS.items():
            if key in docinfo:
                result[name] = str(docinfo[key])
        return result

    def page_size(self, index: int = 0) -> tuple[float, float]:
        """(width, height) of a page in points, as displayed (rotation applied)."""
        (i,) = ops.normalize_indices([index], self.page_count)
        page = self._pdf.pages[i]
        box = [float(v) for v in page.mediabox]
        width, height = abs(box[2] - box[0]), abs(box[3] - box[1])
        rotation = int(page.rotation) % 360
        return (height, width) if rotation in (90, 270) else (width, height)

    # ------------------------------------------------------------------ page operations

    def extract(self, ranges: str) -> PdfDocument:
        """A new document with the pages in ``ranges`` (1-based, e.g. ``"2-5"``)."""
        return self._derive(ops.extract(self._pdf, ranges))

    def split(
        self, *, every: int | None = None, ranges: Sequence[str] | None = None
    ) -> list[PdfDocument]:
        """Split into chunks of ``every`` pages, or one document per range string.

        Holds every part in memory; use :meth:`iter_split` for large splits.
        """
        return list(self.iter_split(every=every, ranges=ranges))

    def iter_split(
        self, *, every: int | None = None, ranges: Sequence[str] | None = None
    ) -> Iterator[PdfDocument]:
        """Like :meth:`split`, but builds each part only when it is asked for.

        Arguments are checked immediately (bad ones raise here, not mid-iteration);
        the parts are then built lazily, so a caller that saves and drops each one
        holds a single part at a time. Don't use this document from another thread
        while iterating.
        """
        plan = ops.plan_split(self.page_count, every=every, ranges=ranges)
        # map() rather than a generator expression: it keeps no reference to the
        # previous part while the next one is built.
        return map(self._derive, ops.iter_parts(self._pdf, plan))

    def rotate(self, degrees: int, pages: Iterable[int] | None = None) -> PdfDocument:
        """Rotate clockwise by a multiple of 90 degrees; all pages unless ``pages`` given."""
        return self._derive(ops.rotate(self._pdf, degrees, pages))

    def delete(self, pages: Iterable[int]) -> PdfDocument:
        """Remove the given pages (0-based)."""
        return self._derive(ops.delete(self._pdf, pages))

    def reorder(self, order: Sequence[int]) -> PdfDocument:
        """Put pages in a new order, e.g. ``[2, 0, 1]``; must name every page once."""
        return self._derive(ops.reorder(self._pdf, order))

    def insert(self, other: PdfDocument | bytes, at: int) -> PdfDocument:
        """Insert all pages of ``other`` before page ``at``; ``at=page_count`` appends."""
        return self._derive(ops.insert(self._pdf, _as_pdf(other), at))

    def set_metadata(
        self,
        *,
        title: str | None = None,
        author: str | None = None,
        subject: str | None = None,
        keywords: str | None = None,
        creator: str | None = None,
        producer: str | None = None,
    ) -> PdfDocument:
        """Set document info fields. ``None`` leaves a field alone; ``""`` removes it."""
        values = dict(
            title=title, author=author, subject=subject,
            keywords=keywords, creator=creator, producer=producer,
        )  # fmt: skip
        result = ops.clone(self._pdf)
        for name in _EDITABLE:
            value = values[name]
            if value is None:
                continue
            key = METADATA_KEYS[name]
            if value == "":
                if key in result.docinfo:
                    del result.docinfo[key]
            else:
                result.docinfo[key] = value
        if "/Metadata" in result.Root:  # keep XMP in step so viewers agree
            with result.open_metadata(set_pikepdf_as_editor=False) as xmp:
                xmp.load_from_docinfo(result.docinfo, delete_missing=True)
        return self._derive(result)

    # ------------------------------------------------------------------ stamps

    def stamp_text(
        self,
        text: str,
        *,
        font_size: float = 48,
        color: str = "#FF0000",
        opacity: float = 0.3,
        angle: float = 45,
        position: Position = "center",
        margin: float = 36,
        pages: Iterable[int] | None = None,
        under: bool = False,
    ) -> PdfDocument:
        """Text watermark in Helvetica, ``angle`` degrees counter-clockwise.

        Western European (cp1252) characters only; use :meth:`stamp_html` for other
        scripts or richer styling. ``margin`` (points) applies to non-center positions.
        """
        result = ops.clone(self._pdf)
        stamp_ops.stamp_text(
            result, text, font_size=font_size, color=color, opacity=opacity, angle=angle,
            position=position, margin=margin, pages=pages, under=under,
        )  # fmt: skip
        return self._derive(result)

    def stamp_image(
        self,
        image: bytes,
        *,
        width: float | None = None,
        position: Position = "center",
        margin: float = 36,
        opacity: float = 1.0,
        pages: Iterable[int] | None = None,
        under: bool = False,
    ) -> PdfDocument:
        """Image stamp (PNG, JPEG, ...; transparency kept). ``width`` in points,
        default the image's size at 96 dpi; shrunk to fit inside the margins."""
        result = ops.clone(self._pdf)
        stamp_ops.stamp_image(
            result, image, width=width, position=position, margin=margin,
            opacity=opacity, pages=pages, under=under,
        )  # fmt: skip
        return self._derive(result)

    def overlay(
        self,
        stamp: PdfDocument | bytes,
        *,
        stamp_page: int = 0,
        opacity: float = 1.0,
        pages: Iterable[int] | None = None,
        under: bool = False,
    ) -> PdfDocument:
        """Draw a page of another PDF (letterhead, form background, ...) on each page,
        scaled to fit and centered. ``under=True`` puts it behind the content."""
        result = ops.clone(self._pdf)
        stamp_ops.overlay_page(
            result, _as_pdf(stamp), stamp_page=stamp_page, opacity=opacity,
            pages=pages, under=under,
        )  # fmt: skip
        return self._derive(ops._detach(result))

    async def stamp_html(
        self,
        renderer: HtmlRenderer,
        html: str,
        *,
        opacity: float = 1.0,
        pages: Iterable[int] | None = None,
        under: bool = False,
        base_url: str | None = None,
    ) -> PdfDocument:
        """Render ``html`` at each target page's size and draw it on the page.

        The HTML page is transparent except for what it draws, so it works for
        watermarks, headers, "PAID" badges and letterheads in any language.
        """
        targets = stamp_ops.target_pages(self._pdf, pages)
        by_size: dict[tuple[float, float], list[int]] = {}
        for index in targets:
            size = self.page_size(index)
            by_size.setdefault((round(size[0], 2), round(size[1], 2)), []).append(index)
        result = self
        for (width, height), indices in by_size.items():
            stamp = await renderer.from_html(
                html,
                RenderOptions(
                    width=f"{width / 72:.4f}in",
                    height=f"{height / 72:.4f}in",
                    margins=Margins(top="0", right="0", bottom="0", left="0"),
                ),
                base_url=base_url,
            )
            # Overlaying is CPU-bound pikepdf work; keep it off the event loop.
            result = await asyncio.to_thread(
                result.overlay, stamp, opacity=opacity, pages=indices, under=under
            )
        return result

    # ------------------------------------------------------------------ images and text

    def to_images(
        self,
        *,
        dpi: int = 150,
        fmt: ImageFormat = "png",
        pages: Iterable[int] | None = None,
        jpeg_quality: int = 85,
        max_pixels: int | None = None,
        max_total_bytes: int | None = None,
    ) -> list[bytes]:
        """Render pages (0-based ``pages``, default all) to PNG or JPEG bytes.

        ``max_pixels`` (per page) and ``max_total_bytes`` (all images) raise
        :class:`LimitExceededError` when passed; see ``images.pdf_to_images``.
        """
        return image_ops.pdf_to_images(
            self.to_bytes(),
            dpi=dpi,
            fmt=fmt,
            pages=pages,
            jpeg_quality=jpeg_quality,
            max_pixels=max_pixels,
            max_total_bytes=max_total_bytes,
        )

    def extract_text(self) -> list[str]:
        """Text of each page. Scanned pages have none (no OCR)."""
        return text_ops.extract_text(self.to_bytes())

    # ------------------------------------------------------------------ forms

    def form_fields(self) -> list[FormField]:
        """The form's fields (empty if the PDF has no form)."""
        return form_ops.list_fields(self._pdf)

    def fill_form(self, values: Mapping[str, FieldValue], *, flatten: bool = False) -> PdfDocument:
        """Fill form fields by name: text for text and choice fields, True/False for
        checkboxes, an option name for radio groups. Everything is checked first, so
        either all values apply or none (``PdfOperationError``). ``flatten=True`` also
        burns the values into the pages and removes the form."""
        result = ops.clone(self._pdf)
        form_ops.fill(result, values, flatten=flatten)
        return self._derive(result)

    def flatten_form(self) -> PdfDocument:
        """Burn the current field values into the pages and remove the form."""
        result = ops.clone(self._pdf)
        form_ops.flatten_form(result)
        return self._derive(result)

    # ------------------------------------------------------------------ signatures

    @property
    def is_signed(self) -> bool:
        """True if the file this document was read from has filled signature fields."""
        return self._signed

    def sign(
        self,
        key: SigningKey,
        *,
        field_name: str = "Signature",
        reason: str | None = None,
        location: str | None = None,
        contact: str | None = None,
        box: SignatureBox | None = None,
        timestamp_url: str | None = None,
    ) -> PdfDocument:
        """Digitally sign (PAdES). Returns the signed document; write it with
        ``to_bytes()`` / ``save()`` and don't change it afterwards, since any change
        writes a new file and invalidates the signature. ``box`` makes the signature
        visible; ``timestamp_url`` adds an RFC 3161 timestamp. Sign last: after
        filling, stamping and so on, and without pending encryption."""
        if self._encryption is not None:
            raise SigningError(
                "can't sign a document with pending encryption: encrypting after signing "
                "would rewrite the file; sign an unencrypted document"
            )
        signed = sign_ops.sign(
            self.to_bytes(), key, field_name=field_name, reason=reason, location=location,
            contact=contact, box=box, timestamp_url=timestamp_url,
        )  # fmt: skip
        return PdfDocument.from_bytes(signed)

    def verify_signatures(self, trust_roots: Iterable[bytes] = ()) -> list[SignatureInfo]:
        """Check each embedded signature. ``trust_roots`` are PEM or DER certificates
        (e.g. your CA); without them no signature counts as ``trusted``."""
        return sign_ops.verify(self.to_bytes(), sign_ops.load_certificates(trust_roots))

    # ------------------------------------------------------------------ encryption

    def encrypt(
        self,
        *,
        user_password: str | SecretStr = "",
        owner_password: str | SecretStr | None = None,
        allow_print: bool = True,
        allow_copy: bool = True,
        allow_modify: bool = True,
        allow_annotate: bool = True,
        allow_forms: bool = True,
    ) -> PdfDocument:
        """A copy that is written encrypted (AES-256).

        ``user_password`` is needed to open the file ("" means anyone can open it).
        ``owner_password`` unlocks full access; if omitted, a random one is used, so
        the restrictions can't be lifted with a password. The ``allow_*`` flags are
        honoured by well-behaved viewers only: anyone who can open the file can
        technically copy or print it. Use a user password to actually protect content.
        Operations on the result keep its encryption.
        """
        user = _plain(user_password)
        owner = _plain(owner_password) if owner_password is not None else secrets.token_urlsafe(32)
        if not owner:
            raise PdfOperationError("owner_password must not be empty")
        permissions = pikepdf.Permissions(
            accessibility=True,
            extract=allow_copy,
            modify_annotation=allow_annotate,
            modify_assembly=allow_modify,
            modify_form=allow_forms,
            modify_other=allow_modify,
            print_lowres=allow_print,
            print_highres=allow_print,
        )
        result = self._derive(ops.clone(self._pdf))
        result._encryption = pikepdf.Encryption(owner=owner, user=user, allow=permissions)
        return result

    def decrypt(self) -> PdfDocument:
        """A copy that is written without encryption."""
        result = self._derive(ops.clone(self._pdf))
        result._encryption = None
        return result

    @property
    def is_encrypted(self) -> bool:
        """True if :meth:`to_bytes` / :meth:`save` will write an encrypted file."""
        return self._encryption is not None

    def _derive(self, pdf: pikepdf.Pdf) -> PdfDocument:
        """A document made from this one: keeps its pending encryption."""
        if self._signed:
            warnings.warn(
                "this document is digitally signed; the change writes a new file, "
                "which invalidates its signatures",
                SignatureInvalidatedWarning,
                stacklevel=3,
            )
        doc = PdfDocument(pdf)
        doc._encryption = self._encryption
        return doc

    def copy(self) -> PdfDocument:
        return self._derive(ops.clone(self._pdf))

    # ------------------------------------------------------------------ output

    def to_bytes(self, *, compress: bool = False) -> bytes:
        """Serialize to PDF bytes.

        A document that was read and not changed is returned byte for byte, so
        digital signatures stay valid. Otherwise the file is written anew, with
        uncompressed streams compressed. ``compress=True`` also drops unused page
        resources, recompresses streams at the highest level and packs objects into
        object streams, which pays off on larger documents (and rewrites the file).
        """
        if self._source is not None and self._encryption is None and not compress:
            return self._source
        if compress and self._signed:
            warnings.warn(
                "compressing rewrites the file, which invalidates its signatures",
                SignatureInvalidatedWarning,
                stacklevel=2,
            )
        buffer = io.BytesIO()
        encryption: pikepdf.Encryption | bool = self._encryption or False
        if compress:
            pdf = ops.clone(self._pdf)
            pdf.remove_unreferenced_resources()
            pdf.save(
                buffer,
                compress_streams=True,
                recompress_flate=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                encryption=encryption,
            )
        else:
            self._pdf.save(buffer, encryption=encryption)
        return buffer.getvalue()

    def save(self, path: str | PathLike[str], *, compress: bool = False) -> None:
        Path(path).write_bytes(self.to_bytes(compress=compress))

    def __repr__(self) -> str:
        return f"<PdfDocument pages={self.page_count}>"
