"""Throwaway certificates for signing tests."""

from __future__ import annotations

import datetime
import io
from typing import TYPE_CHECKING

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

if TYPE_CHECKING:
    from dravenpdf import SigningKey

USER, OWNER = "user-pw", "owner-pw"


def make_p12(common_name: str = "Test Signer", password: str = "p12-pass") -> tuple[bytes, bytes]:
    """A self-signed certificate as (PKCS#12 bytes, certificate PEM)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Kryon Test"),
        ]
    )
    now = datetime.datetime.now(datetime.UTC)
    usage = x509.KeyUsage(
        digital_signature=True, content_commitment=True, key_encipherment=False,
        data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=False,
        encipher_only=False, decipher_only=False,
    )  # fmt: skip
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(usage, critical=True)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    p12 = pkcs12.serialize_key_and_certificates(
        b"test", key, cert, None, serialization.BestAvailableEncryption(password.encode())
    )
    return p12, cert.public_bytes(serialization.Encoding.PEM)


def make_tsa_material() -> tuple[bytes, bytes]:
    """A self-signed timestamping certificate as (certificate DER, private key DER)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test TSA")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_der = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert.public_bytes(serialization.Encoding.DER), key_der


def encrypted_then_signed(*keys: SigningKey) -> bytes:
    """A 2-page file encrypted first (``USER``/``OWNER``), then signed by each key in
    turn as an encrypted file (fields ``Sig1``, ``Sig2``, ...)."""
    import pikepdf
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers

    pdf = pikepdf.new()
    for _ in range(2):
        pdf.add_blank_page(page_size=(400, 400))
    buffer = io.BytesIO()
    pdf.save(buffer, encryption=pikepdf.Encryption(owner=OWNER, user=USER))
    data = buffer.getvalue()
    for number, key in enumerate(keys, start=1):
        writer = IncrementalPdfFileWriter(io.BytesIO(data))
        writer.encrypt(USER)
        metadata = signers.PdfSignatureMetadata(field_name=f"Sig{number}")
        data = signers.PdfSigner(metadata, signer=key._signer).sign_pdf(writer).getvalue()
    return data
