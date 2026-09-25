from __future__ import annotations

import io
from pathlib import Path

import pikepdf
import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from dravenpdf import PdfDocument, PdfOperationError, PdfPasswordError
from dravenpdf.cli import app

SECRET = "correct-horse-battery"


def sample(pages: int = 2) -> PdfDocument:
    pdf = pikepdf.new()
    for i in range(pages):
        pdf.add_blank_page(page_size=(100 + i, 200))
    buffer = io.BytesIO()
    pdf.save(buffer)
    return PdfDocument.from_bytes(buffer.getvalue())


def opened(data: bytes, password: str = "") -> pikepdf.Pdf:
    return pikepdf.open(io.BytesIO(data), password=password)


def test_user_password_protects_the_file() -> None:
    data = sample().encrypt(user_password=SECRET).to_bytes()

    with pytest.raises(PdfPasswordError, match="needs a password"):
        PdfDocument.from_bytes(data)
    with pytest.raises(PdfPasswordError, match="wrong password"):
        PdfDocument.from_bytes(data, password="nope")
    doc = PdfDocument.from_bytes(data, password=SecretStr(SECRET))
    assert doc.page_count == 2
    assert doc.was_encrypted
    assert opened(data, SECRET).encryption.R == 6  # AES-256


def test_owner_password_and_permissions() -> None:
    data = sample().encrypt(owner_password=SECRET, allow_copy=False, allow_print=False).to_bytes()

    pdf = opened(data)  # no user password: anyone can open it...
    assert not pdf.allow.extract  # ...but viewers are told not to copy or print
    assert not pdf.allow.print_highres
    assert pdf.allow.modify_form
    assert opened(data, SECRET).owner_password_matched


def test_random_owner_password_when_omitted() -> None:
    data = sample().encrypt(user_password=SECRET).to_bytes()

    pdf = opened(data, SECRET)
    assert pdf.user_password_matched
    assert not pdf.owner_password_matched


def test_operations_keep_pending_encryption() -> None:
    encrypted = sample(3).encrypt(user_password=SECRET)

    derived = [
        encrypted.rotate(90),
        encrypted.delete([0]),
        encrypted.extract("1-2"),
        encrypted.stamp_text("X"),
        encrypted.set_metadata(title="t"),
        PdfDocument.merge([encrypted, sample()]),
        *encrypted.split(every=2),
    ]

    for doc in derived:
        assert doc.is_encrypted
        with pytest.raises(PdfPasswordError):
            PdfDocument.from_bytes(doc.to_bytes())
    with pytest.raises(PdfPasswordError):
        PdfDocument.from_bytes(encrypted.to_bytes(compress=True))


def test_opening_decrypts_and_decrypt_removes_pending_encryption() -> None:
    data = sample().encrypt(user_password=SECRET).to_bytes()

    opened_doc = PdfDocument.from_bytes(data, password=SECRET)
    assert not opened_doc.is_encrypted
    assert PdfDocument.from_bytes(opened_doc.to_bytes()).page_count == 2
    assert not sample().encrypt(user_password=SECRET).decrypt().is_encrypted


def test_empty_owner_password_is_rejected() -> None:
    with pytest.raises(PdfOperationError, match="owner_password"):
        sample().encrypt(owner_password="")


def test_passwords_stay_out_of_errors_and_repr() -> None:
    doc = sample().encrypt(user_password=SECRET)
    data = doc.to_bytes()

    with pytest.raises(PdfPasswordError) as info:
        PdfDocument.from_bytes(data, password=SECRET + "x")
    assert SECRET not in str(info.value)
    assert info.value.__cause__ is None
    assert SECRET not in repr(doc)


# ---------------------------------------------------------------- CLI

runner = CliRunner()


def test_cli_encrypt_and_decrypt(tmp_path: Path) -> None:
    sample().save(tmp_path / "in.pdf")

    enc = runner.invoke(
        app,
        ["encrypt", str(tmp_path / "in.pdf"), "-o", str(tmp_path / "enc.pdf"), "--no-copy"],
        env={"DRAVENPDF_USER_PASSWORD": SECRET},
    )
    dec = runner.invoke(
        app,
        ["decrypt", str(tmp_path / "enc.pdf"), "-o", str(tmp_path / "dec.pdf")],
        input=SECRET + "\n",  # the hidden prompt
    )

    assert enc.exit_code == 0, enc.output
    assert dec.exit_code == 0, dec.output
    assert SECRET not in enc.output + dec.output
    assert not opened((tmp_path / "enc.pdf").read_bytes(), SECRET).allow.extract
    assert PdfDocument.open(tmp_path / "dec.pdf").page_count == 2


def test_cli_encrypt_needs_something_to_do(tmp_path: Path) -> None:
    sample().save(tmp_path / "in.pdf")

    result = runner.invoke(app, ["encrypt", str(tmp_path / "in.pdf"), "-o", "x.pdf"])

    assert result.exit_code == 1
    assert "set a password" in result.output


def test_cli_hint_for_encrypted_input(tmp_path: Path) -> None:
    import subprocess
    import sys

    (tmp_path / "enc.pdf").write_bytes(sample().encrypt(user_password=SECRET).to_bytes())

    completed = subprocess.run(
        [sys.executable, "-m", "dravenpdf", "rotate", str(tmp_path / "enc.pdf"), "-o", "x.pdf"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip

    assert completed.returncode == 1
    assert "needs a password" in completed.stderr
    assert "dravenpdf decrypt" in completed.stderr
