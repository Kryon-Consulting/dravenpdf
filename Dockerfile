# dravenpdf HTTP service: Python + Chromium (via Playwright) + Noto fonts.
#   docker build -t dravenpdf .
#   docker run --init -p 8000:8000 -e DRAVENPDF_API_KEY=change-me dravenpdf
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --extra server --no-install-project

# Chromium with its system libraries, plus fonts for Latin, CJK and emoji text.
# Missing fonts are the most common cause of wrong-looking PDFs.
RUN apt-get update \
 && playwright install --with-deps chromium \
 && apt-get install -y --no-install-recommends \
      fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

COPY src ./src
RUN uv sync --locked --no-dev --extra server

RUN useradd --create-home --uid 10001 dravenpdf
USER dravenpdf

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

# One Chromium per worker; size workers and DRAVENPDF_MAX_CONCURRENCY to the memory you have.
CMD ["sh", "-c", "exec dravenpdf serve --host 0.0.0.0 --port 8000 --workers ${DRAVENPDF_WORKERS:-1}"]
