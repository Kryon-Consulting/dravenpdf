"""Transport proof: real Chromium, mitmdump, and destination wire records.

The addon here is a probe, not the production policy gate.
"""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError

from dravenpdf.render._proxy_ca import ProxyCA
from dravenpdf.render.pool import DEFAULT_LAUNCH_ARGS, BrowserPool
from https_fixture import serve_https

pytestmark = pytest.mark.browser

ADDON = """
import asyncio
import json
from pathlib import Path
from mitmproxy import ctx

class Probe:
    def running(self):
        Path(ctx.options.confdir, "ready").touch()

    async def requestheaders(self, flow):
        record = {"url": flow.request.pretty_url,
                  "client": str(flow.client_conn.id),
                  "server": str(flow.server_conn.id)}
        with Path(ctx.options.confdir, "requests").open("a") as out:
            out.write(json.dumps(record) + "\\n")
        if "hold" in flow.request.path:
            Path(ctx.options.confdir, "held").touch()
            await asyncio.Event().wait()

    def server_connect(self, data):
        # Test-only DNS mapping: retain the original hostname for TLS validation.
        host, port = data.server.address
        data.server.sni = host
        data.server.address = ("127.0.0.1", port)

addons = [Probe()]
"""


async def wait_for(path: Path) -> None:
    async with asyncio.timeout(15):
        while not await asyncio.to_thread(path.exists):
            await asyncio.sleep(0.02)


@asynccontextmanager
async def proxy(
    ca: ProxyCA, upstream_ca: Path | None = None
) -> AsyncIterator[tuple[asyncio.subprocess.Process, str]]:
    script = ca.confdir / "probe.py"
    script.write_text(ADDON)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    args = [
        str(Path(sys.executable).with_name("mitmdump")),
        "-q",
        "--listen-host",
        "127.0.0.1",
        "--listen-port",
        str(port),
        "--set",
        f"confdir={ca.confdir}",
        "--set",
        "upstream_cert=false",
        "--set",
        "connection_strategy=lazy",
        "--set",
        "ssl_insecure=false",
        "--set",
        "http2=false",
        "-s",
        str(script),
    ]
    if upstream_ca is not None:
        args += ["--set", f"ssl_verify_upstream_trusted_ca={upstream_ca}"]
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        await wait_for(ca.confdir / "ready")
        yield process, f"http://127.0.0.1:{port}"
    finally:
        if process.returncode is None:
            process.kill()
        output, _ = await process.communicate()
        assert b"Error in script" not in output, output.decode()


def requests(ca: ProxyCA) -> list[dict[str, str]]:
    path = ca.confdir / "requests"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def test_pool_ca_trust_cookie_and_cleanup(tmp_path: Path) -> None:
    with serve_https(tmp_path) as sites:
        pool = BrowserPool()
        async with pool:
            ca = pool._proxy_ca
            assert ca is not None
            async with (
                proxy(ca, tmp_path / "test-server.pem") as (_, address),
                pool.context(proxy={"server": address, "bypass": "<-loopback>"}) as context,
            ):
                await context.add_cookies(
                    [
                        {
                            "name": "browser",
                            "value": "selected",
                            "url": sites.a_origin,
                            "secure": True,
                        }
                    ]
                )
                page = await context.new_page()
                response = await page.goto(sites.a_origin + "/secret?tag=cookie")
                assert response is not None
                assert response.status == 200, await response.text()
                assert sites.received("cookie")[0].headers["cookie"] == "browser=selected"
                assert any("tag=cookie" in r["url"] for r in requests(ca))
        assert not ca.confdir.exists()


async def test_upstream_invalid_tls_is_rejected(tmp_path: Path) -> None:
    with serve_https(tmp_path) as sites, ProxyCAContext() as ca:
        async with (
            BrowserPool(
                launch_args=(
                    *DEFAULT_LAUNCH_ARGS,
                    f"--ignore-certificate-errors-spki-list={ca.spki_hash}",
                )
            ) as pool,
            proxy(ca) as (_, address),
            pool.context(proxy={"server": address, "bypass": "<-loopback>"}) as context,
        ):
            page = await context.new_page()
            response = await page.goto(sites.a_origin + "/secret?tag=invalid")
            assert response is not None
            assert response.status == 502
            assert "certificate verify failed" in (await response.text()).lower()
            assert any("tag=invalid" in r["url"] for r in requests(ca))
            assert not sites.received("invalid")


class ProxyCAContext:
    def __enter__(self) -> ProxyCA:
        self.ca = ProxyCA.create()
        return self.ca

    def __exit__(self, *args: object) -> None:
        self.ca.close()


