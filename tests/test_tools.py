"""The structured tools and the case file work out the policy's terms the same way the oracle does.

A tool that reported a different Amount Paid, Usage, role or elapsed time from the answer key would send every
agent wrong in the same way, and the scores would blame the agent. So each term is checked against the oracle's
facts on all 80 scenarios.
"""

import importlib.util
import json
from datetime import timedelta
from math import ceil
from pathlib import Path

import pytest

from world.facts import within
from world.oracle import extract_facts
from world.scenarios import ALL
from world.sql import tables

# Loaded by path: the folder is named `mcp`, and importing it as a package would hide the MCP SDK.
_spec = importlib.util.spec_from_file_location("quillstack_server", Path(__file__).parents[1] / "evals/environment/mcp/server.py")
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


def records(world) -> dict[str, list[dict]]:
    """The rows as the server reads them from the database: every value as JSON."""
    return {name: [json.loads(json.dumps(dict(zip(columns, row)), default=str)) for row in rows]
            for name, (columns, rows) in tables(world).items()}


def charge_in_question(scenario) -> tuple[dict, dict]:
    rows = records(scenario.world)
    charges = server.charge_views(rows["invoices"], rows["invoice_lines"], rows["refunds"], rows["disputes"])
    facts = extract_facts(scenario.world, scenario.request)
    return next(c for c in charges if c["invoice_id"] == facts.invoice_id), rows


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_charge_matches_the_oracle(scenario):
    facts = extract_facts(scenario.world, scenario.request)
    charge, _ = charge_in_question(scenario)
    assert charge["amount_paid_cents"] == facts.amount_paid
    assert charge["billing_period_days"] == facts.period_days
    assert charge["refunded_cents"] == facts.already_refunded
    assert bool(charge["disputes"]) == (facts.dispute != "none")


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_usage_matches_the_oracle(scenario):
    facts = extract_facts(scenario.world, scenario.request)
    charge, rows = charge_in_question(scenario)
    now = str(scenario.request.received_at)
    since_charge = server.usage_view(rows["session_events"], charge["charged_at"], now)
    assert bool(since_charge["sessions_with_usage"]) == facts.usage_since_charge
    period_end = min(charge["billing_period_end"], now, key=server.when)
    in_period = server.usage_view(rows["session_events"], charge["billing_period_start"], period_end)
    assert len(in_period["days_with_usage"]) == facts.usage_days_in_period


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_requester_role_matches_the_oracle(scenario):
    facts = extract_facts(scenario.world, scenario.request)
    rows = records(scenario.world)
    member = next((m for m in rows["members"] if m["id"] == scenario.request.requester_user_id), None)
    changes = [e for e in rows["app_events"] if e["type"] == "member_role_changed"]
    role = server.role_then(member, changes, str(scenario.request.received_at))
    assert (role in {"owner", "billing_admin"}) == facts.authorized


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_case_file_timing_matches_the_oracle(scenario):
    facts = extract_facts(scenario.world, scenario.request)
    charge, rows = charge_in_question(scenario)
    timing = server.charge_timing(charge, rows["session_events"], str(scenario.request.received_at))
    assert bool(timing["usage_since_charge"]["sessions_with_usage"]) == facts.usage_since_charge
    assert timing["days_with_usage_in_billing_period"] == facts.usage_days_in_period
    elapsed = timing["time_from_charge_to_request"]
    assert elapsed["whole_days_rounded_up"] == ceil((facts.now - facts.charged_at) / timedelta(days=1))
    for days in (7, 14, 30, 90):  # every window the policy names
        assert (0 <= elapsed["hours"] <= days * 24) == within(facts, days)
