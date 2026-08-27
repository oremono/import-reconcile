# Transaction Reconciliation — Functional Specification

**Status:** draft for review
**Scope of this document:** what the system does and why. It contains no database design, no framework choice, no API or screen markup. Those decisions follow once this document is agreed.

Where a rule needs a number — a tolerance, a time window — the number is stated here, because that is business policy rather than implementation detail.

---

## 1. Problem statement

We trade with a counterparty. We keep our own record of every trade in our ledger. The counterparty keeps their own record and sends us a statement covering the same period. The two records are supposed to describe the same events, and they do not agree.

They disagree in three different ways, and the three need to be told apart:

- **They describe the same thing differently.** The two systems were built by different companies and were never designed to interoperate. Our ledger calls a field `trade_id`; theirs calls it `reference`. Ours writes a timestamp as `2025-07-01T09:15:00Z`, theirs as `2025-07-01 09:15:00`. Ours says `BUY`, theirs says `B`. None of this is a disagreement about what happened — it is a disagreement about vocabulary, and the system must absorb it silently.
- **They disagree slightly.** Amounts drift by small margins because of rounding and fees. Recorded times drift by small margins because two clocks are never quite synchronised. This is normal and must not generate work for a person.
- **They genuinely disagree.** An amount is materially different. A time is hours apart. A trade appears on one side and not the other, in either direction. These are the real problems, and a person has to look at each one.

Today nobody can tell the three apart without reading both files side by side, so every morning someone spends hours on it and the accounts cannot be closed until they finish. On top of that, the counterparty resends files — sometimes the identical file twice, sometimes a corrected version where a few amounts have been fixed — and any manual work done against the previous version has to be redone by hand.

**What we are building:** the screen a reconciliation analyst opens each morning to see everything that does not agree, understand why, and resolve it — such that today's decisions still hold tomorrow.

A further constraint shapes the design: today there are two sources, but a third counterparty with a third format is expected. Adding one must be a configuration exercise, not a rewrite.

---

## 2. Glossary

These terms are used with exactly these meanings throughout the rest of the document.

| Term | Meaning |
|---|---|
| **Source** | One system that sends us data. Today: our own **ledger**, and the counterparty **statement**. Each source has its own column names, date format, and vocabulary. |
| **File** | One delivery of data from one source, covering one period. |
| **Record** | One transaction as reported by one source — one row of a file, after its source's vocabulary has been translated into ours. |
| **Run** | One reconciliation, comparing the current records of two sources over a period. Normally once each morning. |
| **Pair** | Two records, one from each source, that the system or a person believes describe the same transaction. |
| **Match** | A pair whose compared fields all agree, exactly or within tolerance. |
| **Break** | A pair where at least one field differs by more than tolerance. Needs a person. |
| **Unmatched** | A record with no counterpart on the other side. Occurs in both directions. |
| **Tolerance** | The size of difference we accept as benign. Anything larger is a break. |
| **Excluded** | A record deliberately kept out of matching — today, cancelled transactions. |
| **Suggestion** | A pair the system considers likely but is not confident enough to assert. Shown to a person to confirm or reject. |
| **Resolution** | A decision a person makes about a record or pair, which the system honours on all future runs. |
| **Correction** | A later file from a source that restates a period already received, with some values changed. |

---

## 3. Who uses this, and when

**Primary user: the reconciliation analyst.** One person, once a morning. They are not an engineer. They know the business meaning of the fields and can judge whether a difference is acceptable; they cannot and should not be asked to read raw files.

Their morning goes:

1. The counterparty's file for yesterday has arrived. Our ledger's file is ready too.
2. They start a run.
3. They read a summary: how many transactions were compared, how many agreed, how many need them.
4. They work through the ones that need them, one at a time, until nothing is left requiring a decision.
5. The period can be closed.

**"Done for today"** means: every item in the worklist has either been resolved by the analyst or has been escalated by them outside this system. The system's job is to make that queue as short as honestly possible and to make each item in it fast to understand.

There is exactly one user role. There is no approver, no maker–checker, no separate administrator.

---

