# HTTP API (planned)

The service is the `[server]` extra: `pip install "dravenpdf[server]"`.
It is **synchronous**: each response body is the result (a PDF, ZIP or JSON).
There is no job queue.

## Auth

Every `/v1/*` request must send the header `X-API-Key: <key>`. The key comes
from `DRAVENPDF_API_KEY`. If that variable is unset the server **refuses to
start**, unless `DRAVENPDF_AUTH_DISABLED=true` is set explicitly (for local
development only). Keys are compared in constant time. `/healthz`, `/readyz`
and `/metrics` do not need a key.

## Endpoints

### Rendering (JSON body)

| Endpoint | Body | Response |
|---|---|---|
| `POST /v1/render/html` | `{"html": str, "base_url"?: str, "options"?: RenderOptions, "post"?: PostProcess}` | `application/pdf` |
| `POST /v1/render/url` | `{"url": str, "options"?: RenderOptions, "post"?: PostProcess}` | `application/pdf` |
| `POST /v1/render/template` | `{"template": str, "data": object, "options"?: RenderOptions, "post"?: PostProcess}` | `application/pdf` |

`PostProcess` is optional work to do after rendering:
`{"stamp_text"?: {...}, "metadata"?: {...}, "compress"?: bool}`.

Example:

```bash
curl -X POST http://localhost:8000/v1/render/html \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"html":"<h1>Hello</h1>","options":{"paper":"A4","landscape":false}}' \
  -o hello.pdf
```

### PDF operations (multipart/form-data)

| Endpoint | Fields | Response |
|---|---|---|
| `POST /v1/pdf/merge` | `files` (2+ PDFs, in order) | `application/pdf` |
| `POST /v1/pdf/split` | `file`, `every` or `ranges` | `application/zip` |
| `POST /v1/pdf/extract` | `file`, `ranges` | `application/pdf` |
| `POST /v1/pdf/rotate` | `file`, `degrees`, `pages`? | `application/pdf` |
| `POST /v1/pdf/stamp` | `file`, one of `text` / `image` / `html`, `opacity`?, `angle`?, `position`?, `pages`? | `application/pdf` |
| `POST /v1/pdf/metadata` | `file`, `title`?, `author`?, `subject`?, `keywords`? | `application/pdf` |
| `POST /v1/pdf/compress` | `file` | `application/pdf` |

### Conversion (multipart/form-data)

| Endpoint | Fields | Response |
|---|---|---|
| `POST /v1/convert/images-to-pdf` | `files` (PNG/JPEG), `paper`? | `application/pdf` |
| `POST /v1/convert/pdf-to-images` | `file`, `dpi`?, `format`? (`png`/`jpeg`) | `application/zip` |
| `POST /v1/convert/text` | `file` | `application/json`: `{"pages": [str, ...]}` |

### Operations

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | The process is up |
| `GET /readyz` | The browser pool is running and has capacity |
| `GET /metrics` | Prometheus: render duration, queue depth, active renders, browser restarts, errors by type |

## Errors

Error responses are JSON: `{"error": {"code": str, "message": str}}`.

| Status | `code` | When |
|---|---|---|
| 400 | `invalid_request` | Validation error, bad page range |
| 401 | `unauthorized` | Missing or wrong API key |
| 413 | `payload_too_large` | Body or upload exceeds the limit |
| 415 | `unsupported_media_type` | Not a PDF or image where one is needed |
| 422 | `invalid_pdf` | The uploaded file can't be parsed as a PDF |
| 422 | `blocked_request` | URL or a sub-resource was blocked by the SSRF guard |
| 503 | `busy` | Render queue full (the response includes `Retry-After`) |
| 504 | `render_timeout` | Render exceeded `timeout_ms` |
| 500 | `internal_error` | Anything else (logged with a request ID) |

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `DRAVENPDF_API_KEY` | – (required) | API key |
| `DRAVENPDF_AUTH_DISABLED` | `false` | Local development only |
| `DRAVENPDF_MAX_CONCURRENCY` | `4` | Concurrent renders per worker |
| `DRAVENPDF_MAX_QUEUE` | `16` | Renders waiting before a 503 |
| `DRAVENPDF_RENDER_TIMEOUT_MS` | `30000` | Upper limit; the request's `timeout_ms` can't exceed it |
| `DRAVENPDF_MAX_BODY_MB` | `25` | Request and upload size limit |
| `DRAVENPDF_ALLOWED_HOSTS` | – | Comma-separated host allowlist for URL rendering and sub-resources. Unset = any public host |
| `DRAVENPDF_BROWSER_RECYCLE_AFTER` | `500` | Restart Chromium after N renders |
| `DRAVENPDF_LOG_LEVEL` | `INFO` | |
