"""world/oracle.py gives the same answer key as the oracle at commit dbeef84, field for field.

tests/oracle_reference.py is that oracle. Every case here is labeled by both, and the two Outcomes have to be
equal: action, amount, form, section, the sections considered and cited, the rationale, the records, every claim,
and the proposed outcome inside an escalation. The Facts behind the label are compared as well.

The cases are the 80 scenarios, the same scenarios rebuilt from SQL rows, requests and records perturbed from
them, and the worlds that test_policy_invariants.py and test_oracle_regressions.py build. Of the perturbed cases,
a run compares a sample: the first case on each distinct path the reference takes through the policy, and one
case in SAMPLE_EVERY of the rest. To compare all of them:

    ORACLE_SWEEP=full uv run pytest tests/test_oracle_differential.py
"""

import inspect
import json
import os
from datetime import timedelta
from itertools import product

import pytest

import oracle_reference as reference
import test_oracle_regressions
import test_policy_invariants
from world import oracle
from world.scenarios import ALL
from world.schema import Dispute, Refund, TicketMessage
from world.sql import tables, truth_of, with_truth, world_from_rows

FULL_SWEEP = os.environ.get("ORACLE_SWEEP") == "full"
SAMPLE_EVERY = 90
by_scenario = pytest.mark.parametrize("s", ALL, ids=lambda s: s.id)

# Request Times after the scenario's own, chosen to land on both sides of every window the policy names.
SHIFTS = [timedelta(0), timedelta(hours=1), timedelta(days=5), timedelta(days=8), timedelta(days=13, hours=23),
          timedelta(days=20), timedelta(days=31), timedelta(days=95)]
FLAGS = list(product((False, True), repeat=2))  # mentions_legal, reports_offrecord_promise


def facts_of(module, world, request) -> dict:
    """The facts as a dict. `support` lists the same records whether or not a name with none has an entry."""
    facts = module.extract_facts(world, request).model_dump()
    facts["support"] = {name: records for name, records in facts["support"].items() if records}
    return facts


def differences(world, request) -> dict:
    """The fields in which the two labels differ, as (world/oracle.py, reference). Empty when they agree."""
    if facts_of(oracle, world, request) != facts_of(reference, world, request):
        return {"facts": (facts_of(oracle, world, request), facts_of(reference, world, request))}
    got, want = oracle.label(world, request).model_dump(), reference.label(world, request).model_dump()
    return {field: (got[field], want[field]) for field in want if got[field] != want[field]}


def rebuilt(world):
    """The world as it comes back from the database, with the annotations put back."""
    rows = {table: [json.loads(json.dumps(dict(zip(columns, row)), default=str)) for row in rows]
            for table, (columns, rows) in tables(world).items()}
    return with_truth(world_from_rows(rows), truth_of(world))


def perturbed_requests(s):
    """The scenario's world asked about differently: another charge, later, with other flags, by someone else,
    or about two charges at once."""
    w, r = s.world, s.request
    paid = [i.id for i in w.invoices if i.charge_id]
    for invoice, shift, (legal, offrecord) in product(paid, SHIFTS, FLAGS):
        yield f"{invoice} +{shift} legal={legal} offrecord={offrecord}", w, r.model_copy(update={
            "invoice_id": invoice, "also_invoice_ids": [], "received_at": r.received_at + shift,
            "mentions_legal": legal, "reports_offrecord_promise": offrecord})
    for user in [m.id for m in w.members] + ["usr_nobody"]:
        yield f"by {user}", w, r.model_copy(update={"requester_user_id": user})
    for first, second in product(paid[-3:], repeat=2):
        for shift in (timedelta(0), timedelta(days=40)):
            if first != second:
                yield f"{first} and {second} +{shift}", w, r.model_copy(update={
                    "invoice_id": first, "also_invoice_ids": [second], "received_at": r.received_at + shift})


