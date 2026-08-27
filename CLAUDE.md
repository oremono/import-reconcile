# import-reconcile

Daily reconciliation of our trade ledger against a counterparty's statement. Take-home assignment; the brief is `docs/Transaction Reconciliation.pdf`.

**Status:** design complete, no code yet. The build follows `EXECUTION_PLAN.md` (gitignored — process notes, not deliverable).

---

## Read before changing anything

| Question | Document |
|---|---|
| What does it do, in business terms? | `docs/SPEC.md` — `R*.*` requirements, `D*` decisions, `AC*` acceptance criteria |
| What must it do technically? | `docs/REQUIREMENTS.md` — 82 `TR-*`, each with the test that proves it |
| How is it built, and why that way? | `docs/DESIGN.md` — `DD-*` decisions |
| What do those decisions cost? | `docs/TRADEOFFS.md` |
| How does the build run? | `EXECUTION_PLAN.md` |

Do not re-derive a decision that one of these already records. If you disagree with one, say so — do not quietly implement the other thing.

---

## Commands

```bash
make verify              # the oracle - lint, types, both suites, coverage, TR + AC trace, route smoke
uv run pytest tests/unit # MUST pass with no database present
uv run uvicorn app.main:app --reload
```

**Definition of done:** `make verify` reports `TR 82/82`, `AC 12/12`, `core` coverage ≥ 90%. Not a judgement call — a number.

---

## Invariants

Violating any of these is a defect, not a style preference. Each is enforced by a test.

1. **No float touches money.** `Decimal` end to end. No `float()` on the ingest, match, or compare path. Basis points are `Decimal` literals. — `test_no_float.py`
2. **`core/` imports the standard library only.** No `sqlalchemy`, no `fastapi`, no `app`. The brief requires comparison logic testable with no database and no browser. — `test_boundaries.py`
3. **`record` rows are never updated or deleted.** A correction writes new rows under a new batch. History is a property of the schema. — `test_boundaries.py`
4. **Resolutions key on `(source_id, reference)`, never `record.id`.** Corrections create new record rows; a row-id key detaches exactly when it matters. — `test_durability.py`
5. **Schema changes go through Alembic.** `create_all` only in test fixtures.
6. **Every stored timestamp is timezone-aware UTC.** Naive datetimes rejected at the type boundary.
7. **Tolerances come from `tolerance_profile`.** No threshold is a literal in comparison code.
8. **Every format detail lives in `source.format_config`.** No source-specific branch anywhere in `core/`. A third source is one config row.
9. **Suggestions are never auto-applied.** A pair is created only by reference match or by an explicit stored resolution.
10. **Nothing is hard-deleted.** Revocation and supersession are recorded.

---

## Rules of engagement

- **Never edit a test to make it pass.** A test bent to fit the code is a requirement silently deleted. If a test looks wrong, say so.
- **Never lower a gate.** Coverage thresholds, tolerance values, and mypy strictness are inputs, not variables.
- **Fix the first failure only**, then re-run. Fixing three at once hides which change worked.
- **Three strikes.** Same failure three times → stop and report what was tried and which assumption is suspect.
- **Frozen after Wave 0:** `core/model.py` and `app/db/models.py`. Changing either stops the wave, because everything else builds on them.
- **Do not add a dependency** without asking. The stack is deliberately small.
- **Do not build anything in `TR-8xx`** — those are non-requirements on purpose (no auth, no scheduler, no queue, no notifications, no escalation state, no multi-currency).
- **Stay in your lane.** During parallel waves, write only the files your agent brief lists as owned.

---

## Stack

Python 3.12 · FastAPI + Jinja2 · SQLAlchemy 2.0 (sync) · Alembic · Pydantic v2 · SQLite by default, Postgres via `DATABASE_URL` · pytest · uv · ruff · mypy.

No Redis, no Celery, no Docker requirement, no JS framework, no build step. Rationale in `docs/DESIGN.md` §14.

---

## Layers

```
core/            pure logic, stdlib only          <- the testable part
app/services/    orchestration
app/db/          models, custom types, migrations
app/web/         routes + templates
```

Dataclasses in, dataclasses out of `core/`. Services marshal to and from the database. `core/` never learns a database exists.

---

## Commits

The brief says the reviewers read the history, so it is part of the deliverable.

- Commit per meaningful step, not per file and not one giant drop.
- Imperative subject. Body explains **why**, not what — the diff already shows what.
- Author is `oremono` (set in this repo's git config).
- Never commit with failing tests.
- Never `git push --force` without being asked.

---

## Keeping the docs true

If you make a decision the documents do not cover, record it **in the same commit** as the code:

| Kind of decision | Goes in |
|---|---|
| Business rule or behaviour | `docs/SPEC.md` §8, next `D` number |
| Technical or structural | `docs/DESIGN.md` §14, next `DD` number |
| A new technical requirement | `docs/REQUIREMENTS.md`, next free `TR` in its group, with both trailing columns filled |
| A cost or a known weak point | `docs/TRADEOFFS.md` |

A requirement without a `Verified by` entry does not count as done — `make verify` will report it unverified.
