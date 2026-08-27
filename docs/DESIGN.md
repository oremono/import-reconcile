# Technical Design

How the system is built. Every section names the technical requirements it satisfies.

---

## 1. How to read this

| Document | Answers | Identifiers |
|---|---|---|
| [SPEC.md](SPEC.md) | What the system does, in business terms | `R*.*` requirements, `D*` decisions, `AC*` acceptance criteria |
| [REQUIREMENTS.md](REQUIREMENTS.md) | What it must do technically, each item checkable | `TR-*` |
| **DESIGN.md** (this) | How it is built, and why it is built that way | `DD-*` design decisions (§14) |
| [TRADEOFFS.md](TRADEOFFS.md) | What those decisions cost, and where they break | reversibility, failure modes |

Business rationale lives in `SPEC.md` §8. Technical rationale lives here in §14. Neither is repeated in prose.

The `TR-8xx` group is satisfied by absence — no section below implements them, which is the point. They are written down so that missing authentication, missing scheduling, and missing escalation read as decisions rather than as oversights.

---

## 2. Constraints that drive the design

Six requirements determined the shape of everything below. The rest of the document follows from them.

| Constraint | Requirement | Consequence |
|---|---|---|
| No money value ever touches a float | TR-205, TR-404, TR-505 | A custom column type; `Decimal` end to end; an AST scan that fails the build on `float()` |
| Comparison logic testable with no database and no browser | TR-301, TR-302, TR-707 | A `core/` package importing only the standard library, with the boundary enforced by test |
| A third source is configuration, not code | TR-201, TR-207 | Every format detail lives in a `source` row; normalisation has no source-specific branch |
| Manual decisions must survive corrections | TR-508, TR-317 | Resolutions key on business identity, not row identity |
| Runs are reproducible and auditable | TR-303, TR-702, TR-708 | Deterministic ordering, append-only storage, nothing hard-deleted |
| Clone and run, no service to start | TR-703, TR-704 | SQLite by default, Postgres by URL, migrations in-repo |

---

## 3. Architecture and the purity boundary

The brief requires that "the comparison logic should be testable without a database and without a browser". That is enforced structurally rather than by convention (TR-301, TR-302).

```
core/            pure logic - stdlib + decimal only
                 NO sqlalchemy, NO fastapi, NO app imports
   |  dataclasses in, dataclasses out
   v
app/services/    orchestration - loads rows, calls core, persists results
   |
   v
app/db/          SQLAlchemy models, session, migrations
app/web/         routes + Jinja templates
```

`core/` never learns that a database exists. Its tests build records as Python lists and assert on returned dataclasses — no fixtures, no session, no client.

`test_boundaries.py` walks every module under `core/`, parses its imports, and fails on anything outside the standard library. The boundary cannot rot silently. The same test asserts `create_all` appears nowhere outside test fixtures (TR-507) and that no module branches on database dialect (TR-704).

---

## 4. Module layout

```
core/
  model.py       NormalizedRecord, Side, RecordStatus, RowError
  format.py      SourceFormat - column map, timestamp patterns, timezone, vocabulary
  normalize.py   raw dict -> NormalizedRecord | RowError          TR-2xx
  tolerance.py   Tolerances + the within-tolerance predicates      TR-405..TR-407
  compare.py     pair -> FieldDiff list + verdict                  TR-4xx
  match.py       two record lists -> MatchResult                   TR-3xx
app/
  config.py             settings, source registry, tolerance profiles   TR-705
  db/types.py           ExactDecimal, UtcDateTime                       TR-505, TR-506
  db/models.py          SQLAlchemy models
  services/ingest.py    file -> batch + records                         TR-1xx
  services/reconcile.py run orchestration + persistence                 TR-509
  services/resolve.py   create / revoke resolutions                     TR-508
  web/routes.py         nine routes                                     TR-6xx
  web/templates/
migrations/      alembic
data/            sample CSVs
tests/unit/      no database, no app, no network
tests/integration/
```

---

## 5. Data model

Ten tables. The migration is the authority; this is the intent. Several requirements are satisfied **by a constraint rather than by code**, which is deliberate — application logic can be bypassed, a unique index cannot.