## 4. Scope

### In scope

- Loading files from two or more sources with differing formats.
- Translating each source's format into a common shape via per-source configuration.
- Detecting and refusing duplicate deliveries.
- Accepting corrections, with the earlier values still answerable.
- Excluding cancelled transactions from comparison.
- Matching records between two sources.
- Comparing matched records field by field with tolerances.
- Presenting a daily worklist of everything that needs a person.
- Two manual resolutions: pair two records, or accept that a record has no pair.
- Carrying those resolutions forward to every later run.
- Retaining every run for later reference.

### Out of scope

Listed explicitly so the boundary is deliberate rather than accidental:

- **Authentication, user accounts, permissions.** Single-user, trusted environment.
- **Scheduling.** The run is started by a person pressing a button; nothing runs on a timer.
- **Notifications** of any kind — no email, no alerting.
- **Bulk resolution.** Every resolution is one decision about one item, on purpose: bulk-accepting breaks is how reconciliation systems get quietly ignored.
- **Many-to-one matching.** One ledger record pairs with at most one statement record. Splits, merges, and netting are a real phenomenon and are deliberately not handled — see §9.
- **Multi-currency.** All amounts are assumed to be in one settlement currency. No FX conversion, no rate sourcing.
- **Accounting consequences.** The system reports disagreement; it does not post journal entries, raise claims, or amend either source.
- **Streaming or intraday reconciliation.** Batch files, once a day.
- **Automatic re-matching of manually rejected suggestions** beyond remembering that they were rejected.

---

## 5. Functional requirements

### 5.1 Ingestion

- **R1.1** The system accepts a file for a named source covering a stated period.
- **R1.2** A file whose contents are identical to a file already accepted for the same source is recognised as a resend. It is rejected with a clear message naming when the original was accepted. It does not create records and does not affect any run. Silent acceptance is not acceptable — double-counting a statement is worse than any error message.
- **R1.3** A file for a source and period already received, but whose contents differ, is a **correction**. It is accepted as a new version of that period.
- **R1.4** A file is treated as a **complete restatement** of the period it covers for that source. After a correction is accepted, the correction's rows are the current truth for that source and period. A row present in the earlier version and absent from the correction is marked **withdrawn** and is reported as such rather than vanishing.
- **R1.5** Rows that fail to load — missing required field, unparseable date, unparseable number — are recorded as **rejected rows** with the reason and the original row content, and are visible to the analyst. A file with rejected rows still loads its valid rows; it does not fail wholesale.
- **R1.6** A file whose period only partly overlaps a period already loaded for that source is **rejected** with a message naming the conflict. Periods must either match exactly or not overlap at all. A partial restatement cannot be reconciled with R1.4's rule that a file is a complete restatement of its period.

### 5.2 Normalisation

- **R2.1** Each source has a stated mapping from its own column names to the common field set: reference, timestamp, instrument, side, quantity, unit price, gross amount, status.
- **R2.2** Each source has a stated timestamp format and timezone assumption. Where a source's timestamps carry no timezone, the assumed timezone is part of that source's configuration and is recorded, not guessed per file. All timestamps are held in UTC after loading.
- **R2.3** Each source has a stated vocabulary mapping for coded values — `BUY`/`B` and `SELL`/`S` for side, and the status values including whichever word that source uses for cancelled.
- **R2.4** Adding a third source means adding one more mapping of the kinds above. It must not require changing matching or comparison behaviour. This is a hard requirement, not an aspiration: it is the difference between a tool that survives the next counterparty and one that does not.
- **R2.5** Amounts and quantities are held as exact decimal values. Values are never rounded on the way in; whatever precision a source sends is preserved, and rounding differences are handled by tolerance at comparison time rather than by truncation at load time.

### 5.3 Exclusion

- **R3.1** A record whose status maps to *cancelled* is **excluded** before matching begins.
- **R3.2** An excluded record is never matched, never compared, and never reported as unmatched. A cancelled trade with no counterpart is not a finding.
- **R3.3** Excluded records are counted in the run summary and can be listed, so that "where did that trade go?" has an answer.
- **R3.4** If a transaction is cancelled on one side and not the other, that *is* a finding: the live side becomes unmatched, and the summary shows it as a status disagreement rather than a plain missing record.

