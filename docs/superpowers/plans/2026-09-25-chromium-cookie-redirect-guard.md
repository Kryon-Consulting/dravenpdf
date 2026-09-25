# Chromium Cookie and Redirect Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every redirect hop use Chromium's cookie policy while the guard checks each destination before network access, and prove HTTPS cross-site cookie behavior for every render source.

**Architecture:** Keep `RequestGuard` as the URL and credential policy. Replace its manual HTTP redirect fetch with a per-render Chromium request interceptor that checks each request and redirect hop, then lets Chromium continue the network request and choose cookies. Serve bundles from memory, retain file and WebSocket rules, and cover popups before their first navigation. A feasibility probe gates the replacement; no product path changes if it fails.

**Tech Stack:** Python 3.11+, Playwright Chromium and CDP Fetch, pytest/pytest-asyncio, `cryptography` for test certificates, existing `BrowserPool`, `RenderAuth`, and `AssetBundle`.

**Spec:** [`docs/superpowers/specs/2026-09-25-chromium-cookie-redirect-guard-design.md`](../specs/2026-09-25-chromium-cookie-redirect-guard-design.md)

## Global Constraints

- Every render uses a fresh context; cookies and storage state are loaded before navigation. No auth state crosses renders.
- Every network target, including a popup's first navigation and redirects, must be checked before access. A setup or interception failure fails closed.
- Cookies remain Chromium-managed. Never synthesize or copy `Cookie` from a store or earlier hop; configured headers stay on their exact scheme, host, and port.
- Keep current SSRF policy, file-root and bundle rules, WebSocket guard, `on_blocked` behavior, ten-redirect bound, and secret redaction.
- Public Python, CLI, and HTTP auth inputs stay the same. No unsafe fallback option or TLS-intercepting proxy.
- Python stays 3.11+ and Playwright stays >=1.51 unless the feasibility gate proves a required version change, in which case stop for design review.
- Do not modify the current production guard until Task 2's feasibility gate passes. If it fails, record the precise behavior and stop; do not approximate browser cookie rules in Python.

## Review Focus

These inputs are easy to miss even after the main cookie tests pass. Their tests belong to the indicated task.

1. **Different ports on the same host** (Task 3): a configured header for one port must not reach the other, while cookie behavior remains Chromium's.
2. **A 307 or 308 POST redirect** (Task 4): method and body survive according to Chromium; credentials and SSRF checks still run at the target.
3. **A redirect response that sets a cookie** (Task 4): the next hop sees exactly the cookie Chromium selects, including SameSite restrictions.
4. **A popup opened during a concurrent render** (Task 4): its first request uses its own render's guard and credentials, with no cross-context leakage.
5. **Interception lost while a request is paused** (Task 4): the render fails without sending the request, exposing credentials, or waiting until the deadline.

---

## File map

- `tests/https_fixture.py`: local HTTPS server, ephemeral certificate, test-only browser flags, cookie-policy helper, and wire-header request log; expose its fixtures from `tests/conftest.py`.
- `tests/integration/test_cookie_https.py`: unguarded Chromium baseline and guarded source/redirect cookie matrix.
- `tests/integration/test_cdp_probe.py`: permanent feasibility tests for hop, header, bundle, target, and cancellation coverage; initial exploratory code may be discarded after the gate.
- `src/dravenpdf/render/_cdp_guard.py`: one render's CDP sessions, paused-request lifecycle, target coverage, and CDP response/abort calls.
- `src/dravenpdf/render/guards.py`: existing URL policy, blocked reporting, file/WebSocket routing, and the install/close boundary for `_cdp_guard.py`.
- `src/dravenpdf/render/assets.py`: reusable in-memory response data for CDP fulfillment; no network path for the bundle origin.
- `src/dravenpdf/render/renderer.py`: install guard after creating the page but before loading it, then close the guard before closing its context.
- `tests/unit/test_guards.py`, `tests/integration/test_auth.py`, and `tests/integration/test_guard_network.py`: policy, credential, popup, failure, and compatibility regressions.
- `docs/decisions.md`, `docs/architecture.md`, `docs/library-api.md`, `docs/roadmap.md`, and `CLAUDE.md`: describe only behavior proved by tests.

### Task 1: Build the HTTPS baseline fixture

**Files:** Create `tests/https_fixture.py`, `tests/integration/test_cookie_https.py`; modify `tests/conftest.py`.

