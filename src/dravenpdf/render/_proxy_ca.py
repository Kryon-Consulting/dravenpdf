"""Disposable signing authority shared by one browser pool's proxy processes."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.hazmat.primitives import serialization
from mitmproxy import certs


class ProxyCA:
    def __init__(self, directory: TemporaryDirectory[str], spki_hash: str) -> None:
        self._directory = directory
        self.confdir = Path(directory.name)
        self.spki_hash = spki_hash

    @classmethod
    def create(cls) -> ProxyCA:
        directory = TemporaryDirectory(prefix="dravenpdf-ca-")
        try:
            key, certificate = certs.create_ca("dravenpdf", "dravenpdf temporary proxy CA", 2048)
            pem = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ) + certificate.public_bytes(serialization.Encoding.PEM)
            path = Path(directory.name) / "mitmproxy-ca.pem"
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
                out.write(pem)
            spki = key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            return cls(directory, base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii"))
        except BaseException:
            directory.cleanup()
            raise

    def close(self) -> None:
        self._directory.cleanup()
