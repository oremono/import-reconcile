# Technical Design

Companion to [SPEC.md](SPEC.md). The spec says what the system does; this says how it is built. Every requirement reference (R*.*) and decision reference (D*) points back to the spec.

---

## 1. Stack

| Layer | Choice | Why |
|---|---|---|
| Web | **FastAPI** + **Jinja2** templates | Server-rendered pages, which the brief explicitly permits. FastAPI gives typed request handling and a free OpenAPI view of the same routes. |
| ORM | **SQLAlchemy 2.0** (sync) | Explicit, typed mapping. Sync because nothing here is IO-bound enough to justify async complexity in a batch tool. |
| Migrations | **Alembic** | The schema is the deliverable; it should be versioned, not created by `create_all`. |
| Validation | **Pydantic v2** | Config objects (source formats, tolerance profiles) validate on load rather than failing mid-run. |
| DB | **SQLite** by default, Postgres by URL | Reviewers clone and run with no service to start. Nothing in the schema is SQLite-specific — `DATABASE_URL` switches it. |
| Tests | **pytest** | Two suites: pure logic (no DB, no app), and integration. |
| Deps | **uv** | Already on the machine; `uv sync` is one command. |

### 1.1 Decimal exactness

Reconciliation is arithmetic about money, so no value may ever pass through a float. SQLAlchemy's `Numeric` falls back to float on SQLite and warns about it. A small `ExactDecimal` `TypeDecorator` stores every amount and quantity as **TEXT** and returns `decimal.Decimal`, which is exact on both backends and sorts correctly because values are normalised to a fixed scale before storage.

CSV values are parsed with `Decimal(str)` directly — never `float()`.

---

## 2. Architecture

The brief requires that "the comparison logic should be testable without a database and without a browser". That is enforced structurally, not by convention:

```
core/          pure logic - stdlib + decimal only
               NO sqlalchemy, NO fastapi, NO app imports
   |
   v
app/services/  orchestration - loads rows, calls core, persists results
   |
   v
app/db/        SQLAlchemy models, session, migrations
app/web/       routes + Jinja templates
```

`core/` takes dataclasses in and returns dataclasses out. Its tests construct records in Python lists. A test named `test_core_has_no_infrastructure_imports` walks `core/` and asserts the boundary holds, so it cannot rot.

### 2.1 Layout

```
core/
  model.py        NormalizedRecord, Side, RecordStatus, RowError
  format.py       SourceFormat - column map, date formats, tz, vocab map
  normalize.py    raw dict -> NormalizedRecord | RowError
  tolerance.py    Tolerances + the within-tolerance predicates
  compare.py      pair -> FieldDiff list + verdict
  match.py        two record lists -> MatchResult
app/
  config.py       settings, source registry, tolerance profiles
  db/models.py    SQLAlchemy models
  db/types.py     ExactDecimal
  services/ingest.py      file -> batch + records (hash, version, supersede)
  services/reconcile.py    run orchestration + persistence
  services/resolve.py      create / revoke resolutions
  web/routes.py, web/templates/
migrations/       alembic
data/             sample CSVs
tests/unit/       no DB, no app
tests/integration/
```

---

## 3. Data model

Nine tables. Names below are logical; the migration is the authority.

### `source`
One row per system that sends data. `code` (`ledger`, `statement`, ...), `name`, and `format_config` (JSON) holding the column map, timestamp format and timezone, and the vocabulary map. **Adding a third source is one row here** — this is R2.4 made concrete.

### `tolerance_profile`
Per source pair (D13): `amount_bps`, `amount_abs_floor`, `price_bps`, `qty_bps`, `time_tolerance_seconds`, `suggest_window_seconds`. Seeded with the §5.5 values.

### `file_batch`
One accepted delivery. `source_id`, `period_start`, `period_end`, `filename`, `content_hash`, `version_no`, `superseded_by_id`, `accepted_at`, counters.

- **Duplicate detection (R1.2):** unique index on `(source_id, content_hash)`. A resend collides and is refused. The hash is of file *contents*, never the filename (D6).
- **Corrections (R1.3, D7):** a differing file for the same `(source, period)` inserts a new batch with `version_no + 1` and sets `superseded_by_id` on the previous one. Only the un-superseded batch is current.
- **Partial overlap (R1.6, D21):** on insert, any existing batch for the source whose period overlaps but does not equal the incoming period causes a rejection.

### `record`
One normalised row. `batch_id`, `source_id`, `reference`, `occurred_at` (UTC), `instrument`, `side`, `quantity`, `unit_price`, `gross_amount`, `status`, `is_cancelled`, `row_no`, `raw` (JSON of the original row).

Records are **never mutated**. A correction writes new records under a new batch; the old ones survive under the superseded batch, which is how "what did this row say before?" is answered (R8.2). History for a reference is `SELECT ... WHERE source_id = ? AND reference = ? ORDER BY batch.version_no`.

Current records are those whose batch has `superseded_by_id IS NULL`. A reference present in an earlier version and absent from the current one is **withdrawn** (R1.4, D8), computed at run time by comparing reference sets across versions.

