# Strict Network Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Guard each Chromium network request, including HTTPS redirects and popup first navigations, with an independent fail-closed proxy while leaving cookie selection to Chromium.

**Architecture:** A loopback mitmdump process per render inspects TLS and applies `RequestGuard` policy at CONNECT and at each HTTP request header. A single mandatory per-context proxy and scoped browser trust route all network traffic through it. Playwright retains page control and local-file routing; the proxy is the security boundary for HTTP(S)/WS(S).

**Tech Stack:** Python 3.12+, Playwright Chromium, mitmproxy 12.x addon hooks, cryptography, pytest/pytest-asyncio.

**Spec:** [`docs/superpowers/specs/2026-09-26-strict-network-gate-design.md`](../specs/2026-09-26-strict-network-gate-design.md)

## Global Constraints

- The network gate must be ready before a render context exists; shutdown closes the context before the proxy.
- One proxy per render listens on loopback, requires a random proxy credential, and has no `DIRECT` fallback or implicit loopback bypass.
- Every network request is checked before request bytes go upstream, including requests in reused HTTPS tunnels. No fallback to `route.fetch` or CDP on failure.
- Chromium chooses `Cookie`; the proxy never constructs it. `RenderAuth` headers apply only at their exact scheme, host, and port.
- Upstream TLS validation stays enabled. Proxy trust is scoped to the renderer browser; no system trust-store change or blanket HTTPS-error ignore.
- Preserve existing SSRF allowlists, file-root guard, bundle 200/404 behavior, `on_blocked`, report semantics, ten-redirect bound when Playwright is healthy, and credential redaction.
- The compatibility assumption is Python >=3.12 and mitmproxy 12.x. Do not use an old mitmproxy to retain Python 3.11.
- A failed feasibility check triggers design adjustment and more tests, not a weaker runtime path or a claim that strict enforcement works.

## Review Focus

1. **Proxy death on an open HTTPS tunnel** (Task 1 and Task 4): a reused connection must not reach the server after the gate dies.
2. **Loopback and link-local bypass** (Task 1 and Task 4): requests must go through the proxy even when Chromium would normally bypass it.
3. **CONNECT authority mismatch** (Task 2): a decrypted Host or HTTP/2 authority differing from the approved tunnel target must be rejected.
4. **An addon exception** (Task 2 and Task 4): mitmproxy must block the request and notify the parent; logging an exception is insufficient.
5. **A cross-origin redirect with cookies and headers** (Task 4): the next hop uses Chromium's Cookie and only headers configured for its exact origin.

## File map

- `pyproject.toml`, `uv.lock`: Python floor and mitmproxy/cryptography direct dependencies.
- `src/dravenpdf/render/_proxy_ca.py`: temporary pool CA creation, scoped Chromium SPKI trust, cleanup.
- `src/dravenpdf/render/_proxy_protocol.py`: private startup/block/fatal event format and redaction.
- `src/dravenpdf/render/_proxy_addon.py`: mitmdump CONNECT/request/header checks, in-memory bundle responses, exact-origin headers, fail-closed hook wrapper.
- `src/dravenpdf/render/_proxy_gate.py`: per-render subprocess, private files and control channel, readiness, block events, teardown.
- `src/dravenpdf/render/guards.py`: policy reused by addon and local-file routing only; remove manual HTTP redirect fetch.
- `src/dravenpdf/render/pool.py`: pool CA lifecycle and scoped browser launch trust.
- `src/dravenpdf/render/renderer.py`: start gate, create proxied context, and close context before gate.
- `src/dravenpdf/render/assets.py`: share bundle response data with the addon.
- `tests/integration/test_proxy_gate.py`: wire-level feasibility and failure tests.
- `tests/integration/test_cookie_https.py`, `tests/integration/test_auth.py`, `tests/integration/test_guard_network.py`, `tests/unit/test_guards.py`: cookie/source matrix and regressions.
- `docs/decisions.md`, `docs/architecture.md`, `docs/library-api.md`, `docs/roadmap.md`, `CLAUDE.md`, `README.md`: observed behavior, security and compatibility changes.

## Task 1: Prove mandatory proxy transport and scoped trust

**Files:** Create `tests/integration/test_proxy_gate.py`, `src/dravenpdf/render/_proxy_ca.py`; modify `pyproject.toml`, `uv.lock` and `src/dravenpdf/render/pool.py` for the dependency and CA lifecycle.

