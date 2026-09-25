from __future__ import annotations

import http.server
import io
import json
import threading
import warnings
from collections.abc import Callable, Iterator
from functools import cache
from pathlib import Path

import pikepdf
import pytest
from asn1crypto import keys as asn1_keys
from asn1crypto import tsp
from asn1crypto import x509 as asn1_x509
from pyhanko.pdf_utils import generic as pdf_generic
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.sign import fields, signers
from pyhanko.sign.timestamps.dummy_client import DummyTimeStamper

from dravenpdf import (
    PdfDocument,
    PdfPasswordError,
    SignatureBox,
    SignatureInvalidatedWarning,
    SigningError,
    SigningKey,
    signature_problems,
)
from dravenpdf.document import forms as form_ops
from dravenpdf.document import signing as sign_ops
from forms_fixture import build_form
from signing_fixture import OWNER, USER, encrypted_then_signed, make_p12, make_tsa_material

PASSWORD = "p12-pass"


@cache
def material(name: str = "Test Signer") -> tuple[bytes, bytes]:
    return make_p12(name, PASSWORD)


@pytest.fixture
def key() -> SigningKey:
    return SigningKey.from_pkcs12(material()[0], PASSWORD)


@pytest.fixture
def pem() -> bytes:
    return material()[1]


def blank(pages: int = 1) -> PdfDocument:
    pdf = pikepdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(400, 400))
    buffer = io.BytesIO()
    pdf.save(buffer)
    return PdfDocument.from_bytes(buffer.getvalue())


def test_key(key: SigningKey) -> None:
    assert "Test Signer" in key.subject
    assert PASSWORD not in repr(key)
    assert key.not_after.year >= 2026


def test_wrong_key_password() -> None:
    with pytest.raises(SigningError, match="wrong password") as info:
        SigningKey.from_pkcs12(material()[0], "not-it")
    assert "not-it" not in str(info.value)


def test_sign_and_verify(key: SigningKey, pem: bytes) -> None:
    signed = blank().sign(key, reason="Approved", location="Berlin")

    (untrusted,) = signed.verify_signatures()
    (trusted,) = signed.verify_signatures([pem])
    assert signed.is_signed
    assert untrusted.intact
    assert untrusted.valid
    assert not untrusted.trusted  # no trust roots given
    assert trusted.trusted
    assert trusted.ok
    assert trusted.covers_whole_document
    assert "Test Signer" in trusted.signer
    assert (trusted.reason, trusted.location) == ("Approved", "Berlin")
    assert trusted.signed_at is not None


def test_signed_bytes_are_returned_untouched(key: SigningKey) -> None:
    signed = blank().sign(key)

    first = signed.to_bytes()
    assert signed.to_bytes() == first
    assert PdfDocument.from_bytes(first).to_bytes() == first  # round trip is exact
    (info,) = PdfDocument.from_bytes(first).verify_signatures()
    assert info.intact


def test_visible_signature(key: SigningKey) -> None:
    signed = blank(2).sign(key, box=SignatureBox(page=1, x=50, y=60, width=200, height=50))

    with pikepdf.open(io.BytesIO(signed.to_bytes())) as pdf:
        (widget,) = pdf.pages[1].obj.Annots
        assert [round(float(v)) for v in widget.Rect] == [50, 60, 250, 110]
        assert "/Annots" not in pdf.pages[0].obj


def test_second_signature_gets_its_own_field(key: SigningKey, pem: bytes) -> None:
    other = SigningKey.from_pkcs12(material("Second Signer")[0], PASSWORD)

    twice = blank().sign(key).sign(other)

    first, second = twice.verify_signatures([pem, material("Second Signer")[1]])
    assert (first.field, second.field) == ("Signature", "Signature2")
    assert first.intact
    assert second.intact
    assert not first.covers_whole_document  # a later revision was added
    assert second.covers_whole_document


