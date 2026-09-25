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
Done: Dockerfile, docker-compose, and a CI job that builds the image and renders
through it. CI is disabled for now (manual runs only), so the image has not been built yet.
To do: visual regression tests, a first tagged release, publishing (PyPI or an
internal index, container registry).

## Post-M6 features ☑
From the feature review (in the order they were built):
- Guard fixes: WebSockets are checked like HTTP; a failed guard fetch aborts the
  request instead of hanging the render.
- Render reports (`doc.render_report`) and strict mode (`fail_on_resource_errors`,
  `fail_on_page_errors`); HTTP count headers.
- HTML plus in-memory asset bundles (`from_html(assets=...)`, `/v1/render/bundle`).
- Browser environment: viewport, device scale factor, locale, time zone, colour
  scheme, reduced motion.
- CSS `@page` size and margins, tagged PDFs and heading outlines,
  `wait_for_expression`, and the Python-only `prepare` hook.

- Rendering pages behind a login: `RenderAuth` (cookies, Playwright storage state,
  headers per exact origin) for `from_url`, the sync `Renderer` and
  `POST /v1/render/url`. This also fixed the guard forwarding the first URL's cookies
  to a cross-origin redirect target (see D11).

## M8 – Encryption, forms, signatures ☑
Decided in D12: encryption and passwords, form filling and flattening, digital
signatures with keys configured on the server, and verification.

## Later / out of scope for now
- PDF/A (would need Ghostscript, which is AGPL; see D5)
- Async job queue with callbacks (see D7)
- WeasyPrint as a lightweight backend without a browser
