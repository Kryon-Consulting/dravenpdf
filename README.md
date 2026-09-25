# dravenpdf

HTML to PDF conversion and PDF tools for Python, plus a ready-to-run HTTP service.

dravenpdf renders HTML, URLs and Jinja2 templates to PDF with headless Chromium
(via Playwright), so what you get matches Chrome's "Print to PDF": modern CSS,
web fonts, SVG and JavaScript all work. You can then merge, split, rotate, stamp
and compress the PDFs, convert between PDFs and images, and extract text.

> **Status:** pre-release. The library, CLI and HTTP service work and are tested;
> packaging and a first release are next. See the [roadmap](docs/roadmap.md).

## Features

- **Render:** HTML strings, files, URLs and Jinja2 templates → PDF
- **Page setup:** paper size or custom size, orientation, margins, scale, page ranges
- **Headers and footers** in HTML, with page numbers, total pages, date and title
- **Reliable rendering:** waits for fonts, lazy-loaded images, a CSS selector, a JS flag or expression
- **Self-contained input:** HTML plus its CSS, fonts and images in one call, served from memory
- **Diagnostics:** a report of failed resources and script errors on every render; optional strict mode
- **Browser environment:** viewport, locale, time zone, colour scheme, device scale factor
- **Print control:** CSS `@page` sizes and margins, tagged PDFs, bookmarks from headings
- **Pages behind a login:** cookies, Playwright storage state and per-origin headers, isolated per render
- **Edit PDFs:** merge, split, extract, rotate, delete and reorder pages; set metadata; compress
- **Stamps and watermarks** from text, images or HTML
- **Conversion:** images → PDF, PDF → PNG/JPEG, text extraction
- **HTTP service** (FastAPI) with API key auth, concurrency limits and Prometheus metrics
- **Safe by default:** a fresh browser context per render, SSRF protection, sandboxed templates

- **Passwords:** open protected PDFs, encrypt with AES-256 and permissions, decrypt

Not included yet: digital signatures and form filling (in progress, see the roadmap), PDF/A.

## Installation

```bash
pip install dravenpdf                 # library + CLI
pip install "dravenpdf[server]"       # + HTTP service
playwright install chromium           # one-time browser download
```

## Quick start

### Python

```python
import asyncio
from dravenpdf import AsyncRenderer, RenderOptions, HeaderFooter

async def main():
    async with AsyncRenderer() as r:
        doc = await r.from_html(
            "<h1>Invoice #42</h1><p>Thanks for your business.</p>",
            RenderOptions(
                paper="A4",
                footer=HeaderFooter(html='<div style="font-size:9px;margin:auto">'
                    'Page <span class="pageNumber"></span> of <span class="totalPages"></span></div>'),
            ),
        )
        doc.stamp_text("PAID", opacity=0.15, angle=45).save("invoice.pdf")

asyncio.run(main())
```

Sync version:

```python
from dravenpdf import Renderer, PdfDocument

with Renderer() as r:
    r.from_url("https://example.com").save("example.pdf")

PdfDocument.merge([PdfDocument.open("a.pdf"), PdfDocument.open("b.pdf")]).save("ab.pdf")
```

### CLI

```bash
dravenpdf render page.html -o page.pdf --paper Letter --landscape
dravenpdf template invoice.html --data invoice.json -o invoice.pdf
dravenpdf merge a.pdf b.pdf -o ab.pdf
dravenpdf stamp ab.pdf --text CONFIDENTIAL -o stamped.pdf
dravenpdf images report.pdf --dpi 150 -o pages/
```

### HTTP service

```bash
docker build -t dravenpdf .
docker run --init -p 8000:8000 -e DRAVENPDF_API_KEY=change-me dravenpdf
# or without Docker: DRAVENPDF_API_KEY=change-me dravenpdf serve

curl -X POST http://localhost:8000/v1/render/html \
  -H "X-API-Key: change-me" -H "Content-Type: application/json" \
  -d '{"html":"<h1>Hello</h1>"}' -o hello.pdf
```

All endpoints and settings are in [docs/http-api.md](docs/http-api.md).

## Documentation

| | |
|---|---|
| [Architecture](docs/architecture.md) | How the pieces fit together |
| [Library API](docs/library-api.md) | Python API and CLI reference |
| [HTTP API](docs/http-api.md) | Endpoints, auth, errors, configuration |
| [Development](docs/development.md) | Setup, tests, conventions |
| [Decisions](docs/decisions.md) | Why things are the way they are |
| [Roadmap](docs/roadmap.md) | What's done and what's next |

## Development

```bash
uv sync --all-extras
uv run playwright install chromium
uv run pytest
```

See [docs/development.md](docs/development.md).