**Interfaces:** `https_sites` exposes `a_origin`, `b_origin`, `http_origin`, and `received(tag)`; `test_browser_pool()` launches only the test Chromium with host mappings and test certificate trust. `set_third_party_cookie_restriction(page: Page, enabled: bool)` uses test-only CDP `Network.setCookieControls` before navigation. Later tasks consume these fixtures. No production code changes.

- [ ] **Step 1: Add two HTTPS names and an ephemeral certificate.** Generate a CA and leaf certificate with SANs `a.test` and `b.test` using `cryptography`; bind the existing handler to loopback with `ssl.SSLContext(PROTOCOL_TLS_SERVER)`. Map both names to `127.0.0.1` using test-only Chromium launch arguments. Keep the private key in `tmp_path_factory` and delete it with the fixture. Record received method, path, host, and lowercased headers. The helper should expose URLs such as:

```python
@dataclass(frozen=True)
class HttpsSites:
    a_origin: str  # https://a.test:<port>
    b_origin: str  # https://b.test:<port>
    http_origin: str
    records: list[RecordedRequest]

    def received(self, tag: str) -> list[RecordedRequest]:
        return [record for record in self.records if f"tag={tag}" in record.path]
```

- [ ] **Step 2: Write a browser baseline before using dravenpdf's guard.** Use Playwright directly, install `Strict`, `Lax`, and `None; Secure` cookies with `context.add_cookies()`, and load an image and `fetch()` from the other site. Before navigation, call `Network.setCookieControls` with `enableThirdPartyCookieRestriction=False` for the positive control and `True` for the blocked control; it is test-only and Chromium's API says a reload is required if changed after navigation. Capture server-received headers. Verify that Strict/Lax do not reach the cross-site subresource.

```python
assert "none=allowed" in sites.received("cross-none")[0].headers.get("cookie", "")
assert "strict=" not in sites.received("cross-none")[0].headers.get("cookie", "")
assert sites.received("third-party-blocked")[0].headers.get("cookie", "") == ""
```

- [ ] **Step 3: Run the baseline alone.** Run `uv run pytest -m browser tests/integration/test_cookie_https.py -q`. If the positive control fails, fix the test browser's documented third-party-cookie setting or host/certificate setup; never weaken the expected cross-site behavior to a skipped test. Keep production TLS settings untouched.
- [ ] **Step 4: Commit the fixture and baseline tests.** Commit only test files with `test: add HTTPS cookie baseline`.

### Task 2: Prove interception before changing production routing

**Files:** Create `tests/integration/test_cdp_probe.py`; use the Task 1 fixture. No production files.

**Interfaces:** The probe emits a `Fetch.requestPaused` record containing URL, request ID, `redirectedRequestId`, target/session, and sanitized header names. It has a small handler that either `Fetch.continueRequest`, `Fetch.fulfillRequest`, or `Fetch.failRequest`s each pause. Its pass/fail result is the gate for Tasks 3–5.

- [ ] **Step 1: Prove per-hop pause and cookie preservation.** Attach a CDP session before navigation and enable request-stage interception. Navigate through A→B→A and compare each server hop with an unguarded Chromium baseline from Task 1. Add one header to A only, then verify the next B hop has no A header and its cookie matches baseline. Test without overriding headers as a control; header override must not replace Chromium's chosen cookies.

```python
session.on("Fetch.requestPaused", lambda event: tasks.add(asyncio.create_task(handle(event))))
await session.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
assert [hop.url for hop in pauses] == expected_urls
assert guarded_wire_cookies == baseline_wire_cookies
```

- [ ] **Step 2: Prove complete target coverage.** Open a popup, nested frame, and dedicated worker from a guarded page. Use a browser-level CDP target attach if a page session misses the popup's first request. Place a blocked URL at the popup's first navigation and assert the server sees zero requests. Run two contexts concurrently with distinct auth values and assert no request or CDP event is assigned to the wrong context.
- [ ] **Step 3: Prove non-network and failure paths.** Fulfill a bundle URL from bytes; confirm `.invalid` never resolves or connects. Check a redirect to `file://`, a file-root navigation, and a WebSocket handshake. Force a handler exception and cancel during a paused request; assert a request is failed or its context is closed before it can be sent. Record whether response-stage interception is needed to reject non-HTTP redirect targets before browser handoff.
- [ ] **Step 4: Run and record the gate.** Run `uv run pytest -m browser tests/integration/test_cdp_probe.py -q`. Commit the passing probe tests and a brief observed-behavior note in this plan or the spec as `test: prove Chromium per-hop interception`. If any required observation fails, stop here and request a revised design; do not execute Task 3.

