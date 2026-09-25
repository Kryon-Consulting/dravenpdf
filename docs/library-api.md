# Library API (planned)

This is the target public API. Signatures may change slightly during
implementation. If one does, update this file in the same commit.

Everything below is importable from the top-level `dravenpdf` package.

## Rendering

```python
from dravenpdf import AsyncRenderer, Renderer, RenderOptions, Margins, HeaderFooter

async with AsyncRenderer(
    max_concurrency=4,            # renders at once
    max_queue=16,                 # waiting renders before PoolExhaustedError
    recycle_after=500,            # restart Chromium after N renders
    allowed_hosts=None,           # e.g. ["cdn.example.com", "*.example.org"]
    allow_private_network=False,  # True only for trusted input
    on_blocked="fail",            # or "skip": render without blocked resources
    executable_path=None,         # default: $DRAVENPDF_CHROMIUM_PATH or Playwright's
) as r:
    doc = await r.from_html("<h1>Hello</h1>")
    doc = await r.from_html(html, RenderOptions(paper="A4", landscape=True),
                            base_url="https://assets.example.com/")
    doc = await r.from_url("https://example.com", RenderOptions(wait_for_selector="#ready"))
    doc = await r.from_file("report.html")          # may load files from its own folder only
    doc = await r.from_template("invoice.html", {"items": items},
                                template_dir="templates/")

# Sync wrapper – same methods, no await
with Renderer() as r:
    r.from_html(html).save("out.pdf")
```

### `RenderOptions`

| Field | Type | Default | Notes |
|---|---|---|---|
| `paper` | `"A3" \| "A4" \| "A5" \| "Letter" \| "Legal" \| "Tabloid"` | `"A4"` | Ignored if `width`/`height` are set |
| `width`, `height` | `str \| None` | `None` | CSS units, e.g. `"210mm"` |
| `landscape` | `bool` | `False` | |
| `margins` | `Margins \| None` | 20mm top/bottom, 15mm left/right | `None` sends no margins, leaving them to CSS `@page`; an explicit `@page { margin }` wins either way |
| `prefer_css_page_size` | `bool` | `False` | Use the size from CSS `@page { size }` instead of `paper` / `width` |
| `tagged` | `bool` | `False` | Tagged PDF (structure tree for screen readers); not a PDF/UA guarantee |
| `outline` | `bool` | `False` | Bookmarks from the headings; implies `tagged` (Chromium needs the tags to build it) |
| `scale` | `float` | `1.0` | 0.1–2.0 |
| `print_background` | `bool` | `True` | |
| `media` | `"print" \| "screen"` | `"print"` | CSS media type to emulate |
| `header` / `footer` | `HeaderFooter \| None` | `None` | HTML; supports `pageNumber`, `totalPages`, `date`, `title`, `url` span classes |
| `page_ranges` | `str \| None` | `None` | e.g. `"1-3,5"` |
| `wait_until` | `"load" \| "domcontentloaded" \| "networkidle"` | `"networkidle"` | |
| `wait_for_selector` | `str \| None` | `None` | |
| `wait_for_ready_flag` | `bool` | `False` | Wait for `window.__DRAVENPDF_READY__ === true` |
| `wait_for_expression` | `str \| None` | `None` | JavaScript expression (or function) polled until truthy, e.g. `"window.charts?.every(c => c.done)"` |
| `timeout_ms` | `int` | `30000` | For the whole render, including waiting for a free browser slot and launching Chromium |
| `fail_on_resource_errors` | `bool` | `False` | Raise `IncompleteRenderError` if an image, stylesheet, font, script or fetch fails or returns HTTP 4xx/5xx |
| `fail_on_page_errors` | `bool` | `False` | Raise `IncompleteRenderError` if the page throws an uncaught JavaScript exception |
| `viewport` | `Viewport(width, height) \| None` | `None` (1280 × 720) | Window size while the page loads; matters for scripts that measure the window (charts, responsive dashboards), not for CSS-only layouts |
| `device_scale_factor` | `float` | `1.0` | 1–4; higher gives sharper canvas charts in the PDF |
| `locale` | `str \| None` | `None` | BCP 47 tag like `"de-DE"`: `navigator.language`, `Intl` formatting, `Accept-Language` |
| `timezone` | `str \| None` | `None` | IANA zone like `"Europe/Berlin"` for dates the page formats |
| `color_scheme` | `"light" \| "dark" \| "no-preference" \| None` | `None` | What `prefers-color-scheme` sees |
| `reduced_motion` | `"reduce" \| "no-preference" \| None` | `None` | What `prefers-reduced-motion` sees; `"reduce"` skips many CSS animations |

