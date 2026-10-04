"""Cases built to catch oracle bugs that no catalog scenario exercises."""

from world.builders import WorldBuilder, add_months, days, hours
from world.oracle import extract_facts, label
from world.scenario import Action, Request
from world.scenarios import DANA, MARCUS, RENEWAL, people, team


def ask(b, invoice, after_days):
    request = Request(workspace_id="ws_001", requester_user_id=DANA, received_at=RENEWAL + days(after_days),
                      channel="chat", message="Please refund this.", invoice_id=invoice)
    return b.build(), request


def test_a_promise_already_paid_is_not_paid_again():
    b, inv = team()
    b.ticket("Outage", DANA, RENEWAL + days(1), [
        (DANA, "We want compensation."),
        ("support:Lena Fischer", "I've approved a refund of $40.", {"promise_cents": 4000}),
    ])
    b.refund(inv, RENEWAL + days(3), amount=4000, basis="billing_error")
    b.session(MARCUS, RENEWAL + days(2))
    outcome = label(*ask(b, inv, 15))
    assert (outcome.action, outcome.amount_cents) == (Action.DENY, 0)


def test_a_duplicate_invoice_is_not_an_extra_month_for_goodwill():
    b, inv = team(months=5)
    august = add_months(RENEWAL, -1)
    b.charge(august + hours(0.05), period_start=august)
    b.usage_days(MARCUS, RENEWAL + days(1), 3, every=2)
    world, request = ask(b, inv, 10)
    assert extract_facts(world, request).consecutive_paid_months == 5
    assert label(world, request).action is Action.DENY


def test_a_cancellation_before_signing_up_again_does_not_cover_later_charges():
    b = WorldBuilder(created=add_months(RENEWAL, -10))
    people(b)
    b.monthly_history(add_months(RENEWAL, -8), 3)  # a first subscription, November to January
    cancelled = add_months(RENEWAL, -7) - days(3)
    b.event("cancellation_requested", cancelled, DANA)
    b.event("cancellation_completed", cancelled + hours(0.1))
    inv = b.monthly_history(RENEWAL, 3)  # signed up again in July
    b.session(MARCUS, RENEWAL + days(1))
    outcome = label(*ask(b, inv, 3))
    assert outcome.action is Action.DENY and outcome.section == "5"


def test_cancellation_records_on_opposite_sides_escalate_even_when_the_outcome_is_the_same():
    b, inv = team()  # no Usage, within 7 days: a full refund under either reading
    b.event("cancellation_completed", RENEWAL - hours(0.1))
    b.stripe_cancel(RENEWAL + hours(0.35))
    outcome = label(*ask(b, inv, 3))
    assert outcome.action is Action.ESCALATE and outcome.proposed.amount_cents == 8000