### 5.4 Matching

Matching runs in tiers. Each tier only considers records not already paired by an earlier tier.

- **R4.1 — Carried-forward resolutions.** Before anything else, every manual resolution from previous runs is applied (see §5.7). Records covered by one are already settled.
- **R4.2 — Reference match.** Records with the same reference on both sides are paired. This is the common case and is applied with confidence.
- **R4.3 — Suggested match.** For records left unpaired, the system looks for a plausible counterpart: same instrument, same side, quantity equal within quantity tolerance, and timestamp within the suggestion window (**2 hours**). Where exactly one plausible counterpart exists, it is offered as a **suggestion**. Where several exist, they are all offered, ranked by closeness in time and amount. Candidates are drawn only from the two files being reconciled in this run; records left unmatched by earlier runs are not carried into the pool.
- **R4.4** A suggestion is **never applied automatically.** It is presented for a person to confirm or reject. Rationale: a wrong automatic pairing hides two genuine problems and manufactures a third that looks like a break; leaving them unmatched is a smaller, more visible error.
- **R4.5** Confirming a suggestion is recorded as a manual match and carries forward exactly like one made from scratch.
- **R4.6** Rejecting a suggestion is remembered: that specific pair is not suggested again.
- **R4.7** Any record still unpaired after all tiers is **unmatched**, and this is reported in both directions — ours with nothing of theirs, and theirs with nothing of ours.
- **R4.8** Matching is one-to-one. A record participates in at most one pair.

### 5.5 Comparison and tolerance

Once a pair exists, every shared field is compared and the result recorded: whether it differs, in which direction, and by how much.

| Field | Rule | Rationale |
|---|---|---|
| **Instrument** | Must be identical. Any difference is a break. | Different instrument means it is not the same trade. |
| **Side** | Must be identical after vocabulary mapping. Any difference is a break. | Buy versus sell is never a rounding artefact. |
| **Quantity** | Within tolerance if the difference is at most **1 basis point (0.01%)** of the larger value. | Allows `0.50` versus `0.5` and genuine display rounding, nothing more. Quantity does not drift for economic reasons. |
| **Unit price** | Within tolerance if the difference is at most **5 basis points (0.05%)** of the larger value. | Absorbs rounding at the venue, not a different fill. |
| **Gross amount** | Within tolerance if the difference is at most **5 basis points (0.05%)** of the larger value, or **0.01** in absolute terms, whichever is greater. | Absorbs fees and rounding. The absolute floor stops tiny trades generating noise. |
| **Timestamp** | Within tolerance if the difference is at most **5 minutes**. | Clock skew between two systems. Anything larger is a different event or a real booking error. |
| **Status** | Must be identical after vocabulary mapping (excluding the cancelled case handled in R3.4). | A settled-versus-pending disagreement is a real finding. |

- **R5.1** A pair where every field is within tolerance is **agreed**. It requires no attention. If any field differed at all, however slightly, the pair is marked *agreed with drift* so the analyst can see it if they go looking, but it stays out of the worklist.
- **R5.2** A pair where any field is outside tolerance is a **break** and enters the worklist.
- **R5.3** A break records **every** differing field, not just the first — the analyst needs the whole picture, and a price difference plus an amount difference tell a different story than either alone.
- **R5.4** For each differing field the system records both values, the absolute difference, and the relative difference where meaningful. "The amount differs" is not useful; "ours is 34,000.00, theirs is 34,170.00, they are higher by 170.00, which is 0.50%" is.
- **R5.5** Tolerances are configuration, stated per source pair, not constants buried in behaviour. Different counterparties settle differently.

### 5.6 Outcome states

Every record ends a run in exactly one state.

