# Roadmap

Status key: ☐ not started · ◐ in progress · ☑ done

## M0 – Planning and docs ☑
Research, decisions, architecture, API design (this folder).

## M1 – Project skeleton ☑
`pyproject.toml` (uv + hatchling, `[server]` extra), `src/dravenpdf` package,
`errors.py`, `options.py`, ruff/mypy/pytest config, Makefile, CI workflow.
`RenderOptions.to_pdf_kwargs()` already maps options to Playwright's `page.pdf()`.

## M2 – Rendering core ☑
`BrowserPool`, `AsyncRenderer` (`from_html`, `from_url`, `from_file`), guards,
waits, header/footer, sync `Renderer`, integration tests.
Also a minimal `PdfDocument` (`from_bytes`, `open`, `page_count`, `to_bytes`, `save`)
so renders return the final type; M3 fills in the rest.

## M3 – PdfDocument and page operations ☑
Open/save/compress/metadata, merge, split, extract, rotate, delete, reorder, unit tests.
Also `insert`, `copy`, `page_size`, and 1-based range strings with open ends (`"8-"`).

## M4 – Stamps, images, text, templates ☑
Text/image/HTML stamps, images ↔ PDF, text extraction, Jinja2 `from_template`.
Also `overlay` (a page of another PDF as letterhead or background) and `TemplateError`.

## M5 – CLI ☑
Typer commands listed in `library-api.md`, plus `delete`, `metadata`, `compress`,
`from-images`, `stamp --pdf`, and `python -m dravenpdf`.

## M6 – HTTP service ☑
FastAPI app, API key auth, settings, all `/v1` endpoints, error mapping,
health and metrics, server tests. Also `/v1/pdf/delete` and `/v1/pdf/reorder`,
request IDs, streamed-body size limit, and post-processing on render endpoints.

## M7 – Packaging and deployment ◐
Done: Dockerfile, docker-compose, CI job that builds the image and renders through it.
To do: visual regression tests, a first tagged release, publishing (PyPI or an
internal index, container registry).

## Later / out of scope for now
- Digital signatures (`pyhanko`), form filling, encryption and passwords (pikepdf)
- PDF/A (would need Ghostscript, which is AGPL; see D5)
- Async job queue with callbacks (see D7)
- WeasyPrint as a lightweight backend without a browser
