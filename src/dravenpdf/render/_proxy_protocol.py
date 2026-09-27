"""Private, per-render policy and control messages for the proxy process."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True, repr=False)
class ProxyPolicy:
    allowed_hosts: list[str] | None
    allow_private_network: bool
    bundle: dict[str, Any] | None
    headers: dict[str, dict[str, str]]
    credential: str
    deadline: float | None

    def __repr__(self) -> str:
        return "ProxyPolicy(<redacted>)"

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")

    @staticmethod
    def write_json(path: Path, data: bytes) -> None:
        """Write serialized policy ``data`` to a new file only its owner can read."""
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def write(self, path: Path) -> None:
        self.write_json(path, self.to_json())

    @classmethod
    def read(cls, path: Path) -> ProxyPolicy:
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise PermissionError("proxy policy file must have mode 0600")
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        return cls(**data)


EventKind = Literal["ready", "alive", "blocked", "fatal", "sync"]


def send_event(fd: int, kind: EventKind, target: str = "", reason: str = "") -> None:
    """Write one redacted event; a broken channel raises and denies the flow."""
    message = json.dumps({"kind": kind, "target": target, "reason": reason}) + "\n"
    data = message.encode("utf-8")
    while data:
        data = data[os.write(fd, data) :]
