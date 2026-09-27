# Codebase Review Optimizations (Tiers 1–3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the Tier 1–3 findings of the 2026-09-27 whole-codebase review: bound HTML stamping on the server, take the full package import off every render's proxy start, stop holding the pdfium lock while encoding images, move rules duplicated by the CLI and HTTP API into the library, and remove the smaller duplications and inefficiencies.

**Architecture:** Changes stay inside the existing layers. `render/` gets one new light module, `render/_netpolicy.py`, that the proxy subprocess can import without pydantic, playwright or pikepdf, and the package `__init__`s become lazy. `document/` gets finer-grained pdfium locking, streaming image output and shared helpers. The CLI and server shrink as rules move into `PdfDocument`.

**Tech Stack:** Python 3.12, pikepdf, pypdfium2 5.13, Pillow, img2pdf, Playwright, mitmproxy, FastAPI, Typer, pytest + pytest-asyncio (`asyncio_mode = "auto"`).

## Implementation notes (deviations from the steps below)

Implemented on `claude/eager-keller-upcxyc`, one commit per task. Where the code differs
from the steps below, the code is right:

- **Task 1:** the test's `set & list` expression raised `TypeError`; it compares with
  `set(...)`. Imports left unused by the move (`urlsplit` in `auth.py`, `Awaitable`/`Callable`
  in `guards.py`) are removed.
- **Task 2:** the confdir is built *inside* the start attempt's `try`, so a failure there is
  retried and ends as `RenderError("could not start network proxy")`, as before (tested).
- **Task 4:** `PdfDocument.iter_images` checks its options at once but writes the file and
  opens pdfium only when consumed, so the route does no PDF work on the event loop.
  `zip_response` closes a half-consumed generator in its worker thread, and the route's
  `_named` closes the image iterator, so a failed ZIP never finalizes a pdfium document
  (which takes the pdfium lock) on the event loop. The iterators are typed `Generator`.
  `open_pdf(hold_lock=True)` keeps its single lock hold.
- **Task 5:** every TIFF/MPO frame is checked (img2pdf makes each frame a page).
- **Task 6:** `stamp_html(options=...)` resets all page-layout fields (landscape, scale,
  header/footer, page ranges, tagged/outline, print_background), not just size and margins.
- **Task 7:** `overlay` on a signed document clones twice: once for the input, once for
  `_detach()` of the result, which is needed.
- **Task 8:** `**styling` doesn't type-check under `mypy --strict`. Instead the stamp methods
  take `opacity=None` for "this kind's default" (0.3 for text, else 1.0) and the front ends
  pass the value through. FastAPI already turns an empty `owner_password` field into
  `None`, so the HTTP route needs no `or None`. The CLI keeps its `--no-copy` hint via
  `main()`, and its encrypt test runs `main()` in a subprocess.
- **Task 9:** `json_object_field` takes the expected-value description, so form fill keeps
  its "must be an object of text, true/false or null" message. The `_pages(` grep matched
  unrelated names; the check is `grep -rnw "_pages\|pages_arg"`.

## Global Constraints

- Read `CLAUDE.md` first. Its "Decisions already made" and "Conventions" sections are binding, and none of these tasks may weaken a security invariant (proxy ordering, shielded launches, SecretStr handling, cookie rules).
- `mypy --strict` must pass for `src/`: `uv run mypy src`.
- Lint and format: `uv run ruff check . && uv run ruff format --check .`.
- Unit tests: `uv run pytest -m "not browser"`. Browser tests: `uv run pytest -m browser`. CI is disabled, so run both before every commit.
- Call pdfium only through `document/_pdfium.open_pdf()`, and make every pdfium call while holding `_pdfium.LOCK`.
- `PdfDocument` methods never modify `self`. Any operation that writes a new file starts from `_rewritable()` (directly or through the new `_writable_copy()`) and ends with `_derive()`.
- Route handlers stay thin; error codes map to statuses only in `server/errors.py`; CPU-bound work runs through `asyncio.to_thread`.
- The CLI and HTTP take 1-based page strings; the Python API is 0-based.
- Raise exceptions from `dravenpdf.errors`, never a bare `Exception`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Batches (one review package per batch)

| Batch | Tasks | Layer |
|---|---|---|
| A | 1, 2, 3 | `render/`: proxy import, proxy start, redirect cap |
| B | 4, 5, 6, 7 | `document/` and the convert/stamp routes |
| C | 8, 9 | front-end rules and helpers (library, CLI, server) |

Task 1 guards the proxy subprocess (a security boundary), so its reviewer should check `_netpolicy.py` against the old code line by line: it is a move, not a rewrite.

## File Structure

| File | Change | Responsibility after the change |
|---|---|---|
| `src/dravenpdf/render/_netpolicy.py` | create | `NetworkPolicy`, DNS helpers, `origin_of`/`canonical_origin`; stdlib plus `dravenpdf.errors` only |
| `src/dravenpdf/render/guards.py` | modify | `RequestGuard` only; re-exports the policy names |
| `src/dravenpdf/render/auth.py` | modify | `RenderAuth` models; imports the origin helpers from `_netpolicy` |
| `src/dravenpdf/render/_proxy_addon.py` | modify | imports only light modules |
| `src/dravenpdf/__init__.py`, `src/dravenpdf/render/__init__.py` | modify | lazy (PEP 562) exports |
| `src/dravenpdf/render/_proxy_protocol.py` | modify | `ProxyPolicy.to_json()` / `write_json()` |
| `src/dravenpdf/render/_proxy_gate.py` | modify | per-attempt confdir built in a worker thread |
| `src/dravenpdf/render/renderer.py` | modify | builds the proxy policy in a worker thread |
| `src/dravenpdf/render/_redirect_gate.py` | modify | owns `MAX_REDIRECTS`; the only redirect-cap enforcer |
| `src/dravenpdf/document/_pdfium.py` | modify | `open_pdf(..., hold_lock=False)` |
| `src/dravenpdf/document/images.py` | modify | `iter_pdf_to_images`, encoding outside the lock, input pixel cap |
| `src/dravenpdf/document/stamp.py` | modify | `pages_by_size`, `_size_key`; `_place` takes the geometry |
| `src/dravenpdf/document/pdf.py` | modify | `_writable_copy`, cached `_readable_bytes`, `iter_images`, bounded `stamp_html`, library-owned encrypt/sign rules |
| `src/dravenpdf/errors.py` | modify | `describe_validation_errors` (shared) |
| `src/dravenpdf/server/deps.py` | modify | `within_deadline`, `json_object_field` |
| `src/dravenpdf/server/config.py` | modify | `max_html_stamp_sizes` |
| `src/dravenpdf/server/routes/*.py`, `server/middleware.py`, `server/errors.py`, `cli.py` | modify | use the shared helpers and library rules |

---

## Batch A: render layer

### Task 1: Light imports for the proxy subprocess

Every render starts `mitmdump -s _proxy_addon.py`. Today the addon imports `dravenpdf.errors`, which runs `dravenpdf/__init__.py`, which imports pikepdf, pypdfium2, playwright, pydantic, jinja2, PIL, img2pdf and pyhanko. That was measured at about 330 ms per render. After this task the addon loads none of them.

**Files:**
- Create: `src/dravenpdf/render/_netpolicy.py`
- Modify: `src/dravenpdf/render/guards.py:14-140`, `src/dravenpdf/render/auth.py:63,75-111`, `src/dravenpdf/render/_proxy_addon.py:16-20`, `src/dravenpdf/__init__.py`, `src/dravenpdf/render/__init__.py`
- Test: `tests/unit/test_proxy_imports.py` (new)

**Interfaces:**
- Produces: `dravenpdf.render._netpolicy` with `Resolver`, `resolve_host`, `NetworkPolicy`, `origin_of`, `canonical_origin`, `_is_public`, `_normalize_host`, `_raise_if`. `guards` and `auth` keep re-exporting the public ones, so existing imports keep working.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_proxy_imports.py`:

```python
"""The proxy subprocess starts once per render, so its imports must stay light."""

from __future__ import annotations

import os
import subprocess
import sys

HEAVY = {"pikepdf", "pypdfium2", "playwright", "pydantic", "jinja2", "PIL", "img2pdf", "pyhanko"}


def test_proxy_addon_imports_no_heavy_packages() -> None:
    code = (
        "import sys\n"
        "import dravenpdf.render._proxy_addon\n"
        f"print(sorted({{m.split('.')[0] for m in sys.modules}} & {sorted(HEAVY)!r}))\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("DRAVENPDF_PROXY_")}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
    )

    assert result.stdout.strip() == "[]"


