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
│   │   ├── pool.py             # BrowserPool: launch, concurrency, restart
│   │   ├── guards.py           # request filtering (SSRF), per-request timeouts
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
- **`pool.py` – `BrowserPool`**: owns one Playwright instance and one Chromium
  process (binary from `executable_path` / `$DRAVENPDF_CHROMIUM_PATH`, else
  Playwright's download). An `asyncio.Semaphore` caps concurrent renders (`max_concurrency`) and
  a bounded wait (`max_queue`) raises `PoolExhaustedError` when too many renders
  are waiting. It relaunches Chromium if it disconnects, and can recycle the
  browser after N renders to limit memory growth. A launch runs as one shared
  background task that callers only wait on (`asyncio.shield`): a render whose
  deadline passes stops waiting, but the launch finishes and serves the next render,
  so no half-launched Chromium is leaked and concurrent callers never launch twice.
  Context creation is shielded the same way; a context that arrives after its caller
  gave up is closed.
- **`renderer.py` – `AsyncRenderer`**: `from_html`, `from_url`, `from_file`,
  `from_template`. One deadline (`timeout_ms`) covers the whole call: the wait for a
  slot, launching Chromium if needed, creating the context and page, loading, waiting
  and printing. Each call: take a pool slot → create a new context →
  install guards → load content → run waits → `page.pdf(...)` → close the
  context → return a `PdfDocument`.
- **`assets.py`**: `AssetBundle` for `from_html(..., assets=...)`. The page loads from
  `https://bundle.dravenpdf.invalid/` (a reserved domain that can't resolve); the guard
  answers that origin from memory before any other check (`AssetBundle.fulfill`), so
  bundle requests never reach the network. Paths are validated with `check_path`.
- **`report.py`**: `ReportCollector` listens to the page's `response`, `requestfailed`,
  `pageerror` and `console` events and builds the `RenderReport` attached to each
  rendered document (deduplicated: a 404 stylesheet that Chromium also aborts is listed
  once; Chromium's own "Failed to load resource" console lines and guard blocks are
  filtered). Before printing, the renderer calls `check()` for the strict options.
- **`auth.py`**: `RenderAuth` (cookies, storage state, per-origin headers) with
  validation and `SecretStr` values. The renderer passes the localStorage part as the
  context's `storage_state`, adds cookies with `add_cookies` before any page exists,
  and hands the headers to the guard.
- **`_redact.py`**: `safe_message()` strips Playwright's call log (which lists request
  headers, including credentials) from anything logged or raised.
- **`guards.py`**: a `context.route("**/*")` handler. It allows `data:`/`blob:`/`about:`,
  and `http(s)` only when the host resolves to a public IP (or is on the
  allowlist). It blocks private, loopback, link-local, CGNAT and multicast addresses,
  including the cloud metadata IP `169.254.169.254`. `file://` is blocked except
  inside `file_root`, which `from_file` sets to the rendered file's folder.
  Playwright does not call route handlers for redirect hops, so the guard fetches
  HTTP(S) requests itself (`route.fetch(max_redirects=0)`), checks each redirect
  target, and fulfills the browser with the final response. Headers are rebuilt for
  every hop (`hop_headers`): the browser's `Cookie` header only on the first hop (the
  jar supplies cookies for later URLs), `Authorization` dropped once a redirect leaves
  the original origin, and `RenderAuth` headers added only for their exact origin. If that fetch fails, the
  request is aborted as a network error (never left unanswered, which would hold the
  render until its deadline). WebSockets are routed separately
  (`context.route_web_socket`) and checked with the same host rules; blocked ones are
  closed before connecting. Blocked requests are
  aborted and recorded; with `on_blocked="fail"` (default) the render then raises
  `BlockedRequestError`, with `"skip"` it renders without them.
  Known limit: DNS is resolved separately for the check and the connection
  (DNS rebinding), so production deployments should also restrict egress.
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
- **`images.py`**: `images_to_pdf` (img2pdf, no re-encoding) and `to_images`
  (pypdfium2, DPI and format options).
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
  and returns them from `to_bytes()`, so signatures survive; `_derive()` warns
  (`SignatureInvalidatedWarning`) when an operation rewrites a signed document.
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
