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
- **Pages behind a login:** cookies, Playwright storage state (localStorage and IndexedDB) and per-origin headers for every render source, isolated per render
- **Edit PDFs:** merge, split, extract, rotate, delete and reorder pages; set metadata; compress
- **Stamps and watermarks** from text, images or HTML
- **Conversion:** images → PDF, PDF → PNG/JPEG, text extraction
- **HTTP service** (FastAPI) with API key auth, concurrency limits and Prometheus metrics
- **Safe by default:** a dedicated browser process and network proxy per render, SSRF protection, sandboxed templates

- **Passwords:** open protected PDFs, encrypt with AES-256 and permissions, decrypt
- **Forms:** list fields, fill text, checkboxes, radio buttons and dropdowns, flatten

- **Digital signatures:** sign with server-side keys (visible or invisible, optional timestamp), verify

Not included: PDF/A, EU qualified signatures, creating fillable forms from HTML.

## Installation

Requires Python 3.12 or newer; Python 3.11 support ended with the strict network
gate. Rendering starts a local mitmproxy process for each call, so expect extra
startup time and memory compared with a shared browser.

```bash
pip install dravenpdf                 # library + CLI
pip install "dravenpdf[server]"       # + HTTP service
playwright install chromium           # one-time browser download
```

The proxy checks each network request and redirect before forwarding. Chromium
chooses cookies on every hop, and configured headers stay on their exact origin.
Upstream HTTPS certificates are validated; the proxy CA is trusted only by the
renderer browser and removed when its pool closes. Sites that require certificate
pinning, client certificates or HTTP/3/QUIC cannot use this interception path.
Proxy or internal policy-hook failures stop the render rather than falling back to
direct access. A denied URL follows the configured `on_blocked` behavior.

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
cp .env.example .env              # replace DRAVENPDF_API_KEY with a unique key
docker compose up --build -d
curl http://127.0.0.1:8000/readyz

curl -X POST http://127.0.0.1:8000/v1/render/html \
  -H "X-API-Key: $(sed -n 's/^DRAVENPDF_API_KEY=//p' .env)" \
  -H "Content-Type: application/json" \
  -d '{"html":"<h1>Hello</h1>"}' -o hello.pdf
```

Compose binds the API to localhost by default. The local `.env` stays out of git and
the Docker build context; adjust `DRAVENPDF_PORT`, `DRAVENPDF_WORKERS`, and
`DRAVENPDF_MAX_CONCURRENCY` there as needed. The image also works without Compose
via `docker run --init -p 8000:8000 -e DRAVENPDF_API_KEY=... dravenpdf`.

All endpoints and settings are in [docs/http-api.md](docs/http-api.md).

### Platform report from HTML

The [report script](scripts/build_platform_report.py) bundles a local HTML file and
its referenced images or stylesheets, sends them to the running PDF API, and writes
the returned PDF. The included [example report](reports/platform_report.html)
captures the Valio Guard demo at `https://localhost:9000/`, including two enlarged
Findings charts, and saves screenshots in `images/`.

```bash
uv sync --all-extras
docker compose up -d
.venv/bin/python scripts/build_platform_report.py reports/platform_report.html \
  --capture-manifest reports/platform_views.json --demo-login
```

The PDF is written to `output/pdf/platform-report.pdf`. To render edited HTML
using the existing screenshots, omit `--capture-manifest` and `--demo-login`.
Supply `--output` for another PDF path or `--storage-state` for a Playwright
authentication-state file when the demo login is unavailable. The script reads the
PDF API key from `DRAVENPDF_API_KEY` or the local `.env` file. Referenced assets
must be under `--asset-root` (the repository root by default).

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
