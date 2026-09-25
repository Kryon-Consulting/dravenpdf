# Strict Network Gate for Chromium Rendering

**Status:** Revised design, 2026-09-26. Supersedes the CDP transport proposed in `2026-09-25-chromium-cookie-redirect-guard-design.md`. The earlier HTTPS baseline and failed CDP probe remain evidence for this design.

## Purpose and success criteria

Every HTTP, HTTPS, WS, and WSS request made by a render, including popup first navigations and redirect hops, must pass an independent policy check before any request bytes reach the destination. Chromium remains responsible for selecting cookies and following redirects. A lost Playwright or CDP interception session cannot cause an unchecked request to escape. If the network gate or its policy code fails, the render fails without a direct-network fallback.

Keep the public render/auth inputs, exact-origin header rule, per-render context isolation, SSRF policy, bundle behavior, `on_blocked`, and secret redaction. Preserve the ten-redirect limit when the browser control channel is healthy; the proxy independently enforces the security policy on every destination even if that channel is lost. The existing `file://` root restriction remains a separate local-file guard; the strict network guarantee applies to network schemes.

## Chosen architecture

Run one local, loopback-only TLS-inspecting proxy process per render using current mitmproxy and a small dravenpdf addon. Configure Chromium's fresh context with that one manual proxy for all network schemes and no `DIRECT` fallback. Remove Chromium's implicit localhost/link-local proxy bypass with `<-loopback>`. The proxy process owns that render's immutable policy and `RenderAuth` header map, so neither a popup nor a worker needs late target attachment. It must reject requests from any client other than its render context using a random proxy credential; a listener port alone is not an identity. Pass that credential through a mode-0600 temporary configuration file, never a command-line argument.

At `http_connect`, reject prohibited tunnel destinations before the TLS handshake or upstream connection. Disable upstream certificate sniffing so it cannot connect before policy approval. At `requestheaders`, validate each decrypted request URL, including every redirect hop, before forwarding a body or opening an upstream connection. Apply configured headers for that request's exact scheme, host, and port; never synthesize or copy `Cookie`. Reject malformed authorities, CONNECT/request authority mismatches, unsupported schemes, and any unexpected raw TCP/UDP path. Mitmproxy validates upstream TLS certificates; `ssl_insecure` must remain false. A handler exception produces a blocking response and a render error, because mitmproxy otherwise logs addon exceptions and may continue.

Serve `https://bundle.dravenpdf.invalid/` directly from `AssetBundle` at the proxy's request-header hook, including its 404 and cache headers, with no DNS lookup or upstream connection. Browser-provided cookies are untouched. A local file load still uses the existing `file_root` guard because it never reaches an HTTP proxy.

The proxy is the enforcement boundary. Playwright may still observe requests to preserve the current ten-redirect bound and render report, but its loss is a render failure, not a policy bypass. The proxy process is started and confirmed ready before the browser context can navigate; it is stopped only after that context is closed. If the proxy exits, Chromium must fail requests with a proxy error and must not connect directly. Shutdown closes live proxy connections and removes the per-render credentials and files.

## Certificate and process lifecycle

Create a temporary signing CA per `BrowserPool` lifecycle, with private files accessible only to the dravenpdf process. The shared Chromium process trusts only that CA's SPKI for intercepted connections; no machine-wide trust-store change or blanket `ignore_https_errors` option is allowed. Each per-render proxy reads the pool CA but has its own listener, credential, policy file, and lifecycle. Delete the CA when all contexts and the browser are closed. Test that an invalid upstream certificate still fails: browser-side proxy trust must never turn off upstream validation.

Use the supported `mitmdump` subprocess and documented addon hooks, rather than mitmproxy's internal embedded controller. The subprocess writes no flows, credentials, or certificates outside its temporary directory. It sends minimal ready, blocked, and fatal events to the parent over a private control channel, without header values. Suppress or redact mitmdump request logs and all errors returned to library, CLI, and HTTP callers. A missing or broken control channel makes the proxy deny subsequent requests; otherwise the parent could miss a policy violation while continuing to print a PDF.

Current mitmproxy requires Python 3.12 or later. This design assumes dravenpdf's minimum Python version becomes 3.12 and pins a compatible mitmproxy major version. Do not silently fall back to the old route/fetch transport on 3.11 or when the proxy dependency is unavailable. This is a compatibility change to call out in the release notes and package metadata.

## Fail-closed contract

- No browser context is created or navigated until its proxy has loaded policy, certificate, bundle data, and auth and has passed a readiness probe.
- The proxy checks each network request before forwarding any part of that request upstream. A paused policy check, handler exception, malformed request, missing proxy credential, or proxy teardown sends zero destination request bytes.
- Chromium has exactly one configured proxy and no direct fallback. Loopback, link-local, public, and WebSocket targets are exercised against a wire-logging server to prove no bypass. Proxy death or a Playwright/CDP disconnect must not send a paused request directly.
- A CONNECT may establish a TLS tunnel only after its destination passes policy. Every HTTP request inside the tunnel is checked again, so connection reuse cannot bypass per-request enforcement.
- The proxy resolves and checks destinations using the existing allowlist/public-IP rules. Its connection must not intentionally select an IP the check rejected. The existing DNS check/connect race is documented until an explicit IP-pinning test proves it closed.
- A policy block yields the existing `BlockedRequestError` or `on_blocked="skip"` outcome. An interception/proxy failure yields `RenderError`; a render deadline yields `RenderTimeoutError`. No failure path switches to a less strict transport.

## Validation

First prove proxy trust, upstream TLS validation, loopback proxying, no direct fallback, HTTPS request-header visibility, and zero destination bytes when the proxy is killed with a request pending. Continue iterating on the implementation if a probe fails; do not ship or claim strict enforcement until these properties pass.

Then compare the server's received cookies with an unguarded Chromium baseline for Strict, Lax, and `None; Secure`, with third-party cookies both allowed and blocked. Cover `from_url`, `from_html` with and without `base_url`, `from_file`, templates, and bundles; images, fetch, top-level navigation, and A→B→A redirects. Test a 307/308 POST, a redirect that sets a cookie, exact-origin headers across ports and schemes, popup first requests, frames, workers, WebSockets, bundle fulfillment, and concurrent renders. Inject proxy startup failure, handler exception, proxy exit, browser disconnect, cancellation, and upstream TLS failure. Run `make check` and the full Chromium suite before updating D11's cookie claims.

## Compatibility and limits

The proxy adds startup cost and can affect sites that pin certificates or require HTTP/3, client TLS certificates, or protocols mitmproxy cannot inspect. Such requests fail closed and should be reported clearly. Python 3.11 support ends under the current dependency choice. The proxy sees decrypted page traffic in the local process and must treat it as sensitive. Deployment network egress restrictions remain recommended defense in depth, especially for DNS rebinding and browser implementation bugs.

## Primary references

- [Chromium proxy behavior](https://chromium.googlesource.com/chromium/src/+/HEAD/net/docs/proxy.md)
- [Playwright per-context proxy](https://playwright.dev/python/docs/network)
- [mitmproxy event hooks](https://docs.mitmproxy.org/stable/api/events.html)
- [mitmproxy certificates](https://docs.mitmproxy.org/stable/concepts/certificates/)
- [mitmproxy package metadata](https://pypi.org/project/mitmproxy/)
