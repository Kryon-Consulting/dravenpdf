# Chromium Cookie and Redirect Guard Design

**Status:** Design approved on 2026-09-25. This document specifies the change; it does not authorize an implementation that fails the feasibility gate below.

## Goal

Make authenticated rendering obey Chromium's cookie decisions on every HTTP(S) request, including every redirect hop, while checking each destination before network access. Prove when `SameSite=None; Secure` cookies are sent from HTML, file, template, and bundle pages with a local HTTPS test suite. Preserve the existing SSRF, credential-scope, and render-isolation rules. Popups remain supported and must be guarded.

## Current behavior

`RequestGuard` uses Playwright `context.route()` and manually follows redirects with `route.fetch(max_redirects=0)`, because Playwright route handlers do not see later redirect URLs. The first hop forwards Chromium's `Cookie` header, including an empty value when Chromium chose no cookies. Later hops omit that header, so `route.fetch` may add stored cookies without applying the original page's SameSite context. This can send Lax or Strict cookies to a cross-site redirect target. The test server is HTTP-only, so it cannot prove the positive behavior of `SameSite=None; Secure` cookies.

## Chosen approach

Use Chromium's request interception for network requests so Chromium owns redirect handling and cookie selection. At each paused request, the guard checks the URL before it is sent and adds only the `RenderAuth` headers configured for that request's exact origin. Header overrides must apply to one request only; they must neither replace a browser-selected `Cookie` header nor carry configured credentials to later redirects. Existing host checks, allowlists, file-root checks, blocked-request reporting, bundle serving, and WebSocket checks remain authoritative.

Chrome DevTools Protocol (CDP) `Fetch.requestPaused` reports redirect requests individually, and `Fetch.continueRequest` says header overrides do not extend to redirected requests. This makes it the first candidate, not an assumption of correctness. Playwright's Chromium CDP session is available for the render page. The implementation may use a browser-level session or target attachment if page sessions cannot cover popups and other targets before their first network request.

Do not implement SameSite, third-party-cookie, or redirect cookie-jar rules in Python. Do not replace the guard with `route.continue_()` alone; Playwright routing does not provide the required per-hop check. Do not add a TLS-intercepting proxy.

## Feasibility gate

Before replacing the current route loop, run a disposable Chromium probe that demonstrates all of the following on the locked Playwright/Chromium version:

1. Every hop of a multi-hop redirect is paused before network access, including a redirect to a blocked host or non-HTTP(S) scheme.
2. Adding an exact-origin header to one paused request leaves Chromium's cookie choice unchanged and does not forward the added header to another origin.
3. A popup's first navigation and its redirects, frame requests, and worker-initiated requests are covered. If a target cannot be instrumented before its first request, it must not be allowed to send that request.
4. In-memory bundle requests can be fulfilled without a network connection; local-file and WebSocket requests remain guarded without interception deadlocks.
5. A paused-request error, timeout, target detachment, or render cancellation cannot release a request to the network unchecked or leave it paused until the render deadline.

The probe compares headers received by a local server with a Chromium baseline that has the same context, cookies, source page, and redirects but no dravenpdf interception. If any point fails, stop this design and present the observed failure for a revised design. The existing guarded path stays in place until a replacement passes this gate and the full regression suite.

## Required behavior

