from __future__ import annotations

import io
from functools import cache
from pathlib import Path

import pikepdf
import pytest

from dravenpdf import (
    PdfDocument,
    SignatureBox,
    SignatureInvalidatedWarning,
    SigningError,
    SigningKey,
)
from forms_fixture import build_form
from signing_fixture import make_p12

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


def test_changing_a_signed_document_warns_and_breaks_it(key: SigningKey) -> None:
    signed = blank().sign(key)

    with pytest.warns(SignatureInvalidatedWarning):
        rotated = signed.rotate(90)
    with pytest.warns(SignatureInvalidatedWarning):
        signed.to_bytes(compress=True)

    (info,) = rotated.verify_signatures()
    assert not info.intact


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
