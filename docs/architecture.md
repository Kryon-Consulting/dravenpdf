# Architecture

## Overview

```
            ┌──────────────── dravenpdf ────────────────┐
 HTML/URL → │ render/  ──(Chromium via Playwright)──► PDF bytes │
 template   │                     │                      │
            │                     ▼                      │
 PDF files →│ document/ (pikepdf, pypdfium2, img2pdf)    │ → PDF / images / text
            │                                            │
            │ cli.py        server/ (FastAPI, [server])  │
            └────────────────────────────────────────────┘
```

There are two layers. `render/` turns HTML into PDF bytes. `document/` works on
PDF bytes. `PdfDocument` connects them: every `from_*` render returns a
`PdfDocument`, so rendering and editing chain together. The CLI and the HTTP
server are thin adapters over these two layers and contain no PDF logic of their own.

## Repository layout

```
pdf/
├── pyproject.toml              # package dravenpdf, extra: [server]
├── README.md
├── CLAUDE.md                   # agent entry point
├── docs/                       # this folder
├── Dockerfile                  # service image (Playwright base + fonts)
├── docker-compose.yml
├── Makefile
├── src/dravenpdf/
│   ├── __init__.py             # re-exports the public API
│   ├── py.typed
│   ├── options.py              # RenderOptions, Margins, PaperSize, HeaderFooter
│   ├── errors.py               # exception hierarchy
│   ├── render/
│   │   ├── renderer.py         # AsyncRenderer
│   │   ├── sync.py             # Renderer (sync wrapper)
│   │   ├── pool.py             # BrowserPool: concurrency, Chromium and CA lifecycle
│   │   ├── guards.py           # shared network policy and local-file guard
│   │   ├── _proxy_ca.py        # temporary CA and scoped Chromium trust
│   │   ├── _proxy_gate.py      # per-render mitmdump lifecycle and control channel
│   │   ├── _proxy_addon.py     # pre-forward request checks and header injection
│   │   ├── _proxy_protocol.py  # private policy and event format
│   │   ├── _redirect_gate.py   # browser-wide CDP redirect bound
│   │   ├── assets.py           # in-memory asset bundles for from_html(assets=...)
│   │   ├── report.py           # render reports and strict mode
│   │   ├── waits.py            # fonts ready, lazy images, custom ready signal
│   │   └── templates.py        # Jinja2 environment
│   ├── document/
│   │   ├── pdf.py              # PdfDocument (load/save/compress/metadata, chainable)
│   │   ├── pages.py            # merge, split, extract, rotate, delete, reorder
│   │   ├── stamp.py            # text / image / HTML overlays (watermarks)
│   │   ├── images.py           # image → PDF, PDF → PNG/JPEG
│   │   ├── text.py             # text extraction
│   │   ├── forms.py            # list, fill and flatten AcroForms
│   │   └── signing.py          # sign (pyHanko) and verify signatures
│   ├── cli.py                  # Typer app: `dravenpdf ...`
│   └── server/                 # needs the [server] extra
│       ├── app.py              # create_app(): lifespan starts/stops BrowserPool
│       ├── config.py           # Settings (env prefix DRAVENPDF_)
│       ├── deps.py             # API key check, uploads, PDF/ZIP responses
│       ├── middleware.py       # request ids, body size limit
│       ├── metrics.py          # Prometheus registry
│       ├── errors.py           # maps dravenpdf.errors → HTTP responses
│       ├── schemas.py          # request/response models
│       └── routes/
│           ├── render.py       # /v1/render/*
│           ├── documents.py    # /v1/pdf/*
│           ├── convert.py      # /v1/convert/*
│           └── health.py       # /healthz, /readyz, /metrics
├── tests/
│   ├── conftest.py
│   ├── fixtures/               # html, fonts, images, sample pdfs
│   ├── unit/                   # no browser
│   ├── integration/            # @pytest.mark.browser
│   ├── visual/                 # PDF → PNG compared with reference images
│   └── server/                 # FastAPI TestClient
└── examples/
```

