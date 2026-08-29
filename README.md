# Trade Reconciliation

Replaces the two spreadsheets a reconciliation analyst opens every morning to work out why our
trade ledger and a counterparty's statement disagree. Answers what a spreadsheet cannot:
**which of today's differences actually matter?** The hardest part of that question — _how small
is too small to care about?_ — is the one it was built around.

|                |                                                                                                                     |
| -------------- | ------------------------------------------------------------------------------------------------------------------- |
| **Live app**   | `[RENDER URL]` — free tier, so the first request after a quiet spell takes ~50s to wake                              |
| **Video demo** | <https://drive.google.com/file/d/1cGFp6rpikQKP71QXVs7tM-hXUmDOzxBr/view>                                            |
| **Run locally**| `uv sync && make run` — two commands from clone to working app, nothing to install                                   |
| **Scale**      | 3 sources · 3 file formats · 6 sample files · one week of trades, 40 rows a side                                    |
| **Tests**      | 175 unit tests in 1.5 seconds with no database, plus 150 integration and 7 browser tests                            |
| **Verified**   | 82 technical requirements, each named to a test. `make verify` reports **67/67** automated and **12/12** acceptance |
| **History**    | 21 commits, each one a working state                                                                                |
| **Built with** | Python 3.12 · FastAPI + Jinja · SQLAlchemy 2.0 · Alembic · SQLite or Postgres · pytest · Playwright · ruff · mypy   |

No JavaScript framework, no build step, no Redis, no Celery, no Docker requirement.

---

## Where to find what

