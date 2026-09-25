# CLAUDE.md

Guidance for AI agents (and humans) working in this repository. Read this first,
then the relevant file in `docs/`.

## What this project is

**dravenpdf** is a Python library and HTTP service that converts HTML to PDF and
works with existing PDFs. It aims to match the core features of commercial
libraries like IronPDF, but uses open-source parts:

- **Rendering:** headless Chromium via Playwright (`page.pdf()`), so output
  matches Chrome's "Print to PDF".
- **Editing PDFs:** `pikepdf` (merge, split, rotate, stamp, metadata,
  compression), `pypdfium2` (PDF → image, text extraction), `img2pdf` (image → PDF).
- **Templating:** Jinja2.
- **HTTP service:** FastAPI, shipped in the same package as the `[server]` extra.

## Current status

Planning and documentation are done; code is not yet scaffolded. The planned
build order is in `docs/roadmap.md`. Update that file (and the status line in
`README.md`) as milestones land.

## Where to look

| Topic | File |
|---|---|
| Module layout, responsibilities, data flow | `docs/architecture.md` |
| Python public API (planned signatures) | `docs/library-api.md` |
| HTTP endpoints, auth, limits, config | `docs/http-api.md` |
| Decisions already made, and why | `docs/decisions.md` |
| Setup, commands, testing, conventions | `docs/development.md` |
| Milestones and what's out of scope | `docs/roadmap.md` |

## Decisions already made (don't reopen without asking the user)

1. Package name is **`dravenpdf`** (import `dravenpdf`, CLI `dravenpdf`).
2. **One package**; the HTTP service is the optional `[server]` extra.
3. **No PDF/A and no Ghostscript** (AGPL). Do not add Ghostscript as a dependency.
4. HTTP auth is a **single API key** in the `X-API-Key` header.
5. The HTTP API is **synchronous**: the response body is the result. No job queue.
6. **Out of scope for now:** digital signatures, form filling, encryption and passwords.

Full reasoning is in `docs/decisions.md`.

## Conventions

- Python **3.11+**, `src/` layout, fully type-hinted (`mypy --strict` for `src/`).
- Packaging and environments: **uv** with the **hatchling** build backend.
- Lint and format: **ruff** (`ruff check`, `ruff format`).
- Tests: **pytest** + `pytest-asyncio`. Tests that launch Chromium are marked
  `@pytest.mark.browser` so unit tests can run without it.
- The async API is the primary one (`AsyncRenderer`); `Renderer` is a thin sync
  wrapper. Put new rendering logic in the async code, not in the sync wrapper.
- Functions in `document/` take and return `bytes` or `PdfDocument`, never file
  paths. Only `PdfDocument.open()` / `.save()` and the CLI touch the filesystem.
- Raise the exceptions from `dravenpdf.errors`, not bare `Exception`s. The
  server maps them to HTTP status codes in one place.
- Every Playwright render uses a **new browser context** and must go through the
  request guard in `render/guards.py` (SSRF protection). Never bypass it.

## Commands (once scaffolded)

```bash
uv sync --all-extras                 # install everything incl. dev tools
uv run playwright install chromium   # one-time browser download
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -m "not browser"       # fast, no Chromium
uv run pytest                        # full suite
uv run dravenpdf render in.html -o out.pdf
DRAVENPDF_API_KEY=dev uv run uvicorn dravenpdf.server.app:create_app --factory --reload
```
