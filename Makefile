.PHONY: verify test unit lint types run seed fmt

verify:
	uv run python scripts/verify.py

unit:
	uv run pytest tests/unit

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

types:
	uv run mypy core app

seed:
	uv run alembic upgrade head
	uv run python -m app.seed

run:
	uv run uvicorn app.main:app --reload
