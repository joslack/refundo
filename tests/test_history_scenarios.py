"""The scenarios that turn on an account's history get the answer each was written to have.

world/history_scenarios.py states the outcome every row intends, worked out by hand from the policy. The oracle
has to reach the same one, on the long account.
"""

import pytest

from world.history_scenarios import HISTORY
from world.oracle import label
from world.scenarios import ALL


def test_ids_are_unique_and_new():
    ids = [s.id for s in HISTORY]
    assert len(ids) == len(set(ids))
    assert not set(ids) & {s.id for s in ALL}


def test_the_catalog_list_is_left_as_it_was():
    assert len(ALL) == 80


@pytest.mark.parametrize("scenario", HISTORY, ids=lambda s: s.id)
def test_oracle_matches_intent(scenario):
    got, want = label(scenario.world, scenario.request), scenario.expected
    assert (got.action, got.amount_cents, got.section) == (want.action, want.amount_cents, want.section), got.rationale
    assert (got.proposed.amount_cents if got.proposed else None) == (want.proposed.amount_cents if want.proposed else None)


@pytest.mark.parametrize("scenario", HISTORY, ids=lambda s: s.id)
def test_the_account_is_long(scenario):
    assert len(scenario.world.invoices) > 60 and len(scenario.world.tickets) > 100


@pytest.mark.parametrize("scenario", HISTORY, ids=lambda s: s.id)
def test_no_record_is_dated_after_the_request(scenario):
    world, now = scenario.world, scenario.request.received_at
    assert all(i.created <= now for i in world.invoices)
    assert all(r.created <= now for r in world.refunds)
    assert all(m.at <= now for t in world.tickets for m in t.messages)
    assert all(e.at <= now for e in world.app_events)
