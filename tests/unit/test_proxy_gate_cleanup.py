"""ProxyGate teardown stays fail-closed under cancellation."""

from __future__ import annotations

import asyncio

import pytest

from dravenpdf.render._proxy_gate import ProxyGate


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