**Interfaces:** `ProxyCA.create() -> ProxyCA`, `.confdir: Path`, `.spki_hash: str`, `.close() -> None`; `BrowserPool` adds that SPKI at Chromium launch. The test may start `mitmdump` directly before `_proxy_gate.py` exists.

- [ ] Write a browser test that loads an HTTPS site through one loopback mitmdump proxy and asserts the server sees a request with Chromium's cookie. Use the Task 1 HTTPS fixture and a CA unique to the test browser; assert upstream invalid TLS still fails.
- [ ] Add a test for `http://127.0.0.1`, `https://a.test`, `ws://`, and `wss://` proving each reaches the proxy. Configure a single manual proxy with `bypass="<-loopback>"`; kill it before a second request and assert zero direct destination requests.
- [ ] Add a reused HTTPS connection case: after one successful request, hold the next request before forwarding, kill the proxy, and assert no destination request bytes. This is the strict failure boundary.
- [ ] Generate CA material in a mode-0700 temporary directory and derive the base64 SHA-256 SPKI hash. Verify Chromium accepts only that proxy CA while mitmproxy validates upstream TLS. If CA SPKI trust is not supported by the locked Chromium, replace it with another browser-scoped trust mechanism and repeat the tests; never use `ignore_https_errors=True`.
- [ ] Update the dependency floor to Python 3.12 and add a compatible current mitmproxy major, then run `uv sync --all-extras` and the focused browser tests. Commit the passing transport probe as `test: prove strict proxy transport`.

## Task 2: Build and test the proxy policy addon

**Files:** Create `src/dravenpdf/render/_proxy_protocol.py`, `src/dravenpdf/render/_proxy_addon.py`, `tests/unit/test_proxy_addon.py`; modify `src/dravenpdf/render/assets.py` to share bundle response data and `src/dravenpdf/render/guards.py` to expose a serializable policy snapshot.

**Interfaces:** `ProxyPolicy` is a serializable snapshot of allowed hosts, private-network setting, bundle manifest, exact-origin headers and deadline. `_proxy_addon` reads that snapshot from a mode-0600 file; its `http_connect`, `requestheaders`, and WebSocket-related hooks return an explicit block on policy errors. `_proxy_protocol` carries `ready`, `blocked`, and `fatal` messages without header values.

- [ ] Write failing unit tests for a blocked CONNECT, an allowed CONNECT followed by a disallowed inner authority, a same-host different-port header, a bundle hit/miss, missing proxy auth, and an exception in the policy resolver. Assert every failure creates a blocking response and emits a sanitized event.
- [ ] Extract the network URL decision from `RequestGuard` into a policy unit callable in the addon process. Reuse the same allowlist/public-IP logic; keep the file-root decision in the parent for `file://`.
- [ ] Implement CONNECT and `requestheaders` hooks with an outer `try/except BaseException` that denies and emits `fatal` before return. Reject `Host`/`:authority` mismatches and raw TCP/UDP modes. Keep `connection_strategy=lazy`, `upstream_cert=false`, `ssl_insecure=false`, and `ignore_hosts=[]`; assert no upstream request precedes the hook.
- [ ] Apply `RenderAuth` headers only when `origin_of(flow.request.pretty_url)` matches; leave `Cookie` untouched, including when absent. Fulfill owned bundle URLs directly from data with the existing 200/404 and cache headers, with no upstream connection.
- [ ] Run `uv run pytest tests/unit/test_proxy_addon.py -q`, mypy and ruff; commit as `feat: add fail-closed proxy policy addon`.

## Task 3: Wire the proxy into render lifecycle

**Files:** Create `src/dravenpdf/render/_proxy_gate.py`; modify `src/dravenpdf/render/pool.py`, `src/dravenpdf/render/renderer.py`, `src/dravenpdf/render/guards.py`, `tests/unit/test_guards.py`, `tests/integration/test_proxy_gate.py`.

**Interfaces:** `ProxyGate.start(policy: ProxyPolicy, ca: ProxyCA, deadline: float) -> ProxyGate`, `.proxy_options: dict[str, str]`, `.blocked: list[tuple[str, str]]`, `.fatal: bool`, `.close() -> None`; `BrowserPool` exposes its CA to a context lease. The gate is started before `pool.context(...)`, and closed after the context exits.

