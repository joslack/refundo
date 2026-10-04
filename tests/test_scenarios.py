"""Every scenario's intended outcome must match what the oracle computes from its world."""

import pytest

from world.oracle import label
from world.scenarios import ALL


def test_ids_are_unique():
    ids = [s.id for s in ALL]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_oracle_matches_intent(scenario):
    got, want = label(scenario.world, scenario.request), scenario.expected
    assert (got.action, got.amount_cents, got.section) == (want.action, want.amount_cents, want.section), got.rationale
    if want.proposed:
        assert got.proposed.amount_cents == want.proposed.amount_cents, got.proposed.rationale


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_no_record_is_dated_after_the_request(scenario):
    w, now = scenario.world, scenario.request.received_at
    stamps = [r.created for r in w.invoices + w.refunds + w.credits + w.disputes]
    stamps += [r.at for r in w.session_events + w.app_events]
    stamps += [m.at for t in w.tickets for m in t.messages]
    assert max(stamps) <= now


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_every_claim_cites_records_that_exist(scenario):
    w = scenario.world
    ids = {r.id for group in (w.workspaces, w.members, w.subscriptions, w.invoices, w.refunds, w.credits,
                              w.disputes, w.app_events, w.tickets) for r in group}
    ids |= {e.session_id for e in w.session_events} | {"request"}
    outcome = label(scenario.world, scenario.request)
    assert any(claim.kind == "decisive" for claim in outcome.evidence)
    for claim in outcome.evidence:
        assert claim.records, claim.claim
        for record in claim.records:
            assert record in ids or record.startswith("source:"), (claim.claim, record)