| State | Meaning | In the worklist? |
|---|---|---|
| **Excluded** | Cancelled; deliberately not compared. | No |
| **Agreed** | Paired, all fields within tolerance. | No |
| **Agreed with drift** | Paired, all fields within tolerance, but at least one differed slightly. | No |
| **Break** | Paired, at least one field outside tolerance. | **Yes** |
| **Suggested** | A plausible counterpart exists but is unconfirmed. | **Yes** |
| **Unmatched** | No counterpart found. | **Yes** |
| **Status disagreement** | A counterpart exists but is cancelled on one side and live on the other. | **Yes** |
| **Accepted unmatched** | No counterpart, and a person has accepted that this is genuine. | No |
| **Withdrawn** | Present in an earlier version of the period, absent from the current one. | **Yes** |
| **Rejected row** | Could not be loaded. Counted in the worklist total, but listed under the file that carried it rather than as an item to open. | **Yes** |

The **worklist** is precisely the states marked yes. "Done for today" is an empty worklist.

### 5.7 Resolution and persistence

This is the requirement most likely to be under-built, so it is stated as a hard rule: **a decision made by a person must never need to be made twice.**

- **R7.1** The analyst can **manually pair** two unmatched records, one from each side. They give a reason.
- **R7.2** The analyst can **accept** an unmatched record as genuinely having no counterpart. They give a reason.
- **R7.3** Every resolution records who made it, when, and why.
- **R7.4** A resolution is a statement about **identity**, made in terms of the records themselves, not in terms of the run it was made in. Every subsequent run applies it before matching begins.
- **R7.5** A manually paired record is compared exactly like an automatically paired one. If its fields disagree beyond tolerance it appears as a break — pairing two records asserts they are the same transaction, not that they agree.
- **R7.6** If a correction changes the values underlying a manual pair, the pair stands and is re-compared. It may become a break as a result, which is correct: the pairing was still right, the numbers changed.
- **R7.7** If a record previously accepted as unmatched later acquires a genuine counterpart — because a correction added the missing row — the acceptance is **automatically revoked** and the pair is surfaced to the analyst as needing review. Yesterday's honest decision was made on incomplete information, and quietly keeping it would hide the very thing the correction fixed.
- **R7.8** A resolution can be undone by the analyst, with the reason recorded. Nothing is silently reversed except the case in R7.7, which is reported.

### 5.8 History

- **R8.1** Every run is retained with its summary and its results. Runs are not overwritten.
- **R8.2** After a correction, the previous values of a changed record remain viewable — the analyst can answer "what did this row say before?" without going back to the original file.
- **R8.3** For any record, the analyst can see which file version it came from and when that file was accepted.
- **R8.4** Runs can be compared at the summary level: how many breaks yesterday, how many today.

---

## 6. The morning screen

Described as what the analyst sees, not as markup.

### 6.1 Home — runs

The landing view. A list of previous runs, newest first, each showing when it ran, which sources and period it covered, and its headline counts. A control to start a new run: choose the two sources and the period, confirm, and the run executes and opens its results.

Also here: the ability to load a file, and a list of files accepted so far per source, including any that were rejected as duplicates and any that carried rejected rows.

### 6.2 Run summary

The first thing seen after a run. It answers "how bad is today?" in one glance:

- Total records read from each side.
- Excluded (cancelled) count.
- Agreed count, with the *agreed with drift* portion shown alongside it.
- **Breaks.**
- **Suggestions awaiting confirmation.**
- **Unmatched**, split by side — ours with no counterpart, theirs with no counterpart.
- Accepted-unmatched count, carried forward.
- Withdrawn and rejected-row counts, if any are non-zero.

The three bold groups are the worklist and are the visual focus. Everything else is context. A single prominent number — items needing attention — sits at the top.

### 6.3 Worklist

One list of everything needing a decision, filterable by state and sortable by size of difference so the largest money problems come first. Each line shows enough to triage without opening it: reference, instrument, the headline difference, and the state. Clicking a line opens the detail.

### 6.4 Break detail

For one pair. Side by side, our record and theirs, field by field. Fields that agree are quiet. Fields that differ are prominent and each shows both values, the absolute difference, and the percentage where it means something. The pair's origin is stated — matched on reference, or matched by hand on a given date by a given person for a given reason.