| Looking for                                            | Where                                                     |
| ------------------------------------------------------ | --------------------------------------------------------- |
| The problem being solved                               | [§1](#1-the-problem)                                      |
| What the software does                                 | [§2](#2-what-it-does)                                     |
| What I left out, and why                               | [§3](#3-what-i-deliberately-left-out-and-why)             |
| The decisions that shaped it, and what each one cost   | [§4](#4-the-decisions-that-shaped-it)                     |
| Code structure and the boundary that is enforced       | [§5](#5-how-the-code-is-organized)                        |
| Tests, and how I know they mean something              | [§6](#6-how-i-know-it-works)                              |
| How to read the commits                                | [§7](#7-how-the-work-was-done)                            |
| What is missing, and what I would do next              | [§8](#8-known-gaps-and-what-comes-next)                   |
| Every planning and design document                     | [§9](#9-artifact-index)                                   |
| How it is deployed                                     | [§10](#10-deployment)                                     |
| Running it, the sample data, the demo path             | [§11](#11-running-it)                                     |

---

## 1. The problem

Two companies trade with each other. Both write down what happened. Every morning one sends the
other its version, and the two lists never agree.

They disagree for three completely different reasons, and separating them is the whole job:

- **Vocabulary.** We say `trade_id`, they say `reference`. We write `BUY`, they write `B`. Nobody
  is wrong, and nobody should ever have to look at it.
- **Drift.** Amounts differ by cents because of rounding and fees. Timestamps differ by a minute
  because two clocks are never the same. Normal, on almost every row.
- **Real disagreement.** An amount materially different. A time hours out. A trade one side has
  and the other doesn't.

Done by hand, the third category is buried under the first two. So the analyst reads every row to
find the four that matter, runs out of morning, and starts trusting the list less each week.

That is the real failure — not the time it takes, but that a list nobody trusts stops being read.

---

## 2. What it does

Every capability is specified in [SPEC.md](docs/SPEC.md), which numbers each requirement and
records the reasoning behind each judgement call.

### Get the data in

| The analyst can                | How                                                                                                            |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------- |
| Load a day's file              | Upload a CSV. Valid rows import. Bad rows are rejected one at a time, each with its reason, and never block the good ones. |
| Send the same file twice       | Refused, naming when the original arrived. The check is on the file's contents, never its name.                |
| Send a correction              | Accepted as a new version. The fixed values become current; the old ones stay readable forever.                |
| Add a third counterparty       | One configuration row. No code changes, and a test proves no module names a source.                            |

### Find what does not match

| The analyst asks                            | They get                                                                                                              |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| _How bad is this morning?_                  | One number: how many items need a decision. Everything else on the page is arranged around it.                        |
| _What needs me first?_                      | A worklist sorted by size of difference, largest first, filterable by kind of problem.                                |
| _Why is this pair a problem?_               | Every field, both values, the absolute and relative difference. Fields that agree stay visible but quiet.              |
| _How small is too small to care about?_     | A tolerance she controls per counterparty. Five basis points on amounts, five minutes on time. The boundary is exact: a difference equal to the threshold is fine, one unit past it is a break. |
| _Did anything vanish?_                      | Rows present in an earlier version and absent from the current one are reported as withdrawn, not silently dropped.    |
| _Is this trade cancelled on only one side?_ | Its own state. Cancelled trades are never compared — but a trade cancelled by one party and live on the other is a serious break, and would be invisible if it were merely excluded. |

### Resolve it, once

| The analyst can                     | How                                                                                                                |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Pair two rows the system could not  | Confirm a ranked candidate, or type a reference. A reason and a name are required, in the form and in the service beneath it. |
| Accept that a row has no partner    | Same, with a reason.                                                                                               |
| Trust that it sticks                | Every future run applies the decision before it matches anything. Nobody is asked the same question twice — not tomorrow, and not after a correction rewrites the row underneath. |
| Read a trade's whole history        | Every version, with the file and date each came from, plus every decision ever recorded against it.                 |

**The system never applies a decision on its own.** It proposes candidates and explains why each
qualifies, in a sentence. The one exception is documented in [§4](#4-the-decisions-that-shaped-it),
and it always reports itself.

---

## 3. What I deliberately left out, and why

The specification and its 24 numbered decisions were written before any code:
[**SPEC.md**](docs/SPEC.md). [REQUIREMENTS.md](docs/REQUIREMENTS.md) is the technical restatement
everything downstream was built from — 82 requirements, each naming its test.

**In scope:** everything in [§2](#2-what-it-does), plus the generator that produces the sample data.

**Out, and why:**

| Excluded                            | Reason                                                                                                                                                                                                                  |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Authentication**                  | Deferred, not dismissed. The brief names one user. The resolution author is a typed field, because "who decided this and why" is the question asked six weeks later — but nothing yet proves it is true.                |
| **Many-to-one matching**            | Netting, splits and partial fills are real and common in settlement. They need their own model and their own screen. Handling them badly is worse than declining them clearly, and the unique index that makes one-to-one correct today is exactly what blocks them tomorrow. |
| **Multi-currency**                  | Needs a rate source and a valuation date, which is a separate problem. Stated as an assumption rather than left implicit.                                                                                                |
| **An escalation state**             | A break raised with the counterparty stays on the worklist with no way to mark it as chased. It is the first thing I would add — see [§8](#8-known-gaps-and-what-comes-next).                                            |
| **Scheduling and notifications**    | A run starts from a request. Automating it is easy; deciding who gets told what, and when, is not, and neither was specified.                                                                                            |
| **Bulk resolution**                 | Deliberate. Every resolution is one decision about one item. Bulk-accepting breaks is how reconciliation systems get quietly ignored.                                                                                    |
| **Accounting consequences**         | This reports disagreement. It does not post journal entries, raise claims, or amend either side's records.                                                                                                              |

---

## 4. The decisions that shaped it

Four shaped everything else. Each cost something, and naming the cost is the point — a decision
with no downside is usually an opinion.

Full reasoning in [DESIGN.md §14](docs/DESIGN.md) (fifteen technical decisions, each with the
alternative rejected) and [TRADEOFFS.md](docs/TRADEOFFS.md) (what they cost, and where this design
breaks first).

| Decision | Why | What it cost |
| -------- | --- | ------------ |
| **A manual decision points at the trade, never at the database row.** A resolution stores the source and the reference — never `record.id`. | Records are immutable, so a correction writes entirely new rows. A row-id key would silently detach the moment a counterparty resent a file, which is exactly the morning yesterday's decision must still hold. | No foreign key, so a resolution can name a trade that never arrives and nothing stops it. Correctness rests entirely on references never being reused — an assumption about someone else's system that nothing validates. |
| **Record rows are never updated or deleted.** A correction writes new rows under a new file version and marks the old one superseded. | History becomes a property of the schema rather than a feature bolted onto it. "What did this row say before?" is an ordinary query. | Storage grows with every correction, and "current" becomes derived — every query must filter on the un-superseded version, and the first one that forgets produces double-counted results that look plausible. |
| **No money value ever passes through a float.** `Decimal` end to end, with amounts stored as fixed-width text through a custom column type. | SQLAlchemy's `Numeric` degrades to float on SQLite. In an application whose entire purpose is detecting other people's rounding errors, that is disqualifying. A test parses the money path and fails on a `float()` call or a float literal. | No database-side `SUM` or `AVG` on money without a cast. Ordering is correct only because values are normalised to a fixed scale on the way in — get that wrong and sorting silently breaks. |
| **The comparison logic knows nothing about databases or browsers.** `core/` imports the standard library and nothing else. | The brief requires it, and a convention would decay within weeks. A test walks every module in `core/`, parses its imports, and fails the build on anything outside the standard library. | Services must marshal database rows into dataclasses and back, so the same concept has two shapes that have to be kept in step by hand. |

---

## 5. How the code is organized

Four layers. Dependencies point inward. Nothing points back out.

```
core/            pure logic — normalise, match, compare
                 standard library only; no database, no web, no clock
    ↑
app/services/    orchestration — load records, call core, persist results
    ↑
app/db/          models, custom column types, Alembic migrations
app/web/         ten routes and Jinja templates
```

The top boundary is not a convention. `tests/unit/test_boundaries.py` parses every module under
`core/` and fails the build on any infrastructure import, on a `create_all` outside a test fixture,
and on any code that branches on which database backend is running.

`core/` takes dataclasses and returns dataclasses. Its tests build records as Python lists — no
fixtures, no session, no client.

---

## 6. How I know it works

Four layers, each buying something different.

| Layer | Covers | Buys |
| ----- | ------ | ---- |
| **Unit** — `uv run pytest tests/unit` | Normalisation, tolerance, comparison, matching. 6 files, **175 tests, 1.5 seconds.** No database, no browser, no import of the application. | Fast enough to run on every save, so it gets run. This is the brief's explicit requirement, checked rather than claimed. |
| **Guard tests** — part of the unit suite | The architecture itself: the `core/` import boundary, no floats on the money path, no `UPDATE` on a record, no branching on database backend, migrations not `create_all`. | The invariants cannot decay into comments nobody reads. Written before the code they guard, so a violation fails immediately rather than at integration. |
| **Integration** — `uv run pytest` | 11 files, **150 tests**, against real SQLite — and the same 150 against real Postgres 17 by setting `TEST_DATABASE_URL`. Ingestion, corrections, durability, history, run states, the constraints themselves. | Several requirements are satisfied by a unique index rather than by code, and those are proven by provoking a violation. Running the identical suite on both backends is how "the database is chosen by URL alone" gets proven rather than asserted. |
| **Browser** — `make e2e` | **7 Playwright tests** driving the real application through a real file picker: the whole morning, plus every expected failure. | Passing tests and a usable screen are different claims. Opt-in, so `make verify` stays runnable without a browser installed. |

### The oracle

```
$ make verify

TR verified   67 / 67   (15 manual)
AC verified   12 / 12
```

`make verify` runs lint, types, both suites and coverage — then does something else. It parses the
*Verified by* column out of [REQUIREMENTS.md](docs/REQUIREMENTS.md), runs the suite, and reports how
many of the 82 requirements have a test that **actually ran and actually passed**. Then it boots the
application and requests a page, because a registered route is not a working route.

Done is not a judgement call. A requirement whose test does not exist reports as unverified, so one
cannot be quietly skipped. `core/` coverage is gated at 90% and currently sits at **99%**.

---

## 7. How the work was done

21 commits, each a working state, built in waves: contracts frozen first, then normalisation and
comparison, then matching and ingestion, then reconciliation, resolutions and the web surface.

The guard tests came before the code they guard. `test_boundaries.py` and `test_no_float.py` were
committed in the first wave, so the architecture could not drift while the rest was written.

Commit messages explain **why**, not what — the diff already shows what. Several record a defect and
the reasoning behind its fix:

```
Wave 0: scaffold and freeze the contracts
Wave 1: normalisation, comparison, and the storage boundary
Wave 2: matching, ingestion, and a third source
Wave 3: reconciliation, durable resolutions, and the web surface
Render dates and durations for a person, not for a machine
Never let a skipped setup step look like a broken server
Fix six defects found by walking the app as an analyst
Make a trade reachable after it stops needing attention
```

The last two are worth reading. Both are defects that no test was asking about, found only by using
the application — see [§8](#8-known-gaps-and-what-comes-next).

---

## 8. Known gaps and what comes next

Ranked. The first is what I would build next.

| Gap | Status |
| --- | ------ |
| **Nothing detects a reused reference.** | The most dangerous assumption in the system. Every manual decision is keyed on the trade's reference, so a counterparty that restarted its numbering would attach old decisions to new trades. One hour to detect and refuse. |
| **No pagination on the worklist.** | It degrades exactly when it matters most — a bad reconciliation day renders every item on one page. About an hour. |
| **No escalation state.** | A break already raised with the counterparty sits on the list with no way to mark it as chased, so the list slowly fills with items nobody can clear. Needs a state, an ageing rule and a filter. |
| **Last write wins on a resolution.** | Two analysts resolving at once is undefined behaviour. A version column and a conflict message is about an hour, and losing a financial decision to a race is not acceptable for long. |
| **A run happens inside the request.** | Ten thousand records a side takes 3 seconds against SQLite but **18 against Postgres**, because every row crosses a connection rather than a file handle. Eighteen seconds inside a web request is already poor, and there is no progress indication. This is the gap that a real deployment makes urgent. |
| **A file is assumed to be a complete restatement of its period.** | If a counterparty ever sent only changed rows, this would read it as a restatement and withdraw everything omitted. The worst failure mode here, because it looks like data rather than an error. Not validated at ingest. |
| **SQLite by default.** | Right for review, wrong for use — single writer, so two people uploading during a run will collide. Two hours including a compose file. |
| **No authentication, backups, monitoring or retention policy.** | One user, and a database regenerable from one command. Revisit when this holds anything not reproducible. `run_item` grows without bound. |

Two defects that shipped green and were found only by using the app are worth naming, because they
say something about the limits of the test suite above:

- **A run with no files loaded reported success** — a green zero and "every record read is accounted
  for". True, and useless: nothing had been read. On a morning when the counterparty's file has not
  arrived, that invites closing the period on an empty run.
- **A trade became unreachable once it agreed.** The worklist lists only what needs a person, and
  every route to a record's history ran through it — so the row a correction had just fixed was
  exactly the row that could not be looked up.

Both are fixed and now tested. Tests confirm what you thought to assert; they do not tell you the
screen is misleading.

---

## 9. Artifact index

In the order they were produced. Read top to bottom and the reasoning runs from problem to working
application.

**Requirements**

- [`docs/SPEC.md`](docs/SPEC.md) — what it does in business terms. Glossary, scope, 8 user flows,
  **24 numbered decisions** with reasoning, 12 acceptance criteria, and what is deliberately deferred.
- [`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md) — **82 technical requirements** in eight groups,
  each tracing to the spec and naming the test that proves it. `TR-8xx` records the non-requirements,
  so their absence reads as a decision.

**Design**

- [`docs/DESIGN.md`](docs/DESIGN.md) — how it is built. The six constraints that determined the
  shape, the ten tables and the constraints that enforce requirements, the key mechanisms, the test
  strategy, and **15 design decisions** each with the alternative rejected.
- [`docs/TRADEOFFS.md`](docs/TRADEOFFS.md) — what those decisions cost, with a reversibility column;
  whole approaches considered and rejected; where the design breaks first; and the gap to production.

**Implementation**

- [`CLAUDE.md`](CLAUDE.md) — the operating contract: ten invariants, each naming the test that
  enforces it, and a definition of done that is a number rather than a judgement.
- [`scripts/verify.py`](scripts/verify.py) — the oracle. Parses the requirements document and
  reports what is actually proven.
- [`scripts/make_sample_data.py`](scripts/make_sample_data.py) — generates the six sample files, so
  a reviewer can see how each case was constructed rather than taking the data on trust.
- [`docs/Transaction Reconciliation.pdf`](docs/Transaction%20Reconciliation.pdf) — the original brief.

---

## 10. Deployment

Deployed on [Render](https://render.com) from [`render.yaml`](render.yaml) — one web service and one
Postgres 16 database, both on the free plan. Create a Blueprint from this repository and Render reads
that file; nothing else needs configuring.

Three things in it are worth explaining.

**The start command migrates and seeds before it serves.** Both steps are idempotent, so it is safe
on every restart — and it means the service can never come up against a database with no schema.
SQLite creates its file the moment anything connects, so a missed migration does not fail loudly: the
server starts, reports success, and every page then fails inside a query. Doing it here removes the
failure mode entirely.

**`uv sync --frozen` in the build.** The build fails loudly if `uv.lock` and `pyproject.toml` have
drifted apart, rather than quietly resolving a dependency set the tests never ran against.

**Render hands out a bare `postgres://` URL,** which SQLAlchemy 2.0 no longer accepts. It is rewritten
once, in [`app/config.py`](app/config.py), to name the driver. That is a spelling correction at the
edge, not a branch on backend — nothing downstream behaves differently, which is what TR-704 actually
forbids, and the identical test suite passing on both engines is the evidence.

**The free plan sleeps** after fifteen minutes of inactivity, so the first request after a quiet spell
takes about fifty seconds while the service wakes. Nothing is broken; it is just cold.

**There is no login.** That is the exclusion named in [§3](#3-what-i-deliberately-left-out-and-why),
and on a public URL it means anyone can upload a file or record a resolution. Everything in the demo
is fabricated data generated by [`scripts/make_sample_data.py`](scripts/make_sample_data.py), and the
seeded state is one restart away. It is the one exclusion that must be reversed before this touches a
real trade.

---

## 11. Running it

```bash
uv sync
make run          # migrates, seeds, then serves on http://127.0.0.1:8000
```

`make run` migrates first, because SQLite creates its file the moment anything connects — so
skipping the migration would start a server whose every page fails inside a query. Spelled out:

```bash
uv sync
uv run alembic upgrade head
uv run python -m app.seed
uv run uvicorn app.main:app
```

Postgres is `DATABASE_URL` and nothing else. No code branches on which one is running.

### Tests

```bash
uv run pytest tests/unit    # no database, no browser
uv run pytest               # everything hermetic
make verify                 # the oracle: lint, types, coverage, requirement trace, route smoke
make e2e                    # a real browser; needs `uv run playwright install chromium`
```

The integration suite runs against Postgres too, which is how backend
independence is proven rather than claimed. It drops and recreates the schema,
so point it at a disposable database:

```bash
docker run -d --name recon-pg -e POSTGRES_PASSWORD=recon -e POSTGRES_DB=recon \
  -p 55433:5432 postgres:17-alpine

TEST_DATABASE_URL="postgresql+psycopg://postgres:recon@127.0.0.1:55433/recon" \
  PERF_BUDGET_SECONDS=30 uv run pytest tests/integration
```

All 150 pass. The wider performance budget is the only difference, and the
reason is recorded in [TRADEOFFS.md](docs/TRADEOFFS.md).

### The sample data, and the order to use it

All in `data/`, all for the period **2025-07-01 to 2025-07-07**, which the form defaults to.

| # | Upload as | File | What it demonstrates |
| - | --------- | ---- | -------------------- |
| 1 | `ledger` | `ledger_2025-07-01_07.csv` | Our side. 40 rows, plus 2 deliberately unreadable |
| 2 | `statement` | `statement_2025-07-01_07.csv` | Their side — different columns, dates, `B`/`S` |
|   |            | **Run reconciliation** | 21 items need a decision |
| 3 | `statement` | `statement_2025-07-01_07_resend.csv` | Byte-identical — refused |
| 4 | `statement` | `statement_2025-07-01_07_v2.csv` | Correction: 3 amounts fixed, `C-9002` withdrawn |
| 5 | `statement` | `statement_2025-07-01_07_v3.csv` | Books `T-1012` late — revokes an acceptance |
| 6 | `venue_c` | `venue_c_2025-07-01_07.csv` | Third format. Run **ledger against venue_c** |

Between steps 2 and 4, resolve two things — confirm a suggested pairing, and accept an unmatched row
as having no counterpart — then run again. That re-run is the requirement most worth checking.
