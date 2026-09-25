"""Loopback HTTPS sites and disposable certificate for browser cookie tests."""

from __future__ import annotations

import base64
import datetime
import hashlib
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from conftest import _Handler


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    host: str
    headers: dict[str, str]


@dataclass(frozen=True)
class HttpsSites:
    a_origin: str
    b_origin: str
    http_origin: str
    spki_hash: str
    records: list[RecordedRequest] = field(default_factory=list)

    def received(self, tag: str) -> list[RecordedRequest]:
        return [record for record in self.records if f"tag={tag}" in record.path]


def _certificate(directory: Path) -> tuple[Path, Path, str]:
    now = datetime.datetime.now(datetime.UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "dravenpdf test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "a.test")])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("a.test"), x509.DNSName("b.test")]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    cert_path = directory / "test-server.pem"
    key_path = directory / "test-server-key.pem"
    cert_path.write_bytes(
        leaf_cert.public_bytes(serialization.Encoding.PEM)
        + ca_cert.public_bytes(serialization.Encoding.PEM)
    )
    key_path.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    spki = leaf_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return cert_path, key_path, base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")


@contextmanager
def serve_https(directory: Path) -> Iterator[HttpsSites]:
    cert_path, key_path, spki_hash = _certificate(directory)
    records: list[RecordedRequest] = []

    class Handler(_Handler):
        def _record(self) -> dict[str, str]:
            headers = {name.lower(): value for name, value in self.headers.items()}
            records.append(
                RecordedRequest(self.command, self.path, headers.get("host", ""), headers)
            )
            return headers

        def do_GET(self) -> None:
            headers = self._record()
            if urlsplit(self.path).path == "/auth/cors-api":
                self._send(
                    200,
                    b"CORS-API-OK",
                    "text/plain",
                    {
                        "Access-Control-Allow-Origin": headers.get("origin", ""),
                        "Access-Control-Allow-Credentials": "true",
                    },
                )
            else:
                super().do_GET()

        def do_POST(self) -> None:
            self._record()
            self._send(200, b"OK", "text/plain")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    http_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    http_server.daemon_threads = True
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(str(cert_path), str(key_path))
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    http_thread.start()
    port = server.server_address[1]
    try:
        yield HttpsSites(
            f"https://a.test:{port}",
            f"https://b.test:{port}",
            f"http://a.test:{http_server.server_address[1]}",
            spki_hash,
            records,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        http_server.shutdown()
        http_server.server_close()
        http_thread.join()
        cert_path.unlink()
        key_path.unlink()