## Module responsibilities

### `options.py`
Pydantic models shared by the library, CLI and server, so the HTTP request body
and the Python API accept the same options. `RenderOptions` holds paper size or
custom width/height, orientation, margins, scale, header/footer HTML, page ranges,
`print_background`, `wait_until`, `wait_for_selector`, `timeout_ms`, and media type
(`print`/`screen`).

### `errors.py`
```
DravenPdfError
├── RenderError
│   ├── RenderTimeoutError
│   ├── IncompleteRenderError   # strict render found missing resources / script errors
│   └── BlockedRequestError     # guard refused a URL
├── TemplateError               # Jinja2 template not found / invalid / failed
├── InvalidPdfError             # input bytes are not a readable PDF
├── PdfOperationError           # e.g. page index out of range
│   └── LimitExceededError      # output would pass a size limit (server → 422)
└── PoolExhaustedError          # too many renders waiting (server → 503)
```

### `render/`
- **`pool.py` – `BrowserPool`**: owns Playwright, concurrency slots and a temporary
  proxy CA. Strict renders lease a dedicated Chromium process and fresh context so
  the browser-wide redirect observer cannot affect another render. The binary comes
  from `executable_path` / `$DRAVENPDF_CHROMIUM_PATH`, else Playwright's download.
  An `asyncio.Semaphore` caps concurrent renders (`max_concurrency`); a bounded wait
  (`max_queue`) raises `PoolExhaustedError`. Browser launch and context creation are
  shielded so cancellation closes late contexts and does not leak partial startup.
  Dedicated processes add startup time and memory per concurrent render.
- **`renderer.py` – `AsyncRenderer`**: `from_html`, `from_url`, `from_file`,
  `from_template`. One deadline (`timeout_ms`) covers the whole call: the wait for a
  slot, proxy startup, launching Chromium, creating the context and page, loading,
  waiting and printing. Each call: reserve a bounded pool slot → start the proxy →
  lease an isolated browser and proxied context → install the local-file guard and
  redirect observer → load, wait and print → close the context and observer → close
  the proxy → release the slot → return a `PdfDocument`. A late context cleanup
  keeps its proxy alive until cleanup finishes.
- **`assets.py`**: `AssetBundle` for `from_html(..., assets=...)`. The page loads from
  `https://bundle.dravenpdf.invalid/` (a reserved domain that can't resolve); the
  proxy addon answers owned URLs from memory with the bundle's 200/404 behavior,
  before any upstream connection. Paths are validated with `check_path`.
- **`report.py`**: `ReportCollector` listens to the page's `response`, `requestfailed`,
  `pageerror` and `console` events and builds the `RenderReport` attached to each
  rendered document (deduplicated: a 404 stylesheet that Chromium also aborts is listed
  once; Chromium's own "Failed to load resource" console lines and guard blocks are
  filtered). Before printing, the renderer calls `check()` for the strict options.
- **`auth.py`**: `RenderAuth` (cookies, storage state, per-origin headers) with
  validation and `SecretStr` values (the IndexedDB snapshot is a `Secret`). Every
  `from_*` method takes it. The renderer passes localStorage and IndexedDB as the
  context's `storage_state`, adds cookies with `add_cookies` before any page exists,
  and hands exact-origin headers to the proxy policy.
- **`_redact.py`**: `safe_message()` strips Playwright's call log (which lists request
  headers, including credentials) from anything logged or raised.
- **`guards.py`**: `NetworkPolicy` supplies the shared SSRF rule: HTTP(S) hosts must
  resolve to public IPs or be explicitly allowlisted. It blocks private, loopback,
  link-local, CGNAT and multicast addresses, including `169.254.169.254`. The
  browser route handles only `file://`, restricted to `file_root` for local inputs;
  `data:`, `blob:` and `about:` remain browser-local. With `on_blocked="fail"`
  (default), a recorded block raises `BlockedRequestError`; `"skip"` renders without
  it. DNS is resolved separately for the policy check and upstream connection, so
  production deployments should also restrict egress against DNS rebinding.