| Table | Purpose | Key constraints | Satisfies |
|---|---|---|---|
| `source` | One row per system that sends data. Holds `code`, `name`, and `format_config` (JSON: column map, timestamp patterns, timezone, vocabulary map). Adding a third source is one row here. | unique `code` | TR-201, TR-207 |
| `tolerance_profile` | Thresholds per source pair: `amount_bps`, `amount_abs_floor`, `price_bps`, `qty_bps`, `time_tolerance_seconds`, `suggest_window_seconds`. | unique `(left_source_id, right_source_id)` | TR-405, TR-411 |
| `file_batch` | One accepted delivery. `source_id`, period, `filename`, `content_hash`, `version_no`, `superseded_by_id`, `accepted_at`. | **unique `(source_id, content_hash)`** | TR-102, TR-103, TR-105, TR-503 |
| `record` | One normalised row. Reference, `occurred_at` (UTC), instrument, side, quantity, unit price, gross amount, status, `is_cancelled`, `row_no`, `raw` JSON. **Never updated, never deleted.** | index `(source_id, reference)` | TR-501, TR-505, TR-506 |
| `rejected_row` | `batch_id`, `row_no`, `raw`, `reason`. Bad rows never block good ones. | — | TR-106 |
| `run` | Source pair, period, timings, `counts` JSON. Append-only. | — | TR-502, TR-512 |
| `pair` | `run_id`, both record ids, `origin` (`reference` / `suggested` / `manual`), `verdict`, `resolution_id`. | **unique `(run_id, left_record_id)` and `(run_id, right_record_id)`** | TR-311, TR-504 |
| `field_diff` | One compared field of one pair: both values, absolute and relative difference, whether it is within tolerance. A table rather than a JSON blob on `pair`, because the worklist orders by size of difference. | index `pair_id` | TR-401, TR-402, TR-607 |
| `run_item` | One row per record per run: `state` from the closed set in SPEC §5.6, plus `pair_id`. | unique `(run_id, record_id)` | TR-509, TR-511 |
| `resolution` | The durable decision. `kind`, `(left_source_id, left_reference)`, `(right_source_id, right_reference)`, `reason`, `author`, `created_at`, `revoked_at`, `revoked_reason`. | index on both identity pairs | TR-508, TR-708 |

`run_item` is what makes the summary a single `GROUP BY` and the worklist a single indexed query. It is also what makes acceptance criterion 2 mechanically true: every record read lands in exactly one row with exactly one state, so the counts cannot fail to add up.

---

## 6. Key mechanisms

### 6.1 Duplicates, corrections, and withdrawal

```
read bytes -> sha256 -> duplicate? -> period overlap? -> normalise rows
           -> valid to record, invalid to rejected_row -> supersede prior version
```

All of it in one transaction, so a failure leaves no batch row behind (TR-108).

- **Duplicate** (TR-102, TR-103): the digest is of raw bytes. Filenames are unreliable and often carry a send timestamp, so they play no part. The unique index means a resend is refused even if two uploads race.
- **Correction** (TR-105): same `(source, period)`, different digest, so the new batch takes `version_no + 1` and stamps `superseded_by_id` on its predecessor. Current records are those whose batch is not superseded.
- **Partial overlap** (TR-104): refused before any row is written. A file is a complete restatement of its period (`D7`), and a partial restatement cannot honour that.
- **Withdrawal** (TR-107): derived, not stored. A reference in a superseded version and absent from the current one is withdrawn. Storing a flag would mean mutating a record, which §5 forbids.

### 6.2 Normalisation

Driven entirely by the source's `SourceFormat` (TR-201):

- **Columns** — a dict from our field name to that source's column name.
- **Timestamps** — an ordered list of `strptime` patterns tried in order. A naive result is localised with the source's declared timezone and converted to UTC (TR-202, TR-203). Never guessed per file: a guessed timezone is how a one-hour break gets manufactured twice a year.
- **Vocabulary** — `{"BUY": BUY, "B": BUY, "SELL": SELL, "S": SELL}` and the status map naming that source's word for cancelled. An unmapped token is a row error, never a silent pass-through (TR-204).
- **Numbers** — `Decimal(str(value))`, at the precision received (TR-205, TR-206).

A row missing a required field, or with an unparseable date or number, returns a `RowError` carrying the reason and the original row. It does not raise.

### 6.3 Matching

`core.match.match(left, right, tolerances, prior_resolutions) -> MatchResult` — pure, deterministic, no IO (TR-301, TR-303).

