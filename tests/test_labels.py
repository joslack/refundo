"""The hand labels: one schema, agreement with the oracle, and corrections that keep the original."""

import pytest

from world.labeling import (ACTIONS, CORRECTION_FIELDS, FIELDS, LABELS, ORIGINAL_FIELDS, SECTIONS, SOURCES, agrees,
                            read_rows, sections_agree, uncovered)
from world.oracle import label as oracle_label
from world.scenario import Action, Outcome
from world.scenarios import ALL

SCENARIOS = {s.id: s for s in ALL}
ROWS = read_rows(LABELS / "jonah.jsonl")
by_case = pytest.mark.parametrize("row", ROWS, ids=lambda r: r["case_id"])


def record_ids(s) -> set[str]:
    w = s.world
    groups = [w.members, w.workspaces, w.invoices, w.refunds, w.credits, w.disputes, w.subscriptions, w.app_events,
              w.tickets]
    return {"request"} | {x.id for g in groups for x in g} | {e.session_id for e in w.session_events}


def test_one_row_per_case():
    ids = [r["case_id"] for r in ROWS]
    assert len(ids) == len(set(ids)) and set(ids) <= set(SCENARIOS)


@by_case
def test_row_has_the_one_schema(row):
    assert set(row) in (FIELDS, FIELDS | CORRECTION_FIELDS)
    assert row["action"] in ACTIONS and row["sections"] and set(row["sections"]) <= set(SECTIONS)
    assert set(row["sources"]) <= set(SOURCES)
    assert set(row["records"]) <= record_ids(SCENARIOS[row["case_id"]])


@by_case
def test_label_agrees_with_the_oracle(row):
    s = SCENARIOS[row["case_id"]]
    assert agrees(row, oracle_label(s.world, s.request))


@pytest.mark.parametrize("row", [r for r in ROWS if "corrected_from" in r], ids=lambda r: r["case_id"])
def test_corrected_label_keeps_the_original_and_supports_itself(row):
    s, first = SCENARIOS[row["case_id"]], row["corrected_from"]
    assert set(first) == ORIGINAL_FIELDS and first["action"] in ACTIONS and set(first["sections"]) <= set(SECTIONS)
    assert row["correction_reason"]
    assert any(row[k] != first[k] for k in ("action", "amount_cents", "sections"))
    assert not uncovered(row, oracle_label(s.world, s.request))


def outcome(action, section, considered, must_cite=None, amount=0) -> Outcome:
    return Outcome(action=action, amount_cents=amount, section=section, considered=considered,
                   must_cite=must_cite or [section], rationale="")


MONTHLY_DENIAL = outcome(Action.DENY, "5", ["3", "4", "5", "9", "11"], must_cite=["5", "9"])
AFTER_CANCELLATION = outcome(Action.REFUND, "4.2", ["3", "4", "4.2", "11"], amount=8000)


def test_a_monthly_denial_needs_both_sections():
    assert sections_agree(["5", "9"], MONTHLY_DENIAL)
    assert not sections_agree(["9"], MONTHLY_DENIAL)
    assert not sections_agree(["5"], MONTHLY_DENIAL)


def test_a_case_of_section_4_that_was_not_looked_at_is_off_the_path():
    assert not sections_agree(["4.4", "5", "9"], MONTHLY_DENIAL)
    assert sections_agree(["4", "5", "9"], MONTHLY_DENIAL)


def test_the_general_section_stands_for_its_case_but_another_case_does_not():
    assert sections_agree(["4"], AFTER_CANCELLATION)
    assert sections_agree(["4.2", "10"], AFTER_CANCELLATION)
    assert not sections_agree(["4.1"], AFTER_CANCELLATION)
    assert not sections_agree(["3"], AFTER_CANCELLATION)
