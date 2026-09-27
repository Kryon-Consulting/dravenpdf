"""ProxyGate teardown stays fail-closed under cancellation."""

from __future__ import annotations

import asyncio
import stat
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from dravenpdf.errors import RenderError
from dravenpdf.render import _proxy_gate
from dravenpdf.render._proxy_ca import ProxyCA
from dravenpdf.render._proxy_gate import ProxyGate
from dravenpdf.render._proxy_protocol import ProxyPolicy


async def test_cancelled_close_still_kills_process_and_removes_private_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        returncode: int | None = None

        def __init__(self) -> None:
            self.terminated = asyncio.Event()
            self.killed = False
            self.done = asyncio.Event()

        def terminate(self) -> None:
            self.terminated.set()

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            self.done.set()

        async def wait(self) -> int:
            await self.done.wait()
            return -9

    class Directory:
        cleaned = False

        def cleanup(self) -> None:
            self.cleaned = True

    class Transport:
        closed = False

        def close(self) -> None:
            self.closed = True

    from dravenpdf.render import _proxy_gate as gate_module

    monkeypatch.setattr(gate_module, "_STOP_GRACE", 0.05, raising=False)
    process, directory, transport = Process(), Directory(), Transport()
    gate = ProxyGate(
        process,
        directory,
        asyncio.StreamReader(),
        transport,
        1,
        "secret",  # type: ignore[arg-type]
    )
    first = asyncio.create_task(gate.close())
    await process.terminated.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await gate.close()
    assert process.killed
    assert transport.closed
    assert directory.cleaned


def test_prepare_directory_writes_private_files() -> None:
    ca = ProxyCA.create()
    try:
        policy = ProxyPolicy(None, False, None, {}, "credential", None)
        directory = _proxy_gate._prepare_directory(ca, policy.to_json(), 4321, None)
        try:
            path = Path(directory.name)
            for name in ("mitmproxy-ca.pem", "policy.json", "config.yaml"):
                assert stat.S_IMODE((path / name).stat().st_mode) == 0o600
            assert ProxyPolicy.read(path / "policy.json").credential == "credential"
            assert "regular@127.0.0.1:4321" in (path / "config.yaml").read_text()
        finally:
            directory.cleanup()
    finally:
        ca.close()


async def test_prepared_directory_is_removed_when_the_caller_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = threading.Event(), threading.Event()
    made: list[Path] = []
    real = _proxy_gate._prepare_directory

    def slow(*args: Any) -> TemporaryDirectory[str]:
        started.set()
        release.wait(5)
        directory = real(*args)
        made.append(Path(directory.name))
        return directory

    monkeypatch.setattr(_proxy_gate, "_prepare_directory", slow)
    ca = ProxyCA.create()
    try:
        task = asyncio.create_task(_proxy_gate._prepared_directory(ca, b"{}", 1, None))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        for _ in range(200):
            if made and not made[0].exists():
                break
            await asyncio.sleep(0.01)
        assert made
        assert not made[0].exists()
    finally:
        ca.close()


async def test_confdir_errors_are_retried_then_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    def broken(*_args: object) -> TemporaryDirectory[str]:
        attempts.append(1)
        raise OSError("disk full")

    monkeypatch.setattr(_proxy_gate, "_prepare_directory", broken)
    ca = ProxyCA.create()
    try:
        policy = ProxyPolicy(None, False, None, {}, "credential", None)
        deadline = asyncio.get_running_loop().time() + 15
        with pytest.raises(RenderError, match="could not start network proxy"):
            await ProxyGate.start(policy, ca, deadline)
        assert len(attempts) == 3
    finally:
        ca.close()
