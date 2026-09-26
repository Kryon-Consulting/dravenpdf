# Task 3 report: render lifecycle through strict proxy

## Implementation

- Added `ProxyGate`, a per-render `mitmdump` launcher. It makes a mode-0700 temporary directory, copies the pool CA at mode 0600, writes the mode-0600 policy snapshot, selects a loopback port with bounded startup retry, and uses a pipe inherited only by the proxy for control events. The credential is in the policy file and Playwright context option, not subprocess arguments. The listener is explicitly `regular@127.0.0.1:<port>`; there is no `DIRECT` option. Proxy stdout and stderr go to `/dev/null` so addon or mitmdump errors cannot expose secrets through library callers. Teardown terminates the process, drains it, closes the pipe, and removes the directory.
- Startup waits for `ready` and a completed authenticated control-channel probe before returning. `AsyncRenderer._render` starts the gate before `BrowserPool.context`, supplies Chromium exactly one context proxy with `bypass="<-loopback>"`, and closes the context before the gate. The pool exposes its lifecycle CA.
- Chromium responds to proxy Basic challenges on top-level navigation but did not reliably retry some subresources from `about:blank`/`file:` origins. The renderer therefore uses a separate throwaway page in the same context to authenticate against a proxy-served, no-upstream warmup URL; it closes that page before creating the render page. A regression checks that `from_html` still has `about:blank` and `from_file` still has its file URL. The addon sends a proper Basic challenge. Initial unauthenticated challenge events are not counted as policy blocks.
- Replaced the active HTTP `route.fetch(max_redirects=0)` transport with proxy forwarding. Playwright keeps the local-file root route and observes redirect chains. Network policy, exact-origin headers, bundle serving, and WebSockets run at the addon. Proxy control events are synchronized with a nonce marker over the same pipe before blocked checks and final reporting. EOF, fatal events, failed sync, and proxy death fail the render. Deadline fatal events map through the existing render timeout conversion.
- `BrowserPool._new_context` tracks orphan context cleanup when its caller is cancelled and retires that Chromium slot. The renderer defers gate closure until the orphan closes, with a 2-second maximum grace. If Chromium never resolves context creation, the render deadline still returns promptly and the gate closes after the grace period; a later context has only a dead manual proxy and cannot fall back to direct. Late-success and never-resolving regressions cover both paths.
- Joined redacted proxy targets with Chromium-observed request and WebSocket URLs for existing `BlockedRequestError` and `RenderReport` behavior. A blocked top-level redirect is checked against proxy events before a generic HTTP 403 render error is raised. Proxy control events still contain no path, query, or header values.

## Tests and verification

- `make check`: ruff lint passed; formatting check passed (**81 files**); deptry passed; mypy passed (**42 source files**); non-browser pytest passed (**541 passed, 129 deselected**).
- Full browser suite: `uv run pytest -m browser -q --tb=short`: **129 passed, 541 deselected** in 339.47 seconds.
- Task 1's transport proof remains in `tests/integration/test_proxy_gate.py`; lifecycle tests are in the separate `tests/integration/test_proxy_gate_lifecycle.py`. Together they cover proxy readiness/configuration, startup failure with no context, failed control probe, unauthenticated and policy-blocked requests with zero destination bytes, cancellation cleanup, lost control reader, preserved source origins, concurrent credential/block-event isolation, TLS trust and upstream certificate validation.
- `git diff --check` passed on the final tree.

## Known limits

- The existing DNS policy check and upstream connection still resolve independently. Explicit IP pinning remains future work under the spec.
- Proxy control events intentionally omit path and query. The parent correlates target origins with Chromium-observed URLs for reporting; concurrent blocks to different paths on one origin may be reported with the latest observed URL. Enforcement does not depend on this diagnostic association.

## Review fixes after `1a14d53`

- **Cancellation-safe teardown:** `ProxyGate.close()` now creates one retained cleanup task and shields it from caller cancellation. The worker waits a bounded interval after SIGTERM, escalates to SIGKILL, then closes the event reader and removes private files in `finally`. A test cancelled the first `close()` during `process.wait()` and showed that the old `_closed` flag made the second call a no-op; it now verifies kill and file cleanup.
- **Ten-redirect wire cap:** the addon records redirect targets and depths from upstream response headers and denies a target with depth greater than ten in `requestheaders`, before forwarding. Depths are sticky per target within the render, so ambiguous concurrent reuse can overblock but cannot lower a chain's depth. A real local server logged hop eleven before the fix, including with `on_blocked="skip"`; afterward it logged only hops zero through ten. A second red/green wire case caught `Location` fragments: the browser omits fragments from the next request, so the addon now strips them from its target key. The proxy remains the enforcement boundary; Playwright's redirect observer is only for diagnostics.
- **HTTPS CONNECT reporting:** observed Chromium HTTP(S) URLs are indexed by both origin and `host:port`, allowing a blocked CONNECT event to retain the full URL, path, and query in `BlockedRequestError.url` and skip-mode reports. The regression failed with `127.0.0.1:9443` before the fix and passed with the full `https://.../private.png?case=connect` URL afterward.
- **Final event barrier:** the last proxy control-channel synchronization now runs after `BrowserPool.context` exits and before gate teardown. A regression injected a late block at that barrier; previously it saw `pool.active == 1`, and now it sees zero while the proxy is alive. Both fail mode and skip-mode report assertions pass.