Actions here: unpair (if it was a manual pair), or leave it as an escalation and move on.

### 6.5 Unmatched detail

For one record with no counterpart. Shows the record in full, plus the ranked list of plausible counterparts from the other side, if any, with the reason each is plausible and how it differs.

Actions: confirm one of the candidates as the pair; search the other side for a counterpart not in the candidate list and pair it; or accept that there is no pair. Every action asks for a reason before it is recorded.

### 6.6 Record history

For one record: its current values, its earlier values if it has been corrected, which file version each came from, and every resolution ever applied to it.

---

## 7. User flows

Each as trigger → steps → end state.

### Flow 1 — The morning run

**Trigger:** the analyst arrives; yesterday's files are available.
1. Open the home view.
2. Load our ledger file for the period, then the counterparty file.
3. Start a run for the two sources over that period.
4. The system carries forward existing resolutions, excludes cancelled records, matches, compares, and produces a summary.
5. Read the summary.

**End state:** a stored run with counts by state, and a worklist of known size.

### Flow 2 — Triage to zero

**Trigger:** a run has produced a non-empty worklist.
1. Open the worklist, sorted by size of difference.
2. Take the top item, open it, decide, return to the list.
3. Repeat.

**End state:** the worklist contains only items the analyst has consciously escalated outside the system.

### Flow 3 — Inspect a break

**Trigger:** a pair is reported as a break.
1. Open the item from the worklist.
2. See both records side by side, with the differing fields called out.
3. Read each difference as both an absolute and a relative figure.
4. Judge whether it is an operational error, a fee treatment difference, or a genuine trade discrepancy.

**End state:** the analyst knows exactly which fields disagree and by how much, without opening either source file.

### Flow 4 — Manually match two records

**Trigger:** a record is unmatched but the analyst can see its counterpart.
1. Open the unmatched record.
2. Either confirm one of the ranked candidates, or search the other side and select the counterpart.
3. Give a reason.
4. Confirm.
5. The pair is created and immediately compared; if the values disagree beyond tolerance it now appears as a break, which is correct.

**End state:** a durable pairing, attributed and reasoned, that every future run honours.

### Flow 5 — Accept a record as having no pair

**Trigger:** a record is unmatched and the analyst has established there genuinely is no counterpart.
1. Open the unmatched record.
2. Choose to accept it as unmatched.
3. Give a reason.
4. Confirm.

**End state:** the record leaves the worklist today and does not return in future runs — unless a later correction produces a genuine counterpart, in which case the acceptance is revoked and the analyst is told (R7.7).

### Flow 6 — The same file arrives twice

**Trigger:** the counterparty resends an identical file.
1. The analyst loads it as usual.
2. The system recognises identical contents already accepted for that source and refuses it, naming when the original was accepted.

**End state:** no new records, no effect on any run, and a clear message rather than silent duplication.

### Flow 7 — A correction arrives

**Trigger:** the counterparty resends the period with a few amounts fixed.
1. The analyst loads it.
2. The system sees the same source and period with different contents and accepts it as a new version.
3. Changed rows take the new values; unchanged rows are unaffected; rows now absent are marked withdrawn.
4. The analyst starts a fresh run.
5. Rows fixed by the correction now agree and drop out of the worklist. Any newly introduced disagreement appears.
6. For any row, the analyst can still see what it previously said.

**End state:** the corrected values are authoritative, the previous values remain answerable, and manual pairings survive the correction.

### Flow 8 — Tomorrow

**Trigger:** the next morning's run over the next period, or a re-run of the same period.
1. Start the run.
2. Resolutions from previous runs are applied before matching.
3. Records manually paired yesterday are paired today without being touched. Records accepted as unmatched yesterday do not reappear.

**End state:** yesterday's work is not repeated. Only genuinely new problems are in today's worklist.

---

## 8. Decisions and assumptions

Every judgment call the brief left open, and why it was called that way. This section is the source for the README's "what I decided and why".