### Task 3: Install the per-render guard

**Files:** Create `src/dravenpdf/render/_cdp_guard.py`; modify `src/dravenpdf/render/guards.py`, `src/dravenpdf/render/assets.py`, `src/dravenpdf/render/renderer.py`; test `tests/unit/test_guards.py`, `tests/integration/test_auth.py`.

**Interfaces:** `RequestGuard.install(context: BrowserContext, page: Page, *, deadline: float) -> None` attaches all required interception before `load(page)`. `RequestGuard.close() -> None` resolves or fails pending pauses and detaches sessions. `RequestGuard.blocked` and `check(url)` retain their public behavior. `CdpRequestInterceptor` is internal and receives the owning `RequestGuard` and deadline; it never stores credentials outside that render.

- [ ] **Step 1: Write failing policy tests.** In `tests/unit/test_guards.py`, assert origin-specific headers for A do not appear on B, including a same-host different-port B. Assert a blocked URL records one sanitized reason and asks the interceptor to fail that CDP request. Keep existing `hop_headers` tests until the new path replaces them.

```python
assert auth.headers_for("https://a.test:9443/x") == {}
assert auth.headers_for("https://a.test:8443/x") == {"x-tenant": "acme"}
assert guard.blocked == [(blocked_url, expected_reason)]
```

- [ ] **Step 2: Add the CDP request handler.** In `_cdp_guard.py`, track each attached target/session, paused request ID, and handler task. At a request-stage pause: answer bundle URLs from `AssetBundle.lookup()` with `Fetch.fulfillRequest`; run `RequestGuard._reason_to_block(url)` for other destinations; fail blocked requests with `Fetch.failRequest`; otherwise add only `auth.headers_for(url)` and call `Fetch.continueRequest`. Preserve Chromium's cookie decision exactly as Task 2 proved. Keep the request and its redirect chain under ten hops, including `redirectedRequestId` lineage. Never log full headers or raw CDP errors.

```python
async def fail_paused_request(session: CDPSession, request_id: str) -> None:
    await session.send(
        "Fetch.failRequest",
        {"requestId": request_id, "errorReason": "BlockedByClient"},
    )

async def continue_paused_request(session: CDPSession, request_id: str) -> None:
    await session.send("Fetch.continueRequest", {"requestId": request_id})
```

- [ ] **Step 3: Wire lifecycle before navigation.** In `AsyncRenderer._render`, create the fresh context, add storage/cookies, create the page, then call `await guard.install(ctx, page, deadline=deadline)` before `load(page)`. Always run `await guard.close()` in `finally`, including timeout, page crash, and cancellation. Keep `context.route_web_socket` and the file guard for the schemes established in Task 2. Do not leave the old `route.fetch` HTTP loop active in parallel with CDP.
- [ ] **Step 4: Make bundle response data reusable.** Keep `AssetBundle.lookup(url)` as the source of truth for body and MIME type; factor `AssetBundle.fulfill(route)` so CDP and route paths share the same 200/404 response data and `Cache-Control: no-store`. Assert `bundle.dravenpdf.invalid` is never sent to DNS or network.
- [ ] **Step 5: Verify and commit the core.** Run `uv run pytest tests/unit/test_guards.py -q` and `uv run pytest -m browser tests/integration/test_auth.py tests/integration/test_assets.py tests/integration/test_guard_network.py -q`. Commit as `fix: guard Chromium redirect hops without rewriting cookies` only after Task 2's gate and these tests pass.

### Task 4: Complete target and failure coverage

**Files:** Modify `src/dravenpdf/render/_cdp_guard.py`, `src/dravenpdf/render/guards.py`, `tests/integration/test_cookie_https.py`, `tests/integration/test_auth.py`, `tests/integration/test_guard_network.py`, `tests/unit/test_guards.py`.

**Interfaces:** No new public API. The internal interceptor must cover target creation, detachment, and per-render shutdown before any target can send unchecked network traffic.