### `rejected_row`
`batch_id`, `row_no`, `raw`, `reason` (R1.5, D15). Bad rows never block the good ones.

### `run`
`left_source_id`, `right_source_id`, `period_start`, `period_end`, `started_at`, `finished_at`, `counts` (JSON summary). Runs are append-only (R8.1).

### `pair`
`run_id`, `left_record_id`, `right_record_id`, `origin` (`reference` | `suggested` | `manual`), `verdict` (`agreed` | `agreed_with_drift` | `break`), `resolution_id` when a person created it. Unique on `(run_id, left_record_id)` and `(run_id, right_record_id)` — the database enforces one-to-one (R4.8, D11).

### `run_item`
One row per record per run: `run_id`, `record_id`, `side`, `state`, `pair_id`. `state` is the closed set from §5.6. This is what the worklist queries, and it makes the summary a single `GROUP BY` — every record read lands in exactly one state, which is acceptance criterion 2.

### `resolution`
The durable decision. **Keyed on `(source_id, reference)`, not on `record.id`.**

This is the single most important schema decision. Record ids change whenever a correction lands, so a resolution pointing at a record id would silently detach the moment the counterparty resent the file — exactly when it matters most. Keying on the business identity is what makes R7.4 ("a resolution is a statement about identity, not about a run") true rather than aspirational, and it is why D9 works: the pairing survives a correction and gets re-compared against the new values.

Columns: `kind` (`manual_match` | `accept_unmatched` | `reject_suggestion`), `left_source_id`, `left_reference`, `right_source_id`, `right_reference` (null for `accept_unmatched`), `reason`, `author`, `created_at`, `revoked_at`, `revoked_reason`.

Safe because references are never reused (D19).

---

## 4. Ingestion

```
read CSV -> hash contents -> duplicate? -> period overlap? -> normalize each row
         -> valid rows to record, invalid to rejected_row -> supersede prior version
```

Normalisation (`core/normalize.py`) is driven entirely by the source's `SourceFormat`:

- **Columns** — a dict from our field name to the source's column name.
- **Timestamps** — an ordered list of `strptime` patterns, tried in order; a naive result is localised with the source's declared timezone and converted to UTC (R2.2, D17). Never guessed per file.
- **Vocabulary** — dicts mapping the source's tokens to ours: `{"BUY": BUY, "B": BUY, "SELL": SELL, "S": SELL}`, and the status map naming whichever word that source uses for cancelled.
- **Numbers** — `Decimal(str(value))`, no rounding on the way in (R2.5).

A row missing a required field, or with an unparseable date or number, returns a `RowError` carrying the reason and the original row rather than raising.

---

## 5. Matching

`core.match.match(left, right, tolerances, prior_resolutions) -> MatchResult`

Pure function. Input: two lists of `NormalizedRecord` plus resolutions expressed as reference pairs. Output: pairs, suggestions, unmatched on each side, excluded, and auto-revocations.

**Tier 0 — carry forward (R4.1).** Apply `manual_match` resolutions as forced pairs. Mark `accept_unmatched` references as settled. Load `reject_suggestion` pairs into a blocklist (R4.6).

**Tier 1 — exclude (R3.1).** Partition out cancelled records. If a reference is cancelled on one side and live on the other, the live record is flagged `status_disagreement` rather than plain unmatched (R3.4, D14).

**Tier 2 — reference match (R4.2).** Index the right side by reference; pair on equality.

**Tier 3 — suggest (R4.3).** Bucket the remainder by `(instrument, side)`. Within a bucket, a candidate qualifies when quantity is within `qty_bps` and the timestamp gap is within `suggest_window_seconds` (2h, D5 — deliberately much wider than the 5-minute comparison tolerance, because a suggestion is a question, not an assertion). Candidates are ranked by time gap then amount gap. **Never applied automatically** (R4.4, D4). The candidate pool is this run's records only (D22).

**Tier 4 — unmatched (R4.7).** Whatever is left, reported on both sides.

**Auto-revoke (R7.7, D10).** If a reference previously accepted as unmatched now has a reference match, the acceptance is revoked and the pair surfaced. The only automatic reversal in the system, and it is always reported.

Bucketing by `(instrument, side)` keeps tier 3 close to linear instead of quadratic across the whole file.

---

## 6. Comparison

`core.compare.compare(left, right, tolerances) -> Comparison`

Per field, produce a `FieldDiff(field, left_value, right_value, abs_diff, rel_diff, within_tolerance)`. **Every** differing field is recorded, not the first (R5.3).

- Relative difference is computed against the **larger** of the two magnitudes, so the answer does not change depending on which side is called ours.
- Amount: within tolerance when `abs_diff <= max(amount_abs_floor, amount_bps * larger)`.
- Price, quantity: basis-point rules only.
- Timestamp: `abs_diff <= time_tolerance_seconds`.
- Instrument, side, status: exact equality after vocabulary mapping; any difference is a break.

Verdict: `break` if any field is outside tolerance; `agreed_with_drift` if all are inside but at least one is non-zero; otherwise `agreed` (R5.1, R5.2).

