"""Counterexamples built straight from the policy text, independent of any scenario's expected outcome.

Each test constructs a small world, states what the written policy requires, and checks the oracle.
"""

from world.builders import WorldBuilder, add_months, days, hours
from world.oracle import label
from world.scenario import Action, Request
from world.scenarios import DANA, MARCUS, PRIYA, RENEWAL, TOM, people, team
from world.schema import Interval, Role


def ask(b, invoice, after_days, by=DANA, **flags):
    request = Request(workspace_id="ws_001", requester_user_id=by, received_at=RENEWAL + days(after_days),
                      channel="chat", message="Please refund this.", invoice_id=invoice, **flags)
    return label(b.build(), request)


def demote_or_promote(b, user_id, at, old, new):
    next(m for m in b.world.members if m.id == user_id).role = new
    b.event("member_role_changed", at, DANA, user=user_id, old_role=old.value, new_role=new.value)


def test_an_action_before_the_login_is_not_usage():
    # §2: an Active Session is a login FOLLOWED BY a create, edit or export.
    b, inv = team()
    b.session(TOM, RENEWAL + days(1), ("edit", "login"))
    outcome = ask(b, inv, 3)
    assert (outcome.action, outcome.section) == (Action.REFUND, "5")


def test_a_cancellation_by_a_billing_admin_still_counts_after_they_are_demoted():
    # §2: the request must be submitted by the Owner or a Billing Admin. That is their role when they submit it.
    b, inv = team()
    b.event("cancellation_requested", RENEWAL - days(2), PRIYA)
    b.event("cancellation_failed", RENEWAL - days(2) + hours(0.02), PRIYA, error="billing_provider_timeout")
    demote_or_promote(b, PRIYA, RENEWAL + days(1), Role.BILLING_ADMIN, Role.MEMBER)
    b.session(TOM, RENEWAL + days(1))
    outcome = ask(b, inv, 3)
    assert (outcome.action, outcome.section) == (Action.REFUND, "4.2")


def test_a_cancellation_by_a_member_does_not_count_after_they_are_promoted():
    b, inv = team()
    b.event("cancellation_requested", RENEWAL - days(2), MARCUS)
    b.event("cancellation_failed", RENEWAL - days(2) + hours(0.02), MARCUS, error="insufficient_permissions")
    demote_or_promote(b, MARCUS, RENEWAL + days(1), Role.MEMBER, Role.BILLING_ADMIN)
    b.session(TOM, RENEWAL + days(1))
    outcome = ask(b, inv, 3)
    assert (outcome.action, outcome.section) == (Action.DENY, "5")


def promise_world():
    b, inv = team()
    ticket = b.ticket("Outage", DANA, RENEWAL + days(1), [
        (DANA, "We want compensation."),
        ("support:Lena Fischer", "I've approved a refund of $40.", {"promise_cents": 4000}),
    ])
    b.session(MARCUS, RENEWAL + days(2))
    return b, inv, ticket


def test_a_refund_on_another_charge_does_not_pay_off_a_written_promise():
    b, inv, _ = promise_world()
    august = b.world.invoices[-2].id
    b.refund(august, RENEWAL + days(3), amount=4000, basis="billing_error")   # unrelated to the promise
    outcome = ask(b, inv, 15)
    assert (outcome.action, outcome.amount_cents) == (Action.PARTIAL_REFUND, 4000)


def test_a_refund_issued_for_the_promise_pays_it_off():
    b, inv, ticket = promise_world()
    b.refund(inv, RENEWAL + days(3), amount=4000, basis="billing_error", ticket=ticket)
    outcome = ask(b, inv, 15)
    assert (outcome.action, outcome.amount_cents) == (Action.DENY, 0)


def test_an_off_ticket_promise_escalates_even_when_a_ticket_also_holds_one():
    # §11: a commitment the records can neither confirm nor rule out goes to a specialist.
    b, inv, _ = promise_world()
    outcome = ask(b, inv, 15, reports_offrecord_promise=True)
    assert outcome.action is Action.ESCALATE and outcome.proposed.amount_cents == 4000


def test_a_seat_overcharge_is_the_price_of_the_extra_seats_not_a_share_of_what_was_paid():
    # §10: Amount Paid minus what the charge should have been, with the same discounts and credits.
    b = WorldBuilder(created=add_months(RENEWAL, -3))
    people(b)
    b.monthly_history(add_months(RENEWAL, -1), 3)
    b.credit(RENEWAL - hours(1), 2000, "Referral credit")
    inv = b.charge(RENEWAL, seats=6, credit_applied=2000)       # 6 x $20 - $20 credit = $100; should be 4 x $20 - $20 = $60
    outcome = ask(b, inv, 5)
    assert (outcome.action, outcome.amount_cents, outcome.section) == (Action.PARTIAL_REFUND, 4000, "4.3")


def test_goodwill_needs_the_workspace_to_be_on_a_monthly_plan_now():
    # §9's first condition is about the Workspace, not about the charge.
    b, inv = team(months=11)
    b.usage_days(MARCUS, RENEWAL + days(1), 3, every=2)
    b.plan_change(RENEWAL + days(12), interval=Interval.YEAR, actor=DANA)
    b.charge(RENEWAL + days(12), reason="subscription_update")
    outcome = ask(b, inv, 15)
    assert (outcome.action, outcome.section) == (Action.DENY, "5")