| Tier | Action | Requirement |
|---|---|---|
| 0 | Apply stored resolutions: forced pairs, settled acceptances, rejected-suggestion blocklist | TR-304, TR-310 |
| 1 | Partition out cancelled records; flag status disagreement where one side is cancelled and the other live | TR-305, TR-306 |
| 2 | Reference match on exact equality | TR-307 |
| 3 | Bucket the remainder by `(instrument, side)`; qualify candidates on quantity and time window; rank and **propose** | TR-308, TR-309, TR-314 |
| 4 | Whatever remains is unmatched, reported on both sides | TR-312 |

**Auto-revoke** (TR-313): an acceptance whose reference has since gained a counterpart is revoked in the result and reported. It is the only automatic reversal in the system, and it is never silent.

The suggestion window (2 hours) is deliberately far wider than the comparison tolerance (5 minutes), because a suggestion is a question and a comparison is an assertion.

### 6.4 Comparison

`core.compare.compare(left, right, tolerances) -> Comparison`

One `FieldDiff(field, left_value, right_value, abs_diff, rel_diff, within_tolerance)` per compared field — including fields that agree, so the detail page can render the whole record without a second pass (TR-401). Every differing field is reported, not the first (TR-402).

Relative difference divides by the **larger** magnitude, which makes the result symmetric: swapping the two sides changes the sign and nothing else (TR-403, TR-410). Neither side is authoritative, so the arithmetic must not privilege one.

- Amount: `abs_diff <= max(amount_abs_floor, amount_bps * larger)` (TR-407)
- Price, quantity: basis-point rules
- Timestamp: seconds against `time_tolerance_seconds`
- Instrument, side, status: exact equality after vocabulary mapping (TR-408)

A difference exactly equal to the threshold is inside it (TR-406) — stated because "≤ or <" is precisely the kind of ambiguity that produces a defect nobody notices for a year.

### 6.5 Resolution durability

**Resolutions key on `(source_id, reference)`, never on `record.id`** (TR-508). This is the single most consequential schema decision.

Records are immutable, so a correction writes *new* record rows. A resolution pointing at a record id would silently detach the moment a counterparty resent a file — precisely when the analyst most needs yesterday's work to still be there. Keying on business identity is what makes "a resolution is a statement about identity, not about a run" (`R7.4`) true in the storage layer rather than merely asserted in prose.

It follows that:
- A manual pair survives a correction and is re-compared against the new values, so it may legitimately become a break (TR-317).
- A manual pair is compared by the same code path as an automatic one (TR-316) — pairing asserts identity, not agreement.
- The key is safe only because references are never reused (`D19`).

### 6.6 Exact decimals

Reconciliation is arithmetic about money, so no value may pass through a float (TR-205, TR-404, TR-505).

SQLAlchemy's `Numeric` degrades to float on SQLite and warns about it. `ExactDecimal`, a `TypeDecorator`, stores every amount and quantity as **TEXT** normalised to a fixed scale — exact on both backends, and still correctly sortable. `UtcDateTime` rejects naive datetimes at the type boundary (TR-506).

`test_no_float.py` parses `core/` and `app/services/` and fails on any `float(` call or float literal in an arithmetic position. The guarantee is checked, not trusted.

---

## 7. Web surface

Server-rendered Jinja, no client framework, no build step (TR-601). Every mutation is POST-redirect-GET (TR-603).

| Route | Page | Spec | Requirement |
|---|---|---|---|
| `GET /` | Runs list, upload form, accepted files | §6.1 | TR-602 |
| `POST /files` | Upload. Duplicate, overlap, and malformed rejections render here as messages | §6.1 | TR-605 |
| `POST /runs` | Start a run, redirect to its summary | Flow 1 | TR-603 |
| `GET /runs/{id}` | Summary, counts by state | §6.2 | TR-509 |
| `GET /runs/{id}/worklist` | Filter by state, sort by size of difference | §6.3 | TR-606 |
| `GET /runs/{id}/pairs/{pair_id}` | Break detail, field by field | §6.4 | TR-607 |
| `GET /runs/{id}/records/{record_id}` | Unmatched detail, ranked candidates, both resolution actions | §6.5 | TR-608, TR-609 |
| `POST /resolutions` | Manual match, accept-no-pair, reject-suggestion. Reason and author mandatory | §5.7 | TR-604 |
| `GET /records/{source}/{reference}/history` | Value history across versions | §6.6 | TR-510 |

No expected failure produces a 5xx. A duplicate file is a message on the page, not a stack trace (TR-605).

---

## 8. Configuration

