# Technical Requirements

The middle layer between [SPEC.md](SPEC.md) (what the system does, in business terms) and [DESIGN.md](DESIGN.md) (how it is built).

Every item is a single checkable statement. **Traces to** names the `SPEC.md` requirement (`R*.*`), decision (`D*`), section (`§*`), or acceptance criterion (`AC*`) it derives from, or `NFR` where it is technical-only. **Verified by** names the test or check that proves it — this column is the contract the test suite implements.

Numbering is grouped in hundreds so items can be inserted later without renumbering. IDs are permanent; a withdrawn requirement is struck through, never reused.

---

## TR-1xx — Ingestion and file handling

| ID | Requirement | Traces to | Verified by |
|---|---|---|---|
| TR-101 | A file is accepted only as UTF-8 CSV with a header row. The header is validated against the source's column map before any data row is read. | R1.1 | `test_ingest.py::test_header_validation` |
| TR-102 | Duplicate detection uses a SHA-256 digest of the file's raw bytes. Filename, upload time, and row order play no part in it. | R1.2, D6 | `test_ingest.py::test_identical_resend_refused` |
| TR-103 | A unique constraint on `(source_id, content_hash)` makes accepting a duplicate impossible at the storage layer, not only in application code. | R1.2 | migration review; `test_ingest.py` |
| TR-104 | A file whose period partly overlaps an existing period for the same source is refused before any row is persisted. Periods match exactly or do not overlap. | R1.6, D21 | `test_ingest.py::test_partial_overlap_refused` |
| TR-105 | A file for an existing `(source, period)` with a different digest is stored as version `n+1`, and the previous version is marked superseded in the same transaction. | R1.3, D7 | `test_corrections.py::test_versioning` |
| TR-106 | A row that fails normalisation is persisted with its reason, its original text, and its row number. Valid rows in the same file still load. | R1.5, D15 | `test_ingest.py::test_bad_rows_isolated` |
| TR-107 | A reference present in a superseded version and absent from the current version is derivable as withdrawn without a stored flag. | R1.4, D8 | `test_corrections.py::test_withdrawn` |
| TR-108 | Ingestion is atomic. A file is either fully accepted or leaves no trace, including its batch row. | NFR | `test_ingest.py::test_rollback_on_failure` |

---

## TR-2xx — Normalisation

| ID | Requirement | Traces to | Verified by |
|---|---|---|---|
| TR-201 | Field-to-column mapping is read from the source's stored configuration. No source name and no source-specific branch appears in normalisation code. | R2.1, R2.4 | `test_normalize.py::test_column_mapping` |
| TR-202 | Each source declares an ordered list of timestamp patterns. The first that parses wins; exhausting the list is a row error. | R2.2 | `test_normalize.py::test_date_formats` |
| TR-203 | A parsed timestamp carrying no offset is localised with the source's declared timezone, then converted to UTC. A source declaring no timezone fails configuration validation at startup. | R2.2, D17 | `test_normalize.py::test_naive_localised`; `test_config.py` |
| TR-204 | Coded values are mapped through the source's vocabulary table. An unmapped token is a row error, never a silent pass-through. | R2.3 | `test_normalize.py::test_vocabulary` |
| TR-205 | Numeric fields are parsed with `Decimal(str)`. `float()` appears nowhere on the ingestion, matching, or comparison path. | R2.5 | `test_no_float.py` (AST scan) |
| TR-206 | Values are stored at the precision received. No rounding or truncation occurs at load time. | R2.5 | `test_normalize.py::test_precision_preserved` |
| TR-207 | Reconciling a third source requires one `source` row and its configuration. It requires no change to `core/`, `app/services/`, or the migrations. | R2.4, AC12 | `test_third_source.py` |

---

## TR-3xx — Matching

