"""The web surface, exercised through HTTP rather than through the route functions.

Every test here drives the real ASGI application with the real templates. A
route that raises, a template that references a name the route never passes, and
a form whose field names do not match the handler are all invisible to a test
that calls the function directly, and all three are the failures this suite
exists to catch.

The only thing substituted is the database: ``session_dependency`` is overridden
with the suite's throwaway session so a test cannot write to the repository's
own ``reconcile.db``.

The sample files in ``data/`` are the fixtures. They are what a reviewer will
upload, so they are what these tests upload -- a bug the reviewer would hit on
their first click is one this suite should hit first.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import FileBatch, Pair, Record, Resolution, RunItem
from app.db.session import session_dependency
from app.main import app
from app.services.reconcile import run_summary
from core.compare import format_decimal
from core.model import COMPARED_FIELDS, WORKLIST_STATES, RecordState, ResolutionKind
from tests.integration.conftest import SeededSources

DATA = Path(__file__).resolve().parents[2] / "data"

LEDGER_FILE = "ledger_2025-07-01_07.csv"
STATEMENT_FILE = "statement_2025-07-01_07.csv"
RESEND_FILE = "statement_2025-07-01_07_resend.csv"
CORRECTION_FILE = "statement_2025-07-01_07_v2.csv"

PERIOD = {"period_start": "2025-07-01", "period_end": "2025-07-07"}

#: References the sample files were built to produce. Named so a failure says
#: which behaviour broke rather than which row number moved.
BREAK_REFERENCE = "T-1010"  # 34,000.00 against 34,170.00 -- the brief's own example
SUGGESTION_REFERENCE = "T-1022"  # ours; theirs calls it C-91023, twelve minutes later
SUGGESTION_COUNTERPART = "C-91023"
LEDGER_ONLY_REFERENCE = "T-1012"  # never appears on the statement
STATEMENT_ONLY_REFERENCE = "C-9001"  # never appears in the ledger


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db_session: Session, seeded_sources: SeededSources) -> Iterator[TestClient]:
    """The real application, pointed at the test database.

    ``raise_server_exceptions=False`` so an unhandled exception arrives as a
    500 response rather than as a traceback out of the client call. TR-605 is a
    statement about status codes, and a test asserting it should be able to see
    the status code it is asserting about.
    """

    def _test_session() -> Iterator[Session]:
        yield db_session
        db_session.commit()

    app.dependency_overrides[session_dependency] = _test_session
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def upload(client: TestClient, source: str, filename: str, **period: str) -> Any:
    """POST a file from ``data/`` exactly as the browser form does."""
    payload = {"source": source, **PERIOD, **period}
    with (DATA / filename).open("rb") as handle:
        return client.post(
            "/files",
            data=payload,
            files={"upload": (filename, handle, "text/csv")},
            follow_redirects=False,
        )


def start_run(client: TestClient, **overrides: str) -> Any:
    payload = {"left_source": "ledger", "right_source": "statement", **PERIOD, **overrides}
    return client.post("/runs", data=payload, follow_redirects=False)


def run_id_of(response: Any) -> int:
    location = response.headers["location"]
    found = re.match(r"^/runs/(\d+)", location)
    assert found is not None, f"expected a redirect to a run, got {location!r}"
    return int(found.group(1))


@pytest.fixture
def loaded(client: TestClient) -> TestClient:
    """Both sides of the sample period, loaded through the upload form."""
    assert upload(client, "ledger", LEDGER_FILE).status_code == 303
    assert upload(client, "statement", STATEMENT_FILE).status_code == 303
    return client


@pytest.fixture
def run(loaded: TestClient) -> int:
    """A completed run over the sample period."""
    response = start_run(loaded)
    assert response.status_code == 303, response.text
    return run_id_of(response)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def item_by_reference(session: Session, run_id: int, reference: str) -> RunItem:
    item = session.scalar(
        select(RunItem)
        .join(Record, RunItem.record_id == Record.id)
        .where(RunItem.run_id == run_id, Record.reference == reference)
    )
    assert item is not None, f"no run item for {reference} in run {run_id}"
    return item


def pair_for(session: Session, run_id: int, reference: str) -> Pair:
    item = item_by_reference(session, run_id, reference)
    assert item.pair_id is not None, f"{reference} is not paired"
    pair = session.get(Pair, item.pair_id)
    assert pair is not None
    return pair


def resolution_count(session: Session) -> int:
    return session.scalar(select(func.count(Resolution.id))) or 0


def state_pills(html: str) -> list[str]:
    """The state of every row in a rendered worklist table."""
    return re.findall(r'<td><span class="pill ([a-z_]+)">', html)


def listed_record_ids(html: str) -> set[int]:
    """Every record the rendered worklist accounts for.

    A break is stored twice -- once per side, because every record carries
    exactly one state -- and rendered once, because it is one decision. The
    filter has to leave *all* of it, so what is counted here is the records the
    list covers, not the lines it drew.
    """
    covered: set[int] = set()
    for group in re.findall(r'<tr data-records="([\d,]*)"', html):
        covered.update(int(value) for value in group.split(",") if value)
    return covered


def records_in_state(session: Session, run_id: int, state: str) -> set[int]:
    return {
        row
        for row in session.scalars(
            select(RunItem.record_id).where(RunItem.run_id == run_id, RunItem.state == state)
        )
    }


def numeric_column(html: str, attribute: str) -> list[Decimal]:
    return [Decimal(value) for value in re.findall(rf'data-{attribute}="([^"]*)"', html)]


def follow(client: TestClient, response: Any) -> Any:
    return client.get(response.headers["location"])


# ---------------------------------------------------------------------------
# TR-602
# ---------------------------------------------------------------------------


def test_all_routes(client: TestClient, db_session: Session) -> None:
    """Every route in DESIGN section 7 answers 200 for valid input (TR-602).

    Built as one walk through the morning rather than nine isolated calls,
    because that is the only way the ids handed from one page to the next are
    real ones.
    """
    assert client.get("/").status_code == 200

    assert follow(client, upload(client, "ledger", LEDGER_FILE)).status_code == 200
    assert follow(client, upload(client, "statement", STATEMENT_FILE)).status_code == 200

    started = start_run(client)
    assert started.status_code == 303
    run_id = run_id_of(started)

    assert client.get(f"/runs/{run_id}").status_code == 200
    assert client.get(f"/runs/{run_id}/worklist").status_code == 200

    pair = pair_for(db_session, run_id, BREAK_REFERENCE)
    assert client.get(f"/runs/{run_id}/pairs/{pair.id}").status_code == 200

    unmatched = item_by_reference(db_session, run_id, LEDGER_ONLY_REFERENCE)
    assert client.get(f"/runs/{run_id}/records/{unmatched.record_id}").status_code == 200

    assert client.get(f"/records/ledger/{BREAK_REFERENCE}/history").status_code == 200

    posted = client.post(
        "/resolutions",
        data={
            "kind": ResolutionKind.ACCEPT_UNMATCHED.value,
            "left_source": "ledger",
            "left_reference": LEDGER_ONLY_REFERENCE,
            "reason": "Confirmed by phone: never sent to them.",
            "author": "aoife",
            "return_to": f"/runs/{run_id}/records/{unmatched.record_id}",
        },
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert follow(client, posted).status_code == 200


def test_missing_things_are_404_not_500(client: TestClient) -> None:
    """A run, pair, record or source that does not exist is a page, not a traceback."""
    for path in (
        "/runs/9999",
        "/runs/9999/worklist",
        "/runs/9999/pairs/1",
        "/runs/9999/records/1",
        "/records/no-such-source/T-1/history",
    ):
        response = client.get(path)
        assert response.status_code == 404, f"{path} answered {response.status_code}"
        assert "Not Found" in response.text


# ---------------------------------------------------------------------------
# TR-603
# ---------------------------------------------------------------------------


def test_post_redirect_get(client: TestClient, db_session: Session) -> None:
    """Every mutation answers with a redirect to a GET, and a refresh is safe (TR-603)."""
    upload_response = upload(client, "ledger", LEDGER_FILE)
    assert upload_response.status_code == 303
    location = upload_response.headers["location"]
    assert location.startswith("/")

    # The refresh: re-issuing the GET the redirect named must not load the file
    # a second time. A 302 would have let a browser re-POST here.
    batches_after_upload = db_session.scalar(select(func.count(FileBatch.id)))
    for _ in range(3):
        assert client.get(location).status_code == 200
    assert db_session.scalar(select(func.count(FileBatch.id))) == batches_after_upload

    assert upload(client, "statement", STATEMENT_FILE).status_code == 303

    started = start_run(client)
    assert started.status_code == 303
    run_id = run_id_of(started)
    assert client.get(started.headers["location"]).status_code == 200

    unmatched = item_by_reference(db_session, run_id, LEDGER_ONLY_REFERENCE)
    target = f"/runs/{run_id}/records/{unmatched.record_id}"
    before = resolution_count(db_session)
    resolved = client.post(
        "/resolutions",
        data={
            "kind": ResolutionKind.ACCEPT_UNMATCHED.value,
            "left_source": "ledger",
            "left_reference": LEDGER_ONLY_REFERENCE,
            "reason": "No counterpart: never instructed.",
            "author": "aoife",
            "return_to": target,
        },
        follow_redirects=False,
    )
    assert resolved.status_code == 303
    assert resolved.headers["location"].startswith(target)
    assert resolution_count(db_session) == before + 1

    for _ in range(3):
        assert client.get(resolved.headers["location"]).status_code == 200
    assert resolution_count(db_session) == before + 1


# ---------------------------------------------------------------------------
# TR-604
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "author", "missing"),
    [
        ("", "aoife", "reason"),
        ("Confirmed by phone.", "", "author"),
        ("   ", "   ", "reason"),
        ("\t\n", "aoife", "reason"),
    ],
)
def test_reason_required(
    run: int, client: TestClient, db_session: Session, reason: str, author: str, missing: str
) -> None:
    """No resolution is recorded without a non-empty reason and author (TR-604).

    Whitespace counts as empty. A reason of one space satisfies ``required`` in
    the browser and satisfies nobody reading the audit trail in six months.
    """
    unmatched = item_by_reference(db_session, run, LEDGER_ONLY_REFERENCE)
    target = f"/runs/{run}/records/{unmatched.record_id}"
    before = resolution_count(db_session)

    response = client.post(
        "/resolutions",
        data={
            "kind": ResolutionKind.ACCEPT_UNMATCHED.value,
            "left_source": "ledger",
            "left_reference": LEDGER_ONLY_REFERENCE,
            "reason": reason,
            "author": author,
            "return_to": target,
        },
        follow_redirects=False,
    )

    assert response.status_code < 500
    assert resolution_count(db_session) == before, "a resolution was recorded anyway"

    rendered = follow(client, response)
    assert rendered.status_code == 200
    assert "Nothing was recorded" in rendered.text
    assert missing in rendered.text


def test_reason_and_author_are_required_in_the_markup(
    run: int, client: TestClient, db_session: Session
) -> None:
    """The browser stops the ordinary mistake before the round trip (TR-609)."""
    unmatched = item_by_reference(db_session, run, LEDGER_ONLY_REFERENCE)
    html = client.get(f"/runs/{run}/records/{unmatched.record_id}").text
    for name in ("reason", "author"):
        for field in re.findall(rf'<input[^>]*name="{name}"[^>]*>', html):
            assert "required" in field, field


def test_a_good_resolution_is_recorded(run: int, client: TestClient, db_session: Session) -> None:
    """The same form, filled in, records who decided, when, and why (R7.3)."""
    unmatched = item_by_reference(db_session, run, LEDGER_ONLY_REFERENCE)
    response = client.post(
        "/resolutions",
        data={
            "kind": ResolutionKind.ACCEPT_UNMATCHED.value,
            "left_source": "ledger",
            "left_reference": LEDGER_ONLY_REFERENCE,
            "reason": "Cancelled internally before it ever reached them.",
            "author": "aoife",
            "return_to": f"/runs/{run}/records/{unmatched.record_id}",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    stored = db_session.scalar(
        select(Resolution).where(Resolution.left_reference == LEDGER_ONLY_REFERENCE)
    )
    assert stored is not None
    assert stored.kind == ResolutionKind.ACCEPT_UNMATCHED.value
    assert stored.author == "aoife"
    assert "Cancelled internally" in stored.reason
    assert stored.created_at is not None


# ---------------------------------------------------------------------------
# TR-605
# ---------------------------------------------------------------------------


def test_expected_failures(client: TestClient, db_session: Session) -> None:
    """Duplicate, overlap and malformed are messages on the page, never a 5xx (TR-605).

    Each of these is something a counterparty does on an ordinary Tuesday. The
    assertion is deliberately made on the status code as well as the text: a
    message rendered by a 500 handler is still an outage.
    """
    assert upload(client, "statement", STATEMENT_FILE).status_code == 303
    accepted = db_session.scalar(select(func.count(FileBatch.id)))

    # 1. The byte-identical resend under a different name (TR-102, AC7).
    resend = upload(client, "statement", RESEND_FILE)
    assert resend.status_code < 500
    assert resend.status_code in (200, 303)
    rendered = follow(client, resend)
    assert rendered.status_code == 200
    assert "Already accepted" in rendered.text
    assert db_session.scalar(select(func.count(FileBatch.id))) == accepted, (
        "the resend was loaded anyway"
    )

    # 2. A period that partly overlaps one already loaded (TR-104).
    overlap = upload(
        client,
        "statement",
        CORRECTION_FILE,
        period_start="2025-07-05",
        period_end="2025-07-10",
    )
    assert overlap.status_code < 500
    rendered = follow(client, overlap)
    assert rendered.status_code == 200
    assert "overlaps" in rendered.text
    assert db_session.scalar(select(func.count(FileBatch.id))) == accepted

    # 3. A file that is not the CSV this source is configured to send (TR-101).
    malformed = upload(client, "statement", LEDGER_FILE)
    assert malformed.status_code < 500
    rendered = follow(client, malformed)
    assert rendered.status_code == 200
    assert "column" in rendered.text.lower()
    assert db_session.scalar(select(func.count(FileBatch.id))) == accepted

    # 4. Not a CSV at all, and a period that cannot be read.
    for payload, needle in (
        (b"\xff\xfe not text at all", "UTF-8"),
        (b"", "empty"),
    ):
        response = client.post(
            "/files",
            data={"source": "statement", **PERIOD},
            files={"upload": ("junk.csv", payload, "text/csv")},
            follow_redirects=False,
        )
        assert response.status_code < 500
        assert needle.lower() in follow(client, response).text.lower()

    bad_date = client.post(
        "/files",
        data={"source": "statement", "period_start": "not-a-date", "period_end": "2025-07-07"},
        files={"upload": ("x.csv", b"a,b\n1,2\n", "text/csv")},
        follow_redirects=False,
    )
    assert bad_date.status_code < 500
    assert "not a date" in follow(client, bad_date).text.lower()

    # 5. A run that cannot start is a message too, not a crash.
    same = start_run(client, right_source="ledger")
    assert same.status_code < 500
    assert follow(client, same).status_code == 200

    assert db_session.scalar(select(func.count(FileBatch.id))) == accepted


def test_a_correction_is_accepted_not_refused(client: TestClient, db_session: Session) -> None:
    """A restatement of the same period is a correction, and says so (R1.5)."""
    assert upload(client, "statement", STATEMENT_FILE).status_code == 303
    correction = upload(client, "statement", CORRECTION_FILE)
    assert correction.status_code == 303
    rendered = follow(client, correction)
    assert "Correction accepted" in rendered.text
    assert "version 2" in rendered.text


# ---------------------------------------------------------------------------
# TR-606
# ---------------------------------------------------------------------------


def test_worklist_filters(run: int, client: TestClient, db_session: Session) -> None:
    """The worklist filters by state and sorts by size of difference (TR-606)."""
    counts = run_summary(db_session, run)

    unfiltered = client.get(f"/runs/{run}/worklist")
    assert unfiltered.status_code == 200
    shown = state_pills(unfiltered.text)
    assert shown, "the sample data should produce a non-empty worklist"
    assert set(shown) <= {state.value for state in WORKLIST_STATES}

    # Filtering leaves only that state, and leaves all of it.
    for state in (RecordState.BREAK, RecordState.UNMATCHED, RecordState.SUGGESTED):
        response = client.get(f"/runs/{run}/worklist?state={state.value}")
        assert response.status_code == 200
        rows = state_pills(response.text)
        assert set(rows) == {state.value}, f"{state.value} filter leaked {set(rows)}"

        # Every record in that state is accounted for, and none from any other.
        listed = listed_record_ids(response.text)
        assert listed == records_in_state(db_session, run, state.value)
        assert len(listed) == counts[state.value]

        # ...and a paired decision is drawn once, not once per side.
        assert len(rows) == len(
            {
                item.pair_id if item.pair_id is not None else -item.record_id
                for item in db_session.scalars(
                    select(RunItem).where(RunItem.run_id == run, RunItem.state == state.value)
                )
            }
        )

    # A state nobody is in is an empty list, not an error.
    empty = client.get(f"/runs/{run}/worklist?state={RecordState.AGREED.value}")
    assert empty.status_code == 200
    assert state_pills(empty.text) == []

    # Unfiltered, the list still accounts for every record that needs a person
    # apart from the rejected rows, which have no record to point at.
    everything = listed_record_ids(unfiltered.text)
    expected = set().union(
        *(records_in_state(db_session, run, state.value) for state in WORKLIST_STATES)
    )
    assert everything == expected

    # Sorting: largest difference first, in whichever sense was asked for.
    relative = numeric_column(unfiltered.text, "relative")
    assert relative == sorted(relative, reverse=True), relative
    assert relative[0] > 0, "the sample data contains breaks; the top row should show one"

    by_money = client.get(f"/runs/{run}/worklist?sort=absolute")
    absolute = numeric_column(by_money.text, "absolute")
    assert absolute == sorted(absolute, reverse=True), absolute

    # The two orders genuinely differ -- a small error on a large trade and a
    # large error on a small one are different problems.
    assert numeric_column(by_money.text, "relative") != relative

    # A sort nobody asked for falls back rather than failing.
    fallback = client.get(f"/runs/{run}/worklist?sort=nonsense")
    assert fallback.status_code == 200
    assert numeric_column(fallback.text, "relative") == relative

    # An unknown state is a 404, not a silently empty list that reads as "clean".
    assert client.get(f"/runs/{run}/worklist?state=not-a-state").status_code == 404


def test_summary_leads_with_the_worklist_total(
    run: int, client: TestClient, db_session: Session
) -> None:
    """The one number at the top is the number of items needing a person (SPEC 6.2)."""
    counts = run_summary(db_session, run)
    expected = sum(counts.get(state.value, 0) for state in WORKLIST_STATES)

    page = client.get(f"/runs/{run}")
    assert page.status_code == 200
    headline = re.search(r'<div class="count [a-z]+">(\d+)</div>', page.text)
    assert headline is not None, "the summary has no headline number"
    assert int(headline.group(1)) == expected

    for state in (RecordState.BREAK, RecordState.SUGGESTED, RecordState.UNMATCHED):
        assert f"worklist?state={state.value}" in page.text


# ---------------------------------------------------------------------------
# TR-607
# ---------------------------------------------------------------------------


def test_break_detail(run: int, client: TestClient, db_session: Session) -> None:
    """Every differing field shows both values, the absolute and the relative gap (TR-607).

    ``T-1010`` is the brief's own worked example: ten units at 3,400.00 against
    ten at 3,417.00, so 34,000.00 against 34,170.00.
    """
    pair = pair_for(db_session, run, BREAK_REFERENCE)
    page = client.get(f"/runs/{run}/pairs/{pair.id}")
    assert page.status_code == 200

    _stored, left, right, diffs = _pair_detail(db_session, pair.id)
    assert left.reference == BREAK_REFERENCE
    assert right.reference == BREAK_REFERENCE

    differing = [row for row in diffs if row.differs]
    assert differing, f"{BREAK_REFERENCE} is meant to be a break"

    for row in differing:
        assert _value_shown(row.left_value, page.text), f"{row.field}: left value missing"
        assert _value_shown(row.right_value, page.text), f"{row.field}: right value missing"
        if row.abs_diff is not None:
            assert _decimal_shown(row.abs_diff, page.text), f"{row.field}: no absolute difference"
        if row.rel_diff is not None:
            assert _percent_shown(row.rel_diff, page.text), f"{row.field}: no relative difference"

    # The brief's numbers, spelled out on the page and readable as money -- not
    # padded out to the scale they are stored at.
    assert "34000.00" in page.text
    assert "34170.00" in page.text
    assert "170.00" in page.text
    assert "0.4975%" in page.text  # 170 against 34,170
    assert "34000.000000000000" not in page.text, "storage padding leaked onto the page"

    # Fields that agree are still rendered -- the analyst reads the whole record
    # from one page -- but they are marked quiet, not prominent.
    assert 'class="same"' in page.text
    assert 'class="differs"' in page.text

    # Where the pair came from is stated, not implied.
    assert "Matched on reference" in page.text


def _pair_detail(session: Session, pair_id: int) -> Any:
    from app.services.reconcile import pair_detail

    return pair_detail(session, pair_id)


def _value_shown(stored: str, html: str) -> bool:
    """A stored value is on the page, in whatever readable form it is rendered.

    Money is stored at scale 12 and shown trimmed, so ``34170.000000000000``
    appears as ``34170.00``. Both readings name the same number, which is what
    TR-607 asks for; only the padding is dropped.
    """
    from app.web.routes import _number

    return stored in html or _number(stored) in html


def _decimal_shown(value: Decimal, html: str) -> bool:
    from app.web.routes import _decimal

    return format_decimal(value) in html or _decimal(value) in html


def _percent_shown(rel_diff: Decimal, html: str) -> bool:
    scaled = (rel_diff * 100).quantize(Decimal("0.0001"))
    text = format(scaled, "f").rstrip("0").rstrip(".") or "0"
    return f"{text}%" in html


def test_break_detail_covers_every_compared_field(
    run: int, client: TestClient, db_session: Session
) -> None:
    """All seven compared fields are on the page, agreeing or not (R5.4)."""
    pair = pair_for(db_session, run, BREAK_REFERENCE)
    page = client.get(f"/runs/{run}/pairs/{pair.id}").text
    for name in COMPARED_FIELDS:
        assert name.replace("_", " ") in page, f"{name} is not shown"


# ---------------------------------------------------------------------------
# TR-608
# ---------------------------------------------------------------------------


def test_candidates_listed(run: int, client: TestClient, db_session: Session) -> None:
    """Ranked candidates, each with the reason it qualifies (TR-608).

    ``T-1022`` has no counterpart by reference: the counterparty booked the same
    trade as ``C-91023`` twelve minutes later. That is exactly the case a ranked
    candidate list exists for.
    """
    item = item_by_reference(db_session, run, SUGGESTION_REFERENCE)
    assert item.state in {RecordState.SUGGESTED.value, RecordState.UNMATCHED.value}

    page = client.get(f"/runs/{run}/records/{item.record_id}")
    assert page.status_code == 200

    assert SUGGESTION_COUNTERPART in page.text, "the plausible counterpart is not offered"
    assert "Why it qualifies" in page.text

    from app.services.reconcile import candidates_for

    candidates = candidates_for(db_session, run, item.record_id)
    assert candidates, "the matcher should propose at least one candidate here"
    for candidate, why in candidates:
        assert candidate.reference in page.text
        assert why.strip(), "a candidate offered without a reason is just a list"
        assert why in page.text, f"the reason for {candidate.reference} is not rendered"

    # Ranked: the order the page shows is the order the matcher returned.
    shown = [reference for reference in re.findall(r"<strong>([A-Z]-\d+)</strong>", page.text)]
    assert shown == [candidate.reference for candidate, _ in candidates]

    # A record with genuinely no plausible counterpart says so rather than
    # showing an empty box.
    lonely = item_by_reference(db_session, run, LEDGER_ONLY_REFERENCE)
    barren = client.get(f"/runs/{run}/records/{lonely.record_id}")
    assert barren.status_code == 200
    assert "No candidate" in barren.text


# ---------------------------------------------------------------------------
# TR-609
# ---------------------------------------------------------------------------


def test_resolution_actions(run: int, client: TestClient, db_session: Session) -> None:
    """Manual pairing and accept-no-pair are both offered, and both stick (TR-609).

    The assertion that matters is the last one: a decision made today is applied
    by tomorrow's run without being made again (R7.4).
    """
    item = item_by_reference(db_session, run, SUGGESTION_REFERENCE)
    page = client.get(f"/runs/{run}/records/{item.record_id}").text

    # Both actions are on the page, as forms that POST.
    assert f'value="{ResolutionKind.MANUAL_MATCH.value}"' in page
    assert f'value="{ResolutionKind.ACCEPT_UNMATCHED.value}"' in page
    assert page.count('action="/resolutions"') >= 2
    assert 'method="post"' in page

    # Confirm the candidate as the counterpart.
    paired = client.post(
        "/resolutions",
        data={
            "kind": ResolutionKind.MANUAL_MATCH.value,
            "left_source": "ledger",
            "left_reference": SUGGESTION_REFERENCE,
            "right_source": "statement",
            "right_reference": SUGGESTION_COUNTERPART,
            "reason": "Same trade; they booked it under their own reference.",
            "author": "aoife",
            "return_to": f"/runs/{run}/records/{item.record_id}",
        },
        follow_redirects=False,
    )
    assert paired.status_code == 303
    assert SUGGESTION_COUNTERPART in follow(client, paired).text

    # Accept a record that genuinely has no counterpart.
    lonely = item_by_reference(db_session, run, LEDGER_ONLY_REFERENCE)
    accepted = client.post(
        "/resolutions",
        data={
            "kind": ResolutionKind.ACCEPT_UNMATCHED.value,
            "left_source": "ledger",
            "left_reference": LEDGER_ONLY_REFERENCE,
            "reason": "Booked in error our side; nothing was ever sent.",
            "author": "aoife",
            "return_to": f"/runs/{run}/records/{lonely.record_id}",
        },
        follow_redirects=False,
    )
    assert accepted.status_code == 303

    kinds = {
        resolution.left_reference: resolution.kind
        for resolution in db_session.scalars(select(Resolution))
    }
    assert kinds[SUGGESTION_REFERENCE] == ResolutionKind.MANUAL_MATCH.value
    assert kinds[LEDGER_ONLY_REFERENCE] == ResolutionKind.ACCEPT_UNMATCHED.value

    # Tomorrow morning: the same two files, a new run, and neither decision has
    # to be made again.
    second = start_run(client)
    assert second.status_code == 303
    second_id = run_id_of(second)

    manual = item_by_reference(db_session, second_id, SUGGESTION_REFERENCE)
    assert manual.pair_id is not None, "the manual pairing was not carried forward"
    pair = db_session.get(Pair, manual.pair_id)
    assert pair is not None
    assert pair.origin == "manual"

    carried = item_by_reference(db_session, second_id, LEDGER_ONLY_REFERENCE)
    assert carried.state == RecordState.ACCEPTED_UNMATCHED.value

    # And the pair the analyst made is readable like any other.
    detail = client.get(f"/runs/{second_id}/pairs/{pair.id}")
    assert detail.status_code == 200
    assert "Paired by hand" in detail.text
    assert "aoife" in detail.text
    assert "they booked it under their own reference" in detail.text


def test_rejecting_a_suggestion_is_offered_and_recorded(
    run: int, client: TestClient, db_session: Session
) -> None:
    """Not-this-one is a decision too, and stops the suggestion coming back (R4.6)."""
    item = item_by_reference(db_session, run, SUGGESTION_REFERENCE)
    page = client.get(f"/runs/{run}/records/{item.record_id}").text
    assert f'value="{ResolutionKind.REJECT_SUGGESTION.value}"' in page

    rejected = client.post(
        "/resolutions",
        data={
            "kind": ResolutionKind.REJECT_SUGGESTION.value,
            "left_source": "ledger",
            "left_reference": SUGGESTION_REFERENCE,
            "right_source": "statement",
            "right_reference": SUGGESTION_COUNTERPART,
            "reason": "Different trade; ours settled through a different venue.",
            "author": "aoife",
            "return_to": f"/runs/{run}/records/{item.record_id}",
        },
        follow_redirects=False,
    )
    assert rejected.status_code == 303

    second_id = run_id_of(start_run(client))
    again = item_by_reference(db_session, second_id, SUGGESTION_REFERENCE)
    from app.services.reconcile import candidates_for

    proposed = candidates_for(db_session, second_id, again.record_id)
    offered = [record.reference for record, _ in proposed]
    assert SUGGESTION_COUNTERPART not in offered


# ---------------------------------------------------------------------------
# TR-510 -- value history across versions
# ---------------------------------------------------------------------------


def test_history_shows_every_version(client: TestClient) -> None:
    """A corrected row's earlier values stay readable, with the file each came from."""
    assert upload(client, "statement", STATEMENT_FILE).status_code == 303
    assert upload(client, "statement", CORRECTION_FILE).status_code == 303

    page = client.get(f"/records/statement/{BREAK_REFERENCE}/history")
    assert page.status_code == 200
    assert "34170.00" in page.text, "the superseded value is gone"
    assert "34000.00" in page.text, "the corrected value is missing"
    assert STATEMENT_FILE in page.text
    assert CORRECTION_FILE in page.text
    assert "v1" in page.text
    assert "v2" in page.text


