# Library API (planned)

This is the target public API. Signatures may change slightly during
implementation. If one does, update this file in the same commit.

Everything below is importable from the top-level `dravenpdf` package.

## Rendering

```python
from dravenpdf import AsyncRenderer, Renderer, RenderOptions, Margins, HeaderFooter

async with AsyncRenderer(max_concurrency=4, allowed_hosts=None) as r:
    doc = await r.from_html("<h1>Hello</h1>")
    doc = await r.from_html(html, RenderOptions(paper="A4", landscape=True),
                            base_url="https://assets.example.com/")
    doc = await r.from_url("https://example.com", RenderOptions(wait_for_selector="#ready"))
    doc = await r.from_file("report.html")          # relative assets resolved from the file's folder
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

## Working with PDFs

```python
from dravenpdf import PdfDocument

doc = PdfDocument.open("in.pdf")                    # or PdfDocument.from_bytes(b)
doc.page_count
doc.metadata                                        # dict

merged = PdfDocument.merge([doc_a, doc_b, pdf_bytes])
parts  = doc.split(every=1)                         # list[PdfDocument]
parts  = doc.split(ranges=["1-3", "4-"])
doc.extract("2-5")                                  # new PdfDocument

(doc.rotate(90, pages=[0])
    .delete(pages=[3])
    .reorder([2, 0, 1])
    .stamp_text("CONFIDENTIAL", opacity=0.15, angle=45)
    .stamp_image(logo_png, position="top-right", pages="all")
    .set_metadata(title="Q3 Report", author="Kryon"))

await doc.stamp_html(renderer, "<div class='draft'>DRAFT</div>", under=False)

doc.to_images(dpi=150, fmt="png")                   # list[bytes]
doc.extract_text()                                  # list[str], one per page
doc.to_bytes(compress=True)
doc.save("out.pdf", compress=True)

PdfDocument.from_images([png1, jpg2], paper="A4")
```

`PdfDocument` methods return a new `PdfDocument` (or `self` after an in-place
change, as documented per method) so calls can be chained.
Page numbers in Python arguments are **0-based**. Range strings (`"1-3"`) are
**1-based**, matching how people write page ranges.

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