- **`_proxy_ca.py`, `_proxy_gate.py`, `_proxy_addon.py`**: one loopback mitmdump
  process per render starts before its browser context. A random proxy credential
  and private policy/control files isolate renders. The context has a mandatory proxy
  with loopback bypass disabled. The addon checks CONNECT and each HTTP request
  header, including requests in reused HTTPS tunnels, before upstream bytes. It
  rejects mismatched CONNECT/inner authorities, raw TCP/UDP and unsupported schemes.
  HTTP(S) and WS(S) handshakes use this path, including popups, frames and workers.
  The addon adds configured headers only at their exact origin and leaves Chromium's
  `Cookie` untouched on every hop. A failed proxy, control channel or policy hook
  fails closed and surfaces as a render error or blocked request. Proxy events feed
  the render report without including credential values. Events for blocked traffic
  carry an origin, so simultaneous blocked URLs on that origin can be attributed to
  the most recently observed URL in diagnostics; the proxy still blocks each one.
- **`_redirect_gate.py`**: a browser-root CDP observer enforces the ten-redirect
  chain bound and reports blocks. The proxy remains the network security boundary;
  every hop is checked there even if Playwright/CDP observation fails.
- **TLS trust**: the pool creates a temporary CA in a private directory and launches
  Chromium with that CA's SPKI hash trusted only for that browser. The proxy validates
  upstream certificates (`ssl_insecure=false`); no system trust store or blanket
  browser HTTPS-error setting is changed. The CA is removed when the pool closes.
  Interception is incompatible with sites that require end-to-end certificate
  pinning or client certificates; HTTP/3/QUIC is not supported by this proxy path.
- **`waits.py`**: waits for `document.fonts.ready`, scrolls to trigger lazy images,
  waits for all `<img>` elements to load, and optionally waits for
  `window.__DRAVENPDF_READY__ === true` (for pages that render with JS).
