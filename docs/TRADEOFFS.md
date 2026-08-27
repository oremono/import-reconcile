# Trade-offs

[SPEC.md](SPEC.md) §8 records **what** was decided and why, in business terms. [DESIGN.md](DESIGN.md) §14 does the same for technical calls and names the alternative rejected.

This document records what those decisions **cost**. Every one of them buys something and pays for it somewhere, and the price is usually invisible until the thing it constrains is the thing you need. Nothing here is a justification — the justifications are in the other two documents. This is the bill.

---

## 1. What each decision costs

Reversibility is the column to read first. A cheap decision can be revisited when it starts to hurt; an expensive one has to be right now.

| Decision | Buys | Costs | Reversibility |
|---|---|---|---|
| **DD-2** SQLite by default | Clone and run, nothing to install | One writer at a time. Two analysts uploading while a run executes will collide. No partial indexes, no JSONB, no `NUMERIC` worth using | **Cheap** — `DATABASE_URL` |
| **DD-4** Money stored as TEXT | Exactness on both backends, no float anywhere | No database-side `SUM` or `AVG` on money without a cast. Sorting is correct only because values are normalised to a fixed scale on the way in — get that wrong and ordering silently breaks | **Moderate** — migration plus backfill |
| **DD-5** Resolutions keyed on `(source, reference)` | Survives corrections, which is the whole point | No foreign key, so a resolution can reference a row that never arrives and nothing stops it. Correctness rests entirely on `D19` — references never reused. If a counterparty ever restarts numbering, resolutions silently attach to the wrong trades | **Expensive** — effectively one-way. Changing the key means rewriting every resolution row and all carry-forward logic |
| **DD-6** Records immutable | History is a property of the schema, not a feature | Storage grows with every correction. "Current" becomes derived, so every query must filter on `superseded_by_id IS NULL` — and the first one that forgets produces double-counted results that look plausible | **Expensive** |
| **DD-3** Pure `core/` | The brief's testability requirement, met structurally | Services must marshal rows into dataclasses and back. Two shapes for the same concept — `NormalizedRecord` and the `record` model — that have to be kept in step by hand | **Cheap** |
| **DD-9** Bucketed candidate search | Near-linear tier 3 with no index structure or similarity library | A genuine counterpart booked under a mistyped instrument or a flipped side is **never suggested**, because it never enters the bucket. The analyst must find it by hand | **Cheap** |
| **DD-10** `run_item` per record per run | One-query summary; "every record in exactly one state" is a schema property | Rows grow as records × runs. 10,000 a side, daily, is roughly 7 million rows a year with no retention policy written | **Moderate** |
| **DD-11** Sync SQLAlchemy, run inside the request | No async colouring, no broker, no worker | A large run blocks a request for seconds with no progress indication, and a timeout mid-run leaves the analyst guessing | **Moderate** |
| **D4** Suggestions never auto-applied | No fabricated breaks, no hidden wrong pairings | More clicking. A file where references genuinely do not line up produces a long worklist of confirmations | **Cheap** |
| **D7** A file is a full restatement of its period | Withdrawal is unambiguous | A counterparty that sends true deltas cannot be supported at all without a second ingestion mode | **Moderate** |
| **D11** One-to-one matching only | A simple pair model, and a unique index that makes violation impossible | Netting, splits, and partial fills — common in real settlement — cannot be represented. The index enforcing correctness today is the thing blocking that feature tomorrow | **Expensive** |
| **D13** Tolerances per source pair | A third counterparty settles differently and that is configuration | Every comparison needs a profile loaded, so there is no such thing as comparing two records without knowing which pair they came from | **Cheap** |

---

## 2. Approaches considered and rejected

Not per-decision alternatives — those are in `DESIGN.md` §14. These are whole-approach forks.

### Matching in SQL

A single query with joins and tolerance predicates would be fast and short. Rejected because the brief requires the comparison logic to be testable **without a database**, and logic expressed as SQL cannot be. This is the clearest case in the project of a requirement directly overriding the more efficient implementation, and it is worth being explicit that the cost is real: matching in Python means loading both sides into memory.