@asynccontextmanager
async def wire_origin(
    directory: Path, secure: bool
) -> AsyncIterator[tuple[str, list[bytearray], str]]:
    """Record every decrypted HTTP byte, including partial request headers/bodies."""
    import base64
    import hashlib

    from https_fixture import _certificate

    tls = None
    spki = ""
    if secure:
        cert, key, spki = _certificate(directory)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
    wires: list[bytearray] = []
    tasks: set[asyncio.Task[None]] = set()

    async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        wire = bytearray()
        wires.append(wire)
        pending = bytearray()
        try:
            while chunk := await reader.read(1):
                wire.extend(chunk)
                pending.extend(chunk)
                if pending.endswith(b"\r\n\r\n"):
                    if b"upgrade: websocket" in pending.lower():
                        headers = dict(
                            line.split(b": ", 1)
                            for line in bytes(pending).split(b"\r\n")[1:]
                            if b": " in line
                        )
                        key = next(
                            value
                            for name, value in headers.items()
                            if name.lower() == b"sec-websocket-key"
                        )
                        accept = base64.b64encode(
                            hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
                        )
                        writer.write(
                            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                            b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
                        )
                    else:
                        writer.write(
                            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                            b"Content-Type: text/plain\r\n\r\nOK"
                        )
                    await writer.drain()
                    pending.clear()
        finally:
            writer.close()
            tasks.discard(task)

    server = await asyncio.start_server(receive, "127.0.0.1", 0, ssl=tls)
    port = server.sockets[0].getsockname()[1]
    try:
        yield (
            f"{'https' if secure else 'http'}://{'a.test' if secure else '127.0.0.1'}:{port}",
            wires,
            spki,
        )
    finally:
        server.close()
        await server.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("scheme", ["http", "https", "ws", "wss"])
async def test_all_schemes_use_proxy_and_never_fall_back(tmp_path: Path, scheme: str) -> None:
    secure = scheme in {"https", "wss"}
    async with (
        wire_origin(tmp_path, secure) as (origin, wires, spki),
        BrowserPool(
            launch_args=(
                *DEFAULT_LAUNCH_ARGS,
                "--host-resolver-rules=MAP a.test 127.0.0.1",
                f"--ignore-certificate-errors-spki-list={spki}",
            )
        ) as pool,
    ):
        ca = pool._proxy_ca
        assert ca is not None
        upstream = tmp_path / "test-server.pem" if secure else None
        async with (
            proxy(ca, upstream) as (process, address),
            pool.context(proxy={"server": address, "bypass": "<-loopback>"}) as context,
        ):
            page = await context.new_page()
            url = (
                origin.replace("https:", "wss:").replace("http:", "ws:")
                if scheme in {"ws", "wss"}
                else origin
            )

            async def request(tag: str) -> None:
                if scheme in {"ws", "wss"}:
                    await page.evaluate(
                        """url => new Promise((resolve, reject) => {
                        const ws = new WebSocket(url);
                        ws.onopen = () => { ws.close(); resolve(true); };
                        ws.onerror = () => reject(new Error('websocket failed'));
                    })""",
                        url + "/" + tag,
                    )
                else:
                    await page.goto(url + "/" + tag, timeout=5000)

            await request("first")
            assert any(b"GET /first HTTP/1.1" in wire for wire in wires)
            assert any("/first" in record["url"] for record in requests(ca))
            process.kill()
            await process.wait()
            with pytest.raises(PlaywrightError):
                await asyncio.wait_for(request("after-death"), timeout=10)
            assert all(b"after-death" not in wire for wire in wires)
            # Positive control: the same browser can reach this exact origin directly.
            # A TLS/DNS failure must not masquerade as proof of mandatory proxying.
            async with pool.context() as direct:
                page = await direct.new_page()
                await request("direct-control")
                assert any(b"GET /direct-control HTTP/1.1" in wire for wire in wires)


async def test_reused_https_connection_sends_zero_bytes_while_held(tmp_path: Path) -> None:
    async with (
        wire_origin(tmp_path, True) as (origin, wires, spki),
        BrowserPool(
            launch_args=(
                *DEFAULT_LAUNCH_ARGS,
                "--host-resolver-rules=MAP a.test 127.0.0.1",
                f"--ignore-certificate-errors-spki-list={spki}",
            )
        ) as pool,
    ):
        ca = pool._proxy_ca
        assert ca is not None
        async with (
            proxy(ca, tmp_path / "test-server.pem") as (process, address),
            pool.context(proxy={"server": address, "bypass": "<-loopback>"}) as context,
        ):
            page = await context.new_page()
            await page.goto(origin + "/first")
            before = tuple(bytes(wire) for wire in wires)
            connection_count = len(wires)
            assert connection_count == 1
            # Fetch avoids navigation cancellation replacing the connection.
            pending = asyncio.create_task(
                page.evaluate("""() => fetch('/hold', {
                method: 'POST', body: 'must-not-reach-destination'
            })""")
            )
            try:
                await wait_for(ca.confdir / "held")
                seen = [r for r in requests(ca) if r["url"].endswith(("/first", "/hold"))]
                assert len(seen) == 2
                assert seen[0]["client"] == seen[1]["client"]
                assert seen[0]["server"] == seen[1]["server"]
                assert len(wires) == connection_count
                assert tuple(bytes(wire) for wire in wires) == before
                process.kill()
                await process.wait()
                with pytest.raises(PlaywrightError):
                    await asyncio.wait_for(pending, 10)
                assert len(wires) == connection_count
                assert tuple(bytes(wire) for wire in wires) == before
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def test_browser_does_not_trust_another_proxy_ca(tmp_path: Path) -> None:
    with serve_https(tmp_path) as sites, ProxyCAContext() as other_ca:
        async with (
            BrowserPool() as pool,
            proxy(other_ca, tmp_path / "test-server.pem") as (_, address),
            pool.context(proxy={"server": address, "bypass": "<-loopback>"}) as context,
        ):
            page = await context.new_page()
            with pytest.raises(PlaywrightError, match="ERR_CERT_AUTHORITY_INVALID"):
                await page.goto(sites.a_origin + "/secret?tag=untrusted")
            assert not sites.received("untrusted")