def signed_visibly(key: SigningKey, pages: int = 2) -> PdfDocument:
    return blank(pages).sign(key, box=SignatureBox(page=0, x=50, y=60, width=200, height=50))


def assert_unsigned(data: bytes, *, password: str = "") -> None:
    """A plain file: no signature fields, widgets, permissions or validation data."""
    doc = PdfDocument.from_bytes(data, password=password or None)
    assert not doc.is_signed
    assert doc.verify_signatures(password=password or None) == []
    with pikepdf.open(io.BytesIO(data), password=password) as pdf:
        assert "/AcroForm" not in pdf.Root
        assert "/Perms" not in pdf.Root
        assert "/DSS" not in pdf.Root
        for page in pdf.pages:
            assert "/Annots" not in page.obj


CHANGES: dict[str, Callable[[PdfDocument], bytes]] = {
    "rotate": lambda d: d.rotate(90).to_bytes(),
    "delete": lambda d: d.delete([1]).to_bytes(),
    "reorder": lambda d: d.reorder([1, 0]).to_bytes(),
    "extract": lambda d: d.extract("1").to_bytes(),
    "split": lambda d: d.split(every=1)[0].to_bytes(),
    "iter_split": lambda d: next(d.iter_split(every=1)).to_bytes(),
    "metadata": lambda d: d.set_metadata(title="Changed").to_bytes(),
    "stamp_text": lambda d: d.stamp_text("DRAFT").to_bytes(),
    "overlay": lambda d: d.overlay(blank()).to_bytes(),
    "flatten": lambda d: d.flatten_form().to_bytes(),
    "compress": lambda d: d.to_bytes(compress=True),
    "decrypt": lambda d: d.decrypt().to_bytes(),
    "insert_into": lambda d: d.insert(blank(), 1).to_bytes(),
    "insert_signed": lambda d: blank().insert(d, 1).to_bytes(),
    "insert_signed_bytes": lambda d: blank().insert(d.to_bytes(), 0).to_bytes(),
    "merge_first": lambda d: PdfDocument.merge([d, blank()]).to_bytes(),
    "merge_second": lambda d: PdfDocument.merge([blank(), d.to_bytes()]).to_bytes(),
}


@pytest.mark.parametrize("change", CHANGES.values(), ids=CHANGES.keys())
def test_changing_a_signed_document_removes_its_signature(
    key: SigningKey, change: Callable[[PdfDocument], bytes]
) -> None:
    signed = signed_visibly(key)
    original = signed.to_bytes()

    with pytest.warns(SignatureInvalidatedWarning, match="removed") as record:
        data = change(signed)

    assert_unsigned(data)
    assert signed.to_bytes() == original  # the signed document itself is untouched
    assert signed.is_signed
    assert {Path(w.filename).name for w in record} == {Path(__file__).name}  # points at us


def test_derived_document_reports_unsigned_before_and_after_reopening(key: SigningKey) -> None:
    with pytest.warns(SignatureInvalidatedWarning):
        rotated = signed_visibly(key).rotate(90)

    assert not rotated.is_signed
    assert rotated.verify_signatures() == []
    assert not PdfDocument.from_bytes(rotated.to_bytes()).is_signed


def test_encrypting_a_signed_document_removes_its_signature(key: SigningKey) -> None:
    with pytest.warns(SignatureInvalidatedWarning):
        data = signed_visibly(key).encrypt(user_password="pw").to_bytes()

    assert_unsigned(data, password="pw")


def test_every_signature_is_removed(key: SigningKey) -> None:
    other = SigningKey.from_pkcs12(material("Second Signer")[0], PASSWORD)
    twice = signed_visibly(key).sign(other, box=SignatureBox(page=1, x=0, y=0, width=90, height=40))
    assert len(twice.verify_signatures()) == 2

    with pytest.warns(SignatureInvalidatedWarning):
        data = twice.rotate(90).to_bytes()

    assert_unsigned(data)