def perturbed_records(s):
    """The scenario's request against records changed in one respect that some rule reads."""
    w, r = s.world, s.request
    charge = next(i for i in w.invoices if i.id == r.invoice_id)
    ws, now = w.workspaces[0], r.received_at

    def variants():
        for reason in ("tos_violation", "nonpayment"):
            suspended = ws.model_copy(update={"status": "suspended", "suspension_reason": reason})
            yield f"suspended for {reason}", {"workspaces": [suspended]}
        for status in ("needs_response", "won", "lost"):
            dispute = Dispute(id="dp_900", charge_id=charge.charge_id, amount=charge.amount_paid,
                              created=now - timedelta(hours=2), reason="product_not_received", status=status)
            yield f"dispute {status}", {"disputes": w.disputes + [dispute]}
        for amount in (1000, charge.amount_paid):
            refund = Refund(id="re_900", charge_id=charge.charge_id, amount=amount, created=now - timedelta(hours=1))
            yield f"refund of {amount}", {"refunds": w.refunds + [refund]}
        goodwill = Refund(id="re_901", charge_id=w.invoices[0].charge_id or charge.charge_id, amount=500,
                          created=now - timedelta(days=100), metadata={"quillstack_basis": "goodwill"})
        yield "goodwill refund 100 days ago", {"refunds": w.refunds + [goodwill]}
        yield "no sessions", {"session_events": []}
        yield "no sessions since the charge", {"session_events": [e for e in w.session_events if e.at < charge.created]}
        for cents in (2500, charge.amount_paid * 3):
            if w.tickets:
                promise = TicketMessage(author_type="support_agent", author_id="agt_lena", author_name="Lena Fischer",
                                        at=now - timedelta(hours=3), body="I've approved a refund.",
                                        truth={"promise_cents": cents})
                ticket = w.tickets[0].model_copy(update={"messages": w.tickets[0].messages + [promise]})
                yield f"promise of {cents}", {"tickets": [ticket] + w.tickets[1:]}
        for offset in (-timedelta(hours=1), timedelta(hours=1)):
            cancelled = w.subscriptions[0].model_copy(update={"status": "canceled", "canceled_at": charge.created + offset})
            yield f"cancelled in Stripe {offset} from the charge", {"subscriptions": [cancelled]}

    for (what, change), shift, (legal, offrecord) in product(variants(), (SHIFTS[0], SHIFTS[3], SHIFTS[7]), FLAGS):
        yield f"{what} +{shift} legal={legal} offrecord={offrecord}", w.model_copy(update=change), r.model_copy(update={
            "received_at": r.received_at + shift, "mentions_legal": legal, "reports_offrecord_promise": offrecord})


def perturbed() -> list[tuple]:
    return [(f"{s.id}: {what}", world, request)
            for s in ALL for what, world, request in [*perturbed_requests(s), *perturbed_records(s)]]


def path(world, request) -> tuple:
    """The route the reference takes: what it decides, the sections on the way, and what an escalation proposes."""
    label = reference.label(world, request)
    proposed = (label.proposed.action, label.proposed.section) if label.proposed else None
    return label.action, label.section, tuple(label.considered), tuple(label.must_cite), proposed


def sample(cases: list[tuple]) -> list[tuple]:
    first_on_path: dict[tuple, int] = {}
    for i, (_, world, request) in enumerate(cases):
        first_on_path.setdefault(path(world, request), i)
    keep = set(first_on_path.values()) | set(range(0, len(cases), SAMPLE_EVERY))
    return [case for i, case in enumerate(cases) if i in keep]


@by_scenario
def test_scenario_gets_the_same_label(s):
    assert not differences(s.world, s.request)


@by_scenario
def test_scenario_rebuilt_from_sql_rows_gets_the_same_label(s):
    assert not differences(rebuilt(s.world), s.request)


def test_perturbed_requests_and_records_get_the_same_label():
    cases = perturbed() if FULL_SWEEP else sample(perturbed())
    differing = {what: found for what, world, request in cases if (found := differences(world, request))}
    assert not differing, f"{len(differing)} of {len(cases)} cases differ; the first: {next(iter(differing.items()))}"


@pytest.mark.parametrize("module", [test_policy_invariants, test_oracle_regressions], ids=lambda m: m.__name__)
def test_worlds_that_other_tests_build_get_the_same_label(module, monkeypatch):
    """Runs the module's own tests with a `label` and an `extract_facts` that also compare with the reference."""
    compared = []

    def label(world, request):
        compared.append(differences(world, request))
        return oracle.label(world, request)

    def extract_facts(world, request):
        compared.append(facts_of(oracle, world, request) != facts_of(reference, world, request))
        return oracle.extract_facts(world, request)

    monkeypatch.setattr(module, "label", label)
    monkeypatch.setattr(module, "extract_facts", extract_facts, raising=False)
    for name, test in inspect.getmembers(module, inspect.isfunction):
        if name.startswith("test_"):
            test()
    assert compared and not any(compared)