- A fresh browser context holds cookies and storage state for each render, loaded before navigation. No authentication state crosses renders.
- The guard checks each HTTP(S) destination before the request is sent. It retains the current public-IP and allowlist policy, redirect count bound, and `on_blocked="fail"`/`"skip"` outcomes. A blocked redirect target receives no request bytes.
- The guard continues to reject disallowed `file://` paths, allows only paths under `file_root` where configured, and never lets an HTTP redirect evade those checks. `data:`, `blob:`, and `about:` retain their current treatment.
- Bundle URLs under `https://bundle.dravenpdf.invalid/` are answered only from `AssetBundle` data, with the current 404 behavior for missing paths. They never resolve or connect to the network.
- WebSockets retain the existing host checks and are closed before connecting when blocked.
- Page, frame, popup, and worker network traffic all receive the same guard. Service workers remain disabled. Popup support is retained; silently allowing an unguarded popup is forbidden.
- Configured `RenderAuth` headers are applied to the exact scheme, host, and port of each hop. Browser-managed cookies follow Chromium's SameSite, Secure, path, domain, and third-party-cookie rules. The guard neither invents a `Cookie` value nor copies one from an earlier hop. A page-provided `Authorization` header must not cross an origin boundary.
- Every paused request is continued, fulfilled, or failed within the render deadline. Interception setup failure or loss of coverage fails the render closed. Credential values must not enter logs, errors, render reports, metrics, or response headers.
- Public Python, CLI, and HTTP auth inputs stay the same. No user-facing option is added to select an unsafe fallback.

## HTTPS test environment

Add a loopback HTTPS fixture with two distinct test sites, mapped to loopback only in the test Chromium process. Use a test certificate valid for those hosts and a test-only certificate trust setting. No test depends on an external host or on a production TLS override. The server records method, URL, host, and received headers; assertions compare actual wire headers, not only Playwright request objects.

Create a positive control where unguarded Chromium sends a `SameSite=None; Secure` cookie cross-site under a known third-party-cookie policy. The guarded render must send the same cookie. Also run a configuration in which Chromium blocks third-party cookies and verify that the guard does not restore them. `SameSite=None; Secure` is eligible for cross-site HTTPS requests, not a guarantee that every browser policy will send it.

## Acceptance tests

1. For direct resources from `from_url`, `from_html` (with and without `base_url`), `from_file`, `from_template` (source and folder), and bundle pages, compare guarded and unguarded Chromium for Lax, Strict, and `None; Secure` cookies. Cover images and fetch requests, plus an authenticated top-level navigation.
2. For same-site and cross-site redirects, compare cookies at every hop with Chromium's baseline. Include A→B, A→B→A, scheme changes, a cookie set on a redirect response, and a redirect from a page with no eligible cookies. Distinguish a top-level safe navigation from a subresource request.
3. Show that origin-scoped headers appear only at their configured origin; page-provided `Authorization` is dropped on cross-origin redirects. A redirect to a blocked loopback, metadata, disallowed host, or file URL has no request at the destination.
4. Exercise popup first navigation, popup redirect, nested frame, and worker requests; blocked targets receive no traffic. Verify bundle and WebSocket behavior against existing tests.
5. Verify failure paths: interception setup error, handler exception, timeout/cancellation, network failure, target close, and concurrent renders do not leak requests, contexts, or credentials and do not hang.
6. Run `make check` and all browser-marked pytest tests on the locked Chromium version. No existing render or auth test regresses.

## Documentation and compatibility

Update D11, `docs/architecture.md`, `docs/library-api.md`, `CLAUDE.md`, and the roadmap only after browser tests prove the behavior. Remove the later-hop SameSite exception. Describe cross-site `SameSite=None; Secure` cookies as subject to Chromium's third-party-cookie policy. Document any changed empty-`Cookie` header behavior observed on the wire. Retain Python 3.11+ and the Playwright 1.51 minimum unless the feasibility probe demonstrates a version constraint that requires a separate design review.

The existing DNS check/connect race remains a documented limitation and is outside this change. It must not become worse; deployments rendering untrusted URLs still need network egress restrictions.

## References

- [Playwright Route API](https://playwright.dev/docs/api/class-route): header changes and redirect behavior.
- [Playwright Python CDP sessions](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-new-cdp-session): Chromium CDP access.
- [Chrome DevTools Protocol Fetch domain](https://chromedevtools.github.io/devtools-protocol/tot/Fetch/): per-hop pause events and request continuation.
- [Chromium SameSite FAQ](https://www.chromium.org/updates/same-site/faq/): `SameSite=None; Secure` requirements.
