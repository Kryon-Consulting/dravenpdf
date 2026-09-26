"""One private mitmdump process and control channel per browser context."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import shutil
import socket
import sys
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

from dravenpdf.errors import RenderError
from dravenpdf.render._proxy_ca import ProxyCA
from dravenpdf.render._proxy_protocol import ProxyPolicy

_START_ATTEMPTS = 3
_START_LIMIT = 15.0


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class ProxyGate:
    def __init__(
        self,
        process: asyncio.subprocess.Process,
        directory: TemporaryDirectory[str],
        reader: asyncio.StreamReader,
        transport: asyncio.BaseTransport,
        port: int,
        credential: str,
    ) -> None:
        self._process = process
        self._directory = directory
        self._transport = transport
        self._reader = reader
        self._events = asyncio.create_task(self._read_events())
        self._ready = asyncio.Event()
        self._closed = False
        self._fatal_reason: str | None = None
        self._port = port
        self._sync_waiters: dict[str, asyncio.Future[None]] = {}
        self.blocked: list[tuple[str, str]] = []
        self.proxy_options = {
            "server": f"http://127.0.0.1:{port}",
            "username": "dravenpdf",
            "password": credential,
            "bypass": "<-loopback>",
        }

    @property
    def fatal(self) -> bool:
        return self._fatal_reason is not None or self._process.returncode is not None

    @property
    def fatal_reason(self) -> str | None:
        return self._fatal_reason

    @classmethod
    async def start(cls, policy: ProxyPolicy, ca: ProxyCA, deadline: float) -> ProxyGate:
        """Start a ready loopback proxy or fail before a context can be made."""
        loop = asyncio.get_running_loop()
        for _ in range(_START_ATTEMPTS):
            if loop.time() >= deadline:
                raise TimeoutError("proxy startup deadline")
            directory = TemporaryDirectory(prefix="dravenpdf-proxy-")
            read_fd = write_fd = -1
            gate: ProxyGate | None = None
            process: asyncio.subprocess.Process | None = None
            try:
                path = Path(directory.name)
                shutil.copyfile(ca.confdir / "mitmproxy-ca.pem", path / "mitmproxy-ca.pem")
                os.chmod(path / "mitmproxy-ca.pem", 0o600)
                policy.write(path / "policy.json")
                port = _free_loopback_port()
                # mitmproxy loads this file from confdir. A secret never appears in argv.
                config = {
                    "mode": [f"regular@127.0.0.1:{port}"],
                    "connection_strategy": "lazy",
                    "upstream_cert": False,
                    "ssl_insecure": False,
                    "ignore_hosts": [],
                    "flow_detail": 0,
                    "termlog_verbosity": "error",
                }
                (path / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
                os.chmod(path / "config.yaml", 0o600)
                read_fd, write_fd = os.pipe()
                env = os.environ.copy()
                env["DRAVENPDF_PROXY_POLICY"] = str(path / "policy.json")
                env["DRAVENPDF_PROXY_EVENTS_FD"] = str(write_fd)
                executable = Path(sys.executable).with_name("mitmdump")
                process = await asyncio.create_subprocess_exec(
                    str(executable),
                    "--set",
                    f"confdir={path}",
                    "-s",
                    str(Path(__file__).with_name("_proxy_addon.py")),
                    "--quiet",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=env,
                    pass_fds=(write_fd,),
                )
                os.close(write_fd)
                write_fd = -1
                reader = asyncio.StreamReader()
                protocol = asyncio.StreamReaderProtocol(reader)

                def protocol_factory(
                    chosen: asyncio.StreamReaderProtocol = protocol,
                ) -> asyncio.StreamReaderProtocol:
                    return chosen

                transport, _ = await loop.connect_read_pipe(
                    protocol_factory, os.fdopen(read_fd, "rb", 0)
                )
                read_fd = -1
                gate = cls(process, directory, reader, transport, port, policy.credential)
                await asyncio.wait_for(
                    gate._ready.wait(), min(_START_LIMIT, deadline - loop.time())
                )
                gate.check()
                async with asyncio.timeout_at(deadline):
                    await gate.synchronize()
                return gate
            except TimeoutError:
                if gate is not None:
                    await gate.close()
                else:
                    await _stop_orphan(process)
                    directory.cleanup()
                if loop.time() >= deadline:
                    raise
            except BaseException as exc:
                if gate is not None:
                    await gate.close()
                else:
                    await _stop_orphan(process)
                    directory.cleanup()
                if isinstance(exc, asyncio.CancelledError):
                    raise
            finally:
                for fd in (read_fd, write_fd):
                    if fd >= 0:
                        os.close(fd)
        raise RenderError("could not start network proxy")

    async def _read_events(self) -> None:
        try:
            while line := await self._reader.readline():
                event = json.loads(line)
                kind = event["kind"]
                if kind == "ready":
                    self._ready.set()
                elif kind == "blocked":
                    # Chromium's initial unauthenticated request is a normal
                    # Basic-auth challenge; the retry carries the credential.
                    if event["reason"] != "credential":
                        self.blocked.append((event["target"], event["reason"]))
                elif kind == "fatal":
                    self._fatal_reason = event.get("reason") or "proxy failure"
                elif kind == "sync":
                    waiter = self._sync_waiters.get(event["target"])
                    if waiter is not None and not waiter.done():
                        waiter.set_result(None)
                elif kind != "alive":
                    self._fatal_reason = "proxy failure"
        except (ValueError, KeyError, asyncio.LimitOverrunError):
            self._fatal_reason = "proxy failure"
        finally:
            if not self._closed:
                self._fatal_reason = self._fatal_reason or "proxy failure"
            self._ready.set()

    async def synchronize(self) -> None:
        """Wait for a proxy marker after prior flow events on this control pipe."""
        self.check()
        token = secrets.token_urlsafe(16)
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sync_waiters[token] = waiter
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", self._port)
            credential = self.proxy_options["password"]
            auth = base64.b64encode(f"dravenpdf:{credential}".encode()).decode()
            request = (
                f"GET http://proxy.dravenpdf.invalid/__sync/{token} HTTP/1.1\r\n"
                "Host: proxy.dravenpdf.invalid\r\n"
                f"Proxy-Authorization: Basic {auth}\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(request.encode("ascii"))
            await writer.drain()
            await asyncio.wait_for(waiter, 2)
            response = await asyncio.wait_for(reader.readline(), 2)
            if b" 204 " not in response:
                raise RenderError("network proxy synchronization failed")
        except (OSError, TimeoutError) as exc:
            self.check()
            raise RenderError("network proxy synchronization failed") from exc
        finally:
            self._sync_waiters.pop(token, None)
            if writer is not None:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
        self.check()

    def check(self) -> None:
        if self._fatal_reason == "deadline":
            raise TimeoutError("render deadline expired")
        if self.fatal:
            raise RenderError("network proxy failed")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process.returncode is None:
            with suppress(ProcessLookupError):
                self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), 2)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    self._process.kill()
                await self._process.wait()
        self._transport.close()
        self._events.cancel()
        await asyncio.gather(self._events, return_exceptions=True)
        self._directory.cleanup()


def new_credential() -> str:
    return secrets.token_urlsafe(32)


async def _stop_orphan(process: asyncio.subprocess.Process | None) -> None:
    if process is not None and process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
        await process.wait()