| Lives in | Holds | Validated |
|---|---|---|
| `source.format_config` (DB) | Column map, timestamp patterns, timezone, vocabulary | Pydantic model at load; a source with no declared timezone fails |
| `tolerance_profile` (DB) | The six thresholds, per source pair | Pydantic model at load |
| Environment | `DATABASE_URL`, log level | Pydantic settings at startup |

Everything is validated before the first request is served (TR-705). A tolerance profile that fails to parse fails the process, not the run — discovering it halfway through reconciling is worse than not starting.

---

## 9. Test strategy

Two suites. The split is the brief's requirement, not a preference.

### `tests/unit/` — no database, no app, no network (TR-707)

| Module | Covers | Requirements |
|---|---|---|
| `test_normalize.py` | Both date formats, `BUY`/`B`, naive-timestamp localisation, missing field, bad date, bad number | TR-201..TR-206, AC1 |
| `test_tolerance.py` | Boundary triples for every rule: just inside, exactly on, just outside. Floor beating relative on small amounts and the reverse on large | TR-406, TR-407, TR-411, AC4 |
| `test_compare.py` | Three-field disagreement reports all three; symmetry under swap; the three verdicts | TR-401..TR-403, TR-408..TR-410, AC5 |
| `test_match.py` | All five tiers; cancelled excluded and cancelled-one-side; both-direction unmatched; ranking; one-to-one; carry-forward first; auto-revoke | TR-301, TR-303..TR-313, TR-316, AC3, AC6, AC11 |
| `test_boundaries.py` | `core/` imports nothing outside stdlib; no `create_all`; no dialect branch; no `UPDATE` on `record` | TR-302, TR-501, TR-507, TR-704 |
| `test_no_float.py` | AST scan for float use on the money path | TR-205, TR-404 |

### `tests/integration/` — real SQLite

| Module | Covers | Requirements |
|---|---|---|
| `test_ingest.py` | Header validation, identical resend refused, overlapping period refused, bad rows isolated, rollback | TR-101..TR-104, TR-106, TR-108, AC7 |
| `test_corrections.py` | Versioning, supersession, withdrawal, fixed rows leaving the worklist | TR-105, TR-107, AC8 |
| `test_durability.py` | Manual match survives a re-run and a correction; acceptance stays accepted; confirmed suggestion persists | TR-315, TR-317, TR-508, AC9, AC10 |
| `test_reconcile.py` | State counts sum to records read; excluded listed; runs append-only; idempotent re-run | TR-502, TR-509, TR-511, TR-512, TR-702, AC2 |
| `test_history.py` | One-query history ordered by version | TR-510 |
| `test_types.py` | Decimal round-trip exactness; UTC-only enforcement | TR-505, TR-506 |
| `test_third_source.py` | A third format reconciles with a config row alone | TR-207, AC12 |
| `test_web.py` | Every route 200s; POST-redirect-GET; reason required; expected failures render; break detail content | TR-602..TR-609 |
| `test_perf.py` | 10k per side under 10s; no cross-product in candidate search | TR-314, TR-701 |
| `test_config.py` | Invalid config fails at startup | TR-203, TR-405, TR-705 |
| `test_logging.py` | Structured ingest and run events carry the audit counts | TR-706 |

All twelve acceptance criteria in `SPEC.md` §9 are covered above. `core/` coverage gate is 90% (TR-709).

---

## 10. Sample data

`data/`, one week, 2025-07-01 to 2025-07-07, roughly 40 rows a side — enough to make every case demonstrable in the required video.

| File | Purpose |
|---|---|
| `ledger_2025-07-01_07.csv` | Our side |
| `statement_2025-07-01_07.csv` | Their side: different columns, dates, vocabulary |
| `statement_2025-07-01_07_resend.csv` | Byte-identical to the above — duplicate demo |
| `statement_2025-07-01_07_v2.csv` | Correction: three amounts fixed, one row withdrawn |
| `venue_c_2025-07-01_07.csv` | Third format: different columns again, `d`/`c` for side, epoch-second timestamps |

Cases seeded: exact agreement; drift inside tolerance; amount break; price and amount breaking together; a 40-minute time break; side disagreement; cancelled on both sides; cancelled on one side only; ledger-only row; statement-only row; a near-match with mismatched references that surfaces as a suggestion; two malformed rows.

The worked example from the brief is included verbatim, so a reviewer can check our thresholds against their own intuition. Under the §5.5 values it produces one agreement, one amount break at 0.50%, one 40-minute time break, one statement-only row, and one excluded cancellation.

