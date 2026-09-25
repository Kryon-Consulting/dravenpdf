# HTTP API

The service is the `[server]` extra: `pip install "dravenpdf[server]"`. Run it with
`dravenpdf serve` or the Docker image. Interactive docs are at `/docs` and the
OpenAPI schema at `/openapi.json`.

It is **synchronous**: each response body is the result (a PDF, ZIP or JSON).
There is no job queue.

## Running

```bash
DRAVENPDF_API_KEY=change-me dravenpdf serve --host 0.0.0.0 --port 8000 --workers 2

docker build -t dravenpdf .
docker run --init -p 8000:8000 -e DRAVENPDF_API_KEY=change-me dravenpdf
```

Each worker process runs its own Chromium. Size `--workers` (Docker:
`DRAVENPDF_WORKERS`) and `DRAVENPDF_MAX_CONCURRENCY` to the memory you have,
roughly 100–300 MB per concurrent render. Use `--init` (or `init: true` in Compose)
so Chromium's child processes are reaped.

## Auth

Every `/v1/*` request must send the header `X-API-Key: <key>`. The key comes
from `DRAVENPDF_API_KEY`. If that variable is unset the server **refuses to
start**, unless `DRAVENPDF_AUTH_DISABLED=true` is set explicitly (for local
development only). Keys are compared in constant time. `/healthz`, `/readyz`,
`/metrics`, `/docs` and `/openapi.json` do not need a key.

## Endpoints

### Rendering (JSON body)