def test_other_form_fields_are_kept(key: SigningKey) -> None:
    signed = PdfDocument.from_bytes(build_form()).sign(key)

    with pytest.warns(SignatureInvalidatedWarning):
        filled = signed.fill_form({"name": "Ada"})

    reopened = PdfDocument.from_bytes(filled.to_bytes())
    by_name = {f.name: f for f in reopened.form_fields()}
    assert "Signature" not in by_name
    assert by_name["name"].value == "Ada"
    assert by_name["agree"].kind == "checkbox"
    assert not reopened.is_signed
    with pikepdf.open(io.BytesIO(filled.to_bytes())) as pdf:
        assert "/SigFlags" not in pdf.Root.AcroForm


def test_flattening_does_not_burn_in_the_signature(key: SigningKey) -> None:
    signed = PdfDocument.from_bytes(build_form()).sign(
        key, box=SignatureBox(page=0, x=200, y=20, width=180, height=60)
    )
    # Flattened as it is, the signature's appearance would become page content.
    with pikepdf.open(io.BytesIO(signed.to_bytes())) as raw:
        form_ops.flatten_form(raw)
        assert "Test Signer" in PdfDocument(raw).extract_text()[0]

    with pytest.warns(SignatureInvalidatedWarning):
        flat = signed.fill_form({"name": "Ada"}, flatten=True)

    text = flat.extract_text()[0]
    assert "Ada" in text
    assert "Test Signer" not in text
    assert_unsigned(flat.to_bytes())


def test_empty_signature_fields_are_kept(key: SigningKey) -> None:
    writer = IncrementalPdfFileWriter(io.BytesIO(blank().to_bytes()))
    fields.append_signature_field(writer, fields.SigFieldSpec("Later", box=(10, 10, 100, 50)))
    buffer = io.BytesIO()
    writer.write(buffer)
    signed = PdfDocument.from_bytes(buffer.getvalue()).sign(key)

    with pytest.warns(SignatureInvalidatedWarning):
        rotated = signed.rotate(90)

    assert not rotated.is_signed
    assert [f.name for f in rotated.form_fields()] == ["Later"]
    with pikepdf.open(io.BytesIO(rotated.to_bytes())) as pdf:
        assert pdf.Root.AcroForm.SigFlags == 1  # a signature field, not append-only
        assert len(pdf.pages[0].obj.Annots) == 1


def test_certification_permissions_are_removed(key: SigningKey) -> None:
    writer = IncrementalPdfFileWriter(io.BytesIO(blank().to_bytes()))
    metadata = signers.PdfSignatureMetadata(field_name="Cert", certify=True)
    certified = signers.PdfSigner(metadata, signer=key._signer).sign_pdf(writer)
    doc = PdfDocument.from_bytes(certified.getvalue())
    with pikepdf.open(io.BytesIO(doc.to_bytes())) as pdf:
        assert "/DocMDP" in pdf.Root.Perms

    with pytest.warns(SignatureInvalidatedWarning):
        data = doc.set_metadata(title="x").to_bytes()

    assert_unsigned(data)


def test_signatures_below_a_parent_field_and_validation_data(key: SigningKey) -> None:
    with pikepdf.open(io.BytesIO(signed_visibly(key).to_bytes())) as pdf:
        (field,) = pdf.Root.AcroForm.Fields
        parent = pdf.make_indirect(
            pikepdf.Dictionary(FT=pikepdf.Name.Sig, T=pikepdf.String("group"), Kids=[field])
        )
        del field["/FT"]  # inherited from the parent
        field.Parent = parent
        pdf.Root.AcroForm.Fields = pikepdf.Array([parent])
        pdf.Root.DSS = pikepdf.Dictionary()
        assert sign_ops.has_signature_values(pdf)

        assert sign_ops.strip_signatures(pdf)

        assert not sign_ops.has_signature_values(pdf)
        assert "/AcroForm" not in pdf.Root
        assert "/DSS" not in pdf.Root
        assert "/Annots" not in pdf.pages[0].obj