# ---------------------------------------------------------------------------
# A skipped setup step must not look like a broken server
# ---------------------------------------------------------------------------


def test_schema_is_missing_recognises_both_backends() -> None:
    """SQLite and Postgres word it differently, and neither may be named.

    TR-704 forbids branching on which database is in play, so the two phrasings
    are matched rather than the dialect being asked.
    """
    from sqlalchemy.exc import OperationalError

    from app.main import schema_is_missing

    sqlite_error = OperationalError("select 1", {}, Exception("no such table: source"))
    postgres_error = OperationalError("select 1", {}, Exception('relation "source" does not exist'))
    unrelated = OperationalError("select 1", {}, Exception("database is locked"))

    assert schema_is_missing(sqlite_error)
    assert schema_is_missing(postgres_error)
    assert not schema_is_missing(unrelated)


def test_an_unmigrated_database_explains_itself() -> None:
    """SQLite creates the file on connect, so a database that was never
    migrated does not announce itself: the server starts, and then every page
    fails deep inside a query. The page has to name the step that was skipped.
    """
    from sqlalchemy.exc import OperationalError

    from app.db.session import session_dependency
    from app.main import app

    def _unmigrated():
        raise OperationalError("select 1", {}, Exception("no such table: source"))
        yield  # pragma: no cover - unreachable, keeps this a generator

    previous = app.dependency_overrides.get(session_dependency)
    app.dependency_overrides[session_dependency] = _unmigrated
    try:
        with TestClient(app, raise_server_exceptions=False) as unmigrated_client:
            response = unmigrated_client.get("/")
    finally:
        if previous is None:
            app.dependency_overrides.pop(session_dependency, None)
        else:
            app.dependency_overrides[session_dependency] = previous

    assert response.status_code == 503
    assert "alembic upgrade head" in response.text
    assert "app.seed" in response.text
    assert "Traceback" not in response.text
