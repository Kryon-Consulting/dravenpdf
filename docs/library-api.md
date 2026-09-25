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
| `margins` | `Margins` | 20mm top/bottom, 15mm left/right | |
| `scale` | `float` | `1.0` | 0.1–2.0 |
| `print_background` | `bool` | `True` | |
| `media` | `"print" \| "screen"` | `"print"` | CSS media type to emulate |
| `header` / `footer` | `HeaderFooter \| None` | `None` | HTML; supports `pageNumber`, `totalPages`, `date`, `title`, `url` span classes |
| `page_ranges` | `str \| None` | `None` | e.g. `"1-3,5"` |
| `wait_until` | `"load" \| "domcontentloaded" \| "networkidle"` | `"networkidle"` | |
| `wait_for_selector` | `str \| None` | `None` | |
| `wait_for_ready_flag` | `bool` | `False` | Wait for `window.__DRAVENPDF_READY__ === true` |
| `timeout_ms` | `int` | `30000` | For the whole render |

`from_url` raises `RenderError` if the page itself returns HTTP 400 or above.

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
`insert`) keep document info (title, author, ...) but not bookmarks. `rotate`,
`delete`, `set_metadata` and `copy` keep everything.

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
doc.extract_text()                                      # list[str], one per page; no OCR
```

## Templates

```python
doc = await r.from_template("<h1>Hi {{ name }}</h1>", {"name": "Ada"})       # source string
doc = await r.from_template("invoice.html", data, template_dir="templates/")  # file in a folder
```

Templates run in Jinja2's **sandbox** with **autoescaping** on. With `template_dir`,
templates may `extend`/`include` others in that folder, relative assets (CSS, images)
resolve from the folder, and the page may load local files from that folder only.
Template problems raise `TemplateError`.

## Errors

All exceptions subclass `dravenpdf.errors.DravenPdfError`. See
[architecture.md](architecture.md#errorspy) for the hierarchy.

## CLI

```
dravenpdf render   INPUT(.html|URL) -o out.pdf [--paper A4] [--landscape] [--footer FILE]
dravenpdf template TEMPLATE --data data.json -o out.pdf
dravenpdf merge    a.pdf b.pdf -o out.pdf
dravenpdf split    in.pdf --every 1 -o outdir/
dravenpdf rotate   in.pdf --degrees 90 --pages 1,3 -o out.pdf
dravenpdf stamp    in.pdf --text DRAFT | --image logo.png | --html stamp.html -o out.pdf
dravenpdf images   in.pdf --dpi 150 -o outdir/
dravenpdf text     in.pdf
dravenpdf serve    [--host 0.0.0.0] [--port 8000] [--workers 2]   # needs [server]
```
