.PHONY: verify test unit e2e lint types run seed fmt

verify:
	uv run python scripts/verify.py

unit:
	uv run pytest tests/unit

test:
	uv run pytest

e2e:
	uv run pytest -m e2e

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

# Depends on seed so `make run` cannot start against an unmigrated database.
# Both steps are idempotent.
run: seed
	uv run uvicorn app.main:app --reload
