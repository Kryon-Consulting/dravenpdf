# Decisions

A record of the decisions made so far. Add new entries at the bottom and don't
rewrite old ones; if a decision changes, add a new entry that replaces it.

## D1 – Build our own instead of buying IronPDF
IronPDF (about $749–$2,999 perpetual plus yearly support fees) is headless
Chromium with a PDF editing layer on top. Playwright drives the same Chromium
print engine for free. The features we need (render, merge/split, stamp,
images, text) are covered by open-source libraries. Estimated effort: 3–5 weeks.

## D2 – Python rather than TypeScript
Rendering quality is identical (the same Chromium engine either way). Python's
PDF editing libraries (pikepdf, pypdfium2) are stronger than Node's pdf-lib.

## D3 – Package name `dravenpdf`
Import name, distribution name and CLI command are all `dravenpdf`.

## D4 – One package, service as the `[server]` extra
The service is a thin layer (a few hundred lines), and one package keeps
versions aligned. Library users don't install FastAPI unless they ask for it.

## D5 – No PDF/A, no Ghostscript
Ghostscript is AGPL, and real PDF/A conversion needs it. Left out for now.
Compression uses pikepdf only.

## D6 – Simple API key auth
One shared key in `X-API-Key`, from an environment variable. No users, roles
or OAuth. The server refuses to start without a key unless auth is explicitly
disabled.

## D7 – Synchronous HTTP API
The response body is the result. No job queue, callbacks or result storage.
Big documents are handled by raising timeouts and size limits.

## D8 – Out of scope for now
Digital signatures, form filling, encryption and passwords. If added later:
`pyhanko` for signatures, pikepdf for encryption and forms.

## D9 – Playwright over Puppeteer-style alternatives and WeasyPrint
Playwright has official Python support, good async support, and produces the
same PDFs as Chrome. WeasyPrint doesn't run JavaScript, so it isn't the default.
It could be added later as an optional lightweight backend.

## D10 – Security defaults for rendering
Every render gets a fresh browser context, and every request the page makes is
checked by the SSRF guard (block `file://`, private, loopback, link-local and
metadata IPs; optional host allowlist). Jinja2 runs sandboxed with autoescaping.

## D11 – Page credentials are per render, per exact origin
`RenderAuth` is separate from `RenderOptions` (layout) and from the service's own
`X-API-Key`. Cookies and storage state use the browser's own mechanisms (installed in
the fresh context before the first navigation) rather than a `Cookie` header, so the
browser's cookie rules apply. Extra headers are added by the guard per hop and only for
their exact origin; they are rebuilt on every redirect hop, `Authorization` is dropped
when a redirect leaves the original origin, and cookies after the first hop come from
the jar for the new URL. Values are `SecretStr` and Playwright's call logs (which list
headers) are stripped from logs and errors.
