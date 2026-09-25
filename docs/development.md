# Development

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Chromium, installed through Playwright (one time): `uv run playwright install --with-deps chromium`.
  To use a different Chromium binary (for example one preinstalled in a container
  whose version doesn't match Playwright's), set `DRAVENPDF_CHROMIUM_PATH=/path/to/chrome`.
- Fonts for the scripts you render (for example `fonts-noto`, `fonts-noto-cjk`,
  `fonts-noto-color-emoji` on Debian/Ubuntu). Missing fonts are the most common
  cause of "wrong looking" PDFs.

## Setup

```bash
uv sync --all-extras
uv run playwright install chromium
```

## Everyday commands

| Task | Command |
|---|---|
| Lint | `uv run ruff check .` |
| Dependency check | `uv run deptry src` (imports vs. declared dependencies) |
| Format | `uv run ruff format .` |
| Type check | `uv run mypy src` |
| Unit tests (no browser) | `uv run pytest -m "not browser"` |
| All tests | `uv run pytest` |
| Run the service | `DRAVENPDF_API_KEY=dev uv run uvicorn dravenpdf.server.app:create_app --factory --reload` |
| Build the Docker image | `docker build -t dravenpdf .` |

CI is disabled for now (`.github/workflows/ci.yml` only runs when started by hand),
so run `make check` and the browser tests locally before pushing.

A `Makefile` wraps these (`make lint`, `make test`, `make serve`, `make docker`).

## Tests

- `tests/unit/` has no browser and no network. Options validation, guards
  (with a stubbed DNS lookup), and all `document/` operations on fixture PDFs.
- `tests/integration/` is marked `@pytest.mark.browser`. It renders real HTML
  and checks page count, page size and extracted text.
- `tests/visual/` (planned, M7) will render fixtures to PNG with pypdfium2 and compare
  them with reference images using a small pixel tolerance.
- `tests/server/` uses FastAPI's `TestClient`, covering auth, error mapping,
  size limits and each endpoint. `test_api.py` uses a `FakeRenderer` (no browser);
  `test_api_browser.py` runs the real one.
- `tests/unit/test_cli.py` and `tests/integration/test_cli_render.py` drive the CLI
  through Typer's `CliRunner`.

Tests must not hit the public internet. URL rendering tests use a local HTTP
server fixture (`server` in `tests/conftest.py`) and the `local_renderer` fixture,
whose allowlist is `["localhost"]`; `127.0.0.1` URLs to the same server stay
blocked, which is how redirect and SSRF tests get a "forbidden" target.

## Conventions

- Type hints everywhere; `mypy --strict` must pass on `src/`.
- Library code never prints. It uses `logging.getLogger("dravenpdf....")`.
- Keep the CLI and server thin: if logic would be useful from Python, it
  belongs in `render/` or `document/`.
- When the public API or an endpoint changes, update `docs/library-api.md`
  or `docs/http-api.md` in the same commit.
- Record new architectural decisions in `docs/decisions.md`.

## Docker

The image is based on `python:3.12-slim-bookworm`. `playwright install --with-deps
chromium` adds Chromium and its system libraries at build time, plus Noto fonts. It runs
`dravenpdf serve` as a non-root user. Chromium runs with `--no-sandbox`
inside the container, so the container is the isolation boundary. Don't run
the service outside a container with untrusted HTML.