- **`templates.py`**: a sandboxed Jinja2 environment with autoescaping on. Loads
  templates from a directory or a string. `from_template` with a `template_dir`
  first navigates to the folder's `file://` URL and then swaps in the rendered HTML,
  so relative assets resolve from the folder (and the guard's `file_root` is the folder).
- **`sync.py` – `Renderer`**: runs an event loop in a background thread and
  mirrors the `AsyncRenderer` API. It is a convenience for scripts and Django,
  not a separate implementation.

### `document/`
All functions work on bytes in memory. `PdfDocument` wraps a `pikepdf.Pdf` and
exposes chainable methods that each return a **new** `PdfDocument` (inputs are never
modified). pikepdf copies pages from another PDF lazily and needs the source to stay
open until the copy is saved, so operations that assemble pages from other PDFs
save-and-reopen their result (`pages._detach`) before returning it.
- **`pages.py`**: `merge`, `extract`, `rotate`, `delete`, `reorder`, `insert`, and
  splitting in two steps: `plan_split` validates and returns each part's page indices
  (plain integers), then `iter_parts` builds the parts lazily, one at a time.
  `PdfDocument.iter_split` and the split endpoint use this so only one part is in
  memory at once (the endpoint serializes each part in the ZIP worker thread).
- **`stamp.py`**: every stamp (text, image, a page of another PDF, rendered HTML) is a
  Form XObject drawn in the page's *displayed* coordinates, then placed with one
  matrix that undoes `/Rotate` and the MediaBox offset, so stamps stay upright on
  rotated pages. Opacity is an ExtGState on the placement plus a transparency group
  on the form. Text uses the built-in Helvetica font (cp1252 only). `stamp_html`
  renders the HTML at each distinct page size and overlays it.
- **`images.py`**: `images_to_pdf` (img2pdf, no re-encoding) and `pdf_to_images` /
  `iter_pdf_to_images` (pypdfium2, DPI and format options). Pages are rendered under
  the pdfium lock and encoded outside it.
- **`text.py`**: per-page text via pypdfium2.
- **`forms.py`**: AcroForm listing, filling and flattening with `pikepdf.form`. Values are
  validated before any change; appearances come from QPDF's generator (cp1252 text),
  other scripts fall back to `NeedAppearances`. Radio groups are set by selecting the
  option object (assigning `.value` stores a string and draws nothing). After
  flattening, widgets QPDF couldn't draw are removed. Page operations copy pages with
  `add_pages_from`, which keeps form fields.
- **`signing.py`**: `SigningKey` (PKCS#12), `sign()` (pyHanko incremental update,
  PAdES, optional timestamp) and `verify()` (explicit trust roots only).
  `PdfDocument` keeps the exact bytes it was read from while unmodified (`_source`)
  and returns them from `to_bytes()`, so signatures survive. Operations that write a
  new file start from `_rewritable()`, which for a signed document returns a copy with
  its signatures removed (`signing.strip_signatures()`: the signed fields, their
  widgets, `/Perms`, `/DSS`) and warns (`SignatureInvalidatedWarning`); inputs to
  `merge`/`insert` go through `_pages_source()` the same way. Stripping happens before
  the operation, so flattening can't burn a signature's appearance into the page.
  `is_signed` reads the document itself, so it agrees with the written file.
- **`_pdfium.py`**: pdfium is not thread-safe, so every pdfium call holds one global
  lock (`_pdfium.LOCK`, via `open_pdf()`).
- **Compression** lives in `PdfDocument.save(compress=True)`: pikepdf object
  streams, stream compression, and removing unused objects. No Ghostscript.

### `cli.py`
Typer app, entry point `dravenpdf` (and `python -m dravenpdf`). It only parses
arguments, converts 1-based page strings with `parse_page_ranges`, and calls
`Renderer` / `PdfDocument`. `main()` turns `DravenPdfError` into a one-line message.

### `server/`
Endpoints are in [http-api.md](http-api.md).
- **`app.py`**: `create_app(settings=None, renderer=None)`. The lifespan creates and
  starts one `AsyncRenderer` per worker process (tests can pass their own renderer).
- **`config.py`**: `Settings` from `DRAVENPDF_*` variables; refuses to build without
  an API key unless auth is disabled.
- **`middleware.py`**: pure ASGI. `RequestIdMiddleware` (outermost) sets and logs
  `X-Request-ID`; `BodyLimitMiddleware` answers 413 both for a large Content-Length
  and for streamed bodies that grow too big (it replaces whatever the app replied).
- **`deps.py`**: API key check, timeout clamping, PDF upload reading (sniffs
  `%PDF-`), and PDF/ZIP responses. CPU-bound pikepdf/pdfium work runs in
  `asyncio.to_thread`.
- **`errors.py`**: the only place error codes become HTTP statuses. Unexpected
  exceptions return 500 with the request ID and no internals.
- **`metrics.py`**: a Prometheus registry per app, plus a collector reading the pool.
- **`routes/`**: `render.py` (JSON), `documents.py` and `convert.py` (multipart),
  `health.py`. Handlers parse, call the library, and return the result.

## Data flow: `POST /v1/render/html`

1. Check the API key (`deps.py`), then the body size limit.
2. Validate the body into `RenderHtmlRequest` (`html`, `options`).
3. `AsyncRenderer.from_html` → the pool slot (a full queue returns 503) → a new
   context with guards → `set_content` → waits → `page.pdf()`.
4. Optional post-processing from the request (`stamp`, `metadata`, `compress`)
   runs on the `PdfDocument`.
5. Respond with `application/pdf` and a `Content-Disposition` header.
   Library errors are mapped to HTTP statuses in `server/errors.py`.

## Concurrency model

- Chromium runs as a separate process, so the GIL is not a bottleneck.
- One `BrowserPool` per uvicorn worker. Scale by adding workers or containers,
  and size `max_concurrency` to memory (roughly 100–300 MB per active page).
- CPU-bound pikepdf and pypdfium2 work runs in `asyncio.to_thread` so it doesn't
  block the event loop.
