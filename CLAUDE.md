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

M1–M6 are done: the library (`render/`, `document/`), the CLI (`cli.py`) and the
HTTP service (`server/`). After M6: guard fixes (WebSockets, failed fetches), render
reports and strict mode, in-memory asset bundles, browser environment options, CSS
page size, tagged PDFs/outlines, expression waits, `prepare` hooks, and rendering
behind a login (`RenderAuth`; see `docs/roadmap.md` and D11 in `docs/decisions.md`).
M7 (packaging) is partly done: Dockerfile and compose exist but the Docker image has
never been built; visual tests and a first release remain.
Update that file (and the status line in `README.md`) as milestones land.

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
6. **Signing keys live on the server** (configured files or key services); requests name
   a key and never upload private keys. Signatures are advanced electronic signatures;
   EU *qualified* signatures (eIDAS, certified signing hardware) are not a goal.
7. **Forms:** fill and flatten existing PDF forms only; no creating fillable forms from HTML.

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
- `PdfDocument` methods never modify `self`; they return a new document. Anything
  that copies pages between pikepdf PDFs must end with `pages._detach()` (see
  `docs/architecture.md`), or the result breaks once its source is garbage-collected.
- Call pdfium (pypdfium2) only through `document/_pdfium.open_pdf()`: pdfium is not
  thread-safe and the server runs PDF work in threads.
- New stamp kinds go through `stamp._stamp_each`/`_place`, which handle page rotation.
- Server: map new error codes to statuses only in `server/errors.py`; run CPU-bound
  PDF work with `asyncio.to_thread`; keep route handlers thin. Tests in `tests/server/`
  use a `FakeRenderer` so they don't need Chromium.
- CLI and HTTP page arguments are 1-based strings; the Python API is 0-based.
- Raise the exceptions from `dravenpdf.errors`, not bare `Exception`s. The
  server maps them to HTTP status codes in one place.
- Every Playwright render uses a **new browser context** and must go through the
  request guard in `render/guards.py` (SSRF protection). Never bypass it.
- Never `await` a Chromium launch or `new_context()` directly from a render: go
  through `BrowserPool._ensure_browser()` / `_new_context()`, which shield them so a
  render deadline can't leak a half-created browser or context.
- Build many-part outputs lazily (`PdfDocument.iter_split`) and don't keep earlier
  parts referenced; note that `enumerate()` holds its previous item (see the split route).
- Credentials (`RenderAuth`) must never reach logs, exceptions, reports, metrics or
  response headers. Keep values as `SecretStr` until the moment they're handed to
  Playwright, and pass any Playwright error text through `render/_redact.safe_message`
  first: its call log lists request headers. Per-hop header rules live in
  `RequestGuard.hop_headers`; redirect tests in `tests/integration/test_auth.py` fail if
  credentials follow a redirect to another origin.
- Every guard route handler must answer the browser on every path (fulfill, continue
  or abort), including when its own fetch fails; an unanswered route hangs the render.
  New kinds of browser traffic (like WebSockets) need their own guard route.
- Don't replace the guard's `route.fetch(max_redirects=0)` loop with
  `route.continue_()`: Playwright doesn't route redirect hops, so a public URL
  could redirect Chromium to an internal one unchecked (tests cover this).

## CI

CI (`.github/workflows/ci.yml`) is **disabled**: it only runs when started by hand.
Nothing checks pushes automatically, so run `make check` (and the browser tests,
`uv run pytest`) before every commit. Don't re-enable the triggers without asking.

## Commands

```bash
uv sync --all-extras                 # install everything incl. dev tools
uv run playwright install chromium   # one-time browser download
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -m "not browser"       # fast, no Chromium
uv run pytest                        # full suite
# If the installed Chromium doesn't match Playwright's version (e.g. a preinstalled
# one), point at it: DRAVENPDF_CHROMIUM_PATH=/path/to/chrome uv run pytest
make check                           # lint + typecheck + unit tests
uv run dravenpdf render in.html -o out.pdf   # CLI; see `dravenpdf --help`
DRAVENPDF_API_KEY=dev uv run dravenpdf serve  # HTTP service on :8000, docs at /docs
DRAVENPDF_API_KEY=dev uv run uvicorn dravenpdf.server.app:create_app --factory --reload
```