| Endpoint | Body | Response |
|---|---|---|
| `POST /v1/render/html` | `{"html", "base_url"?, "options"?, "post"?, "filename"?}` | `application/pdf` |
| `POST /v1/render/url` | `{"url", "options"?, "post"?, "filename"?}` | `application/pdf` |
| `POST /v1/render/template` | `{"template", "data"?, "base_url"?, "options"?, "post"?, "filename"?}` | `application/pdf` |
| `POST /v1/render/bundle` | multipart: `files` (assets; each file's name is its bundle path), `html` or a file named `index.html`, `data`? (JSON: render as a template), `options`? / `post`? (JSON), `filename`? | `application/pdf` |

- `options` is `RenderOptions` (see [library-api.md](library-api.md#renderoptions)):
  paper, size, margins, header/footer, waits, `timeout_ms`, ... Its `timeout_ms` is
  capped at `DRAVENPDF_RENDER_TIMEOUT_MS`. It covers the whole request, including
  time spent waiting for a free browser and (re)launching Chromium.
- `/v1/render/url` takes an optional `auth` object for pages behind a login:
  `{"cookies": [...], "storage_state": {...}, "headers": {"<origin>": {"Name": "value"}}}`
  (see [library-api.md](library-api.md#rendering-pages-behind-a-login)). Storage state
  is sent as data (Playwright's `storage_state()` JSON), never a server path. It applies
  to that one render. It is unrelated to `X-API-Key`, which authenticates the caller to
  dravenpdf. Credential values never appear in error messages, logs, metrics or
  response headers.

  ```bash
  curl -X POST http://localhost:8000/v1/render/url -H "X-API-Key: $KEY" \
    -H "Content-Type: application/json" -d '{
      "url": "https://app.example.com/reports/42",
      "auth": {
        "cookies": [{"name": "session", "value": "…", "url": "https://app.example.com"}],
        "headers": {"https://api.example.com": {"Authorization": "Bearer …"}}
      }}' -o report.pdf
  ```
- `/v1/render/bundle` sends the HTML together with its CSS, fonts, images and scripts.
  Relative references resolve inside the bundle; files are served from memory and never
  reach the network (other URLs the page loads go through the SSRF guard as usual).
  Paths must be relative, without `..`; a missing file is a 404 counted in
  `X-DravenPdf-Resource-Errors`. The request body limit applies to the whole bundle.

  ```bash
  curl -X POST http://localhost:8000/v1/render/bundle -H "X-API-Key: $KEY" \
    -F "files=@index.html" \
    -F "files=@site.css;filename=css/site.css" \
    -F "files=@logo.png;filename=img/logo.png" \
    -F 'options={"paper":"A4","fail_on_resource_errors":true}' -o out.pdf
  ```
- `template` is Jinja2 **source** (sandboxed, autoescaped). The service does not read
  template files from its own disk.
- `post` is optional work on the result:
  `{"stamp_text"?: {"text", "font_size", "color", "opacity", "angle", "position", "margin"},
  "metadata"?: {"title", "author", "subject", "keywords"}, "compress"?: bool}`.
- `filename` (default `document.pdf`) sets `Content-Disposition`; unsafe characters
  are replaced.
- Unknown fields are rejected (400), which catches typos.

```bash
curl -X POST http://localhost:8000/v1/render/html \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"html":"<h1>Hello</h1>","options":{"paper":"A4","landscape":false},
       "post":{"stamp_text":{"text":"DRAFT"}}}' \
  -o hello.pdf
```

### PDF operations (multipart/form-data)

Page fields are **1-based** strings like `1,3-5,8-` (`8-` = page 8 to the end).

| Endpoint | Fields | Response |
|---|---|---|
| `POST /v1/pdf/merge` | `files` (2+ PDFs, in order) | `application/pdf` |
| `POST /v1/pdf/split` | `file`, and `every` or `ranges` (repeat the field once per part) | `application/zip` (`part-1.pdf`, ...); parts are built one at a time |
| `POST /v1/pdf/extract` | `file`, `ranges` | `application/pdf` |
| `POST /v1/pdf/rotate` | `file`, `degrees` (default 90), `pages`? | `application/pdf` |
| `POST /v1/pdf/delete` | `file`, `pages` | `application/pdf` |
| `POST /v1/pdf/reorder` | `file`, `order` (every page once, e.g. `3,1,2`) | `application/pdf` |
| `POST /v1/pdf/stamp` | `file`, exactly one of `text` / `image` (file) / `html`; `opacity`?, `angle`?, `font_size`?, `color`?, `width`?, `position`?, `margin`?, `under`?, `pages`? | `application/pdf` |
| `POST /v1/pdf/metadata` | `file`, `title`?, `author`?, `subject`?, `keywords`? (`""` removes) | `application/pdf` |
| `POST /v1/pdf/compress` | `file` | `application/pdf` |
| `POST /v1/pdf/form/fields` | `file`, `password`? | `application/json`: `{"fields": [{name, kind, value, options, read_only, required, multiline, max_length}]}` |
| `POST /v1/pdf/form/fill` | `file`, `values` (JSON object: text, true/false or an option per field name), `flatten`?, `password`? | `application/pdf` |
| `POST /v1/pdf/form/flatten` | `file`, `password`? | `application/pdf` |
| `POST /v1/pdf/encrypt` | `file`, `user_password`?, `owner_password`? (random if omitted), `password`? (if the input is already protected), `allow_print`/`allow_copy`/`allow_modify`/`allow_annotate`/`allow_forms`? | `application/pdf` (AES-256) |
| `POST /v1/pdf/decrypt` | `file`, `password` | `application/pdf` |

Other PDF endpoints refuse password-protected uploads with 422 `pdf_password`;
decrypt them first. Passwords never appear in responses or logs.

```bash
curl -X POST http://localhost:8000/v1/pdf/merge -H "X-API-Key: $KEY" \
  -F files=@a.pdf -F files=@b.pdf -o merged.pdf
```

### Conversion (multipart/form-data)

| Endpoint | Fields | Response |
|---|---|---|
| `POST /v1/convert/images-to-pdf` | `files` (PNG/JPEG/...), `paper`?, `landscape`?, `margin`? | `application/pdf` |
| `POST /v1/convert/pdf-to-images` | `file`, `dpi`? (10–600), `format`? (`png`/`jpeg`), `pages`? | `application/zip` (`page-1.png`, ...) |

A page larger than `DRAVENPDF_MAX_IMAGE_MEGAPIXELS` at the requested `dpi` is refused
before it is rendered. ZIP responses (`split`, `pdf-to-images`) stop at
`DRAVENPDF_MAX_OUTPUT_MB` of content. Either limit returns 422 `limit_exceeded`.
| `POST /v1/convert/text` | `file` | `application/json`: `{"pages": [str, ...]}` |

### Operations

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | The process is up |
| `GET /readyz` | 200 when the browser pool is running and not saturated, else 503 |
| `GET /metrics` | Prometheus: `dravenpdf_render_seconds{source}`, `dravenpdf_renders_active`, `dravenpdf_renders_waiting`, `dravenpdf_renders_total`, `dravenpdf_browser_launches_total`, `dravenpdf_browser_restarts_total` |

Every response has an `X-Request-ID` header (the client's own, if it sent one), and
every request is logged with it.

Render responses also carry counts from the render report:
`X-DravenPdf-Resource-Errors` (failed loads and 4xx/5xx sub-resources),
`X-DravenPdf-Page-Errors` (uncaught JavaScript exceptions) and `X-DravenPdf-Blocked`.
Details are in the server log. To fail instead, set `fail_on_resource_errors` or
`fail_on_page_errors` in `options`.

## Errors

Error responses are JSON: `{"error": {"code": str, "message": str}}`. The mapping
lives in `src/dravenpdf/server/errors.py`.

| Status | `code` | When |
|---|---|---|
| 400 | `invalid_request` | Validation error, bad page range, bad stamp arguments |
| 400 | `invalid_template` | Template can't be parsed or rendered |
| 401 | `unauthorized` | Missing or wrong API key |
| 413 | `payload_too_large` | Body larger than `DRAVENPDF_MAX_BODY_MB` (with or without Content-Length) |
| 415 | `unsupported_media_type` | An upload that should be a PDF isn't one |
| 422 | `limit_exceeded` | The result would pass an output limit (image pixels, ZIP size) |
| 422 | `invalid_pdf` | Looks like a PDF but can't be read |
| 422 | `pdf_password` | The PDF is password-protected and no (or the wrong) password was given |
| 422 | `blocked_request` | The URL or something the page loads was blocked by the SSRF guard |
| 422 | `render_failed` | The page couldn't be rendered, e.g. the URL returned HTTP 404 |
| 422 | `render_incomplete` | A strict render (`fail_on_resource_errors` / `fail_on_page_errors`) found problems; the message lists them |
| 503 | `busy` | Render queue full (the response includes `Retry-After`) |
| 504 | `render_timeout` | Render exceeded `timeout_ms` (time queued for a browser or launching one counts) |
| 500 | `internal_error` | Anything else; the message has the request ID, details are only in the log |

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `DRAVENPDF_API_KEY` | – (required) | API key |
| `DRAVENPDF_AUTH_DISABLED` | `false` | Local development only |
| `DRAVENPDF_MAX_CONCURRENCY` | `4` | Concurrent renders per worker |
| `DRAVENPDF_MAX_QUEUE` | `16` | Renders waiting before a 503 |
| `DRAVENPDF_RENDER_TIMEOUT_MS` | `30000` | Upper limit; a request's `timeout_ms` is capped at it |
| `DRAVENPDF_MAX_BODY_MB` | `25` | Request and upload size limit |
| `DRAVENPDF_MAX_OUTPUT_MB` | `100` | Content limit for ZIP responses and for all images from `pdf-to-images` |
| `DRAVENPDF_MAX_IMAGE_MEGAPIXELS` | `40` | Largest page `pdf-to-images` renders (A4 at 600 dpi is 35) |
| `DRAVENPDF_ALLOWED_HOSTS` | – | Comma-separated host allowlist for URLs and sub-resources (`*.example.com` allowed). Unset = any public host |
| `DRAVENPDF_BROWSER_RECYCLE_AFTER` | `500` | Restart Chromium after N renders |
| `DRAVENPDF_LOG_LEVEL` | `INFO` | |
| `DRAVENPDF_CHROMIUM_PATH` | – | Use this Chromium binary instead of Playwright's |
| `DRAVENPDF_WORKERS` | `1` | Docker image only: worker processes |