| ID | Requirement | Traces to | Verified by |
|---|---|---|---|
| TR-301 | `core.match.match` is a pure function. Identical inputs produce identical outputs, and it performs no IO of any kind. | brief, R4.* | `test_match.py`; `test_boundaries.py` |
| TR-302 | `core/` imports only the standard library. Any `sqlalchemy`, `fastapi`, or `app` import fails the test suite. | brief | `test_boundaries.py::test_core_imports` |
| TR-303 | Output ordering is deterministic. Equally-ranked candidates break ties on reference, then on source row number. | NFR | `test_match.py::test_deterministic` |
| TR-304 | Stored resolutions are applied before any automatic matching runs. | R4.1, R7.4 | `test_match.py::test_carry_forward_first` |
| TR-305 | Cancelled records are partitioned out before matching and appear in no matched, suggested, or unmatched result. | R3.1, R3.2, AC3 | `test_match.py::test_cancelled_excluded` |
| TR-306 | A reference cancelled on one side and live on the other yields a status disagreement on the live record, not a plain unmatched. | R3.4, D14 | `test_match.py::test_cancelled_one_side` |
| TR-307 | Reference matching pairs on exact string equality only. No normalisation, trimming, or case folding beyond what ingestion already applied. | R4.2 | `test_match.py::test_reference_match` |
| TR-308 | A candidate requires identical instrument and side, quantity within quantity tolerance, and a timestamp gap within the suggestion window. Candidates are drawn only from records in the current run. | R4.3, D5, D22 | `test_match.py::test_suggestions` |
| TR-309 | A suggestion never becomes a pair without an explicit stored resolution. | R4.4, D4 | `test_match.py::test_no_auto_apply`; `test_web.py` |
| TR-310 | A pair a person has rejected is never suggested again. | R4.6 | `test_match.py::test_rejected_not_resuggested` |
| TR-311 | Each record appears in at most one pair. This holds in the match result and is separately enforced by database constraint. | R4.8, D11 | `test_match.py::test_one_to_one`; migration |
| TR-312 | Unmatched records are reported for both sources in the same result object. | R4.7, AC6 | `test_match.py::test_both_directions` |
| TR-313 | An `accept_unmatched` resolution whose reference gains a counterpart is revoked in the result and reported as a revocation. | R7.7, D10, AC11 | `test_match.py::test_auto_revoke` |
| TR-314 | Candidate search buckets by `(instrument, side)`. It never compares every left record against every right record. | NFR | `test_perf.py::test_no_cross_product` |
| TR-315 | Confirming a suggestion is stored as a manual match and carries forward identically to one created from scratch. | R4.5 | `test_durability.py::test_confirmed_suggestion_persists` |
| TR-316 | A manually created pair is compared by the same code path as an automatic one, and is therefore reportable as a break. Neither side is treated as authoritative. | R7.5, D12 | `test_match.py::test_manual_pair_compared` |
| TR-317 | A resolution survives a correction that changes the values beneath it. The pair stands and is re-compared against the new values. | R7.6, D9 | `test_durability.py::test_survives_correction` |

---

## TR-4xx — Comparison and tolerance

| ID | Requirement | Traces to | Verified by |
|---|---|---|---|
| TR-401 | Comparison emits one `FieldDiff` per compared field, including fields that agree, so the detail page can render the whole record. | R5.3, R5.4 | `test_compare.py::test_all_fields_emitted` |
| TR-402 | Every differing field is reported. Comparison does not stop at the first difference. | R5.3, AC5 | `test_compare.py::test_all_diffs` |
| TR-403 | Relative difference is computed against the larger of the two magnitudes. Swapping the two sides changes only the sign, never the magnitude. | R5.4 | `test_compare.py::test_symmetry` |
| TR-404 | All comparison arithmetic is `Decimal`. Basis-point thresholds are `Decimal` literals, not float literals. | R2.5 | `test_no_float.py` |
| TR-405 | Tolerances are resolved from the tolerance profile for the source pair. No threshold is a literal in comparison code. | R5.5, D13 | `test_compare.py::test_tolerances_from_profile` |
| TR-406 | A difference exactly equal to the tolerance is within tolerance. The first representable unit beyond it is a break. | R5.1, R5.2, AC4 | `test_tolerance.py` boundary triples |
| TR-407 | The amount rule allows the greater of the absolute floor and the relative allowance. | D1 | `test_tolerance.py::test_floor_vs_relative` |
| TR-408 | Instrument, side, and status compare by exact equality after vocabulary mapping. No tolerance applies to them. | R5.5 | `test_compare.py::test_exact_fields` |
| TR-409 | Verdict is `break` when any field is out of tolerance, `agreed_with_drift` when all are within tolerance and at least one is non-zero, and `agreed` otherwise. | R5.1, R5.2, R5.6 | `test_compare.py::test_verdicts` |
| TR-410 | Comparison designates no authoritative side. The output carries both values and a signed difference, never a winner. | D12 | `test_compare.py::test_no_authoritative_side` |
| TR-411 | The tolerance profile carries independent thresholds for amount, price, quantity, and time, each separately configurable. | D1, D2, D3, D13 | `test_tolerance.py::test_profile_fields` |

