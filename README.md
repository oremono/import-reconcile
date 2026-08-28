# import-reconcile

Reconciles our own trade ledger against a counterparty's statement, and gives the
analyst a screen to work the differences each morning.

The two systems were built by different companies. They use different column
names, different date formats, and different words for the same value. Some
differences are benign — rounding, fees, clock skew — and some are real. This
tells them apart, and remembers what a person decided about the ones it could
not settle on its own.

---

## Running it

```bash
uv sync
make run          # migrates, seeds, then serves on http://127.0.0.1:8000
```

Or the same thing spelled out:

```bash
uv sync
uv run alembic upgrade head
uv run python -m app.seed
uv run uvicorn app.main:app
```

SQLite creates its file the moment anything connects, so skipping the migration
does not fail loudly — the server starts and every page then fails inside a
query. `make run` migrates first, and a database with no schema renders a page
naming the step that was missed rather than a stack trace.

Then open <http://127.0.0.1:8000>. Four commands, no service to install: the
database is SQLite by default. Postgres is a `DATABASE_URL` and nothing else.

**Tests**

```bash
uv run pytest tests/unit    # no database, no browser
uv run pytest               # everything hermetic
make verify                 # lint, types, both suites, coverage, requirement trace
make e2e                    # a real browser against a real server
```

The browser suite is opt-in, so `make verify` stays runnable by a reviewer who
has not installed one. To run it: `uv run playwright install chromium`.

`make verify` is the definition of done. It parses the *Verified by* column of
[docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) and reports how many of the 82
requirements have a test that ran and passed:

```
TR verified   67 / 67   (15 manual)
AC verified   12 / 12
```

A requirement whose test does not exist reports unverified, so it cannot be
quietly skipped.

---

## A five-minute tour

Sample data is in `data/`, one week, forty rows a side.

1. Load `ledger_2025-07-01_07.csv` as **ledger**, then `statement_2025-07-01_07.csv`
   as **statement**, both for 2025-07-01 to 2025-07-07. The ledger reports two
   rows it could not read — they are listed under the file, not lost.
2. Run ledger against statement. The summary leads with **21 items need a
   decision**; everything else is context.
3. Open the worklist. `T-1010` is the brief's own example: gross amount 34,000.00
   against 34,170.00, off by 0.4975% — ten times the tolerance, so a break. A row
   two cents apart is *not* there; it is agreed-with-drift.
4. Open a break. Every field is shown, both values, absolute and relative
   difference. Instrument, side and status carry no magnitude, and say so.
5. Pair `T-1022` with `C-91023` by hand — one trade booked under two references.
   Accept `T-1012` as having no counterpart. Both need a reason and a name.
6. Re-run. Both decisions still hold and you are not asked again.
7. Re-upload `statement_2025-07-01_07_resend.csv`. Refused, naming when the
   original arrived.
8. Upload `statement_2025-07-01_07_v2.csv`. Three amounts are corrected and
   `C-9002` is withdrawn. Re-run: the fixed rows leave the worklist, and each
   row's history still shows what it used to say.
9. Upload `statement_2025-07-01_07_v3.csv`, where the counterparty finally books
   `T-1012`. Re-run: the acceptance from step 5 is **automatically revoked and
   reported**, because it was an honest decision made on incomplete information.
10. Run ledger against **venue_c** — a third format with epoch timestamps and
    `d`/`c` side codes. It reconciles with no code change.

---

## How it is put together

```
core/            pure logic - standard library only, no database, no web
app/services/    orchestration
app/db/          models, custom column types, migrations
app/web/         nine server-rendered routes
```

`core/` is where the rules live and it imports nothing but the standard library.
That is not a convention: `tests/unit/test_boundaries.py` parses every module
under `core/` and fails the suite on any infrastructure import, so the brief's
"testable without a database and without a browser" cannot decay.

**Four decisions worth arguing with**

- **Resolutions key on `(source, reference)`, never `record.id`.** Records are
  immutable, so a correction writes new rows. A row-id key would silently detach
  exactly when the analyst most needs yesterday's decision to hold.
  `test_survives_correction` proves it by asserting the old row id is no longer
  current.
- **Money is stored as text through an exact decimal type.** SQLAlchemy's
  `Numeric` degrades to float on SQLite. In an application that exists to do
  arithmetic about money, that is disqualifying. Passing a float raises.
- **Suggestions are proposed, never applied.** A wrong automatic pairing hides
  two real problems and invents a third that looks like a break.
- **Neither side is authoritative.** The system reports the disagreement and
  never picks a winner. Relative differences divide by the larger magnitude, so
  swapping the two sides changes only a sign.

Full reasoning: [SPEC §8](docs/SPEC.md) for the business calls,
[DESIGN §14](docs/DESIGN.md) for the technical ones.

---

## Documents

| Document | Answers |
|---|---|
| [docs/SPEC.md](docs/SPEC.md) | What it does, in business terms. 24 decisions with rationale |
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | 82 technical requirements, each traced to a test |
| [docs/DESIGN.md](docs/DESIGN.md) | How it is built. 15 design decisions with the alternative rejected |
| [docs/TRADEOFFS.md](docs/TRADEOFFS.md) | What those decisions cost, and where they break first |

---

## What I left out, and why

Written as decisions rather than omissions. The full list with reasoning is
[TRADEOFFS §5](docs/TRADEOFFS.md); the short version:

- **No authentication.** The resolution author is a typed field. Single-user scope.
- **No scheduler, no queue, no notifications.** A run starts from a request.
- **One-to-one matching only.** Netting, splits and partial fills are real and
  are declined outright rather than handled badly. The unique index that makes
  one-to-one correct today is the same index that blocks them tomorrow.
- **Single currency.** Multi-currency needs a rate source and a valuation date.
- **No escalation state.** A break raised with the counterparty stays in the
  worklist. It is the first thing I would add.
- **No pagination.** The worklist degrades exactly when it matters most.

## What I would do next

In order of value per hour, not interest:

1. **Pagination on the worklist** — an hour, and the main screen stops degrading
   on a bad day.
2. **A guard against reference reuse** — an hour. Nothing currently detects a
   counterparty restarting its numbering, and the whole resolution model assumes
   they do not. It is the most dangerous silent assumption in the system.
3. **Optimistic concurrency on resolutions** — an hour. Last-write-wins on a
   financial decision is not good enough for two analysts.
4. **Move the run off the request** — half a day, before any file large enough to
   matter.
5. **Postgres by default** — SQLite is right for review and wrong for use.

Two assumptions are worth naming because both are about someone *else's* system
and neither is validated at ingest: that a file is a complete restatement of its
period, and that a reference is never reused. A counterparty sending true deltas
would be ingested as a restatement that withdraws everything it omits — the worst
failure mode in the system, because it looks like data rather than an error.