- [ ] **Step 1: Write failing HTTPS redirect matrix tests.** Parameterize page source (`url`, `html`, `html_base_url`, `file`, `template`, `template_dir`, `bundle`, `bundle_template`), cookie (`Strict`, `Lax`, `None; Secure`), and request shape (direct image, fetch, A→B, A→B→A). For each, assert guarded wire cookies equal the unguarded Chromium baseline. Include a safe top-level GET navigation and a 307/308 POST redirect that preserves method/body. Assert a cookie set by the first redirect response is treated by Chromium at the second hop.
- [ ] **Step 2: Write blocked-target and header tests.** Verify no bytes reach blocked loopback, metadata IP, disallowed host, or `file://` redirect targets. Verify configured headers on A do not follow A→B and that headers configured for B appear only on B; assert page-provided `Authorization` is absent after a cross-origin redirect. Include same host with different ports and a redirect loop that ends at the existing ten-hop limit.
- [ ] **Step 3: Write popup, frame, worker, and isolation tests.** Open a popup whose first URL is blocked, and another whose first URL is allowed but redirects to blocked. Include nested frame and dedicated-worker fetches. In concurrent renders, give the same origin two different secret headers and assert each popup/worker sees only its render's value; neither value may appear in logs or errors.
- [ ] **Step 4: Write forced-failure tests.** Inject CDP setup failure, handler exception, target detachment, request timeout, cancellation, browser disconnect, and network refusal. Assert `BlockedRequestError` for policy blocks, `RenderTimeoutError` for the render deadline, and `RenderError` for interception failure; no case may send an unchecked request, leak a context/task, or hang. Assert existing `on_blocked="skip"` behavior for blocked subresources. Use the Task 2 trace to confirm no paused request is silently continued during teardown.
- [ ] **Step 5: Run targeted suites and commit.** Run `uv run pytest -m browser tests/integration/test_cookie_https.py tests/integration/test_auth.py tests/integration/test_guard_network.py -q` and `uv run pytest tests/unit/test_guards.py -q`. Commit as `test: cover HTTPS cookies and guarded redirect targets`.

### Task 5: Document observed behavior and run release checks

**Files:** Modify `docs/decisions.md`, `docs/architecture.md`, `docs/library-api.md`, `docs/roadmap.md`, `CLAUDE.md`; test all suites.

**Interfaces:** No new API. D11 states that Chromium chooses cookies on every hop and that `None; Secure` is eligible for cross-site HTTPS only when browser policy allows it.

- [ ] **Step 1: Update D11 and developer guidance.** Remove the sentence saying redirect hops use stored cookies without SameSite. Replace the `route.fetch` warning in `CLAUDE.md` with the new per-hop CDP rule and fail-closed target coverage. Keep the DNS check/connect race note. Document the observed presence or absence of an empty `Cookie:` header on the wire.
- [ ] **Step 2: Update architecture and public docs.** Explain `RequestGuard` policy versus `_cdp_guard.py` transport, popup/worker coverage, bundle fulfillment, WebSockets, and clean shutdown. State `SameSite=None; Secure` does not override third-party-cookie blocking. Keep CLI/HTTP auth schema unchanged.
- [ ] **Step 3: Run the full gates.** Run `make check`, `uv run pytest -m browser`, and `git diff --check`. Confirm the first reports 0 lint/type/dependency/test errors, the second reports 0 browser failures, and the diff reports no whitespace errors. Check test counts from fresh output; do not reuse the prior 508/113 counts.
- [ ] **Step 4: Review requirements against the spec.** Compare each item under the spec's feasibility gate, required behavior, HTTPS environment, and acceptance tests with test evidence. Record any unproved behavior as a remaining limit rather than claiming completion. Commit documentation as `docs: record Chromium cookie and redirect behavior`.

## Handoff

Start with Task 1, then Task 2. Task 2 is a hard go/no-go decision. Do not begin production guard changes or update D11's claims if the gate fails. After Task 5, request a whole-branch review before integration; the implementation changes a security boundary.

## API references for implementation

- [CDP Fetch](https://chromedevtools.github.io/devtools-protocol/tot/Fetch/): `requestPaused`, redirect IDs, and per-request header overrides.
- [CDP Network](https://chromedevtools.github.io/devtools-protocol/tot/Network/): test-only `setCookieControls` and its reload requirement.
- [Playwright BrowserContext](https://playwright.dev/python/docs/api/class-browsercontext): CDP sessions; popup `page` events occur after the initial request has begun, so that event alone cannot satisfy the popup gate.