| # | Question left open | Decision | Why |
|---|---|---|---|
| D1 | How large is "a tiny difference" in an amount? | 5 basis points of the larger value, with an absolute floor of 0.01. | Comfortably covers fee and rounding drift; the worked example of 34,000.00 versus 34,170.00 is 0.50%, ten times the threshold, and is correctly a break. |
| D2 | How large is "a tiny difference" in a time? | 5 minutes. | Generous for clock skew between two systems. The worked example of 09:15 versus 09:15 agrees; 10:00 versus 10:40 is 40 minutes and is correctly a break. |
| D3 | Should quantity get the same tolerance as amount? | No — 1 basis point. | Quantity does not drift for economic reasons. `0.50` versus `0.5` is formatting; anything beyond that is a different fill. |
| D4 | Should fuzzy matches be applied automatically? | No. They are proposed and a person confirms. | A wrong automatic pairing conceals two genuine problems and invents a fake break. An unmatched pair is a smaller and more visible error than a wrong match. |
| D5 | How wide is the window for proposing a candidate? | 2 hours, same instrument and side, quantity within tolerance. | Wide enough to catch a genuine counterpart booked late, narrow enough that the candidate list stays short. Deliberately much wider than the 5-minute comparison tolerance, because a suggestion is a question, not an assertion. |
| D6 | What makes two files "the same file"? | Identical contents for the same source. Not the filename. | Filenames are unreliable and are often stamped with the send time. Contents are what matters. |
| D7 | Does a correction replace the period or merge into it? | It replaces it. Each file is a complete restatement of its period for that source. | Matches how counterparties actually resend, and gives an unambiguous answer for rows that disappear. Merging leaves withdrawn rows silently alive forever. |
| D8 | What happens to a row that a correction drops? | Marked withdrawn and surfaced, not deleted. | A row vanishing is itself a finding worth a person's attention. |
| D9 | Does a manual pairing survive a correction to its values? | Yes. It is re-compared and may become a break. | The pairing asserts identity, not agreement. The identity did not change; the numbers did. |
| D10 | Does an accepted-unmatched decision survive a correction that supplies a counterpart? | No — it is revoked automatically and reported. | The decision was made on incomplete information. Honouring it would hide exactly what the correction fixed. This is the one automatic reversal in the system, and it is always reported. |
| D11 | Can one record pair with several? | No. One-to-one only. | Splits, merges, and netting are real but need their own model and their own UI. Handling them badly is worse than declining them clearly. |
| D12 | Which side is authoritative when they disagree? | Neither. The system reports the disagreement and never picks a winner. | Deciding who is right is the analyst's job and often the counterparty's; a system that silently prefers one side destroys the evidence. |
| D13 | Are tolerances global? | No — configured per source pair. | Different counterparties settle differently. Hard-coded thresholds are the first thing to break with a third source. |
| D14 | How is a cancelled row on only one side treated? | The live side becomes unmatched, and the summary distinguishes it as a status disagreement. | A trade cancelled by one party and not the other is a genuine and fairly serious break, and would be invisible if the cancelled row were merely excluded. |
| D15 | What about rows that will not load at all? | Loaded rows still load; bad rows are listed with their reason and original content, and appear in the worklist. | Failing an entire file because of one bad row means nothing reconciles that morning. |
| D16 | Currency? | Single settlement currency assumed across sources; no FX. | Multi-currency needs rate sourcing and a valuation date, which is a separate problem. Stated as an assumption rather than left implicit. |
| D17 | Timezones? | Held in UTC internally. Each source declares its timezone in configuration; a source sending naive timestamps is interpreted by that declaration. | Guessing per file is how a one-hour reconciliation break gets created twice a year. |
| D18 | Is there any concept of a user account? | No. Resolutions record an author name entered by the analyst. | Single-user scope; but attribution is still recorded, because "who decided this and why" is the question asked six weeks later. |
| D19 | Can a counterparty reuse a reference in a later period? | No. A reference identifies one transaction for all time. | True of most venues, whose identifiers are sequential. Keeps the pairing key, every resolution, and every history lookup to a single value. Revisit only if a source is observed restarting its numbering. |
| D20 | Is there a state for "raised with the counterparty, awaiting reply"? | No. Two resolutions only: pair by hand, or accept no pair. | The brief asks for exactly those two. A third state needs its own ageing and chasing behaviour to be worth anything, and a half-built one is worse than none. Recorded in §10 as the first thing to add next. |
| D21 | What if a file's period only partly overlaps one already loaded? | Rejected with a message. Exact match or no overlap. | Superseding only the overlap would break the complete-restatement rule (D7) and make withdrawn rows undetectable. A partial delivery is a problem with the delivery, and the analyst should hear about it immediately. |
| D22 | Which records can a suggestion draw on? | Only the two files in the current run. | Predictable and fast, and the analyst can always pair manually when the counterpart is elsewhere. Carrying unmatched records forward needs a persistent open-items pool and an ageing rule, which is a larger design than the value it adds here. |
| D24 | A rejected row needs a person, but has no record to open. Where does it live? | Counted in the worklist total, listed under its file rather than as a worklist item. | A rejected row never became a record, so there is nothing to pair, compare, or resolve - the action is to fix the file, not to reconcile it. Counting it keeps the analyst from reaching an empty worklist while two rows silently never loaded; listing it under its file puts it where the fix is. Surfaced while building the run summary. |
| D23 | Is a one-sided cancellation its own outcome state, or a flavour of unmatched? | Its own state, `status disagreement`. | R3.4 requires the summary to distinguish it, and a summary built from one state per record (R5.6) cannot distinguish what is not a state. Folding it into unmatched would hide a genuinely serious break - one party cancelled a trade and the other did not - among ordinary missing rows. Surfaced while building the schema. |

