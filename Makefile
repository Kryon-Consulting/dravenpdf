.PHONY: install browser lint format typecheck test test-all check serve docker

install:
	uv sync --all-extras

browser:
	uv run playwright install chromium

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy

test:
	uv run pytest -m "not browser"

test-all:
	uv run pytest

check: lint typecheck test

serve:
	DRAVENPDF_API_KEY=$${DRAVENPDF_API_KEY:-dev} uv run dravenpdf serve

docker:
	docker build -t dravenpdf .
