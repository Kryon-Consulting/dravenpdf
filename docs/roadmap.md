# Roadmap

Status key: ☐ not started · ◐ in progress · ☑ done

## M0 – Planning and docs ☑
Research, decisions, architecture, API design (this folder).

## M1 – Project skeleton ☑
`pyproject.toml` (uv + hatchling, `[server]` extra), `src/dravenpdf` package,
`errors.py`, `options.py`, ruff/mypy/pytest config, Makefile, CI workflow.
`RenderOptions.to_pdf_kwargs()` already maps options to Playwright's `page.pdf()`.

## M2 – Rendering core ☐
`BrowserPool`, `AsyncRenderer` (`from_html`, `from_url`, `from_file`), guards,
waits, header/footer, sync `Renderer`, integration tests.

## M3 – PdfDocument and page operations ☐
Open/save/compress/metadata, merge, split, extract, rotate, delete, reorder, unit tests.

## M4 – Stamps, images, text, templates ☐
Text/image/HTML stamps, images ↔ PDF, text extraction, Jinja2 `from_template`.

## M5 – CLI ☐
Typer commands listed in `library-api.md`.

## M6 – HTTP service ☐
FastAPI app, API key auth, settings, all `/v1` endpoints, error mapping,
health and metrics, server tests.

## M7 – Packaging and deployment ☐
Dockerfile, docker-compose, visual tests, README usage examples, first release.

## Later / out of scope for now
- Digital signatures (`pyhanko`), form filling, encryption and passwords (pikepdf)
- PDF/A (would need Ghostscript, which is AGPL; see D5)
- Async job queue with callbacks (see D7)
- WeasyPrint as a lightweight backend without a browser