---

## 9. Acceptance criteria

Each maps to a flow, is observable without reading code, and doubles as the script for the required demo video.

1. Loading a ledger file and a statement file with different column names, different date formats, and `BUY`/`B` vocabulary produces records that compare correctly, with no manual intervention. *(§5.2)*
2. A run produces a summary whose counts add up: every record read is in exactly one outcome state. *(§5.6)*
3. A cancelled transaction present on one side only appears neither as agreed nor as a plain unmatched record, but is counted as excluded and, where its counterpart is live, surfaced as a status disagreement. *(§5.3)*
4. A pair differing by less than the tolerances does not appear in the worklist; a pair differing by more does. *(§5.5)*
5. Opening a break shows every differing field with both values and the size of each difference, absolute and relative. *(§6.4)*
6. Records present on one side only appear as unmatched, and this is demonstrated in **both** directions. *(§5.4)*
7. Re-loading a byte-identical file is refused with a message naming the original acceptance, and the run counts are unchanged. *(§5.1)*
8. Loading a corrected file changes the affected values, drops the fixed rows out of the worklist on the next run, and leaves the previous values viewable. *(§5.1, §5.8)*
9. Manually pairing two unmatched records and re-running leaves them paired, with the reason and author still shown. *(§5.7)*
10. Accepting a record as unmatched and re-running leaves it out of the worklist. *(§5.7)*
11. A correction that supplies a counterpart for a previously accepted record revokes the acceptance and surfaces it. *(R7.7)*
12. Adding a third source is demonstrated as a configuration change only. *(R2.4)*

---

## 10. Deliberately deferred

Nothing in this specification is undecided. These are the things left out on purpose, in the order they are worth adding next. This section is the source for the README's "what I would do next".

1. **An escalated state.** Today a break the analyst has raised with the counterparty stays in the worklist with no way to mark it as chased. The first real user will ask for this within a week. It needs a state, an ageing rule, and a filter (D20).
2. **Many-to-one matching.** Splits, merges, and netting are real and are declined outright here (D11). They need their own model, not a variation on the pair.
3. **Carrying unmatched records forward.** A trade booked a day late by one side will never be suggested today (D22). Fixing it means a persistent open-items pool with an ageing rule.
4. **Multi-currency.** Assumed away (D16). Needs a rate source and a valuation date before it is even a design question.
5. **Scheduling and notification.** The run is started by a person on purpose (§4). Automating it is easy; deciding who gets told what, and when, is not.