All comparisons are `Decimal`. Basis points are `Decimal("0.0005")`, not `0.0005`.

---

## 7. Web

Eight routes, all server-rendered.

| Route | Page |
|---|---|
| `GET /` | Runs list + upload form + accepted files (§6.1) |
| `POST /files` | Upload. Duplicate and overlap rejections render as a message here |
| `POST /runs` | Start a run, redirect to its summary |
| `GET /runs/{id}` | Run summary, counts by state (§6.2) |
| `GET /runs/{id}/worklist` | Filterable, sortable by size of difference (§6.3) |
| `GET /runs/{id}/pairs/{pair_id}` | Break detail, field by field (§6.4) |
| `GET /runs/{id}/records/{record_id}` | Unmatched detail + ranked candidates (§6.5) |
| `POST /resolutions` | Manual match, accept-unmatched, reject-suggestion. Reason required |
| `GET /records/{source}/{reference}/history` | Value history across versions (§6.6) |

No JavaScript framework. Forms post, server redirects. Every resolution form requires a non-empty reason and author before it will submit (R7.3).

---

## 8. Tests

Mapped to the twelve acceptance criteria in SPEC §9.

**`tests/unit/` — no database, no app, no browser.** This is the suite the brief singles out.

- `test_normalize.py` — both date formats; `BUY`/`B` and `SELL`/`S`; naive timestamps localised by declared timezone; missing field, bad date, bad number each return `RowError` (AC 1).
- `test_tolerance.py` — boundary triples for every rule: just inside, exactly on, just outside. The absolute floor beating the relative rule on small amounts, and the reverse on large (AC 4).
- `test_compare.py` — a pair differing in three fields reports all three; relative difference is symmetric under swapping sides; `agreed` vs `agreed_with_drift` vs `break` (AC 5).
- `test_match.py` — reference matching; cancelled excluded and never unmatched (AC 3); cancelled one side surfaces as status disagreement; unmatched reported in both directions (AC 6); suggestion ranking; one-to-one enforced; resolutions applied before matching; auto-revoke fires (AC 11).
- `test_boundaries.py` — `core/` imports nothing from `app/`, sqlalchemy, or fastapi.

**`tests/integration/` — real SQLite.**

- Ingest: identical resend refused, counts unchanged (AC 7); overlapping period refused; bad rows land in `rejected_row` while good rows load.
- Corrections: values change, history preserved, next run drops the fixed rows from the worklist (AC 8).
- Durability: manual match then re-run leaves the pair intact with reason and author (AC 9); accept-unmatched then re-run keeps it out of the worklist (AC 10).
- Summary: state counts sum to records read (AC 2).
- Third source: a new `source` row alone reconciles a third format (AC 12).
- Web smoke: every route returns 200; a resolution POST redirects and takes effect.

---

## 9. Sample data

`data/`, one week, 2025-07-01 to 2025-07-07, ~40 rows a side. Built to make every case in §9 demonstrable in the video.

| File | Purpose |
|---|---|
| `ledger_2025-07-01_07.csv` | Our side |
| `statement_2025-07-01_07.csv` | Their side, different columns, dates, vocabulary |
| `statement_2025-07-01_07_resend.csv` | Byte-identical to the above - duplicate demo |
| `statement_2025-07-01_07_v2.csv` | Correction: 3 amounts fixed, 1 row withdrawn |
| `venue_c_2025-07-01_07.csv` | Third format: different columns again, `d`/`c` for side, epoch-second timestamps |

Cases seeded: exact agreement; drift inside tolerance; amount break; price+amount break together; 40-minute time break; side disagreement; cancelled on both sides; cancelled on one side only; ledger-only row; statement-only row; a near-match pair with mismatched references that should surface as a suggestion; and two malformed rows.

The worked example from the brief is included verbatim so a reviewer can check our thresholds against their own intuition.

---

## 10. Build order

Roughly the commit sequence. Estimates assume no interruptions.

1. Scaffold: `uv` project, config, SQLite session, Alembic init — **30 min**
2. `core/` model, format, normalize + unit tests — **50 min**
3. `core/` tolerance, compare + boundary tests — **40 min**
4. `core/` match + unit tests — **60 min**
5. DB models and first migration — **40 min**
6. Ingest service: hash, dedupe, versioning, rejected rows + integration tests — **50 min**
7. Reconcile service: run, persist pairs/diffs/items + integration tests — **50 min**
8. Web: routes and templates — **70 min**
9. Resolutions: create, revoke, carry-forward + durability tests — **50 min**
10. Sample data, third source, README — **50 min**

Total roughly **8 hours**, above the brief's 5-6 hour guide. If time runs short, steps 1-8 stand alone as a working system and step 9 is the one that must not be cut, since durable resolutions are the requirement most likely to be under-built.

---

## 11. Running it

```
uv sync
uv run alembic upgrade head
uv run python -m app.seed          # sources, tolerance profiles, sample files
uv run uvicorn app.main:app --reload
```

Tests:

```
uv run pytest tests/unit           # no database, no browser
uv run pytest
```
