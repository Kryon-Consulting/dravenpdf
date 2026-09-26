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