---

## TR-5xx — Persistence and data integrity

| ID | Requirement | Traces to | Verified by |
|---|---|---|---|
| TR-501 | Record rows are never updated or deleted. A correction writes new rows under a new batch. | R8.1, R8.2 | `test_corrections.py`; `test_boundaries.py::test_no_record_update` |
| TR-502 | Run rows are append-only. A re-run creates a new run rather than overwriting one. | R8.1 | `test_reconcile.py::test_runs_append_only` |
| TR-503 | Unique constraint on `(source_id, content_hash)`. | R1.2 | migration |
| TR-504 | Unique constraints on `(run_id, left_record_id)` and `(run_id, right_record_id)`. | R4.8 | migration; `test_types.py::test_pair_uniqueness` |
| TR-505 | Money and quantity columns store exact decimals on both SQLite and Postgres. No value round-trips through a float on either backend. | R2.5 | `test_types.py::test_decimal_roundtrip` |
| TR-506 | Every stored timestamp is timezone-aware UTC. Naive datetimes are rejected at the type boundary. | D17 | `test_types.py::test_utc_only` |
| TR-507 | Schema is produced by Alembic migrations. `create_all` appears only in test fixtures. | NFR | `test_boundaries.py::test_no_create_all_outside_tests` |
| TR-508 | Resolutions reference records by `(source_id, reference)`, never by record id, so a correction cannot detach them. | R7.4, D9, D19, AC9 | schema; `test_durability.py::test_survives_correction` |
| TR-509 | Every record read in a run appears in exactly one `run_item` row carrying exactly one state. State counts sum to records read. | R5.6, AC2 | `test_reconcile.py::test_states_sum` |
| TR-510 | Value history for a `(source, reference)` is retrievable in one query, ordered by version. | R8.2, R8.3, AC8 | `test_history.py` |
| TR-511 | Excluded records are counted in the run summary and listable, so a cancelled trade has a traceable outcome. | R3.3 | `test_reconcile.py::test_excluded_listed` |
| TR-512 | Run summaries are comparable across runs by state counts without re-reading either run's items. | R8.4 | `test_reconcile.py::test_summary_comparison` |

---

## TR-6xx — Web surface

| ID | Requirement | Traces to | Verified by |
|---|---|---|---|
| TR-601 | Pages are server-rendered Jinja templates. No client-side framework and no asset build step. | brief, §6 | template review |
| TR-602 | Every route in DESIGN §7 exists and answers 200 for valid input. | §6 | `test_web.py::test_all_routes` |
| TR-603 | Every mutation is a POST answering with a redirect to a GET. A refresh never re-submits. | NFR | `test_web.py::test_post_redirect_get` |
| TR-604 | A resolution is rejected without a non-empty reason and a non-empty author. | R7.3 | `test_web.py::test_reason_required` |
| TR-605 | Duplicate file, overlapping period, and malformed upload render as a message on the page. No expected failure produces a 5xx. | R1.2, R1.5, R1.6, AC7 | `test_web.py::test_expected_failures` |
| TR-606 | The worklist filters by state and sorts by size of difference. | §6.3 | `test_web.py::test_worklist_filters` |
| TR-607 | The break detail page shows both values, the absolute difference, and the relative difference for every differing field. | R5.4, §6.4, AC5 | `test_web.py::test_break_detail` |
| TR-608 | The unmatched detail page lists ranked candidates with the reason each qualifies. | §6.5 | `test_web.py::test_candidates_listed` |
| TR-609 | The unmatched detail page offers manual pairing and accept-no-pair, each requiring a reason before it will submit. | R7.1, R7.2 | `test_web.py::test_resolution_actions` |