---

## 11. Performance and complexity

Tier 3 is the only part with non-trivial cost. Bucketing by `(instrument, side)` reduces candidate search from a full cross-product to a scan within each small bucket (TR-314), which for realistic files means the run is dominated by IO rather than matching.

The two hot pages are single queries by construction: the summary is a `GROUP BY state` over `run_item`, and the worklist is an indexed filter on `(run_id, state)` with an order-by on the stored difference magnitude. Neither reads `pair` or `field_diff` to render its list.

Target: 10,000 records per side in under 10 seconds against SQLite (TR-701), asserted by test rather than assumed.

---

## 12. Build order

Commit sequence, with estimates assuming no interruptions.

| # | Step | Est. |
|---|---|---|
| 1 | Scaffold: `uv` project, config, session, Alembic init | 30 min |
| 2 | `core/` model, format, normalize + unit tests | 50 min |
| 3 | `core/` tolerance, compare + boundary tests | 40 min |
| 4 | `core/` match + unit tests | 60 min |
| 5 | DB models, custom types, first migration | 40 min |
| 6 | Ingest service: hash, dedupe, versioning, rejected rows | 50 min |
| 7 | Reconcile service: run, persist pairs, diffs, items | 50 min |
| 8 | Web: routes and templates | 70 min |
| 9 | Resolutions: create, revoke, carry-forward + durability tests | 50 min |
| 10 | Sample data, third source, README | 50 min |

Roughly **8 hours**, above the brief's 5–6 hour guide. Steps 1–8 stand alone as a working system. **Step 9 is the one that must not be cut** — durable resolutions are the requirement most likely to be under-built and the most likely to be probed in review.

---

## 13. Running it

```
uv sync
uv run alembic upgrade head
uv run python -m app.seed
uv run uvicorn app.main:app --reload
```

Four commands, no external service (TR-703). Postgres is `DATABASE_URL` and nothing else (TR-704).

Tests:

```
uv run pytest tests/unit
uv run pytest
```

---

## 14. Design decisions

The technical counterpart to `SPEC.md` §8. Each is a call that could reasonably have gone the other way.

| # | Decision | Why | Alternative rejected |
|---|---|---|---|
| DD-1 | FastAPI + Jinja | Typed handlers and a free OpenAPI view of the same routes, with server-rendered pages the brief explicitly permits | Django: more free machinery, but its ORM and admin would obscure the schema design that is itself a deliverable |
| DD-2 | SQLite default, Postgres by URL | Reviewers clone and run with nothing to install | Postgres-only: one more setup step between a reviewer and a working app |
| DD-3 | Pure `core/` with an enforced import boundary | The brief names this requirement explicitly; a convention would decay | Trusting discipline, or a mocked session in unit tests |
| DD-4 | `ExactDecimal` storing money as TEXT | `Numeric` degrades to float on SQLite. In an app whose entire purpose is arithmetic about money, that is disqualifying | Integer minor units: correct, but quantities like `0.00000001 BTC` need a scale that varies by instrument |
| DD-5 | Resolutions key on `(source_id, reference)` | Corrections write new record rows, so a row-id key detaches exactly when it matters most | Foreign key to `record.id`: simpler, and silently wrong after the first correction |
| DD-6 | Records immutable; corrections write new batches | History is then a consequence of the schema rather than a feature bolted onto it | Update in place with an audit table: two sources of truth to keep in step |
| DD-7 | One-to-one enforced by unique index | Application logic can be bypassed by a future endpoint; an index cannot | Validation in the service layer only |
| DD-8 | Withdrawal derived, not stored | Storing it would require mutating a record, which DD-6 forbids | A `withdrawn_at` column on `record` |
| DD-9 | Bucketed candidate search by `(instrument, side)` | Keeps tier 3 near-linear without an index structure or a similarity library | Full cross-product: fine at 40 rows, quadratic at 40,000 |
| DD-10 | `run_item` as the single per-record state table | Makes "every record is in exactly one state" a schema property, and the summary one query | Deriving state by joining `pair` and `record` at render time |
| DD-11 | Sync SQLAlchemy | Nothing here is IO-bound enough to justify async colouring every function | Async: matches the reference stack, but buys nothing for a batch tool |
| DD-12 | Alembic from the first commit | The schema is a deliverable and a reviewer will read its history | `create_all`: faster to start, discards the evolution the brief says it reviews |
