"""An account given years of earlier history has the same right answer, and the earlier years hold nothing owed.

world/history.py puts an earlier subscription in front of each scenario's records. If that changed what the policy
requires, a large account would be a different case with an unchecked answer, and the hand labels would not apply
to it. So the oracle is asked on every scenario, and the tools are checked against it as they are without history.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from world.history import with_history
from world.oracle import extract_facts, label
from world.scenario import Action
from world.scenarios import ALL
from world.sql import tables

# Loaded by path: the folder is named `mcp`, and importing it as a package would hide the MCP SDK.
_spec = importlib.util.spec_from_file_location("quillstack_server", Path(__file__).parents[1] / "evals/environment/mcp/server.py")
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)

GRANTS = {Action.REFUND, Action.PARTIAL_REFUND, Action.CREDIT}
LONG = {s.id: with_history(s) for s in ALL}


def answer(outcome) -> tuple:
    proposed = outcome.proposed
    return (outcome.action, outcome.amount_cents, outcome.section, tuple(outcome.must_cite),
            proposed and (proposed.action, proposed.amount_cents))


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_the_answer_is_the_same_with_history(scenario):
    long = LONG[scenario.id]
    assert answer(label(long.world, long.request)) == answer(label(scenario.world, scenario.request))


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_no_earlier_charge_is_owed_anything(scenario):
    long = LONG[scenario.id]
    first = min(i.created for i in scenario.world.invoices)
    for invoice in long.world.invoices:
        if invoice.created < first and invoice.status == "paid":
            about_it = long.request.model_copy(update={"invoice_id": invoice.id, "also_invoice_ids": []})
            assert label(long.world, about_it).action not in GRANTS, invoice.id


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_the_tools_match_the_oracle_with_history(scenario):
    long = LONG[scenario.id]
    facts = extract_facts(long.world, long.request)
    rows = {name: [json.loads(json.dumps(dict(zip(columns, row)), default=str)) for row in found]
            for name, (columns, found) in tables(long.world).items()}
    charges = server.charge_views(rows["invoices"], rows["invoice_lines"], rows["refunds"], rows["disputes"])
    charge = next(c for c in charges if c["invoice_id"] == facts.invoice_id)
    timing = server.charge_timing(charge, rows["session_events"], str(long.request.received_at))
    assert charge["amount_paid_cents"] == facts.amount_paid
    assert charge["refunded_cents"] == facts.already_refunded
    assert bool(timing["usage_since_charge"]["sessions_with_usage"]) == facts.usage_since_charge
    assert timing["days_with_usage_in_billing_period"] == facts.usage_days_in_period
    member = next((m for m in rows["members"] if m["id"] == long.request.requester_user_id), None)
    changes = [e for e in rows["app_events"] if e["type"] == "member_role_changed"]
    role = server.role_then(member, changes, str(long.request.received_at))
    assert (role in {"owner", "billing_admin"}) == facts.authorized


def charges_of(scenario) -> list[dict]:
    rows = {name: [json.loads(json.dumps(dict(zip(columns, row)), default=str)) for row in found]
            for name, (columns, found) in tables(scenario.world).items()}
    return server.charge_views(rows["invoices"], rows["invoice_lines"], rows["refunds"], rows["disputes"])


def test_a_read_with_no_range_returns_the_newest_and_counts_the_rest():
    charges = charges_of(LONG[ALL[0].id])
    kept, counts = server.narrowed(charges, "charged_at", None, None, 24)
    assert kept == charges[:24]  # charges come newest first, and stay in that order
    assert counts == {"returned": 24, "matched_but_older_than_these": len(charges) - 24, "on_the_account_in_all": len(charges)}


def test_a_range_selects_by_date_and_a_bare_date_is_read_as_utc():
    charges = charges_of(LONG[ALL[0].id])
    kept, counts = server.narrowed(charges, "charged_at", "2023-01-01", "2023-12-31T23:59:59+00:00", 24)
    assert kept and all(c["charged_at"].startswith("2023") for c in kept)
    assert counts["returned"] == len(kept) == sum(c["charged_at"].startswith("2023") for c in charges)
    assert server.bad_range("last March", None) and not server.bad_range("2023-01-01", None)


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_a_short_account_is_returned_whole_by_default(scenario):
    """The limits a read has with no arguments leave nothing out of an account without the earlier years."""
    assert len(scenario.world.invoices) <= 36 and len(scenario.world.tickets) <= 10 and len(scenario.world.credits) <= 24


def test_a_sql_result_over_its_size_returns_no_rows_and_says_how_much_was_asked_for():
    charges = charges_of(LONG[ALL[0].id])
    refused = server.sql_result(charges)
    assert set(refused) == {"too_large", "rows_matched", "characters"}
    assert refused["rows_matched"] == len(charges) and refused["characters"] > server.RESULT_BUDGET
    few = [{"id": c["invoice_id"], "paid": c["amount_paid_cents"]} for c in charges]
    assert server.sql_result(few) == few  # the same rows with two columns fit
    many = [{"n": n} for n in range(server.ROW_LIMIT + 50)]
    cut = server.sql_result(many)
    assert cut["rows"] == many[:server.ROW_LIMIT] and cut["rows_matched"] == len(many)


def test_the_history_is_the_same_every_time():
    assert with_history(ALL[0]).world == with_history(ALL[0]).world


def test_an_id_does_not_show_which_records_were_added():
    long = LONG[ALL[0].id]
    ids = [r.id for r in (*long.world.invoices, *long.world.tickets, *long.world.app_events, *long.world.session_events)]
    assert all(i.split("_")[1].isdigit() for i in ids)
    assert [i.id for i in long.world.invoices] == sorted(i.id for i in long.world.invoices)