def test_package_exports_still_resolve() -> None:
    import dravenpdf
    import dravenpdf.render

    for name in dravenpdf.__all__:
        assert getattr(dravenpdf, name) is not None
    for name in dravenpdf.render.__all__:
        assert getattr(dravenpdf.render, name) is not None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/test_proxy_imports.py -v`
Expected: `test_proxy_addon_imports_no_heavy_packages` FAILS, listing `['PIL', 'img2pdf', 'jinja2', 'pikepdf', 'playwright', 'pydantic', 'pyhanko', 'pypdfium2']`.

- [ ] **Step 3: Create `_netpolicy.py` by moving code, unchanged**

Create `src/dravenpdf/render/_netpolicy.py`. Its body is code **moved verbatim**; do not edit the logic.

```python
"""Network policy shared by the renderer and the per-render proxy process.

The proxy subprocess (mitmdump) imports this for every render, so it must stay light:
the standard library and ``dravenpdf.errors`` only. Never import pydantic, playwright,
pikepdf or other dravenpdf modules here (tests/unit/test_proxy_imports.py checks).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from dravenpdf.errors import BlockedRequestError

if TYPE_CHECKING:
    from dravenpdf.render.assets import AssetBundle

Resolver = Callable[[str], Awaitable[list[str]]]

_ALWAYS_ALLOWED_SCHEMES = frozenset({"data", "blob", "about"})
_WEBSOCKET_SCHEMES = {"ws": "http", "wss": "https"}
_DEFAULT_PORTS = {"http": 80, "https": 443}
```

Then append, in this order and byte for byte:
1. `canonical_origin` and `origin_of`, from `src/dravenpdf/render/auth.py:75-111`.
2. `resolve_host`, `_is_public`, `_normalize_host`, `_raise_if` and the whole `NetworkPolicy` class, from `src/dravenpdf/render/guards.py:46-140`, but without `_host_port` (lines 63-69), which stays in `guards.py` because only `RequestGuard` uses it.

- [ ] **Step 4: Point the old modules at the new one**

In `src/dravenpdf/render/auth.py`:
- delete `_DEFAULT_PORTS` (line 63) and the two moved functions (lines 75-111);
- add this after the other imports:

```python
# Re-exported: callers and tests import these from dravenpdf.render.auth.
from dravenpdf.render._netpolicy import canonical_origin as canonical_origin
from dravenpdf.render._netpolicy import origin_of as origin_of
```

(The redundant `as` aliases mark an explicit re-export for `mypy --strict`. Don't add an `__all__` to `auth.py`.)

In `src/dravenpdf/render/guards.py`:
- delete lines 38 (`Resolver`), 42-43 (the scheme constants), 46-60 (`resolve_host`, `_is_public`, `_normalize_host`), 72-140 (`_raise_if`, `NetworkPolicy`);
- delete the now-unused imports `ipaddress` and `socket`;
- replace `from dravenpdf.render.auth import origin_of` with:

```python
from dravenpdf.render._netpolicy import (
    NetworkPolicy as NetworkPolicy,
    Resolver as Resolver,
    _raise_if,
    origin_of,
    resolve_host as resolve_host,
)
```

In `src/dravenpdf/render/_proxy_addon.py`, replace lines 16-20 with:

```python
from dravenpdf.errors import BlockedRequestError
from dravenpdf.render._netpolicy import NetworkPolicy, Resolver, origin_of, resolve_host
from dravenpdf.render._proxy_protocol import ProxyPolicy, send_event
from dravenpdf.render.assets import AssetBundle
```

Also delete the stale comment at `_proxy_addon.py:81-82` ("A render-wide budget is conservative…"): the budget it describes no longer exists.

- [ ] **Step 5: Make the package `__init__`s lazy**

Replace `src/dravenpdf/render/__init__.py` with:

```python
"""HTML to PDF with headless Chromium."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dravenpdf.render.guards import RequestGuard
    from dravenpdf.render.pool import BrowserPool
    from dravenpdf.render.renderer import AsyncRenderer
    from dravenpdf.render.sync import Renderer

__all__ = ["AsyncRenderer", "BrowserPool", "Renderer", "RequestGuard"]

# Imported on first use: the proxy subprocess imports dravenpdf.render._netpolicy and
# must not pay for playwright (see tests/unit/test_proxy_imports.py).
_LAZY = {
    "AsyncRenderer": "dravenpdf.render.renderer",
    "BrowserPool": "dravenpdf.render.pool",
    "Renderer": "dravenpdf.render.sync",
    "RequestGuard": "dravenpdf.render.guards",
}


def __getattr__(name: str) -> object:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value
```

In `src/dravenpdf/__init__.py`, keep the `dravenpdf.errors` import and the `__version__` block eager, because both are cheap. Move every other import (lines 7-13 and 31-34: `document`, `forms`, `signing`, `options`, `render`, `render.auth`, `render.report`) under `if TYPE_CHECKING:`, and add the same `__getattr__` pattern with this map:

```python
_LAZY = {
    "PdfDocument": "dravenpdf.document",
    "FormField": "dravenpdf.document.forms",
    "SignatureBox": "dravenpdf.document.signing",
    "SignatureInfo": "dravenpdf.document.signing",
    "SigningKey": "dravenpdf.document.signing",
    "signature_problems": "dravenpdf.document.signing",
    "HeaderFooter": "dravenpdf.options",
    "Margins": "dravenpdf.options",
    "RenderOptions": "dravenpdf.options",
    "Viewport": "dravenpdf.options",
    "AsyncRenderer": "dravenpdf.render",
    "BrowserPool": "dravenpdf.render",
    "Renderer": "dravenpdf.render",
    "RequestGuard": "dravenpdf.render",
    "Cookie": "dravenpdf.render.auth",
    "RenderAuth": "dravenpdf.render.auth",
    "StorageState": "dravenpdf.render.auth",
    "FailedRequest": "dravenpdf.render.report",
    "HttpError": "dravenpdf.render.report",
    "RenderReport": "dravenpdf.render.report",
}
```

Leave `__all__` unchanged.

- [ ] **Step 6: Run the new tests, then the whole suite**

Run: `uv run pytest tests/unit/test_proxy_imports.py -v`
Expected: both PASS.

Run: `uv run mypy src && uv run ruff check . && uv run pytest -m "not browser" -q`
Expected: clean, all pass. If a test monkeypatches `dravenpdf.render.guards.resolve_host` or `_is_public`, point it at `dravenpdf.render._netpolicy`, because the code that calls them now lives there.

Run: `uv run pytest tests/integration/test_proxy_gate.py tests/integration/test_guard_network.py -q`
Expected: all pass (these start real proxies).

- [ ] **Step 7: Commit**

```bash
git add src/dravenpdf/render/_netpolicy.py src/dravenpdf/render/guards.py src/dravenpdf/render/auth.py src/dravenpdf/render/_proxy_addon.py src/dravenpdf/__init__.py src/dravenpdf/render/__init__.py tests/unit/test_proxy_imports.py
git commit -m "perf(proxy): keep the per-render proxy's imports light

Move NetworkPolicy and the origin helpers into render/_netpolicy.py and make
the package __init__s lazy, so mitmdump no longer imports pikepdf, playwright
and pydantic on every render.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Build the proxy's policy and confdir off the event loop

Today three things run synchronously on the event loop. `guard.proxy_policy()` base64-encodes the whole asset bundle. Then `ProxyGate.start` runs `TemporaryDirectory`, `copyfile`, `chmod` and a `json.dump` of that bundle, and it repeats this on each of up to 3 attempts. With a multi-MB bundle this stalls every concurrent render.

**Files:**
- Modify: `src/dravenpdf/render/_proxy_protocol.py:25-32`, `src/dravenpdf/render/_proxy_gate.py:66-156`, `src/dravenpdf/render/renderer.py:292-294`
- Test: `tests/unit/test_proxy_gate_cleanup.py`

**Interfaces:**
- Produces: `ProxyPolicy.to_json() -> bytes`, `ProxyPolicy.write_json(path: Path, data: bytes) -> None` (staticmethod), `_proxy_gate._prepare_directory(ca: ProxyCA, policy: bytes, port: int, upstream_ca: Path | None) -> TemporaryDirectory[str]`, `_proxy_gate._prepared_directory(...)` (async, same arguments).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_proxy_gate_cleanup.py`:

```python
import stat
import threading
from pathlib import Path

