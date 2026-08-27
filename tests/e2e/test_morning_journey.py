"""The analyst's morning, in a real browser.

One long test, deliberately. The claim these defend is not "each page renders"
- the integration suite covers that - but "a person can start with two files
and finish with a resolved worklist, and yesterday's decisions are still there
tomorrow". That claim is sequential, and splitting it into independent tests
would test something weaker.

Ordering matters, so the flow runs as one function with the steps marked. The
focused tests below it cover the failure paths, which are independent.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import DATA, PERIOD_END, PERIOD_START

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Actions, written the way the analyst performs them
# ---------------------------------------------------------------------------


def upload(page: Page, base_url: str, source: str, filename: str) -> None:
    page.goto(base_url)
    page.select_option("select[name=source]", source)
    page.fill("input[name=period_start]", PERIOD_START)
    page.fill("input[name=period_end]", PERIOD_END)
    page.set_input_files("input[name=upload]", str(DATA / filename))
    page.get_by_role("button", name="Load file").click()


def start_run(page: Page, base_url: str, left: str = "ledger", right: str = "statement") -> int:
    page.goto(base_url)
    page.select_option("select[name=left_source]", left)
    page.select_option("select[name=right_source]", right)
    page.get_by_role("button", name="Run reconciliation").click()
    page.wait_for_url(re.compile(r"/runs/\d+"))
    found = re.search(r"/runs/(\d+)", page.url)
    assert found, f"expected to land on a run, got {page.url}"
    return int(found.group(1))


def banner(page: Page) -> str:
    return page.locator(".flash, .message, [role=status]").first.inner_text()


# ---------------------------------------------------------------------------
# The journey
# ---------------------------------------------------------------------------


def test_a_morning_from_two_files_to_a_worked_worklist(page: Page, base_url: str) -> None:
    # --- 1. The files arrive -------------------------------------------------
    upload(page, base_url, "ledger", "ledger_2025-07-01_07.csv")
    expect(page.locator("body")).to_contain_text("40 rows")
    expect(page.locator("body")).to_contain_text("2 could not be read")

    upload(page, base_url, "statement", "statement_2025-07-01_07.csv")
    expect(page.locator("body")).to_contain_text("File accepted as version 1")

    # --- 2. The run ----------------------------------------------------------
    run_id = start_run(page, base_url)
    expect(page.locator("body")).to_contain_text("items need a decision")
    expect(page.locator("body")).to_contain_text(re.compile("break", re.I))
    expect(page.locator("body")).to_contain_text(re.compile("agreed", re.I))

    # --- 3. The worklist, largest difference first ---------------------------
    page.goto(f"{base_url}/runs/{run_id}/worklist")
    first_row = page.locator("tbody tr").first
    expect(first_row).to_contain_text("T-1016")
    expect(page.locator("body")).to_contain_text("could not be read at all")

    # --- 4. A break, field by field -----------------------------------------
    page.get_by_role("link", name="Open").nth(1).click()  # T-1010, the brief's example
    expect(page.locator("body")).to_contain_text("34000.00")
    expect(page.locator("body")).to_contain_text("34170.00")
    expect(page.locator("body")).to_contain_text("0.4975%")
    expect(page.locator("body")).to_contain_text("out of tolerance")
    # Fields that agree are still shown, so the whole record reads in one pass.
    expect(page.locator("body")).to_contain_text("agrees")

    # --- 5. A resolution needs a reason and a name ---------------------------
    page.goto(f"{base_url}/runs/{run_id}/worklist?state=unmatched")
    page.get_by_role("link", name="Open").first.click()
    record_url = page.url

    accept = page.locator("form").filter(has=page.locator("input[value=accept_unmatched]"))
    accept.locator("input[name=reason]").fill("   ")
    accept.locator("input[name=author]").fill("aoife")
    accept.get_by_role("button").click()
    expect(page.locator("body")).to_contain_text("needs a reason")

    # --- 6. Accept it properly ----------------------------------------------
    page.goto(record_url)
    reference = page.locator("h1").inner_text().strip().split()[0]
    accept = page.locator("form").filter(has=page.locator("input[value=accept_unmatched]"))
    accept.locator("input[name=reason]").fill("Counterparty confirmed they have no record of it.")
    accept.locator("input[name=author]").fill("aoife")
    accept.get_by_role("button").click()
    expect(page.locator("body")).to_contain_text("accepted as having no counterpart")

    # --- 7. Pair the suggestion by hand -------------------------------------
    page.goto(f"{base_url}/runs/{run_id}/worklist?state=suggested")
    page.get_by_role("link", name="Open").first.click()
    # The page offers one pairing form per ranked candidate, plus one for
    # entering a reference by hand. Take the first candidate, which is what an
    # analyst confirming the top suggestion would click.
    pair_form = page.locator("form").filter(has=page.locator("input[value=manual_match]")).first
    right_reference = pair_form.locator("input[name=right_reference]").first
    if right_reference.get_attribute("type") != "hidden":
        right_reference.fill("C-91023")
    pair_form.locator("input[name=reason]").first.fill("Same trade under their own reference.")
    pair_form.locator("input[name=author]").first.fill("aoife")
    pair_form.get_by_role("button").first.click()
    expect(page.locator("body")).to_contain_text("Future runs will honour it")

    # --- 8. Tomorrow: the decisions still hold ------------------------------
    second_run = start_run(page, base_url)
    assert second_run != run_id, "a re-run must create a new run, never edit the old one"

    page.goto(f"{base_url}/runs/{second_run}/worklist?state=unmatched")
    expect(page.locator("body")).not_to_contain_text(f">{reference}<")

    page.goto(f"{base_url}/runs/{second_run}/worklist?state=suggested")
    expect(page.locator("tbody tr")).to_have_count(0)


def test_a_correction_lands_and_the_manual_pair_survives_it(page: Page, base_url: str) -> None:
    """Runs after the journey above, against the same seeded server."""
    upload(page, base_url, "statement", "statement_2025-07-01_07_v2.csv")
    expect(page.locator("body")).to_contain_text("Correction accepted as version 2")
    expect(page.locator("body")).to_contain_text("Withdrawn: C-9002")

    run_id = start_run(page, base_url)

    # The brief's own example was corrected, so it must have left the worklist.
    page.goto(f"{base_url}/runs/{run_id}/worklist?state=break")
    expect(page.locator("tbody")).not_to_contain_text("T-1010")

    # And the pairing a person made survives a correction that replaced every
    # record row beneath it.
    page.goto(f"{base_url}/runs/{run_id}/worklist?state=suggested")
    expect(page.locator("tbody tr")).to_have_count(0)

    # The previous values are still answerable.
    page.goto(f"{base_url}/records/statement/T-1010/history")
    expect(page.locator("body")).to_contain_text("34170.00")
    expect(page.locator("body")).to_contain_text("34000.00")
    expect(page.locator("body")).to_contain_text("current")


def test_an_acceptance_is_revoked_when_the_counterparty_books_late(
    page: Page, base_url: str
) -> None:
    """The one place the system overrides a person, and it must say so."""
    upload(page, base_url, "statement", "statement_2025-07-01_07_v3.csv")
    run_id = start_run(page, base_url)

    page.goto(f"{base_url}/runs/{run_id}")
    expect(page.locator("body")).to_contain_text(re.compile("accepted unmatched", re.I))

    page.goto(f"{base_url}/records/ledger/T-1012/history")
    body = page.locator("body")
    expect(body).to_contain_text("T-1012")
    # The decision is recorded as revoked, not deleted (TR-708).
    expect(body).to_contain_text(re.compile("revok", re.I))


# ---------------------------------------------------------------------------
# Failure paths - independent of the journey
# ---------------------------------------------------------------------------


def test_a_byte_identical_resend_is_refused_with_a_message(page: Page, base_url: str) -> None:
    upload(page, base_url, "ledger", "ledger_2025-07-01_07.csv")
    expect(page.locator("body")).to_contain_text("Already accepted")
    expect(page.locator("body")).to_contain_text("Nothing has changed")


def test_a_partly_overlapping_period_is_refused(page: Page, base_url: str) -> None:
    page.goto(base_url)
    page.select_option("select[name=source]", "ledger")
    page.fill("input[name=period_start]", "2025-07-05")
    page.fill("input[name=period_end]", "2025-07-10")
    page.set_input_files("input[name=upload]", str(DATA / "statement_2025-07-01_07.csv"))
    page.get_by_role("button", name="Load file").click()
    expect(page.locator("body")).to_contain_text("overlaps")


def test_no_expected_failure_produces_a_server_error(page: Page, base_url: str) -> None:
    """TR-605. A missing run is a page, not a traceback."""
    responses: list[int] = []
    page.on(
        "response", lambda r: responses.append(r.status) if r.url.startswith(base_url) else None
    )

    for path in ("/runs/999999", "/runs/1/pairs/999999", "/records/ledger/NOPE/history"):
        page.goto(f"{base_url}{path}")
        expect(page.locator("body")).not_to_contain_text("Traceback")

    assert not [s for s in responses if s >= 500], f"a 5xx was served: {responses}"


def test_a_third_source_reconciles_with_no_code_change(page: Page, base_url: str) -> None:
    """AC12, through the same screens the other two use."""
    upload(page, base_url, "venue_c", "venue_c_2025-07-01_07.csv")
    expect(page.locator("body")).to_contain_text("File accepted")

    run_id = start_run(page, base_url, left="ledger", right="venue_c")
    page.goto(f"{base_url}/runs/{run_id}")
    expect(page.locator("body")).to_contain_text("items need a decision")
