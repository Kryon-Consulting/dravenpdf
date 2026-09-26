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