Verification for review fixes: focused proxy/guard/auth/assets suite **107 passed** before the added fragment case; both redirect wire cases **2 passed** afterward. Final `make check` passed with **542 non-browser tests** plus ruff, format, deptry, and mypy. Final `uv run pytest -m browser -q --tb=short`: **134 passed, 542 deselected**. No remaining test failure.

## Review fix round 2: canonical redirect keys

The proxy's ten-hop map previously stored the raw `Location` URL. Chromium lowercases hosts and normalizes dot segments before its next request, so an absolute `Location: http://LOCALHOST:<port>/...` escaped the lookup and reached hop eleven. An encoded `%2e%2e` path exposed the same issue. The addon now uses one canonical key on insertion and lookup: lowercase scheme and host, IDNA host encoding, explicit numeric port, normalized percent-escape case and dot segments (including encoded dots), and no fragment. Unsupported or malformed targets remain fatal. Sticky maximum depth is unchanged.

The browser wire oracle now checks eight combinations of uppercase absolute or relative `Location`, encoded-dot or ordinary path, and fragment present or absent. In every case, with `on_blocked="skip"`, the destination logs hops zero through ten and receives zero bytes for hop eleven. The uppercase absolute case and encoded-dot case both failed before their respective fixes, each logging hop eleven. Unit cases cover host/scheme case, default ports, IDNA, IPv6, query escape case, fragments, and dot paths.

Round 2 verification:

- `uv run pytest tests/unit/test_proxy_addon.py tests/integration/test_proxy_gate_lifecycle.py -q --tb=short`: **50 passed in 42.69s**.
- `make check`: ruff lint passed; formatting check passed (**82 files**); deptry passed; mypy passed (**42 source files**); non-browser pytest passed (**547 passed, 140 deselected in 29.76s**).
- `uv run pytest -m browser -q --tb=short`: **140 passed, 547 deselected in 334.00s (0:05:33)**.
- `git diff --check`: passed. No remaining regression in the completed suites.

## Review fix round 3: redirect budget independent of URL parsing

A new real Chromium wire case used `Location: /a\\..\\chain?n=...`. Chromium treated the backslashes as separators, but the proxy's Python URL-key map did not; all four backslash variants sent hop 11 to the destination before the browser observer reported the limit. The red command was `uv run pytest tests/integration/test_proxy_gate_lifecycle.py::test_eleventh_redirect_destination_gets_zero_bytes_even_when_skipping -k backslash -q --tb=short`: **4 failed, 8 deselected in 9.69s**, with each server log containing hop 11. A unit test also proved that a succession of unmatched URL keys left the eleventh redirect response unchanged (`1 failed in 0.13s`).

The key map and its URL-normalization helper have been removed. The proxy now counts redirect responses with nonempty `Location` across the render. It allows the first ten and replaces the eleventh redirect response with a policy block in mitmproxy's completed `response` hook, before Chromium receives it. The server can receive hops 0 through 10; hop 10's response is the eleventh redirect and cannot start a request to hop 11. The proxy continues to check every request's destination independently, including redirects. This mechanism is unaffected by Chromium/Python differences in parsing a redirect target.

**Compatibility tradeoff:** this is a render-wide budget, not a per-chain budget. Separate images or navigations that collectively exceed ten redirects will be blocked even if each chain has fewer than ten. The proxy has no reliable chain identifier across Chromium connections; source/target URL maps can disagree with Chromium's URL parser and cannot enforce an exact cap. Exact per-chain behavior with a zero-byte hop-11 guarantee would require a synchronous browser interception mechanism that fails closed on session loss, or a browser-to-proxy chain token protocol. This is an intentional conservative limit pending that architecture work.

Replacing the response from mitmproxy's `responseheaders` hook passed the wire assertion but caused each render to wait about its 30-second deadline (the 12-case focused run took **370.60s**). Moving replacement to the completed `response` hook preserved the zero-byte assertion and removed the delay: the same focused unit/wire command, `uv run pytest tests/unit/test_proxy_addon.py tests/integration/test_proxy_gate_lifecycle.py::test_eleventh_redirect_destination_gets_zero_bytes_even_when_skipping -q --tb=short`, passed **38 tests in 28.72s**. The wire test now covers twelve combinations of plain, encoded-dot, or backslash path; relative or uppercase absolute Location; and fragment absent or present, all with `on_blocked="skip"`. In every case the destination log is exactly hops 0–10.