### Probabilistic matching

Record-linkage scoring — edit distance on references, weighted field agreement, a confidence threshold — has better recall on genuinely messy data. Rejected for two reasons. The references in this data are clean, so the recall gain is small. More importantly, a confidence score is not auditable by the person who has to defend the number at period close: "0.87" is not a reason. The bucketed candidate search is a deterministic subset of the same idea, and every suggestion it makes can be explained in one sentence.

### A matching rules engine

A small DSL letting an analyst define new matching rules without a deploy. Genuinely useful at scale, and genuinely out of scope: the variation the brief actually names is *format* variation, which per-source configuration already handles, not *rule* variation. Building a DSL nobody asked for would consume the entire time budget.

### Event sourcing

Model every file, correction, and resolution as an event and project current state. Perfect audit, natural time travel. Rejected as disproportionate — immutable records plus append-only runs answer every question the spec actually poses ("what did this row say before?", "what did we decide and why?") at a small fraction of the complexity.

### Background job queue

Celery or similar, with runs executed off-request. Necessary the moment runs take minutes. Today a run is seconds, and a broker would break the clone-and-run property (TR-703) that makes the repo reviewable. Deferred deliberately, not overlooked — see §4.

### Diffs as JSON on the pair row

Fewer tables, simpler writes. Rejected because the worklist must sort by size of difference (TR-606), and sorting on a value buried in a JSON blob means either a generated column or sorting in Python across the whole result set. A `field_diff` table keeps that a plain indexed `ORDER BY`.

---

## 3. Where this design breaks first

Ordered by how soon it bites.

1. **Two people at once.** SQLite's single writer plus no optimistic concurrency on resolutions. Two analysts resolving simultaneously is undefined behaviour today.
2. **A long worklist.** No pagination. A bad reconciliation day renders every item on one page.
3. **A run that takes minutes.** It happens in the request (DD-11). The first genuinely large file makes this visible.
4. **A source that sends deltas.** D7 assumes full restatements. A counterparty sending only changed rows would be silently mis-ingested as a restatement that withdraws everything it omits — the worst failure mode in the system, because it looks like data rather than an error.
5. **Netting or partial fills.** D11's unique index refuses them outright. At least this fails loudly.
6. **Reference reuse.** D19 is an assumption about someone else's system, and nothing detects its violation.
7. **`run_item` growth.** No retention policy. Not urgent, but nothing bounds it either.

Items 4 and 6 share a shape worth naming: both are assumptions about a **counterparty's** behaviour, and neither is validated at ingest. If anything here is under-built, it is that.

---

## 4. What I would fix first

Ranked by value per hour, not by interest.

1. **Pagination on the worklist.** An hour. Without it the main screen degrades exactly when it matters most.
2. **A guard against reference reuse.** An hour. Detect a reference arriving in a period after one it already appeared in, and refuse the file. Turns the most dangerous silent assumption in the system into a loud error.
3. **Optimistic concurrency on resolutions.** An hour. A version column and a conflict message beats a last-write-wins race over a financial decision.
4. **Move the run off the request.** Half a day. Needed before any file large enough to matter.
5. **Postgres as the default.** Two hours including a compose file. SQLite is right for review and wrong for use.

---

## 5. The gap to production

Not a to-do list — an honest statement of what "this works" does not yet mean.

| Missing | Consequence |
|---|---|
| Authentication (TR-801) | The resolution author is a typed string. Nothing prevents anyone claiming to be anyone |
| Backup and restore | The database is the only record of every manual decision ever made |
| Monitoring | Nobody learns the morning run failed except the analyst who opened the page |
| Alerting on break-rate change | A counterparty system quietly changing its fee treatment shows up as a slowly rising break count that nobody is watching |
| Retention policy | `run_item` and superseded records grow without bound |
| Structured audit export | Resolutions are queryable but not exportable, and an auditor will ask |

The reconciliation logic is the part that is finished. The operational surface around it is the part that is not, and the distinction matters more than a feature list.