from dravenpdf.render import _proxy_gate
from dravenpdf.render._proxy_ca import ProxyCA
from dravenpdf.render._proxy_protocol import ProxyPolicy


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

    def slow(*args: object) -> object:
        started.set()
        release.wait(5)
        directory = real(*args)  # type: ignore[arg-type]
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
        assert made and not made[0].exists()
    finally:
        ca.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_proxy_gate_cleanup.py -v`
Expected: both new tests FAIL with `AttributeError` (`to_json` / `_prepare_directory` do not exist).

- [ ] **Step 3: Split serialization from writing in `ProxyPolicy`**

Replace `ProxyPolicy.write` (`_proxy_protocol.py:25-32`) with:

```python
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
```

- [ ] **Step 4: Build the confdir in a worker thread, serialize once**

In `src/dravenpdf/render/_proxy_gate.py`, add these module-level functions after `_free_loopback_port`:

```python
def _prepare_directory(
    ca: ProxyCA, policy: bytes, port: int, upstream_ca: Path | None
) -> TemporaryDirectory[str]:
    """A private confdir for one mitmdump attempt: CA, policy and config, all 0600."""
    directory = TemporaryDirectory(prefix="dravenpdf-proxy-")
    try:
        path = Path(directory.name)
        shutil.copyfile(ca.confdir / "mitmproxy-ca.pem", path / "mitmproxy-ca.pem")
        os.chmod(path / "mitmproxy-ca.pem", 0o600)
        ProxyPolicy.write_json(path / "policy.json", policy)
        # mitmproxy loads this file from confdir. A secret never appears in argv.
        config: dict[str, object] = {
            "mode": [f"regular@127.0.0.1:{port}"],
            "connection_strategy": "lazy",
            "upstream_cert": False,
            "ssl_insecure": False,
            "ignore_hosts": [],
            "flow_detail": 0,
            "termlog_verbosity": "error",
        }
        if upstream_ca is not None:
            trusted = path / "trusted-upstream-ca.pem"
            shutil.copyfile(upstream_ca, trusted)
            os.chmod(trusted, 0o600)
            config["ssl_verify_upstream_trusted_ca"] = str(trusted)
        (path / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
        os.chmod(path / "config.yaml", 0o600)
    except BaseException:
        directory.cleanup()
        raise
    return directory


async def _prepared_directory(
    ca: ProxyCA, policy: bytes, port: int, upstream_ca: Path | None
) -> TemporaryDirectory[str]:
    """:func:`_prepare_directory` in a worker thread. If the caller is cancelled first,
    the directory is removed once the thread finishes, so no private files are left."""
    task = asyncio.ensure_future(
        asyncio.to_thread(_prepare_directory, ca, policy, port, upstream_ca)
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_cleanup_prepared)
        raise


def _cleanup_prepared(task: asyncio.Future[TemporaryDirectory[str]]) -> None:
    if not task.cancelled() and task.exception() is None:
        task.result().cleanup()
```

Then rewrite the top of `ProxyGate.start` (lines 71-101). Everything from `read_fd, write_fd = os.pipe()` onwards stays as it is.

```python
        loop = asyncio.get_running_loop()
        # The policy can hold a large asset bundle: serialize it once, off the loop.
        payload = await asyncio.to_thread(policy.to_json)
        for _ in range(_START_ATTEMPTS):
            if loop.time() >= deadline:
                raise TimeoutError("proxy startup deadline")
            port = _free_loopback_port()
            directory = await _prepared_directory(ca, payload, port, upstream_ca)
            read_fd = write_fd = -1
            gate: ProxyGate | None = None
            process: asyncio.subprocess.Process | None = None
            try:
                path = Path(directory.name)
                read_fd, write_fd = os.pipe()
```

Delete the old inline copy/chmod/`policy.write`/config lines. `port` is now chosen before the directory is built, because the config names it.

- [ ] **Step 5: Build the policy off the loop in the renderer**

In `src/dravenpdf/render/renderer.py`, replace:

```python
                gate = await ProxyGate.start(
                    guard.proxy_policy(new_credential(), deadline), self.pool.proxy_ca, deadline
                )
```

with:

```python
                # proxy_policy base64-encodes the asset bundle: keep it off the event loop.
                policy = await asyncio.to_thread(guard.proxy_policy, new_credential(), deadline)
                gate = await ProxyGate.start(policy, self.pool.proxy_ca, deadline)
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/unit/test_proxy_gate_cleanup.py tests/unit/test_proxy_addon.py -v`
Expected: all PASS.

Run: `uv run pytest tests/integration/test_proxy_gate.py tests/integration/test_proxy_gate_lifecycle.py tests/integration/test_assets.py -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/dravenpdf/render/_proxy_protocol.py src/dravenpdf/render/_proxy_gate.py src/dravenpdf/render/renderer.py tests/unit/test_proxy_gate_cleanup.py
git commit -m "perf(proxy): serialize the policy once and build the confdir in a thread

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: One enforcer for the ten-redirect cap

Both `RequestGuard._observe_request` (by walking `redirected_from`) and `RedirectGate._handle` (through CDP) block at more than 10 redirects. The CDP gate is the one that actually stops the request. The guard's copy can record the same overflow a second time, which turns the error into "blocked X (and 1 more)".

**Files:**
- Modify: `src/dravenpdf/render/guards.py:41,265-283`, `src/dravenpdf/render/_redirect_gate.py:17`
- Test: `tests/integration/test_proxy_gate_lifecycle.py` (browser)

**Interfaces:**
- Produces: `dravenpdf.render._redirect_gate.MAX_REDIRECTS = 10`. `guards.MAX_REDIRECTS` is removed; `grep -rn MAX_REDIRECTS src tests` must show only `_redirect_gate.py`.

- [ ] **Step 1: Write the regression test**

Append to `tests/integration/test_proxy_gate_lifecycle.py`. It reuses the module's existing imports (`BaseHTTPRequestHandler`, `ThreadingHTTPServer`, `threading`, `parse_qs`, `urlsplit`, `AsyncRenderer`, `BlockedRequestError`, `pytest`):

```python
@pytest.mark.browser
async def test_redirect_overflow_is_reported_once() -> None:
    class Redirects(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_GET(self) -> None:
            hop = int(parse_qs(urlsplit(self.path).query)["n"][0])
            self.send_response(302)
            self.send_header("Location", f"/chain?n={hop + 1}")
            self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Redirects)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        async with AsyncRenderer(allow_private_network=True) as renderer:
            with pytest.raises(BlockedRequestError) as blocked:
                await renderer.from_url(f"http://127.0.0.1:{httpd.server_port}/chain?n=0")
        assert "more than 10 redirects" in str(blocked.value)
        assert "more)" not in str(blocked.value)
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/integration/test_proxy_gate_lifecycle.py::test_redirect_overflow_is_reported_once -v`
Expected: FAIL with "(and 1 more)" in the message. If it already PASSES, Chromium did not emit a `request` event for the eleventh hop in this setup. Keep the test as a regression guard and continue: the guard copy is still redundant.

- [ ] **Step 3: Remove the guard's copy**

In `src/dravenpdf/render/guards.py`:
- delete `MAX_REDIRECTS = 10` (line 41);
- add `Request` to the top-level playwright import: `from playwright.async_api import BrowserContext, Page, Request, Route, WebSocket`;
- replace `_observe_request` (lines 265-283) with:

```python
    def _observe_request(self, request: Request) -> None:
        """Remember where each origin's requests came from, for block messages.
        The redirect cap itself is enforced by RedirectGate."""
        parent = request.redirected_from
        origin = origin_of(request.url)
        if origin is not None:
            self._observed_urls[origin] = request.url
            if (target := _host_port(request.url)) is not None:
                self._observed_urls[target] = request.url
        if parent is not None and origin is not None:
            self._redirect_sources[origin] = origin_of(parent.url) or "unknown"
```

- update the module docstring (lines 6-8) to: "Chromium redirect chains are observed for block messages; a dedicated browser CDP interceptor (`_redirect_gate`) enforces the per-chain redirect cap while the proxy independently checks every network destination."
- Also move the function-local import in `proxy_policy` (line 184) to the top of the module: `from dravenpdf.render._proxy_protocol import ProxyPolicy`. `_proxy_protocol` imports only the standard library, so there is no cycle. Drop it from the `TYPE_CHECKING` block.

In `src/dravenpdf/render/_redirect_gate.py`, replace `from dravenpdf.render.guards import MAX_REDIRECTS` with:

```python
MAX_REDIRECTS = 10
"""Chromium's own limit; one more hop is failed here before it is sent."""
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_guards.py -q && uv run pytest tests/integration/test_proxy_gate_lifecycle.py -q`
Expected: all PASS, including `test_eleventh_redirect_destination_gets_zero_bytes_even_when_skipping` and the new test.

- [ ] **Step 5: Commit**

```bash
git add src/dravenpdf/render/guards.py src/dravenpdf/render/_redirect_gate.py tests/integration/test_proxy_gate_lifecycle.py
git commit -m "refactor(guard): leave the redirect cap to RedirectGate alone

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

**Batch A gate:** run `make check && uv run pytest -m browser -q`, then send Tasks 1–3 for review together.

---

## Batch B: document layer

### Task 4: Encode images outside the pdfium lock, and stream them

`pdf_to_images` holds the global pdfium `LOCK` while it PNG/JPEG-encodes every page, so one high-dpi request blocks every other pdfium user on the server. Its bitmaps are also never closed, so the last one is freed by a finalizer after the lock is released. The route collects every image before zipping. `_readable_bytes()` writes a derived document out again on every call.

**Files:**
- Modify: `src/dravenpdf/document/_pdfium.py`, `src/dravenpdf/document/images.py:78-133`, `src/dravenpdf/document/pdf.py:88-103,385-412,575-582`, `src/dravenpdf/server/routes/convert.py:42-70`, `CLAUDE.md` (pdfium convention line)
- Test: `tests/unit/test_images_text.py`, `tests/unit/test_document.py`

**Interfaces:**
- Produces:
  - `open_pdf(data: bytes, *, hold_lock: bool = True)`;
  - `images.iter_pdf_to_images(pdf: bytes, *, dpi=150, fmt="png", pages=None, jpeg_quality=85, max_pixels=None, max_total_bytes=None) -> Iterator[bytes]`;
  - `PdfDocument.iter_images(...)`, with the same keyword arguments as `to_images`, returning `Iterator[bytes]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_images_text.py`:

```python
import threading

import pikepdf

from dravenpdf.document import _pdfium
from dravenpdf.document import images as image_ops


def pdf_pages(count: int) -> bytes:
    pdf = pikepdf.new()
    for _ in range(count):
        pdf.add_blank_page(page_size=(200, 300))
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def test_encoding_runs_outside_the_pdfium_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    free: list[bool] = []
    real = image_ops._encode

    def spy(image: Image.Image, fmt: str, quality: int) -> bytes:
        def probe() -> None:
            got = _pdfium.LOCK.acquire(timeout=1)
            if got:
                _pdfium.LOCK.release()
            free.append(got)

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()
        return real(image, fmt, quality)

    monkeypatch.setattr(image_ops, "_encode", spy)
    image_ops.pdf_to_images(pdf_pages(2), dpi=20)

    assert free == [True, True]


def test_iter_pdf_to_images_checks_arguments_immediately() -> None:
    with pytest.raises(PdfOperationError, match="dpi"):
        image_ops.iter_pdf_to_images(pdf_pages(1), dpi=5)


def test_iter_images_matches_to_images() -> None:
    doc = PdfDocument.from_bytes(pdf_pages(3))

    assert list(doc.iter_images(dpi=20, pages=[2, 0])) == doc.to_images(dpi=20, pages=[2, 0])
```

Append to `tests/unit/test_document.py`. It uses that file's existing `make_pdf` helper; check its signature first and adapt the call if it differs.

```python
def test_readable_bytes_are_written_once(monkeypatch: pytest.MonkeyPatch) -> None:
    derived = PdfDocument.from_bytes(make_pdf(2)).rotate(90)
    saves: list[object] = []
    real_save = pikepdf.Pdf.save

    def counting_save(self: pikepdf.Pdf, *args: object, **kwargs: object) -> None:
        saves.append(self)
        real_save(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pikepdf.Pdf, "save", counting_save)
    derived.extract_text()
    derived.to_images(dpi=10)

    assert len(saves) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_images_text.py tests/unit/test_document.py -v -k "lock or iter or readable"`
Expected:
- `_encode` / `iter_pdf_to_images` / `iter_images` tests FAIL with `AttributeError`;
- `test_readable_bytes_are_written_once` FAILS with `assert 2 == 1`.

- [ ] **Step 3: Allow fine-grained locking in `_pdfium`**

Replace `open_pdf` in `src/dravenpdf/document/_pdfium.py`:

```python
@contextmanager
def open_pdf(data: bytes, *, hold_lock: bool = True) -> Iterator[pdfium.PdfDocument]:
    """Open ``data`` with pdfium and close it afterwards.

    By default ``LOCK`` is held throughout. With ``hold_lock=False`` only opening and
    closing take it: the caller must hold ``LOCK`` around every other pdfium call,
    including closing pages and bitmaps. Use that to do slow work that isn't pdfium
    (image encoding) without blocking other threads.
    """
    with LOCK:
        try:
            pdf = pdfium.PdfDocument(data)
        except pdfium.PdfiumError as exc:
            raise InvalidPdfError(f"pdfium could not open the PDF: {exc}") from exc
    try:
        if hold_lock:
            with LOCK:
                yield pdf
        else:
            yield pdf
    finally:
        with LOCK:
            pdf.close()
```

In `CLAUDE.md`, change the convention "Call pdfium (pypdfium2) only through `document/_pdfium.open_pdf()`: pdfium is not thread-safe and the server runs PDF work in threads." to end with: "With `open_pdf(..., hold_lock=False)`, hold `_pdfium.LOCK` around every pdfium call yourself (see `images._render_pages`)."

- [ ] **Step 4: Rewrite `pdf_to_images` as a lazy iterator**

In `src/dravenpdf/document/images.py`:
- change the imports to `from collections.abc import Iterable, Iterator, Sequence`;
- add `from PIL import Image`;
- change `from dravenpdf.document._pdfium import open_pdf` to `from dravenpdf.document._pdfium import LOCK, open_pdf`.

Replace `pdf_to_images` (lines 78-133) with:

```python
def pdf_to_images(
    pdf: bytes,
    *,
    dpi: int = 150,
    fmt: ImageFormat = "png",
    pages: Iterable[int] | None = None,
    jpeg_quality: int = 85,
    max_pixels: int | None = None,
    max_total_bytes: int | None = None,
) -> list[bytes]:
    """Render pages to PNG or JPEG. ``pages`` are 0-based indices (default: all).

    ``max_pixels`` caps one page's width x height at ``dpi``, checked before the page
    is rendered. ``max_total_bytes`` caps the encoded images together; rendering stops
    as soon as it is passed. Either raises :class:`LimitExceededError`.
    """
    return list(
        iter_pdf_to_images(
            pdf, dpi=dpi, fmt=fmt, pages=pages, jpeg_quality=jpeg_quality,
            max_pixels=max_pixels, max_total_bytes=max_total_bytes,
        )  # fmt: skip
    )


def iter_pdf_to_images(
    pdf: bytes,
    *,
    dpi: int = 150,
    fmt: ImageFormat = "png",
    pages: Iterable[int] | None = None,
    jpeg_quality: int = 85,
    max_pixels: int | None = None,
    max_total_bytes: int | None = None,
) -> Iterator[bytes]:
    """Like :func:`pdf_to_images`, one image at a time.

    ``dpi``, ``fmt`` and ``jpeg_quality`` are checked now; the PDF is opened and each
    page rendered as the iterator is consumed, so page and limit errors come from it.
    """
    if not MIN_DPI <= dpi <= MAX_DPI:
        raise PdfOperationError(f"dpi must be between {MIN_DPI} and {MAX_DPI}")
    if fmt not in ("png", "jpeg"):
        raise PdfOperationError("fmt must be 'png' or 'jpeg'")
    if not 1 <= jpeg_quality <= 100:
        raise PdfOperationError("jpeg_quality must be between 1 and 100")
    targets = None if pages is None else list(pages)
    return _render_pages(pdf, dpi, fmt, targets, jpeg_quality, max_pixels, max_total_bytes)


def _render_pages(
    pdf: bytes,
    dpi: int,
    fmt: ImageFormat,
    pages: list[int] | None,
    jpeg_quality: int,
    max_pixels: int | None,
    max_total_bytes: int | None,
) -> Iterator[bytes]:
    total = 0
    with open_pdf(pdf, hold_lock=False) as doc:
        with LOCK:
            count = len(doc)
        indices = range(count) if pages is None else normalize_indices(pages, count)
        for index in indices:
            with LOCK:
                image = _render_page(doc, index, dpi, max_pixels)
            data = _encode(image, fmt, jpeg_quality)  # outside the lock: the slow part
            del image
            total += len(data)
            if max_total_bytes is not None and total > max_total_bytes:
                raise LimitExceededError(
                    f"the images are larger than the {max_total_bytes:,}-byte limit "
                    "(lower the dpi, use jpeg, or ask for fewer pages)"
                )
            yield data
            del data  # don't hold this image while the next one renders


def _render_page(doc: Any, index: int, dpi: int, max_pixels: int | None) -> Image.Image:
    """Render one page to a PIL image that owns its pixels. Call with ``LOCK`` held."""
    scale = dpi / 72
    page = doc[index]
    try:
        if max_pixels is not None:
            width, height = page.get_size()
            pixels = round(width * scale) * round(height * scale)
            if pixels > max_pixels:
                raise LimitExceededError(
                    f"page {index + 1} would be {pixels:,} pixels at {dpi} dpi; "
                    f"the limit is {max_pixels:,} (lower the dpi)"
                )
        bitmap = page.render(scale=scale)
        try:
            # to_pil() shares the bitmap's buffer for some formats: copy before closing.
            return bitmap.to_pil().copy()
        finally:
            bitmap.close()
    finally:
        page.close()


def _encode(image: Image.Image, fmt: str, quality: int) -> bytes:
    buffer = io.BytesIO()
    if fmt == "png":
        image.save(buffer, format="PNG", optimize=False)
    else:
        image.convert("RGB").save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()
```

Replace `doc: Any` with `doc: pdfium.PdfDocument` (adding `import pypdfium2 as pdfium`) if mypy accepts it; keep `Any` if pypdfium2's stubs make that noisy.

- [ ] **Step 5: `PdfDocument.iter_images` and a cached `_readable_bytes`**

In `src/dravenpdf/document/pdf.py`:

In `__init__`, after `self._original`, add:

```python
        # _readable_bytes() of a derived document, written once (the PDF never changes).
        self._readable: bytes | None = None
```

Replace `_readable_bytes`:

```python
    def _readable_bytes(self) -> bytes:
        """The document as a file for reading it (images, text, verification):
        unencrypted, and with nothing removed. Written once, then kept."""
        if self._source is not None:
            return self._source
        if self._readable is None:
            buffer = io.BytesIO()
            self._pdf.save(buffer)
            self._readable = buffer.getvalue()
        return self._readable
```

After `to_images`, add:

```python
    def iter_images(
        self,
        *,
        dpi: int = 150,
        fmt: ImageFormat = "png",
        pages: Iterable[int] | None = None,
        jpeg_quality: int = 85,
        max_pixels: int | None = None,
        max_total_bytes: int | None = None,
    ) -> Iterator[bytes]:
        """Like :meth:`to_images`, but renders each page only when it is asked for,
        so a caller that writes and drops each image holds one at a time."""
        return image_ops.iter_pdf_to_images(
            self._readable_bytes(), dpi=dpi, fmt=fmt, pages=pages,
            jpeg_quality=jpeg_quality, max_pixels=max_pixels, max_total_bytes=max_total_bytes,
        )  # fmt: skip
```

- [ ] **Step 6: Stream images into the ZIP in the route**

In `src/dravenpdf/server/routes/convert.py`:
- add `from collections.abc import Iterator`;
- add `ZIP_RESPONSE` to the `deps` import;
- change the decorator to `@router.post("/pdf-to-images", response_class=Response, responses=ZIP_RESPONSE)`;
- replace the body after `settings = settings_of(request)` with:

```python
    images = doc.iter_images(
        dpi=dpi,
        fmt=format,
        pages=targets,
        max_pixels=settings.max_image_pixels,
        max_total_bytes=settings.max_output_bytes,
    )
    extension = "jpg" if format == "jpeg" else "png"
    # Rendered one page at a time inside zip_response's worker thread.
    # PNG and JPEG are compressed already; deflating them again only costs CPU.
    return await zip_response(
        _named(targets, images, extension), "pages.zip",
        max_bytes=settings.max_output_bytes, compress=False,
    )  # fmt: skip


def _named(targets: list[int], images: Iterator[bytes], ext: str) -> Iterator[tuple[str, bytes]]:
    # Not zip()/enumerate(): they keep their last item, which would hold the previous
    # image while the next one renders.
    for index in targets:
        data = next(images)
        yield f"page-{index + 1}.{ext}", data
        del data
```

`targets` is already `list[int]` (`pages_arg(...) or list(range(...))`).

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/unit/test_images_text.py tests/unit/test_document.py tests/server/test_api.py -q`
Expected: all PASS, including `test_pdf_to_images_limits` and `test_images_to_pdf_and_back`.

Run: `uv run mypy src`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/dravenpdf/document/_pdfium.py src/dravenpdf/document/images.py src/dravenpdf/document/pdf.py src/dravenpdf/server/routes/convert.py CLAUDE.md tests/unit/test_images_text.py tests/unit/test_document.py
git commit -m "perf(images): encode outside the pdfium lock and stream pages to the ZIP

Close each bitmap under the lock, render pdf-to-images lazily, and cache a
derived document's readable bytes.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Pixel cap for `images-to-pdf` inputs

`max_image_megapixels` only guards `pdf-to-images`. A small upload with huge dimensions can make img2pdf/Pillow decode hundreds of megapixels.

**Files:**
- Modify: `src/dravenpdf/document/images.py:46-75`, `src/dravenpdf/document/pdf.py:134-147`, `src/dravenpdf/server/routes/convert.py:28-39`, `docs/http-api.md:200`
- Test: `tests/unit/test_images_text.py`, `tests/server/test_api.py`

**Interfaces:**
- Produces: `images_to_pdf(..., max_pixels: int | None = None)` and `PdfDocument.from_images(..., max_pixels: int | None = None)`, which raise `LimitExceededError`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_images_text.py`:

```python
def test_from_images_pixel_cap() -> None:
    small, large = image_bytes("PNG", size=(10, 10)), image_bytes("PNG", size=(100, 50))

    assert PdfDocument.from_images([small], max_pixels=100).page_count == 1
    with pytest.raises(LimitExceededError, match=r"image 2 is 5,000 pixels"):
        PdfDocument.from_images([small, large], max_pixels=1_000)
```

Append to `tests/server/test_api.py`. It uses the file's existing `make_client`, `Settings`, `API_KEY`, `AUTH`, `FakeRenderer` and `io` names, and Pillow:

```python
def test_images_to_pdf_pixel_cap(fake: FakeRenderer) -> None:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (50, 50)).save(buffer, "PNG")
    settings = Settings(api_key=API_KEY, max_image_megapixels=0.001)  # 1,000 pixels
    with make_client(settings, fake) as c:
        response = c.post(
            "/v1/convert/images-to-pdf",
            files={"files": ("a.png", buffer.getvalue(), "image/png")},
            headers=AUTH,
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "limit_exceeded"
```

If `io` is not imported in `test_api.py`, add `import io`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_images_text.py::test_from_images_pixel_cap tests/server/test_api.py::test_images_to_pdf_pixel_cap -v`
Expected: FAIL (`unexpected keyword argument 'max_pixels'`, then status 200).

- [ ] **Step 3: Implement the cap**

In `src/dravenpdf/document/images.py`:
- add `import warnings`;
- extend the PIL import to `from PIL import Image, UnidentifiedImageError`;
- add this function above `images_to_pdf`:

```python
def _check_pixels(images: Sequence[bytes], max_pixels: int) -> None:
    """Refuse images larger than ``max_pixels``, reading only their headers."""
    for number, data in enumerate(images, start=1):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    width, height = image.size
        except Image.DecompressionBombError:
            raise LimitExceededError(f"image {number} is too large to open") from None
        except (UnidentifiedImageError, OSError):
            continue  # img2pdf reports unreadable images with its own message
        if width * height > max_pixels:
            raise LimitExceededError(
                f"image {number} is {width * height:,} pixels; the limit is {max_pixels:,}"
            )
```

Add `max_pixels: int | None = None` as the last keyword parameter of `images_to_pdf`. After the `if not images:` check, add:

```python
    if max_pixels is not None:
        _check_pixels(images, max_pixels)
```

Extend the docstring with: "``max_pixels`` refuses any image with more pixels (:class:`LimitExceededError`)."

In `pdf.py`, give `from_images` the same `max_pixels: int | None = None` parameter and pass it through to `image_ops.images_to_pdf`.

In `convert.py`, change the route to take `request: Request` first, and call:

```python
    doc = await asyncio.to_thread(
        PdfDocument.from_images, images, paper=paper, landscape=landscape, margin=margin,
        max_pixels=settings_of(request).max_image_pixels,
    )  # fmt: skip
```

In `docs/http-api.md:200`, change the description to: "Largest page `pdf-to-images` renders (A4 at 600 dpi is 35), and largest image `images-to-pdf` accepts".

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_images_text.py tests/server/test_api.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dravenpdf/document/images.py src/dravenpdf/document/pdf.py src/dravenpdf/server/routes/convert.py docs/http-api.md tests/unit/test_images_text.py tests/server/test_api.py
git commit -m "fix(convert): apply the pixel limit to images-to-pdf inputs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Bounded HTML stamping, with one size-grouping rule

`POST /v1/pdf/stamp` with `html` runs one Chromium render per distinct page size, one after another. Each uses the default 30 s timeout rather than `DRAVENPDF_RENDER_TIMEOUT_MS`, there is no cap on how many sizes there can be, and the size grouping runs on the event loop. `stamp_html` also duplicates `_stamp_each`'s "group by rounded displayed size" rule, and `_place` recomputes the geometry that `_stamp_each` already has.

**Files:**
- Modify: `src/dravenpdf/document/stamp.py:100-149`, `src/dravenpdf/document/pdf.py:345-381`, `src/dravenpdf/render/sync.py:145-160`, `src/dravenpdf/server/deps.py`, `src/dravenpdf/server/config.py`, `src/dravenpdf/server/routes/documents.py:125-164`, `docs/http-api.md` (settings table and the stamp section), `docs/library-api.md:333`
- Test: `tests/unit/test_stamps.py`, `tests/server/conftest.py`, `tests/server/test_api.py`

**Interfaces:**
- Produces:
  - `stamp.pages_by_size(pdf: pikepdf.Pdf, pages: Iterable[int] | None) -> dict[tuple[float, float], list[int]]`;
  - `PdfDocument.stamp_html(..., options: RenderOptions | None = None, max_sizes: int | None = None)`;
  - `Renderer.stamp_html(..., options=None, max_sizes=None)`;
  - `deps.within_deadline(timeout_ms: int, work: Awaitable[T]) -> T`;
  - `Settings.max_html_stamp_sizes: int` (default 10).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_stamps.py`:

```python
from dravenpdf import LimitExceededError, RenderOptions
from dravenpdf.document.stamp import pages_by_size


class _Renderer:
    def __init__(self) -> None:
        self.options: list[RenderOptions | None] = []

    async def from_html(
        self, html: str, options: RenderOptions | None = None, *, base_url: str | None = None
    ) -> PdfDocument:
        self.options.append(options)
        return blank((100, 100))


def test_pages_by_size_groups_displayed_sizes() -> None:
    doc = blank((200, 300), (200.001, 300), (300, 200)).rotate(90, pages=[2])

    assert pages_by_size(doc._pdf, None) == {(200.0, 300.0): [0, 1, 2]}


async def test_stamp_html_keeps_render_options_but_sets_the_page_size() -> None:
    renderer = _Renderer()
    base = RenderOptions(timeout_ms=5_000, paper="Letter")

    await blank((144, 72)).stamp_html(renderer, "<p>x</p>", options=base)

    (used,) = renderer.options
    assert used is not None
    assert used.timeout_ms == 5_000
    assert (used.width, used.height) == ("2.0000in", "1.0000in")


async def test_stamp_html_max_sizes() -> None:
    renderer = _Renderer()

    with pytest.raises(LimitExceededError, match="3 different sizes"):
        await blank((100, 100), (110, 100), (120, 100)).stamp_html(
            renderer, "<p>x</p>", max_sizes=2
        )
    assert renderer.options == []
```

In `tests/server/conftest.py`:
- add `import asyncio`;
- add the field `delay: float = 0` to `FakeRenderer` (after `error`);
- make `_answer` start with:

```python
        if self.delay:
            await asyncio.sleep(self.delay)
```

Append to `tests/server/test_api.py`:

```python
def test_stamp_html_uses_the_server_timeout(client: TestClient, fake: FakeRenderer) -> None:
    response = client.post(
        "/v1/pdf/stamp", files={"file": upload(pdf_bytes())}, data={"html": "<p>x</p>"},
        headers=AUTH,
    )  # fmt: skip

    assert response.status_code == 200, response.text
    (_, _, options) = fake.calls[0]
    assert options is not None and options.timeout_ms == 10_000


def test_stamp_html_caps_distinct_page_sizes(fake: FakeRenderer) -> None:
    settings = Settings(api_key=API_KEY, max_html_stamp_sizes=2)
    with make_client(settings, fake) as c:
        response = c.post(
            "/v1/pdf/stamp", files={"file": upload(pdf_bytes(3))}, data={"html": "<p>x</p>"},
            headers=AUTH,
        )  # fmt: skip

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "limit_exceeded"
    assert fake.calls == []


def test_stamp_html_whole_request_timeout() -> None:
    slow = FakeRenderer(delay=1.0)
    settings = Settings(api_key=API_KEY, render_timeout_ms=100)
    with make_client(settings, slow) as c:
        response = c.post(
            "/v1/pdf/stamp", files={"file": upload(pdf_bytes())}, data={"html": "<p>x</p>"},
            headers=AUTH,
        )  # fmt: skip

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "render_timeout"
```

(`pdf_bytes(3)` makes pages 200, 201 and 202 pt wide, which is three sizes.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_stamps.py tests/server/test_api.py -v -k "by_size or stamp_html"`
Expected: FAIL (`ImportError: pages_by_size`, `unexpected keyword argument 'options'`, `max_html_stamp_sizes` ignored, timeout 30_000, status 200).

- [ ] **Step 3: One size-grouping rule in `stamp.py`**

In `src/dravenpdf/document/stamp.py`, add after `target_pages`:

```python
def _size_key(geometry: _Geometry) -> tuple[float, float]:
    """Pages whose displayed sizes round to the same key share one stamp form."""
    return round(geometry.width, 2), round(geometry.height, 2)


def pages_by_size(
    pdf: pikepdf.Pdf, pages: Iterable[int] | None
) -> dict[tuple[float, float], list[int]]:
    """Target pages (0-based, default all) grouped by displayed size in points."""
    groups: dict[tuple[float, float], list[int]] = {}
    for index in target_pages(pdf, pages):
        groups.setdefault(_size_key(_geometry(pdf.pages[index])), []).append(index)
    return groups
```

Change `_place` to take the geometry instead of computing it:

```python
def _place(
    pdf: pikepdf.Pdf,
    index: int,
    form: pikepdf.Object,
    geometry: _Geometry,
    *,
    opacity: float,
    under: bool,
) -> None:
    """Draw ``form`` (already in ``pdf``, in displayed coordinates) on page ``index``,
    whose geometry is ``geometry``."""
    page = pdf.pages[index]
    name = page.add_resource(form, pikepdf.Name.XObject, prefix="DpStamp")
```

The rest of `_place` stays as it is (delete only the `geometry = _geometry(page)` line). Replace the loop in `_stamp_each`:

```python
    for index in target_pages(pdf, pages):
        geometry = _geometry(pdf.pages[index])
        size = _size_key(geometry)
        if size not in forms:
            forms[size] = make_form(geometry.width, geometry.height)
        _place(pdf, index, forms[size], geometry, opacity=opacity, under=under)
```

- [ ] **Step 4: Bounded `stamp_html`**

In `pdf.py`:
- add `LimitExceededError` to the `dravenpdf.errors` import;
- replace `stamp_html` (lines 345-381) with:

```python
    async def stamp_html(
        self,
        renderer: HtmlRenderer,
        html: str,
        *,
        opacity: float = 1.0,
        pages: Iterable[int] | None = None,
        under: bool = False,
        base_url: str | None = None,
        options: RenderOptions | None = None,
        max_sizes: int | None = None,
    ) -> PdfDocument:
        """Render ``html`` at each target page's size and draw it on the page.

        The HTML page is transparent except for what it draws, so it works for
        watermarks, headers, "PAID" badges and letterheads in any language. It is
        rendered once per distinct page size, with ``options`` (timeout, waits, ...)
        except that the paper size and margins are the page's. ``max_sizes`` caps the
        number of renders (:class:`LimitExceededError`).
        """
        by_size = await asyncio.to_thread(stamp_ops.pages_by_size, self._pdf, pages)
        if not by_size:
            return self
        if max_sizes is not None and len(by_size) > max_sizes:
            raise LimitExceededError(
                f"the pages have {len(by_size)} different sizes; stamping HTML renders "
                f"once per size and allows at most {max_sizes}"
            )
        base = options or RenderOptions()
        stamps: list[tuple[pikepdf.Pdf, Iterable[int] | None]] = []
        for (width, height), indices in by_size.items():
            page_options = base.model_copy(
                update={
                    "width": f"{width / 72:.4f}in",
                    "height": f"{height / 72:.4f}in",
                    "margins": Margins(top="0", right="0", bottom="0", left="0"),
                    "prefer_css_page_size": False,
                }
            )
            stamp = await renderer.from_html(html, page_options, base_url=base_url)
            stamps.append((stamp._pdf, indices))
        # Overlaying is CPU-bound pikepdf work; keep it off the event loop. All sizes go
        # onto one copy, so the document is rewritten once however many sizes it has.
        return await asyncio.to_thread(self._overlay_all, stamps, opacity=opacity, under=under)
```

In `src/dravenpdf/render/sync.py`, `Renderer.stamp_html`:
- add the parameters `options: RenderOptions | None = None, max_sizes: int | None = None`;
- pass them through as `options=options, max_sizes=max_sizes`;
- import `RenderOptions` from `dravenpdf.options` if it isn't already.

- [ ] **Step 5: Server setting, deadline helper, route**

In `src/dravenpdf/server/config.py`, after `max_image_megapixels`:

```python
    # HTML stamps render once per distinct page size.
    max_html_stamp_sizes: int = Field(default=10, ge=1)
```

In `src/dravenpdf/server/deps.py`:
- add `RenderTimeoutError` to the `dravenpdf.errors` import;
- add after `timed`:

```python
async def within_deadline[T](timeout_ms: int, work: Awaitable[T]) -> T:
    """``work``, bounded as a whole by the server's render timeout."""
    try:
        async with asyncio.timeout(timeout_ms / 1000):
            return await work
    except TimeoutError:
        raise RenderTimeoutError(
            f"render did not finish within {timeout_ms} ms", timeout_ms=timeout_ms
        ) from None
```

In `src/dravenpdf/server/routes/documents.py`:
- add `timed` and `within_deadline` to the `deps` import;
- add `from dravenpdf.options import RenderOptions`;
- replace the `else:` branch of `stamp` with:

```python
    else:
        assert html is not None
        settings = settings_of(request)
        work = doc.stamp_html(
            renderer_of(request), html, opacity=1.0 if opacity is None else opacity,
            pages=targets, under=under,
            options=RenderOptions(timeout_ms=settings.render_timeout_ms),
            max_sizes=settings.max_html_stamp_sizes,
        )  # fmt: skip
        result = await timed(request, "stamp", within_deadline(settings.render_timeout_ms, work))
```

Task 8 replaces the opacity literal here, so leave it as it is.

Docs:
- in `docs/http-api.md`'s settings table add `| DRAVENPDF_MAX_HTML_STAMP_SIZES | 10 | Most distinct page sizes a stamp with html may have (one render each) |`;
- where the stamp endpoint is described, add: "`html` renders once per distinct page size; the whole request is bounded by `DRAVENPDF_RENDER_TIMEOUT_MS`."
- in `docs/library-api.md` near line 333, document the `options` and `max_sizes` parameters.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/unit/test_stamps.py tests/server -q && uv run mypy src`
Expected: all PASS, mypy clean.

Run: `uv run pytest tests/integration/test_templates_and_stamps.py tests/integration/test_cli_render.py tests/server/test_api_browser.py -q -k stamp`
Expected: all PASS (real Chromium).

- [ ] **Step 7: Commit**

```bash
git add src/dravenpdf/document/stamp.py src/dravenpdf/document/pdf.py src/dravenpdf/render/sync.py src/dravenpdf/server/deps.py src/dravenpdf/server/config.py src/dravenpdf/server/routes/documents.py docs/http-api.md docs/library-api.md tests/unit/test_stamps.py tests/server/conftest.py tests/server/test_api.py
git commit -m "fix(stamp): bound HTML stamping by the server timeout and a size cap

Share one size-grouping rule between stamp_html and _stamp_each, group sizes
off the event loop, and pass each page's geometry into _place.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: `_writable_copy()` for the clone-then-modify pattern

`ops.clone(self._rewritable())` appears 7 times in `pdf.py`. For a signed document `_rewritable()` has already cloned, so the file is written out and parsed again twice.

**Files:**
- Modify: `src/dravenpdf/document/pdf.py:249,285,305,338,425,431,523,529`
- Test: `tests/unit/test_signing.py`

**Interfaces:**
- Produces: `PdfDocument._writable_copy(*, stacklevel: int = 4) -> pikepdf.Pdf`, a private PDF the caller may modify, with signatures removed.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_signing.py`. It uses that file's existing signed-document fixture: find the helper that returns a signed `PdfDocument` (e.g. `signed_visibly(key)`, with the `key` fixture) and use it here.

```python
def test_signed_edits_clone_once_and_warn_at_the_caller(
    key: SigningKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dravenpdf.document import pages as ops

    signed = signed_visibly(key)
    clones: list[object] = []
    real_clone = ops.clone

    def counting_clone(pdf: pikepdf.Pdf) -> pikepdf.Pdf:
        clones.append(pdf)
        return real_clone(pdf)

    monkeypatch.setattr(ops, "clone", counting_clone)
    with pytest.warns(SignatureInvalidatedWarning) as caught:
        signed.stamp_text("x")

    assert len(clones) == 1
    assert caught[0].filename == __file__
```

Add any missing imports (`pikepdf`, `SignatureInvalidatedWarning` from `dravenpdf`).

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/test_signing.py -v -k clone_once`
Expected: FAIL with `assert 2 == 1`.

- [ ] **Step 3: Add `_writable_copy` and use it**

In `pdf.py`, add after `_rewritable`:

```python
    def _writable_copy(self, *, stacklevel: int = 4) -> pikepdf.Pdf:
        """A private copy for an operation to modify: :meth:`_rewritable`'s PDF, which
        is already a fresh copy for a signed document, else a clone of this one."""
        source = self._rewritable(stacklevel=stacklevel)
        return ops.clone(source) if source is self._pdf else source
```

Replace each `ops.clone(self._rewritable())` with `self._writable_copy()`, in `set_metadata`, `stamp_text`, `stamp_image`, `fill_form`, `flatten_form`, `encrypt` and `decrypt`. In `_overlay_all`, replace `ops.clone(self._rewritable(stacklevel=stacklevel))` with `self._writable_copy(stacklevel=stacklevel + 1)`. Leave `copy()` and `to_bytes()` unchanged, because they don't follow the pattern.

Warning attribution: before, the call chain was `_rewritable` (1) → public method (2) → user (3). Now `_writable_copy` adds a frame, which is why its default is 4. `overlay` passes `stacklevel=4` to `_overlay_all`, which adds 1 more for `_writable_copy`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dravenpdf/document/pdf.py tests/unit/test_signing.py
git commit -m "refactor(document): one _writable_copy() instead of clone(_rewritable())

Signed documents are now copied once per operation instead of twice.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

**Batch B gate:** run `make check && uv run pytest -m browser -q`, then send Tasks 4–7 for review together.

---

## Batch C: front ends

### Task 8: The library owns the encrypt, signature-box and stamp-opacity rules

The CLI and HTTP API each re-implement three rules, and they have drifted apart:
- **Encrypt:** "set a password or restrict a permission". The CLI tests `owner_password is None`, the HTTP route tests `not owner_password`.
- **Visible signature box:** the CLI checks nothing, so `--visible 0,...` becomes page index -1, which pyHanko reads as the *last* page.
- **Stamp opacity defaults:** 0.3 for text and 1.0 for the others, written out in both front ends.

**Files:**
- Modify: `src/dravenpdf/document/pdf.py` (`encrypt`, `sign`), `src/dravenpdf/cli.py` (stamp ~418-443, sign ~531-540, encrypt ~613-618), `src/dravenpdf/server/routes/documents.py` (stamp 147-163, encrypt 207-212, sign 323-327), `docs/library-api.md` (encrypt and sign sections)
- Test: `tests/unit/test_encryption.py`, `tests/unit/test_signing.py`, `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: Task 6's `stamp` route branch.
- Produces:
  - `PdfDocument.encrypt()` raises `PdfOperationError("set a password or restrict a permission")` when there is no user password, `owner_password is None`, and every `allow_*` is True;
  - `PdfDocument.sign(box=...)` raises `PdfOperationError` for a box off the page range, with a negative x/y, or with a non-positive width/height.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_encryption.py`:

```python
def test_encrypt_needs_a_password_or_a_restriction() -> None:
    with pytest.raises(PdfOperationError, match="set a password or restrict a permission"):
        sample().encrypt()
    assert sample().encrypt(allow_copy=False).is_encrypted
```

Append to `tests/unit/test_signing.py` (the `blank()` helper and `key` fixture are already in that file):

```python
@pytest.mark.parametrize(
    ("box", "message"),
    [
        (SignatureBox(page=5, x=10, y=10, width=100, height=40), "past the last page"),
        (SignatureBox(page=-1, x=10, y=10, width=100, height=40), "must not be negative"),
        (SignatureBox(page=0, x=-1, y=10, width=100, height=40), "x and y"),
        (SignatureBox(page=0, x=10, y=10, width=0, height=40), "width and height"),
    ],
)
def test_sign_checks_the_box(key: SigningKey, box: SignatureBox, message: str) -> None:
    with pytest.raises(PdfOperationError, match=message):
        blank().sign(key, box=box)
```

Append to `tests/unit/test_cli.py` (the `pdf` fixture already makes an input PDF). Find the fixture or helper that writes a `.p12` key for the CLI sign tests in that file or `tests/signing_fixture.py`, and pass its path as `--key`.

```python
def test_sign_visible_page_zero_is_refused(pdf: Path, tmp_path: Path, p12_path: Path) -> None:
    result = runner.invoke(
        app,
        ["sign", str(pdf), "-o", str(tmp_path / "s.pdf"), "--key", str(p12_path),
         "--key-password", "", "--visible", "0,10,10,100,40"],
    )  # fmt: skip

    assert result.exit_code == 1
    assert "1-based" in result.output
```

If no `.p12` fixture exists for CLI tests, build one with the helpers in `tests/signing_fixture.py` inside a local fixture named `p12_path`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_encryption.py tests/unit/test_signing.py tests/unit/test_cli.py -v -k "password_or or checks_the_box or page_zero"`
Expected: all FAIL (no error raised / exit code 0).

- [ ] **Step 3: Put the rules in `PdfDocument`**

In `pdf.py` `encrypt`, directly after `user = reveal(user_password)` (and before the `owner = ...` line):

```python
        if (
            not user
            and owner_password is None
            and all((allow_print, allow_copy, allow_modify, allow_annotate, allow_forms))
        ):
            raise PdfOperationError("set a password or restrict a permission")
```

Add to the docstring: "With no password and no restriction there is nothing to protect, so that raises :class:`PdfOperationError`."

In `pdf.py` `sign`, after the encryption check:

```python
        if box is not None:
            _check_box(box, self.page_count)
```

Add this module-level function after `_pages_source`:

```python
def _check_box(box: SignatureBox, page_count: int) -> None:
    if box.page < 0:
        raise PdfOperationError("signature page must not be negative (pages are 0-based)")
    if box.page >= page_count:
        raise PdfOperationError(
            f"signature page {box.page + 1} is past the last page ({page_count})"
        )
    if box.x < 0 or box.y < 0:
        raise PdfOperationError("signature box x and y must not be negative")
    if box.width <= 0 or box.height <= 0:
        raise PdfOperationError("signature box width and height must be positive")
```

- [ ] **Step 4: Remove the copies from the front ends**

`server/routes/documents.py`:
- **stamp:** before the branches, add `styling = {} if opacity is None else {"opacity": opacity}`. In each of the three calls, replace the `opacity=... if opacity is None else opacity` argument with `**styling`. This works for `asyncio.to_thread(doc.stamp_text, text, ..., **styling)` and for `doc.stamp_html(..., **styling)`.
- **encrypt:** delete the `if (not user_password and ...)` block (lines 207-212), and pass `owner_password=owner_password or None`, because an empty form field means "not given", as before.
- **sign:** replace lines 324-327 with:

```python
    if page is not None and x is not None and y is not None and width and height:
        box = SignatureBox(page=page - 1, x=x, y=y, width=width, height=height)
```

The library now reports a page past the end ("past the last page"; still 400 because `PdfOperationError` is `invalid_request`).

`cli.py`:
- **stamp:** same `styling` dict and `**styling` in the four calls (`stamp_text`, `stamp_image`, `overlay`, `renderer.stamp_html`).
- **encrypt:** delete the `if (not user_password and owner_password is None ...)` block (lines 613-618). `main()` prints the library's `PdfOperationError` message as `error: ...`. Check the existing CLI encrypt test's expected text and update it to "set a password or restrict a permission" if needed.
- **sign:** after parsing `page`, add the 1-based check, which is a CLI parsing rule (the library is 0-based):

```python
        if page < 1 or page != int(page):
            _fail("--visible PAGE is 1-based: 1 is the first page")
```

- [ ] **Step 5: Update docs**

In `docs/library-api.md`:
- in the encrypt section, note that `encrypt()` with no password and no restriction raises `PdfOperationError`;
- in the sign section, note that the box is checked against the page count.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/unit tests/server -q`
Expected: all PASS, including `test_encrypt_needs_a_password_or_restriction` (HTTP, still 400) and `test_sign_errors` ("past the last page", still 400).

- [ ] **Step 7: Commit**

```bash
git add src/dravenpdf/document/pdf.py src/dravenpdf/cli.py src/dravenpdf/server/routes/documents.py docs/library-api.md tests/unit/test_encryption.py tests/unit/test_signing.py tests/unit/test_cli.py
git commit -m "fix: enforce encrypt, signature-box and opacity rules in the library

The CLI accepted --visible page 0 (signing the last page) and disagreed with
the HTTP API on an empty owner password; both now defer to PdfDocument.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Shared helpers for validation errors, JSON objects and the 413 body

**Files:**
- Modify: `src/dravenpdf/errors.py`, `src/dravenpdf/server/errors.py:55-60`, `src/dravenpdf/cli.py:72-94,314,491,469`, `src/dravenpdf/server/deps.py:84-86`, `src/dravenpdf/server/routes/render.py:121-128`, `src/dravenpdf/server/routes/documents.py:255-262`, `src/dravenpdf/server/routes/convert.py` (pages_arg use), `src/dravenpdf/server/middleware.py:97-116`
- Test: `tests/unit/test_cli.py`, `tests/server/test_api.py`

**Interfaces:**
- Produces:
  - `dravenpdf.errors.describe_validation_errors(errors: Sequence[Mapping[str, Any]]) -> str`, re-exported by `server/errors.py`;
  - `deps.json_object_field(raw: str | None, field: str) -> dict[str, Any] | None`;
  - `cli._read_json_object(path: Path, flag: str) -> dict[str, Any]`.
- Removes: `cli._pages` and `deps.pages_arg`; callers use `optional_page_ranges(spec, doc.page_count)` directly.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_cli.py`:

```python
@pytest.mark.parametrize("command", ["template", "fill-form"])
def test_invalid_json_data_is_a_clean_error(tmp_path: Path, pdf: Path, command: str) -> None:
    (tmp_path / "t.html").write_text("x")
    (tmp_path / "d.json").write_text("{not json")
    source = str(tmp_path / "t.html") if command == "template" else str(pdf)

    result = runner.invoke(
        app, [command, source, "--data", str(tmp_path / "d.json"), "-o", str(tmp_path / "o.pdf")]
    )

    assert result.exit_code == 1
    assert "not valid JSON" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
```

Append to `tests/server/test_api.py`:

```python
def test_body_limit_response_shape(fake: FakeRenderer) -> None:
    with make_client(Settings(api_key=API_KEY, max_body_mb=0.0001), fake) as c:
        response = c.post("/v1/render/html", content=b"x" * 1000, headers=AUTH)

    assert response.status_code == 413
    assert response.headers["content-type"] == "application/json"
    assert response.json()["error"]["code"] == "payload_too_large"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_cli.py -v -k invalid_json`
Expected: FAIL. `JSONDecodeError` escapes, so `result.exception` is a `JSONDecodeError`.

Run: `uv run pytest tests/server/test_api.py -v -k response_shape`
Expected: PASS already. It pins the current behaviour before the refactor.

- [ ] **Step 3: One validation-error formatter**

Move `describe_validation_errors` from `server/errors.py:55-60` to the end of `src/dravenpdf/errors.py`, unchanged except for the signature:

```python
def describe_validation_errors(errors: Sequence[Mapping[str, Any]]) -> str:
    """One line from pydantic/FastAPI validation errors: "field.path: message; ...".
    Uses only each error's location and message, never its input (which may be secret)."""
    details = "; ".join(
        f"{'.'.join(str(p) for p in e['loc'] if p != 'body')}: {e['msg']}" for e in errors
    )
    return details or "invalid request"
```

Add `from collections.abc import Mapping, Sequence` and `Any` to `errors.py`'s imports.

In `server/errors.py`:
- delete the function;
- add `from dravenpdf.errors import describe_validation_errors as describe_validation_errors`, so `routes/render.py` keeps importing it from there.

In `cli.py` `_load_auth`, replace the `problems = ...` expression with:

```python
        problems = describe_validation_errors(exc.errors(include_input=False))
```

and add it to the `dravenpdf.errors` import.

- [ ] **Step 4: One JSON-object parser per front end**

In `server/deps.py`, add `import json` and:

```python
def json_object_field(raw: str | None, field: str) -> dict[str, Any] | None:
    """A form field holding a JSON object, or None when absent."""
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError("invalid_request", f"{field}: not valid JSON ({exc.msg})") from None
    if not isinstance(value, dict):
        raise ApiError("invalid_request", f"{field}: must be a JSON object")
    return value
```

In `routes/render.py`:
- replace lines 121-128 with `values = json_object_field(data, "data")`;
- drop the now-unused `json` import.

In `routes/documents.py` `form_fill`, replace lines 255-262 with:

```python
    parsed = json_object_field(values, "values") or {}
    if not all(isinstance(v, str | bool) or v is None for v in parsed.values()):
        raise ApiError("invalid_request", "values: must be an object of text, true/false or null")
```

Then drop the now-unused `json` import.

In `cli.py`, add after `_load_auth`:

```python
def _read_json_object(path: Path, flag: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError):
        _fail(f"{flag} {path}: not valid JSON")
    if not isinstance(value, dict):
        _fail(f"{flag} must contain a JSON object")
    return value
```

In `template`, replace the two `values = json.loads(...)` / `isinstance` lines with `values = _read_json_object(data, "--data") if data is not None else {}`. In `fill_form`, use `values = _read_json_object(data, "--data")`. Also delete the dead `assert output is not None` after `_fail(...)` in `metadata` (line 469): `_fail` is `NoReturn`.

- [ ] **Step 5: Inline the page-range wrappers**

Delete `cli._pages` (lines 94-95) and `deps.pages_arg` (lines 84-86). Replace every call:
- `_pages(pages, doc)` → `optional_page_ranges(pages, doc.page_count)` (in `cli.py`);
- `pages_arg(pages, doc)` → `optional_page_ranges(pages, doc.page_count)` (in `routes/documents.py` and `routes/convert.py`).

Import `optional_page_ranges` from `dravenpdf.document.pages` where needed, and remove `pages_arg` from the `deps` imports. Run `grep -rn "pages_arg\|_pages(" src tests`; the result must be empty.

- [ ] **Step 6: Build the 413 body with `error_response`**

In `server/middleware.py`:
- drop `import json`;
- add `from dravenpdf.server.errors import error_response`;
- change the two `await self._reject(send)` calls to `await self._reject(scope, receive, send)`;
- replace `_reject` with:

```python
    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = error_response(
            "payload_too_large", f"request body is larger than {self.max_bytes} bytes"
        )
        await response(scope, receive, send)
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/unit/test_cli.py tests/server -q && uv run mypy src && uv run ruff check .`
Expected: all PASS, clean. `test_bad_json_data`, `test_body_limit_*` and the form-fill tests must still pass unchanged.

- [ ] **Step 8: Commit**

```bash
git add src/dravenpdf/errors.py src/dravenpdf/server/errors.py src/dravenpdf/cli.py src/dravenpdf/server/deps.py src/dravenpdf/server/routes/render.py src/dravenpdf/server/routes/documents.py src/dravenpdf/server/routes/convert.py src/dravenpdf/server/middleware.py tests/unit/test_cli.py tests/server/test_api.py
git commit -m "refactor: share validation, JSON-object and 413 helpers across front ends

Bad JSON in the CLI's --data is now a one-line error instead of a traceback.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

**Batch C gate:** run `make check && uv run pytest -m browser -q`, then send Tasks 8–9 for review together.

---

## Final verification

- [ ] `make check` passes.
- [ ] `uv run pytest` (full suite, with Chromium) passes.
- [ ] `grep -rn "ops.clone(self._rewritable" src` is empty.
- [ ] `grep -rn "MAX_REDIRECTS" src` shows only `_redirect_gate.py`.
- [ ] `grep -rn "pages_arg\|0.3 if opacity" src` is empty.
- [ ] `uv run python -X importtime -c "import dravenpdf.render._proxy_addon" 2>&1 | grep -E "pikepdf|playwright|pydantic"` is empty.
- [ ] Update the status line in `docs/roadmap.md` only if a milestone changed (none is expected).