Environment options apply to that render's own browser context only.

`from_url` raises `RenderError` if the page itself returns HTTP 400 or above.

### Rendering pages behind a login

```python
from dravenpdf import RenderAuth, Cookie, StorageState

auth = RenderAuth(
    cookies=[Cookie(name="session", value=session_id, url="https://app.example.com")],
    storage_state=StorageState.model_validate(saved_state),   # Playwright's storage_state() JSON
    headers={"https://api.example.com": {"Authorization": f"Bearer {token}"}},
)
doc = await r.from_url("https://app.example.com/reports/42", auth=auth)
doc = renderer.from_url(url, auth=auth)                         # sync Renderer too
```

- **Cookies** (`url`, or `domain` + `path`; `expires`, `http_only`, `secure`,
  `same_site`) and **storage state** (cookies plus `localStorage` per origin, in
  Playwright's own format) go into the render's fresh browser context before the first
  navigation. The browser applies its normal cookie rules.
- **Headers** are keyed by exact origin (scheme, host, port; default ports optional).
  The request guard adds them to every request and redirect hop for that origin only:
  images, fonts and API calls on the same origin get them; other origins don't.
- **Redirects:** headers are rebuilt per hop. After the first hop the cookie jar
  supplies the cookies for each URL, `Authorization` is dropped once a redirect leaves
  the original origin (also when the page's own script set it), and a second origin
  only receives headers configured for it. Every hop still passes the SSRF checks:
  configuring headers for an origin does not allowlist it.
- **Isolation:** every render gets its own context; a later render starts logged out.
- **Secrets:** all values are `SecretStr`, masked in `repr`, `str`, logs and JSON.
  Validation errors don't show them (use `errors(include_input=False)` if you inspect
  `ValidationError.errors()`). The guard strips Playwright's call log, which lists
  request headers, from anything it logs or raises.
- `Cookie`, `Host`, `Content-Length` and connection headers can't be configured; use
  `cookies` / `storage_state` for cookies. Configured headers aren't added to
  WebSocket connections.
- Limits: 200 cookies, 50 origins, 30 headers per origin, 8 KiB per header value,
  1 MiB of secret values in total.

### Preparing the page (Python only)

Every `from_*` method on `AsyncRenderer` takes `prepare`, an async function called
with the Playwright `Page` after it loads and before the waits and printing:

```python
async def expand_all(page):
    await page.click("text=Expand all")

doc = await r.from_url("https://dashboard.example/report", prepare=expand_all)
```

It runs under the render deadline, and the SSRF guard still applies to anything it
loads. It runs after the first navigation, so use `auth` (above), not `prepare`, to
log in. It isn't available over HTTP or on the sync `Renderer`.

### Render reports

Every rendered document carries `doc.render_report`, a `RenderReport` of what went
wrong while rendering, even when the render succeeded:

```python
doc = await r.from_url("https://example.com/report")
report = doc.render_report
report.ok                    # False if anything failed, errored or was blocked
report.http_errors           # [HttpError(url, status, resource_type)] – sub-resources with 4xx/5xx
report.failed_requests       # [FailedRequest(url, reason, resource_type)] – refused, DNS, aborted
report.page_errors           # ["boom", ...] – uncaught JavaScript exceptions
report.console_errors        # console.error() messages (don't affect .ok)
report.blocked               # [(url, reason)] – refused by the SSRF guard
report.summary()             # short text, e.g. for logs
```

Each problem URL is listed once. Documents made from a rendered one (`rotate`, `merge`,
...) have `render_report = None`. Set `fail_on_resource_errors` / `fail_on_page_errors`
to turn problems into an `IncompleteRenderError` (its `.report` has the details).

## Working with PDFs

```python
from dravenpdf import PdfDocument

doc = PdfDocument.open("in.pdf")                    # or PdfDocument.from_bytes(b)
doc.page_count                                      # also len(doc)
doc.metadata                                        # {"title": ..., "author": ...} – set fields only
doc.page_size(0)                                    # (width, height) in points, rotation applied

merged = PdfDocument.merge([doc_a, doc_b, pdf_bytes])   # metadata from the first
parts  = doc.split(every=1)                         # list[PdfDocument]
parts  = doc.split(ranges=["1-3", "4-"])
for part in doc.iter_split(every=10):              # lazy: one part in memory at a time
    part.save(...)                                  # (arguments are checked at the call)
doc.extract("2-5,8")                                # new PdfDocument
doc.insert(other_doc_or_bytes, at=1)                # before page index 1; at=page_count appends

(doc.rotate(90, pages=[0])                          # clockwise; all pages if pages is omitted
    .delete([3, -1])                                # 0-based; negative counts from the end
    .reorder([2, 0, 1])                             # must name every page exactly once
    .set_metadata(title="Q3 Report", author=""))    # None = leave, "" = remove
doc.copy()

doc.to_bytes()
doc.to_bytes(compress=True)                         # + object streams, max recompression
doc.save("out.pdf", compress=True)
```

**Every method returns a new `PdfDocument` and leaves the original unchanged**, so
calls chain and documents can be shared. A single `PdfDocument` is not safe to use
from several threads at once. Bad arguments (unknown page, bad range, deleting
every page) raise `PdfOperationError`; unreadable or password-protected input
raises `InvalidPdfError`.

Page numbers in Python arguments are **0-based**. Range strings (`"1-3,5,8-"`) are
**1-based**, matching how people write page ranges; `"8-"` means page 8 to the end.

Operations that build a new page list (`extract`, `split`, `reorder`, `merge`,
`insert`) keep document info (title, author, ...) but not bookmarks, and the tag
structure of a `tagged` PDF won't match the new pages. `rotate`, `delete`,
`set_metadata` and `copy` keep everything. Render with `tagged`/`outline` last if the
result must stay accessible.

### Stamps and watermarks

```python
doc.stamp_text("CONFIDENTIAL", font_size=48, color="#FF0000", opacity=0.3, angle=45,
               position="center", margin=36, pages=None, under=False)
doc.stamp_image(logo_png, width=120, position="top-right", margin=36, opacity=1.0)
doc.overlay(letterhead_pdf, stamp_page=0, under=True)      # page of another PDF, fit + centered
await doc.stamp_html(renderer, "<div class='draft'>DRAFT</div>", opacity=0.5)
```

- `position`: `center`, `top-left`, `top`, `top-right`, `left`, `right`,
  `bottom-left`, `bottom`, `bottom-right`. `margin` is in points.
- Stamps are placed as the page is **displayed**, so they stay upright on rotated pages.
- `stamp_text` uses Helvetica and supports Western European (cp1252) characters only.
  For other scripts, custom fonts or styling, use `stamp_html`.
- `stamp_image` accepts PNG, JPEG, GIF, WebP, ... and keeps transparency. Its default
  size is the image's pixel size at 96 dpi, and it always shrinks to fit inside the margins.
- `stamp_html` renders the HTML once per distinct page size, at that size and with no
  margins. The HTML page is transparent except for what it draws.
- `opacity` applies to the stamp as a whole; `under=True` draws it behind the page content.

### Images and text

```python
PdfDocument.from_images([png, jpg])                     # page = image size (96 dpi default)
PdfDocument.from_images([png, jpg], paper="A4", landscape=False, margin=36)  # fit on paper
doc.to_images(dpi=150, fmt="png", pages=None)           # list[bytes]; fmt "png" | "jpeg"
doc.to_images(dpi=300, max_pixels=40_000_000, max_total_bytes=100 * 2**20)  # LimitExceededError past either
doc.extract_text()                                      # list[str], one per page; no OCR
```

## HTML with its assets, from memory

```python
doc = await r.from_html(
    '<link rel="stylesheet" href="css/site.css"><img src="img/logo.png">',
    assets={"css/site.css": css_bytes, "img/logo.png": png_bytes},
)
```

The document is served from a private origin that exists only inside the render
(`https://bundle.dravenpdf.invalid/`), and every request for that origin is answered
from `assets`, never the network. Relative URLs (including `../` from a stylesheet)
resolve inside the bundle; a missing file is a 404 in `doc.render_report`. Paths are
relative with `/` separators and no `..`, empty or `.` parts (else `AssetError`), at
most 1000 files. Other URLs the page loads still go through the SSRF guard.
`assets` can't be combined with `base_url` (or `template_dir` for templates).

## Templates

```python
doc = await r.from_template("<h1>Hi {{ name }}</h1>", {"name": "Ada"})       # source string
doc = await r.from_template("invoice.html", data, template_dir="templates/")  # file in a folder
doc = await r.from_template(source, data, assets={"logo.png": png_bytes})     # assets in memory
```

Templates run in Jinja2's **sandbox** with **autoescaping** on. With `template_dir`,
templates may `extend`/`include` others in that folder, relative assets (CSS, images)
resolve from the folder, and the page may load local files from that folder only.
Template problems raise `TemplateError`.

## Errors

All exceptions subclass `dravenpdf.errors.DravenPdfError`. See
[architecture.md](architecture.md#errorspy) for the hierarchy.

## CLI

`dravenpdf --help` and `dravenpdf COMMAND --help` list every option. Page arguments
are **1-based** (`1,3-5,8-`). `-o -` writes a single PDF to stdout. Library errors
print one line (`error: ...`) and exit with status 1.

```
dravenpdf render      page.html|URL -o out.pdf [--paper A4] [--landscape] [--margin 10mm]
                      [--header h.html] [--footer f.html] [--wait-for "#ready"]
                      [--wait-for-ready-flag] [--media print|screen] [--timeout 30]
                      [--allow-host cdn.example.com ...] [--allow-private] [--skip-blocked]
                      [--compress] [--fail-on-resource-errors] [--fail-on-page-errors]
                      [--viewport 1920x1080] [--device-scale-factor 2] [--locale de-DE]
                      [--timezone Europe/Berlin] [--color-scheme dark] [--reduced-motion reduce]
                      [--prefer-css-page-size] [--css-margins] [--tagged] [--outline]
                      [--wait-for-expression "window.done === true"]
dravenpdf template    invoice.html --data data.json -o out.pdf [--paper] [--landscape] [--footer]
dravenpdf merge       a.pdf b.pdf ... -o out.pdf
dravenpdf split       in.pdf -o outdir/ (--every 2 | --range 1-3 --range 4-)
dravenpdf extract     in.pdf 2-5,8 -o out.pdf
dravenpdf rotate      in.pdf -o out.pdf [--degrees 90] [--pages 1,3]
dravenpdf delete      in.pdf 1,3 -o out.pdf
dravenpdf stamp       in.pdf -o out.pdf (--text DRAFT | --image logo.png | --html s.html | --pdf letterhead.pdf)
                      [--opacity] [--angle] [--font-size] [--color] [--width] [--position] [--under] [--pages]
dravenpdf metadata    in.pdf                          # show info as JSON
dravenpdf metadata    in.pdf --title "Q3" -o out.pdf  # write a changed copy
dravenpdf compress    in.pdf -o out.pdf
dravenpdf images      in.pdf -o outdir/ [--dpi 150] [--format png|jpeg] [--pages]
dravenpdf from-images a.png b.jpg -o out.pdf [--paper A4] [--landscape] [--margin 36]
dravenpdf text        in.pdf                          # pages separated by form feeds
dravenpdf serve       [--host 127.0.0.1] [--port 8000] [--workers 1]   # needs [server]
```

`render` blocks private and localhost addresses like the library does; pass
`--allow-private` to render a local dev server.