def test_copy_and_reading_keep_the_signature(key: SigningKey) -> None:
    signed = signed_visibly(key)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        copied = signed.copy()
        signed.extract_text()
        signed.to_images(dpi=20)
        signed.verify_signatures()

    assert copied.is_signed
    assert copied.to_bytes() == signed.to_bytes()


def test_opened_encrypted_signed_file_is_written_unsigned(key: SigningKey) -> None:
    buffer = io.BytesIO()
    with pikepdf.open(io.BytesIO(signed_visibly(key).to_bytes())) as pdf:
        pdf.save(buffer, encryption=pikepdf.Encryption(owner="o", user="u"))
    doc = PdfDocument.from_bytes(buffer.getvalue(), password="u")
    assert doc.is_signed  # opening decrypted it, so writing it can't keep the signature

    with pytest.warns(SignatureInvalidatedWarning):
        data = doc.to_bytes()

    assert_unsigned(data)


def test_tampering_is_detected(key: SigningKey) -> None:
    data = bytearray(blank().sign(key).to_bytes())
    index = data.find(b"/MediaBox")
    data[index + 11 : index + 12] = b"9"

    (info,) = PdfDocument.from_bytes(bytes(data)).verify_signatures()
    assert not info.intact


def test_sign_after_filling_a_form(key: SigningKey) -> None:
    filled = PdfDocument.from_bytes(build_form()).fill_form({"name": "Ada"}, flatten=True)

    (info,) = filled.sign(key).verify_signatures()

    assert info.intact
    assert info.covers_whole_document


def test_pending_encryption_blocks_signing(key: SigningKey) -> None:
    with pytest.raises(SigningError, match="pending encryption"):
        blank().encrypt(user_password="pw").sign(key)


def test_unreachable_timestamp_server(key: SigningKey) -> None:
    with pytest.raises(SigningError, match="signing failed"):
        blank().sign(key, timestamp_url="http://localhost:1/tsa")


def test_unsigned_document(key: SigningKey) -> None:
    doc = blank()

    assert not doc.is_signed
    assert doc.verify_signatures() == []


def test_cli_sign_and_verify(tmp_path: Path) -> None:
    import json

    from typer.testing import CliRunner

    from dravenpdf.cli import app

    p12, cert = material()
    (tmp_path / "key.p12").write_bytes(p12)
    (tmp_path / "root.pem").write_bytes(cert)
    blank().save(tmp_path / "in.pdf")
    runner = CliRunner()

    signed = runner.invoke(
        app,
        ["sign", str(tmp_path / "in.pdf"), "-o", str(tmp_path / "s.pdf"),
         "--key", str(tmp_path / "key.p12"), "--visible", "1,50,50,200,60", "--reason", "OK"],
        env={"DRAVENPDF_KEY_PASSWORD": PASSWORD},
    )  # fmt: skip
    checked = runner.invoke(
        app, ["verify", str(tmp_path / "s.pdf"), "--trust", str(tmp_path / "root.pem")]
    )
    (tmp_path / "other.pem").write_bytes(material("Second Signer")[1])
    untrusted = runner.invoke(
        app, ["verify", str(tmp_path / "s.pdf"), "--trust", str(tmp_path / "other.pem")]
    )

    assert signed.exit_code == 0, signed.output
    assert PASSWORD not in signed.output
    assert checked.exit_code == 0, checked.output
    assert json.loads(checked.stdout)[0]["trusted"]
    assert untrusted.exit_code == 1
    assert json.loads(untrusted.stdout)[0]["trusted"] is False


def test_bad_trust_root_file() -> None:
    with pytest.raises(SigningError, match="not a certificate"):
        blank().verify_signatures([b"this is not a certificate"])