Round 3 `make check` passed: ruff, formatting (**82 files**), deptry, mypy (**42 source files**), and **543 non-browser tests passed, 144 deselected in 29.45s**.
Full browser verification: `uv run pytest -m browser -q --tb=short` passed **144 tests, 543 deselected in 344.73s (0:05:44)**. Final `git diff --check`, ruff lint/format, and mypy checks passed. No failing regression remains; the aggregate-budget compatibility limit above remains open.

## Review fix round 4: BLOCKED on scoped browser redirect interception

No production changes were made. The shared redirect budget remains incompatible with the binding spec; this round does not claim Task 3 complete.

Fresh disposable wire evidence is saved in `.superpowers/sdd/2026-09-26-strict-network-gate/test_round4_probe.py`. Command: `uv run pytest .superpowers/sdd/2026-09-26-strict-network-gate/test_round4_probe.py -q --tb=short` (run with sandbox escalation for existing uv cache and local Chromium/listeners). Result: **3 failed in 5.24s**, as expected:

- Eleven independent one-hop image chains: only **10** `/end` destinations received requests, violating the expected 11.
- A `BrowserContext.route("**/*", ...)` handler counted `request.redirected_from` parents and aborted depth >10. For a normal navigation with uppercase-host/backslash Location values, server received hop **11**; handler was called only at depth **0**.
- The identical context route probe with a popup also received hop **11**, with handler called only at depth **0**.

Installed Playwright's public API documentation explicitly states the route handler is called only for the first URL when the response is a redirect (`playwright/async_api/_generated.py:10580`). Thus replacing the request observer with a route handler cannot implement the bound. These route probes deliberately isolate Chromium routing without the proxy budget; the separate renderer image probe exercises the production mandatory proxy.

A browser-root CDP Fetch interceptor could pause every hop while retaining proxy destination enforcement independently. Prior archived probes indicate browser-root Fetch can see popup and worker traffic. However, production ownership needs a per-browser dispatcher: BrowserPool shares one Chromium process across concurrent render contexts; Fetch.enable has no browserContextId filter and Fetch.requestPaused does not identify browserContextId. Installing a root Fetch session per render is not a proven isolated solution. Per-page sessions alone do not establish interception before a popup's first navigation/redirect. Required follow-up design must specify target/frame/context association, worker and popup attachment, concurrent render isolation, and lost-session failure notification. A session loss may release paused requests, but the mandatory proxy must continue independently checking their destinations and the render must fail.

Alternatives for controller consideration: (1) design and prove one pool-owned CDP dispatcher with scoped per-context observers and failure propagation; (2) isolate browser processes per render, accepting an explicit pool architecture/performance change. A render-wide budget and a new URL canonicalization map are not acceptable alternatives under the current spec.

No GREEN result exists and no full gate was run: production code is unchanged, and a full suite would only repeat earlier passing tests that omit this compatibility regression. The red probes remain in ignored SDD storage to preserve reproducibility without adding knowingly failing tests to the normal suite. Only this evidence report is committed; the disposable probes remain in ignored SDD storage.

Per-render browser alternative evaluated against the current implementation: it preserves the proxy boundary and gives a root Fetch session a single render owner. However, `BrowserPool._open` reserves a shared `_Slot` before context creation and `_release` retires based on completed render count; `recycle_after=1` does not isolate overlapping contexts. Existing `tests/unit/test_pool.py` explicitly asserts three concurrent contexts use one launch. Implementing isolation therefore requires a separate lease path or replacement slot architecture with semaphore/queue/deadline handling and orphan browser/context cleanup. Renderer use of `recycle_after` and the documented shared-process behavior would change. This is feasible as an explicit revised architecture, not established as a drop-in redirect fix.

## Review fix round 5: isolated per-chain redirect control

Strict renders now reserve a dedicated Chromium process while retaining the pool's semaphore, queue, and deadline controls. A browser-root CDP Fetch session follows Chromium's `redirectedRequestId` lineage and fails hop 11. The proxy remains the independent destination-policy boundary if CDP is lost. The proxy's incompatible render-wide redirect budget was removed. Real wire tests cover 24 top-level and popup redirect variants and eleven unrelated one-hop image chains.

The first full browser run exposed an intermittent teardown timeout: pool release could close the isolated browser before the root CDP session detached. A cleanup-order regression failed with `['release', 'redirect close']`; closing the context and detaching CDP before pool release made it pass. The two existing tests that timed out in that run passed after the fix. The browser suite then exposed two stale shared-process assertions in `test_render.py`; they now verify per-render process isolation, recovery after crashing an active browser, and the slow-launch deadline under the new lease path.

Final Task 3 evidence: `make check` passed (Ruff, format, deptry, mypy, **543 non-browser tests**); `uv run pytest -m browser -q -x --tb=short` passed **159 browser tests**; `git diff --check` passed. The browser run took 387.59 seconds. The new session cleanup and isolated-process design still need the Task 4 failure and concurrency matrix and the whole-branch security review.
