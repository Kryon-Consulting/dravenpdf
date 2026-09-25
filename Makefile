.PHONY: install browser lint format typecheck test test-all check

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