def test_cli_verify_exit_codes(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from dravenpdf.cli import app

    (tmp_path / "root.pem").write_bytes(material()[1])
    key = SigningKey.from_pkcs12(material()[0], PASSWORD)
    signed = blank().sign(key).to_bytes()
    (tmp_path / "signed.pdf").write_bytes(signed)
    blank().save(tmp_path / "plain.pdf")
    tampered = bytearray(signed)
    index = tampered.find(b"/MediaBox")
    tampered[index + 11 : index + 12] = b"9"
    (tmp_path / "tampered.pdf").write_bytes(bytes(tampered))
    runner = CliRunner()

    def verify(name: str, *extra: str) -> tuple[int, str]:
        result = runner.invoke(app, ["verify", str(tmp_path / name), *extra])
        return result.exit_code, result.stderr

    trust = ("--trust", str(tmp_path / "root.pem"))
    assert verify("signed.pdf", *trust) == (0, "")
    code, message = verify("signed.pdf")  # untrusted: no roots given
    assert code == 1
    assert "not trusted" in message
    assert "--integrity-only" in message
    assert verify("signed.pdf", "--integrity-only") == (0, "")
    code, message = verify("plain.pdf", "--integrity-only")
    assert (code, "no signatures" in message) == (1, True)
    code, message = verify("tampered.pdf", "--integrity-only", *trust)
    assert (code, "broken" in message) == (1, True)


def test_cli_verify_rejects_changes_after_the_last_signature(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from dravenpdf.cli import app

    key = SigningKey.from_pkcs12(material()[0], PASSWORD)
    writer = IncrementalPdfFileWriter(io.BytesIO(blank().sign(key).to_bytes()))
    writer.root["/Lang"] = pdf_generic.TextStringObject("de")  # an unsigned revision
    writer.update_root()
    buffer = io.BytesIO()
    writer.write(buffer)
    (tmp_path / "later.pdf").write_bytes(buffer.getvalue())

    result = CliRunner().invoke(app, ["verify", str(tmp_path / "later.pdf"), "--integrity-only"])

    assert result.exit_code == 1
    assert "changed after its last signature" in result.stderr


class _Tsa(http.server.BaseHTTPRequestHandler):
    stamper: DummyTimeStamper

    def do_POST(self) -> None:
        request = tsp.TimeStampReq.load(self.rfile.read(int(self.headers["Content-Length"])))
        body = self.stamper.request_tsa_response(request).dump()
        self.send_response(200)
        self.send_header("Content-Type", "application/timestamp-reply")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def tsa_url() -> Iterator[str]:
    cert, private_key = make_tsa_material()
    _Tsa.stamper = DummyTimeStamper(
        tsa_cert=asn1_x509.Certificate.load(cert),
        tsa_key=asn1_keys.PrivateKeyInfo.load(private_key),
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Tsa)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/tsa"
    server.shutdown()
    server.server_close()


def test_timestamped_signature(key: SigningKey, pem: bytes, tsa_url: str) -> None:
    signed = blank().sign(key, timestamp_url=tsa_url)

    (info,) = signed.verify_signatures([pem])
    assert info.timestamped
    assert info.intact
    assert info.valid
    (plain,) = blank().sign(key).verify_signatures([pem])
    assert not plain.timestamped


# ---------------------------------------------------------------- document-level result


def test_every_signature_of_a_multi_signed_file_is_ok(key: SigningKey, pem: bytes) -> None:
    other_p12, other_pem = material("Second Signer")
    twice = blank().sign(key).sign(SigningKey.from_pkcs12(other_p12, PASSWORD))

    first, second = twice.verify_signatures([pem, other_pem])

    assert first.ok  # a later signature doesn't make the earlier one fail
    assert not first.covers_whole_document  # still reported, as a separate fact
    assert second.ok
    assert signature_problems([first, second]) == []


def test_signature_problems(key: SigningKey, pem: bytes) -> None:
    signed = blank().sign(key)
    (untrusted,) = signed.verify_signatures()
    (trusted,) = signed.verify_signatures([pem])

    assert signature_problems([]) == ["the file has no signatures"]
    assert signature_problems([trusted]) == []
    assert signature_problems([untrusted]) == ["Signature: the signer is not trusted"]
    assert signature_problems([untrusted], require_trust=False) == []
    later = PdfDocument.from_bytes(appended_revision(signed.to_bytes()))
    (changed,) = later.verify_signatures([pem])
    assert changed.ok
    assert signature_problems([changed]) == ["the file was changed after its last signature"]


def appended_revision(data: bytes) -> bytes:
    """``data`` plus an unsigned incremental update."""
    writer = IncrementalPdfFileWriter(io.BytesIO(data))
    writer.root["/Lang"] = pdf_generic.TextStringObject("de")
    writer.update_root()
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------- encrypted signed files


@pytest.mark.parametrize("password", [USER, OWNER])
def test_verify_a_file_signed_while_encrypted(key: SigningKey, pem: bytes, password: str) -> None:
    doc = PdfDocument.from_bytes(encrypted_then_signed(key), password=password)

    (info,) = doc.verify_signatures([pem], password=password)

    assert doc.was_encrypted
    assert doc.is_signed
    assert type(info.field) is str  # not one of pyHanko's decrypted-object proxies
    assert info.ok
    assert info.covers_whole_document


def test_verify_an_encrypted_file_needs_its_password(key: SigningKey) -> None:
    doc = PdfDocument.from_bytes(encrypted_then_signed(key), password=USER)

    with pytest.raises(PdfPasswordError, match="needs a password") as missing:
        doc.verify_signatures()  # the password isn't kept on the document
    with pytest.raises(PdfPasswordError, match="wrong password") as wrong:
        doc.verify_signatures(password="guess-pw")

    for info in (missing, wrong):
        assert info.value.__cause__ is None
        assert "guess-pw" not in str(info.value)


def test_verify_an_encrypted_file_with_several_signatures(key: SigningKey, pem: bytes) -> None:
    other_p12, other_pem = material("Second Signer")
    data = encrypted_then_signed(key, SigningKey.from_pkcs12(other_p12, PASSWORD))

    infos = PdfDocument.from_bytes(data, password=USER).verify_signatures(
        [pem, other_pem], password=USER
    )

    assert [i.field for i in infos] == ["Sig1", "Sig2"]
    assert all(i.ok for i in infos)
    assert signature_problems(infos) == []


def test_writing_an_opened_encrypted_signed_file(key: SigningKey, pem: bytes) -> None:
    doc = PdfDocument.from_bytes(encrypted_then_signed(key), password=USER)

    with pytest.warns(SignatureInvalidatedWarning):
        written = doc.to_bytes()  # opening decrypted it: writing is a rewrite
    with pytest.warns(SignatureInvalidatedWarning):
        rotated = doc.rotate(90)

    assert_unsigned(written)
    assert rotated.verify_signatures() == []
    (info,) = doc.verify_signatures([pem], password=USER)  # the original still verifies
    assert info.ok
    (copied,) = doc.copy().verify_signatures([pem], password=USER)
    assert copied.ok


def test_cli_verify_encrypted_file(tmp_path: Path, key: SigningKey) -> None:
    from typer.testing import CliRunner

    from dravenpdf.cli import app

    (tmp_path / "enc.pdf").write_bytes(encrypted_then_signed(key))
    (tmp_path / "root.pem").write_bytes(material()[1])
    runner = CliRunner()
    args = ["verify", str(tmp_path / "enc.pdf"), "--trust", str(tmp_path / "root.pem")]

    checked = runner.invoke(app, args, env={"DRAVENPDF_PDF_PASSWORD": USER})
    missing = runner.invoke(app, args, env={"DRAVENPDF_PDF_PASSWORD": ""})

    assert checked.exit_code == 0, checked.output
    assert json.loads(checked.stdout)[0]["ok"] is True
    assert isinstance(missing.exception, PdfPasswordError)