---

## TR-7xx — Non-functional

| ID | Requirement | Traces to | Verified by |
|---|---|---|---|
| TR-701 | A run over 10,000 records per side completes in under 10 seconds on a developer laptop against SQLite. | NFR | `test_perf.py::test_run_duration` |
| TR-702 | Re-running over unchanged inputs and unchanged resolutions produces identical states, pairs, and counts. | NFR, AC10 | `test_reconcile.py::test_idempotent` |
| TR-703 | Clone to running application in at most four commands, with no external service to start. | NFR | fresh-clone check; README |
| TR-704 | The database is selected by `DATABASE_URL` alone. No code path branches on backend. | NFR | `test_boundaries.py::test_no_dialect_branching` |
| TR-705 | Configuration is validated at startup. Invalid source config or tolerance profile fails before the first request is served. | NFR | `test_config.py::test_fails_fast` |
| TR-706 | Ingest and run emit structured log events carrying source, period, version, and result counts. | NFR | `test_logging.py` |
| TR-707 | `tests/unit` runs with no database, no network, and no import of `app`. | brief | `pytest tests/unit` in a clean environment |
| TR-708 | Nothing is hard-deleted. Revocation and supersession are recorded, never removed. | R7.8, R8.1 | schema review |
| TR-709 | The unit suite covers `core/` at 90% of lines or above. | NFR | `pytest --cov=core --cov-fail-under=90` |

---

## TR-8xx — Explicit non-requirements

Stated as requirements so their absence reads as a decision rather than an oversight. All are verified by code review and by their absence from the route table and dependency list.

| ID | Requirement | Traces to |
|---|---|---|
| TR-801 | No authentication or authorisation. The resolution author is a typed field, not an identity. | §4, D18 |
| TR-802 | No scheduler. A run starts from a request and nothing else. | §4 |
| TR-803 | No background workers or queues. A run completes within its request. | §4 |
| TR-804 | No notifications of any kind. | §4 |
| TR-805 | Single process, single node. No horizontal scaling, no distributed locking. | §4 |
| TR-806 | Single settlement currency. No FX rates, no valuation date. | §4, D16 |
| TR-807 | No bulk resolution. Every resolution is one decision about one item. | §4 |
| TR-808 | No client-side state and no JavaScript beyond progressive enhancement. | §4 |
| TR-809 | No escalated or awaiting-counterparty state. A break raised with the counterparty stays in the worklist. | §10, D20 |

---

## Coverage

| Group | Items |
|---|---|
| TR-1xx Ingestion | 8 |
| TR-2xx Normalisation | 7 |
| TR-3xx Matching | 17 |
| TR-4xx Comparison | 11 |
| TR-5xx Persistence | 12 |
| TR-6xx Web | 9 |
| TR-7xx Non-functional | 9 |
| TR-8xx Non-requirements | 9 |
| **Total** | **82** |

All twelve acceptance criteria in `SPEC.md` §9 are reachable: AC1 (TR-201, TR-202, TR-204), AC2 (TR-509), AC3 (TR-305), AC4 (TR-406), AC5 (TR-402, TR-607), AC6 (TR-312), AC7 (TR-102, TR-605), AC8 (TR-105, TR-510), AC9 (TR-508), AC10 (TR-702), AC11 (TR-313), AC12 (TR-207).