- [ ] Write failing lifecycle tests: proxy startup error creates no context, render cancellation closes proxy and context, parent loses control channel and proxy denies, and two concurrent renders never exchange credentials or block events.
- [ ] Implement the subprocess launcher with a private policy/config directory, random proxy auth, bounded startup/readiness, stdout event parsing, stderr draining without surfacing secrets, and deterministic teardown. Use a single port selected with bounded retry; refuse any `DIRECT` or non-loopback listener configuration.
- [ ] Change `_render` to start the proxy before `pool.context`, pass `proxy=gate.proxy_options` with `<-loopback>`, and close the context before the gate. Keep the local-file route and WebSocket policy where needed, but remove the `route.fetch(max_redirects=0)` HTTP path. The proxy becomes the only network transport.
- [ ] Keep blocked-event synchronization deterministic before `guard.raise_if_blocked()` and before reporting. Map policy blocks, proxy failures, and deadline expiry to existing error types. A lost event/control channel must fail the render.
- [ ] Run `uv run pytest tests/unit/test_guards.py tests/integration/test_proxy_gate.py tests/integration/test_auth.py tests/integration/test_assets.py -q`; commit as `feat: route renders through strict proxy gate`.

## Task 4: Complete wire-level regressions and failure injection

**Files:** Modify `tests/integration/test_cookie_https.py`, `tests/integration/test_auth.py`, `tests/integration/test_guard_network.py`, and `tests/integration/test_proxy_gate.py`. Fix failures in their owning implementation files from Tasks 1–3.

**Interfaces:** No new public API. The HTTPS fixture's wire records are the oracle for cookies and destination traffic.

- [ ] Add the source matrix (`url`, `html`, `html_base_url`, `file`, `template_source`, `template_dir`, `bundle`, `bundle_template`) comparing Strict, Lax and `None; Secure` cookies with an unguarded Chromium baseline under both third-party-cookie settings.
- [ ] Test image, fetch, top-level GET, A→B and A→B→A redirects, scheme changes, `Set-Cookie` on a redirect, and 307/308 POST body preservation. Assert each wire hop's Cookie equals baseline and configured headers appear only at the exact origin/port.
- [ ] Test blocked loopback, metadata, disallowed host, HTTPS CONNECT, redirects, popup first navigation and popup redirect, frames, workers, WS and WSS. Assert zero destination request bytes, including after a proxy is killed or addon policy raises.
- [ ] Test concurrent renders, mismatched CONNECT authority, missing proxy credential, invalid upstream certificate, browser disconnect, context cancellation, deadline, proxy process exit, and a request paused in the proxy during shutdown. Assert no leaks, hangs, or secret-bearing logs/errors.
- [ ] Run all targeted browser suites and `make check`; commit as `test: cover strict proxy cookies and failures`.

## Task 5: Document and verify the new contract

**Files:** Modify `docs/decisions.md`, `docs/architecture.md`, `docs/library-api.md`, `docs/roadmap.md`, `CLAUDE.md`, and `README.md` for the observed contract. The Python floor belongs to Task 1's `pyproject.toml` change.

**Interfaces:** No public auth input changes. Python minimum becomes 3.12 under the selected dependency choice.

- [ ] Update D11 to state that Chromium selects cookies on every hop and the proxy checks and adds exact-origin headers before forwarding. Remove the later-hop SameSite limitation only after Task 4 proves it.
- [ ] Document temporary CA trust, upstream TLS validation, proxy lifecycle, local-file guard, certificate-pinning/HTTP3 limits, performance cost, Python floor, and fail-closed errors. Remove old `route.fetch` development guidance.
- [ ] Run `make check`, `uv run pytest -m browser`, and `git diff --check` from fresh output. Record actual test counts and any remaining limitation; commit as `docs: describe strict network gate`.
- [ ] Request an independent whole-branch review of the network boundary. Fix findings and rerun affected gates before presenting the branch for integration.

## Handoff

The user requested one subagent at a time and authorized continuing through design and feasibility checks. Run each task with one implementer, then one reviewer, sequentially. A failed probe requires changing the design and repeating its test; it never authorizes a weaker runtime path. Do not push or merge without separate authorization.

## Primary references

- [Chromium proxy behavior](https://chromium.googlesource.com/chromium/src/+/HEAD/net/docs/proxy.md)
- [Playwright context proxy](https://playwright.dev/python/docs/network)
- [mitmproxy hooks](https://docs.mitmproxy.org/stable/api/events.html)
- [mitmproxy options](https://docs.mitmproxy.org/stable/concepts/options/)
