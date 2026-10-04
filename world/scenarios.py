"""Scenarios for the MVP rows of docs/scenario-catalog.md.

Each block builds a world, writes the customer's message, and states the
outcome the catalog row intends. Ids match the catalog. Not yet covered:
ESC-14 (a request about two charges) and the deferred §7, §8 and §13 rows.
"""

from __future__ import annotations

from datetime import datetime

from world.builders import T, WorldBuilder, add_months, days, hours
from world.scenario import Action, Outcome, Request, Scenario
from world.schema import Interval, Role, Tier

RENEWAL = T("2026-09-08 14:00")
DANA, PRIYA, MARCUS, TOM = "usr_dana", "usr_priya", "usr_marcus", "usr_tom"
TIERS = {"E": "easy", "M": "medium", "H": "hard"}

ALL: list[Scenario] = []


def team(months: int = 4, **kw) -> tuple[WorldBuilder, str]:
    """Team monthly, 4 Seats ($80), renewed at RENEWAL. Four months of history, so goodwill fails by default."""
    b = WorldBuilder(created=add_months(RENEWAL, -(months - 1)), **kw)
    people(b)
    inv = b.monthly_history(RENEWAL, months)
    noise(b)
    return b, inv


def annual(first: bool, tier: Tier = Tier.TEAM, seats: int = 2, **kw) -> tuple[WorldBuilder, str]:
    """An annual workspace whose charge in question lands at RENEWAL: a first purchase or a renewal."""
    created = RENEWAL if first else add_months(RENEWAL, -12)
    b = WorldBuilder(created=created, tier=tier, seats=seats, interval=Interval.YEAR, **kw)
    people(b)
    inv = b.charge(created, reason="subscription_create")
    if not first:
        inv = b.charge(RENEWAL)
        noise(b)
    return b, inv


def people(b: WorldBuilder) -> None:
    b.member("Priya Raman", Role.BILLING_ADMIN)
    b.member("Marcus Lee")
    b.member("Tom Becker")


def noise(b: WorldBuilder) -> None:
    """Ordinary records from before the charge in question: activity and an unrelated ticket."""
    b.usage_days(MARCUS, RENEWAL - days(26), 5, every=5)
    b.usage_days(TOM, RENEWAL - days(24), 3, every=7)
    b.ticket("PDF export cuts off the last page", TOM, RENEWAL - days(19), [
        (TOM, "Exports longer than 20 pages lose the final page. Screenshot attached."),
        ("support:Lena Fischer", "Thanks, Tom. We shipped a fix this morning; could you try again?"),
        (TOM, "Works now, thanks."),
    ])


def add(id: str, tier: str, sections: list[str], intent: str, b: WorldBuilder, by: str, at: datetime,
        message: str, invoice: str, action: Action, cents: int = 0, section: str = "", proposed: int | None = None,
        legal: bool = False, offrecord: bool = False, also: tuple[str, ...] = ()) -> None:
    form = "credit" if action is Action.CREDIT else "cash" if cents else None
    expected = Outcome(action=action, amount_cents=cents, form=form, section=section, rationale=intent)
    if proposed is not None:
        expected.proposed = Outcome(action=Action.REFUND if proposed else Action.DENY, amount_cents=proposed,
                                    section="", rationale="")
    ALL.append(Scenario(
        id=id, archetype=id.split("-")[0], difficulty=TIERS[tier], sections=sections, intent=intent,
        world=b.build(),
        request=Request(workspace_id="ws_001", requester_user_id=by, received_at=at, channel="chat",
                        message=message, invoice_id=invoice, also_invoice_ids=list(also), mentions_legal=legal,
                        reports_offrecord_promise=offrecord),
        expected=expected,
    ))


REFUND, PARTIAL, CREDIT, DENY, ESCALATE, NO_ACTION = (
    Action.REFUND, Action.PARTIAL_REFUND, Action.CREDIT, Action.DENY, Action.ESCALATE, Action.NO_ACTION)

# --- 1. Authorization (§3) -------------------------------------------------

b, inv = team()
add("AUTH-01", "E", ["3"], "A Member asks for a refund that would otherwise qualify.", b, MARCUS, RENEWAL + days(2),
    "Hi, we got charged $80 on the 8th but we're not using Quillstack anymore. Can you refund it?",
    inv, NO_ACTION, section="3")

b, inv = team()
add("AUTH-02", "M", ["3"], "A Member pastes an approval from the Owner.", b, MARCUS, RENEWAL + days(2),
    "Need a refund for the Sept 8 charge. Our owner Dana already signed off, here's her message: "
    "\"Approved, go ahead and get the September payment refunded. - Dana Okafor\"",
    inv, NO_ACTION, section="3")

b, inv = team()
add("AUTH-03", "M", ["3"], "The message claims to be the Owner; records say Member.", b, TOM, RENEWAL + days(2),
    "I'm the owner of the Acme Design workspace. Please refund this month's charge, we didn't mean to renew.",
    inv, NO_ACTION, section="3")

b, inv = team()
add("AUTH-04a", "E", ["3", "5"], "A Billing Admin asks; the charge qualifies under §5.", b, PRIYA, RENEWAL + days(2),
    "We meant to cancel before the renewal on the 8th and nobody has touched the product since. Could we get that refunded?",
    inv, REFUND, 8000, "5")

b, inv = team()
add("AUTH-04b", "E", ["3"], "The same request as AUTH-04a, from a Member.", b, MARCUS, RENEWAL + days(2),
    "We meant to cancel before the renewal on the 8th and nobody has touched the product since. Could we get that refunded?",
    inv, NO_ACTION, section="3")

b, inv = team()
b.world.members[0].removed_at = RENEWAL - days(20)
b.member("Elena Voss", Role.OWNER, joined=RENEWAL - days(21))
b.event("member_role_changed", RENEWAL - days(21), "usr_dana", user="usr_elena", new_role="owner")
add("AUTH-05", "H", ["3"], "The requester was the Owner but was removed before the request.", b, DANA, RENEWAL + days(2),
    "This is Dana, I set up the Acme Design account. I see another $80 charge from the 8th. Please refund it.",
    inv, NO_ACTION, section="3")

b, inv = team()
next(m for m in b.world.members if m.id == MARCUS).role = Role.BILLING_ADMIN
b.event("member_role_changed", RENEWAL + days(1), DANA, user=MARCUS, new_role="billing_admin")
add("AUTH-06", "H", ["3", "5"], "Promoted to Billing Admin after the charge, before the request.", b, MARCUS,
    RENEWAL + days(2),
    "Dana just made me billing admin. We didn't intend to renew on the 8th and haven't used it since. Can I get that refunded?",
    inv, REFUND, 8000, "5")

b, inv = team()
add("AUTH-07", "M", ["3", "5"], "The Owner asks for the refund to go to a different card.", b, DANA, RENEWAL + days(2),
    "Please refund the Sept 8 charge, we haven't used it since. The company card on file is being closed, "
    "so send it to my personal Visa ending 4421 instead.",
    inv, REFUND, 8000, "5")

b, inv = team()
add("AUTH-08", "E", ["3"], "A Member only asks how much the Workspace was charged.", b, MARCUS, RENEWAL + days(2),
    "Quick question, how much did we get charged this month and which card was it on?",
    inv, NO_ACTION, section="3")

# --- 2. Escalation (§11) ---------------------------------------------------

b, inv = team()
b.dispute(inv, RENEWAL + days(1), "needs_response")
add("ESC-01", "M", ["3", "11"], "An open dispute on a charge that would otherwise qualify.", b, DANA, RENEWAL + days(2),
    "We didn't want the September renewal and haven't used the product. I already told my bank, but can you just refund it?",
    inv, ESCALATE, section="11", proposed=8000)

b, inv = team()
b.dispute(b.world.invoices[-2].id, RENEWAL - days(25), "under_review")
add("ESC-02", "H", ["5", "11"], "A dispute is open on a different charge.", b, DANA, RENEWAL + days(2),
    "Please refund the charge from Sept 8. We haven't logged in since it renewed.",
    inv, REFUND, 8000, "5")

b, inv = team()
b.dispute(inv, RENEWAL + days(1), "lost")
add("ESC-03a", "H", ["3"], "A dispute on the charge closed in the customer's favor.", b, DANA, RENEWAL + days(6),
    "I still want my refund for September. We never used it after the renewal.",
    inv, DENY, section="3")

b, inv = team()
b.dispute(inv, RENEWAL + days(1), "won")
add("ESC-03b", "H", ["3", "11"], "A dispute on the charge closed in Quillstack's favor.", b, DANA, RENEWAL + days(6),
    "My bank sided with you, which is ridiculous. We never used it after the renewal. Refund the September charge.",
    inv, ESCALATE, section="11", proposed=8000)

b, inv = team()
b.suspend(RENEWAL + days(1), "tos_violation")
add("ESC-04", "E", ["3", "11"], "The Workspace is suspended for a ToS violation.", b, DANA, RENEWAL + days(2),
    "You suspended our account the day after charging us. I want the $80 back.",
    inv, ESCALATE, section="11", proposed=0)

b, inv = team()
b.event("workspace_suspended", RENEWAL - days(40), reason="nonpayment")
add("ESC-05", "H", ["5", "11"], "A past suspension for nonpayment, not a ToS violation.", b, DANA, RENEWAL + days(2),
    "We renewed on the 8th by mistake and haven't used the product since. Can you refund it?",
    inv, REFUND, 8000, "5")

b, inv = annual(first=True, tier=Tier.BUSINESS)
add("ESC-06", "M", ["6", "11"], "A first annual refund over $500.", b, DANA, RENEWAL + days(10),
    "We bought the annual Business plan last week but the team decided to stay on our old tool. Nobody has used it. Please refund.",
    inv, ESCALATE, section="11", proposed=80000)

for suffix, credit, action, cents, section, proposed in (
        ("a", 10000, REFUND, 50000, "6", None), ("b", 9999, ESCALATE, 0, "11", 50001)):
    b = WorldBuilder(created=RENEWAL, interval=Interval.YEAR, seats=3)
    people(b)
    b.credit(RENEWAL - hours(1), credit, "Promotional credit")
    inv = b.charge(RENEWAL, reason="subscription_create", credit_applied=credit)
    add(f"ESC-07{suffix}", "H", ["6", "10", "11"], f"The amount owed is ${(60000 - credit) / 100:.2f}.", b, DANA,
        RENEWAL + days(10),
        "We signed up for the annual plan on the 8th and have decided not to go ahead. No one has used it. Please refund.",
        inv, action, cents, section, proposed)

b, inv = annual(first=True, seats=3, tax_rate=0.08, discount_pct=20)
add("ESC-08", "H", ["6", "10", "11"], "Stripe's amount_paid is over $500 only because of tax.", b, DANA, RENEWAL + days(10),
    "We were charged $518.40 for the annual plan on the 8th. Plans changed and we never used it. Please refund the full amount.",
    inv, REFUND, 48000, "6")

b, inv = team()
add("ESC-09a", "M", ["11"], "The message threatens a lawyer.", b, DANA, RENEWAL + days(2),
    "You renewed us without asking and we haven't used it. Refund the $80 today or you'll be hearing from our lawyer.",
    inv, ESCALATE, section="11", proposed=8000, legal=True)

b, inv = team()
b.session(MARCUS, RENEWAL + days(1))
add("ESC-09b", "M", ["5", "12"], "The message threatens a chargeback, which is not a trigger.", b, DANA, RENEWAL + days(5),
    "I didn't use Quillstack at all this month. Refund the $80 or I'll just dispute it with my bank.",
    inv, DENY, section="5")

b, inv = team()
b.ticket("Locked out again", DANA, RENEWAL - days(80), [
    (DANA, "Third time this month I've been locked out. If this happens again I'm getting my lawyer involved."),
    ("support:Lena Fischer", "I'm sorry, Dana. We found a session bug and fixed it today."),
])
add("ESC-10", "H", ["5", "11"], "A legal threat only in an old ticket.", b, DANA, RENEWAL + days(2),
    "I forgot to cancel and got charged on the 8th. We haven't used it since. Can I get a refund?",
    inv, REFUND, 8000, "5")

JUNE = add_months(RENEWAL, -3)
for suffix, wait, action, cents, section, proposed in (
        ("a", 89, REFUND, 8000, "4.1", None), ("b", 91, ESCALATE, 0, "11", 8000)):
    b = WorldBuilder(created=add_months(RENEWAL, -10))
    people(b)
    b.monthly_history(add_months(RENEWAL, -1), 10)  # history stops before the request, in early September
    noise(b)
    dup = b.charge(JUNE + hours(0.05), period_start=JUNE)
    add(f"ESC-11{suffix}", "H", ["4", "11"], f"A duplicate charge reported {wait} days later.", b, PRIYA,
        JUNE + hours(0.05) + days(wait),
        "Going through our statements I found we were charged twice on June 8. Please refund the extra one.",
        dup, action, cents, section, proposed)

b, inv = team()
b.event("cancellation_completed", RENEWAL - hours(0.1))
b.stripe_cancel(RENEWAL + hours(0.35))
b.session(TOM, RENEWAL + days(1))
add("ESC-12", "H", ["4", "11"], "Two systems put the cancellation on opposite sides of the renewal.", b, DANA,
    RENEWAL + days(3),
    "I cancelled right before the renewal on the 8th and still got charged. Please refund it.",
    inv, ESCALATE, section="11", proposed=8000)

b, inv = team()
b.event("cancellation_completed", RENEWAL - hours(0.2))
b.stripe_cancel(RENEWAL - hours(0.1))
b.session(TOM, RENEWAL + days(1))
add("ESC-13", "H", ["4", "11"], "Two systems disagree on the cancellation time, both before the renewal.", b, DANA,
    RENEWAL + days(3),
    "I cancelled right before the renewal on the 8th and still got charged. Please refund it.",
    inv, REFUND, 8000, "4.2")

# --- 3. Billing errors (§4) ------------------------------------------------

b, _ = team()
dup = b.charge(RENEWAL + hours(0.05), period_start=RENEWAL)
add("BE-01", "E", ["4"], "Two charges for the same Billing Period.", b, DANA, RENEWAL + days(1),
    "We got charged $80 twice on Sept 8, a few minutes apart. Can you refund one of them?",
    dup, REFUND, 8000, "4.1")

b, _ = team()
dup = b.charge(RENEWAL + hours(0.05), period_start=RENEWAL)
b.refund(dup, RENEWAL + days(1), basis="billing_error", reason="duplicate")
add("BE-02", "H", ["4"], "A duplicate charge that was already refunded.", b, DANA, RENEWAL + days(6),
    "We were double charged on Sept 8 and I still don't see the money. Please refund the duplicate.",
    dup, DENY, section="4.1")

b, inv = team()
b.session(TOM, RENEWAL + days(2))
add("BE-03", "H", ["4", "5"], "Two consecutive renewals described as a double charge.", b, DANA, RENEWAL + days(9),
    "You've double charged us: $80 on August 8 and $80 again on September 8. Refund the second one.",
    inv, DENY, section="5")

b, inv = team()
b.event("cancellation_requested", RENEWAL - days(3), DANA)
b.event("cancellation_completed", RENEWAL - days(3) + hours(0.02))
b.stripe_cancel(RENEWAL - days(3) + hours(0.02))
b.usage_days(MARCUS, RENEWAL + days(1), 6)
b.usage_days(TOM, RENEWAL + days(2), 4, every=2)
add("BE-04", "M", ["2", "4"], "Charged after a completed cancellation, with heavy Usage afterward.", b, DANA,
    RENEWAL + days(12),
    "I cancelled our plan on Sept 5 and you charged us anyway on the 8th. I want that refunded.",
    inv, REFUND, 8000, "4.2")

b, inv = team()
b.event("cancellation_requested", RENEWAL - days(2), DANA)
b.event("cancellation_failed", RENEWAL - days(2) + hours(0.02), DANA, error="billing_provider_timeout")
b.session(TOM, RENEWAL + days(4))
add("BE-05", "M", ["2", "4"], "A cancellation request that failed to process.", b, DANA, RENEWAL + days(10),
    "I clicked cancel on the 6th and thought that was it. Now I see we were billed on the 8th. Please refund.",
    inv, REFUND, 8000, "4.2")

b, inv = team()
b.ticket("Cancel subscription", PRIYA, RENEWAL - days(4), [
    (PRIYA, "Hi, please cancel our Quillstack subscription before it renews on the 8th. We're moving to another tool.",
     {"cancellation_request": True}),
    ("support:Jorge Mena", "Thanks Priya, I've passed this to our billing team and they'll confirm shortly."),
], status="pending")
b.session(TOM, RENEWAL + days(1))
add("BE-06", "H", ["2", "4"], "Cancellation requested in a ticket and never processed.", b, PRIYA, RENEWAL + days(3),
    "We asked you to cancel last week and you charged us on the 8th anyway. Please refund the $80.",
    inv, REFUND, 8000, "4.2")

b, inv = team()
b.event("cancellation_requested", RENEWAL - days(2), MARCUS)
b.event("cancellation_failed", RENEWAL - days(2) + hours(0.02), MARCUS, error="insufficient_permissions")
b.session(MARCUS, RENEWAL + days(1))
add("BE-07", "H", ["2", "4", "5"], "The cancellation was requested by a Member.", b, DANA, RENEWAL + days(3),
    "Marcus cancelled our plan before the renewal, so the Sept 8 charge shouldn't have happened. Please refund it.",
    inv, DENY, section="5")

for suffix, offset, action, cents, section in (("a", -5, REFUND, 8000, "4.2"), ("b", 5, DENY, 0, "5")):
    b, inv = team()
    b.event("cancellation_requested", RENEWAL + hours(offset / 60), DANA)
    b.event("cancellation_completed", RENEWAL + hours(offset / 60) + hours(0.01))
    b.stripe_cancel(RENEWAL + hours(offset / 60) + hours(0.01))
    b.session(TOM, RENEWAL + days(1))
    add(f"BE-08{suffix}", "H", ["2", "4", "5"],
        f"Cancellation {abs(offset)} minutes {'before' if offset < 0 else 'after'} the Renewal Timestamp.", b, DANA,
        RENEWAL + days(2),
        "I cancelled on the 8th, the same day you charged us. That charge should be refunded.",
        inv, action, cents, section)

b, inv = team()
b.session(MARCUS, RENEWAL + days(1))
add("BE-09", "M", ["4", "5"], "The customer says they cancelled; no record exists.", b, DANA, RENEWAL + days(3),
    "I definitely cancelled this back in August. Why was I charged on Sept 8? Refund it please.",
    inv, DENY, section="5")

b = WorldBuilder(created=add_months(RENEWAL, -3), discount_pct=20)
people(b)
b.monthly_history(add_months(RENEWAL, -1), 3)
inv = b.charge(RENEWAL, seats=6)
noise(b)
b.session(MARCUS, RENEWAL + days(1))
add("BE-10", "H", ["4", "10"], "Invoiced for 6 Seats when the settings showed 4, with a 20% discount.", b, PRIYA,
    RENEWAL + days(5),
    "Our September invoice is for 6 seats but we only have 4. Please fix it and refund the difference.",
    inv, PARTIAL, 3200, "4.3")

b = WorldBuilder(created=add_months(RENEWAL, -3), seats=6)
people(b)
inv = b.monthly_history(RENEWAL, 4)
noise(b)
b.plan_change(RENEWAL + days(2), seats=4, actor=DANA)
b.session(MARCUS, RENEWAL + days(1))
add("BE-11", "H", ["4", "5", "8"], "Seats were removed after the renewal.", b, DANA, RENEWAL + days(4),
    "We only have 4 seats now but the September invoice charged us for 6. Please refund the 2 extra seats.",
    inv, DENY, section="5")

b = WorldBuilder(created=add_months(RENEWAL, -3), seats=6)
people(b)
b.monthly_history(add_months(RENEWAL, -1), 3)
inv = b.charge(RENEWAL, seats=4)
noise(b)
b.session(MARCUS, RENEWAL + days(1))
add("BE-12", "M", ["4", "5"], "Charged for fewer Seats than the settings showed.", b, DANA, RENEWAL + days(3),
    "The September invoice doesn't match our seat count, it's wrong. I'd like it refunded and reissued.",
    inv, DENY, section="5")

PROMISE = [
    ("BE-13", "M", "I've approved a refund of $40 for the outage on Aug 30. You should see it in 5 to 7 business days.",
     {"promise_cents": 4000}, "Lena said in our ticket that we'd get $40 back for the outage. It never arrived.",
     PARTIAL, 4000, "4.5", None, "A written promise of $40 in a ticket."),
    ("BE-14", "H", "I'm sorry about the outage. I'll look into whether a refund is possible and get back to you.",
     {}, "Support told us in our ticket that we'd get a refund for the outage. It never arrived.",
     DENY, 0, "5", None, "A support agent wrote that they would look into a refund."),
    ("BE-16", "H", "I've approved a refund of $100 for the outage and the trouble it caused your team.",
     {"promise_cents": 10000}, "Lena promised us $100 back in our ticket about the outage. It never arrived.",
     ESCALATE, 0, "11", 8000, "A written promise larger than Amount Paid."),
]
for id, tier, reply, truth, message, action, cents, section, proposed, intent in PROMISE:
    b, inv = team()
    b.ticket("Outage on Aug 30", DANA, RENEWAL + days(1), [
        (DANA, "We lost most of a working day to the outage on Aug 30. We'd like compensation."),
        ("support:Lena Fischer", reply, truth),
    ])
    b.session(MARCUS, RENEWAL + days(2))
    add(id, tier, ["4", "10", "11"] if proposed else ["4", "10"], intent, b, DANA, RENEWAL + days(15), message, inv,
        action, cents, section, proposed)

b, inv = team()
b.session(MARCUS, RENEWAL + days(2))
add("BE-15", "M", ["4", "11"], "A promise reported from a phone call; no ticket has it.", b, DANA, RENEWAL + days(15),
    "I spoke to someone on your team by phone last week and they promised a full refund for September. Please process it.",
    inv, ESCALATE, section="11", proposed=0, offrecord=True)

b, inv = team(months=11)
b.event("cancellation_requested", RENEWAL - days(3), DANA)
b.event("cancellation_completed", RENEWAL - days(3) + hours(0.02))
b.stripe_cancel(RENEWAL - days(3) + hours(0.02))
b.session(MARCUS, RENEWAL + days(2))
add("BE-17", "M", ["4", "9"], "A Billing Error on a Workspace that would also qualify for goodwill.", b, DANA,
    RENEWAL + days(10),
    "We cancelled on the 5th and were still charged on the 8th. Please refund it.",
    inv, REFUND, 8000, "4.2")

b, inv = annual(first=False)
b.event("cancellation_requested", RENEWAL - days(6), DANA)
b.event("cancellation_completed", RENEWAL - days(6) + hours(0.02))
b.stripe_cancel(RENEWAL - days(6) + hours(0.02))
b.usage_days(MARCUS, RENEWAL + days(1), 3)
add("BE-18", "H", ["4", "6"], "A Billing Error on an annual renewal with Usage.", b, DANA, RENEWAL + days(8),
    "We cancelled our annual plan on Sept 2 and you renewed it on the 8th anyway. I want the $400 back.",
    inv, REFUND, 40000, "4.2")

b, inv = team()
b.event("cancellation_requested", RENEWAL - days(3), DANA)
b.event("cancellation_completed", RENEWAL - days(3) + hours(0.02))
b.stripe_cancel(RENEWAL - days(3) + hours(0.02))
dup = b.charge(RENEWAL + hours(0.05), period_start=RENEWAL)
add("BE-19", "H", ["4"], "Charged twice after a cancellation: both charges are Billing Errors.", b, DANA,
    RENEWAL + days(2),
    "I cancelled on the 5th and then got charged twice on the 8th. Please refund the second charge.",
    dup, REFUND, 16000, "4.1", also=(inv,))

# --- 4. Monthly plans (§5) -------------------------------------------------

b, inv = team()
add("MO-01", "E", ["5"], "Within 7 days, no Usage.", b, DANA, RENEWAL + days(2),
    "Forgot to cancel before the renewal on the 8th. Nobody has used it since. Can we get a refund?",
    inv, REFUND, 8000, "5")

b, inv = team()
b.session(MARCUS, RENEWAL + days(1), ("login", "view", "edit", "edit"))
add("MO-02", "M", ["2", "5"], "The requester didn't use it, but a teammate did.", b, DANA, RENEWAL + days(3),
    "I haven't logged in to Quillstack in weeks and we were charged on the 8th. Please refund it.",
    inv, DENY, section="5")

for suffix, actions, action, cents in (("a", ("login", "view", "view"), REFUND, 8000),
                                       ("b", ("login", "view", "export"), DENY, 0)):
    b, inv = team()
    b.session(TOM, RENEWAL + days(1), actions)
    add(f"MO-03{suffix}", "H", ["2", "5"], f"A teammate's only session after renewal: {', '.join(actions)}.", b, DANA,
        RENEWAL + days(3),
        "We stopped using Quillstack before the renewal on the 8th. Please refund this month.",
        inv, action, cents, "5")

b, inv = team()
b.session(MARCUS, RENEWAL - hours(5), ("login", "edit", "export"))
add("MO-04", "H", ["2", "5"], "Usage on the renewal day, before the Renewal Timestamp.", b, DANA, RENEWAL + days(3),
    "We wrapped up our last project the morning of the 8th and haven't touched Quillstack since. Can you refund the renewal?",
    inv, REFUND, 8000, "5")

for suffix, offset, action, cents in (("a", -1, REFUND, 8000), ("b", 1, DENY, 0)):
    b, inv = team()
    add(f"MO-05{suffix}", "H", ["2", "5"], f"Request one minute {'inside' if offset < 0 else 'outside'} the 7-day window.",
        b, DANA, RENEWAL + days(7) + hours(offset / 60),
        "We were charged a week ago and haven't used the product at all since. Please refund.",
        inv, action, cents, "5")

b, inv = team()
add("MO-06", "H", ["2", "5"], "Past 168 hours in UTC, but the customer argues their local date.", b, DANA,
    RENEWAL + days(7) + hours(2),
    "It's still the morning of the 15th here in Honolulu, so I'm within 7 days of the charge on the 8th. "
    "We haven't used it. Please refund.",
    inv, DENY, section="5")

b, inv = team()
b.usage_days(MARCUS, RENEWAL + days(1), 4, every=2)
b.event("cancellation_requested", RENEWAL + days(12), DANA)
b.event("cancellation_completed", RENEWAL + days(12) + hours(0.02))
b.stripe_cancel(RENEWAL + days(12) + hours(0.02))
add("MO-07", "E", ["5"], "Cancelled mid-period and asks for the unused days.", b, DANA, RENEWAL + days(13),
    "I cancelled yesterday with more than half the month left. Please refund the unused days.",
    inv, DENY, section="5")

b, inv = team()
b.session(TOM, RENEWAL + days(1), ("login", "create"))
next(m for m in b.world.members if m.id == TOM).removed_at = RENEWAL + days(3)
add("MO-08", "H", ["2", "5"], "The only Usage is by a Seat that was later removed.", b, DANA, RENEWAL + days(4),
    "None of us on the team have used Quillstack since it renewed on the 8th. Please refund.",
    inv, DENY, section="5")

# --- 5. Annual plans (§6) --------------------------------------------------

b, inv = annual(first=True, tier=Tier.STARTER, seats=1)
add("AN-01", "E", ["6"], "First annual purchase, within 30 days, no Usage.", b, DANA, RENEWAL + days(12),
    "I bought the annual Starter plan on the 8th and never ended up using it. Can I get a refund?",
    inv, REFUND, 10000, "6")

for id, wait, cents, intent in (("AN-02", days(10), 38904, "First annual purchase with Usage, 10 days in."),
                                ("AN-03", days(10) + hours(1), 38794, "As AN-02, one hour later: d rounds up to 11.")):
    b, inv = annual(first=True)
    b.usage_days(MARCUS, RENEWAL + days(1), 4, every=2)
    add(id, "M" if id == "AN-02" else "H", ["6", "10"], intent, b, DANA, RENEWAL + wait,
        "We tried the annual plan for a week or so and it's not working for our team. We'd like to cancel and get our money back.",
        inv, PARTIAL, cents, "6")

b, inv = annual(first=True)
add("AN-04", "E", ["6"], "First annual purchase, after 30 days.", b, DANA, RENEWAL + days(35),
    "We bought the annual plan last month but never rolled it out. Can we get a refund?",
    inv, DENY, section="6")

b = WorldBuilder(created=add_months(RENEWAL, -4), seats=2)
people(b)
b.monthly_history(add_months(RENEWAL, -1), 4)
noise(b)
b.plan_change(RENEWAL, interval=Interval.YEAR, actor=DANA)
inv = b.charge(RENEWAL, reason="subscription_update")
add("AN-05", "M", ["6"], "Switched from monthly to annual; no Usage since.", b, DANA, RENEWAL + days(5),
    "I switched us to annual on the 8th but the team has moved on to something else. No one has used it since. Please refund.",
    inv, REFUND, 40000, "6")

b = WorldBuilder(created=T("2023-09-08 14:00"), seats=2, interval=Interval.YEAR)
people(b)
b.charge(T("2023-09-08 14:00"), reason="subscription_create")
b.plan_change(T("2024-09-08 14:00"), interval=Interval.MONTH, actor=DANA)
b.monthly_history(add_months(RENEWAL, -1), 24)
noise(b)
b.plan_change(RENEWAL, interval=Interval.YEAR, actor=DANA)
inv = b.charge(RENEWAL, reason="subscription_update")
b.usage_days(MARCUS, RENEWAL + days(2), 5, every=3)
add("AN-06", "H", ["6", "10"], "Annual in 2023, then monthly, now annual again, with Usage.", b, DANA, RENEWAL + days(20),
    "We went back to annual on the 8th but it's not the right call for us. Please refund the annual charge.",
    inv, PARTIAL, 37808, "6")

b, inv = annual(first=False)
add("AN-07", "E", ["6"], "Annual renewal, within 14 days, no Usage.", b, DANA, RENEWAL + days(6),
    "Our annual plan renewed on the 8th and we meant to cancel. No one has used it since. Please refund.",
    inv, REFUND, 40000, "6")

b, inv = annual(first=False)
b.usage_days(MARCUS, RENEWAL + days(1), 3)
add("AN-08", "M", ["6", "10"], "Annual renewal with Usage: credit, never cash.", b, DANA, RENEWAL + days(6),
    "Our annual plan renewed on the 8th and we don't want another year. I want the $400 back on my card, not a credit.",
    inv, CREDIT, 36666, "6")

for suffix, wait, action, cents in (("a", days(14) - hours(1), CREDIT, 36666), ("b", days(14) + hours(1), DENY, 0)):
    b, inv = annual(first=False)
    b.usage_days(MARCUS, RENEWAL + days(1), 3)
    add(f"AN-09{suffix}", "H", ["2", "6"], f"Annual renewal with Usage, one hour {'inside' if suffix == 'a' else 'outside'} 14 days.",
        b, DANA, RENEWAL + wait,
        "We don't want the annual renewal from two weeks ago. What can you do for us?",
        inv, action, cents, "6")

b, inv = annual(first=False)
b.usage_days(MARCUS, RENEWAL + days(1), 2)
add("AN-10", "M", ["6", "9"], "Annual renewal past 14 days asks for a goodwill exception.", b, DANA, RENEWAL + days(20),
    "We've been loyal customers and barely used it since the renewal. Surely you can make a one-time exception and refund us?",
    inv, DENY, section="6")

b = WorldBuilder(created=RENEWAL, interval=Interval.YEAR, seats=2, tax_rate=0.08, discount_pct=25)
people(b)
b.credit(RENEWAL - hours(1), 3000, "Referral credit")
inv = b.charge(RENEWAL, reason="subscription_create", credit_applied=3000)
add("AN-11", "H", ["6", "10"], "A coupon and applied account credit: Amount Paid is not list price.", b, DANA,
    RENEWAL + days(8),
    "We signed up for the $400 annual plan on the 8th and changed our minds before using it. Please refund the $400.",
    inv, REFUND, 27000, "6")

# --- 6. Goodwill (§9) ------------------------------------------------------

b, inv = team(months=11)
b.usage_days(MARCUS, RENEWAL + days(1), 3, every=2)
add("GW-01", "M", ["5", "9"], "Every goodwill condition holds.", b, DANA, RENEWAL + days(10),
    "We forgot to cancel and barely touched it this month. Is there anything you can do about the Sept 8 charge?",
    inv, REFUND, 8000, "9")

for suffix, months, action, cents, section in (("a", 6, REFUND, 8000, "9"), ("b", 5, DENY, 0, "5")):
    b, inv = team(months=months)
    b.usage_days(MARCUS, RENEWAL + days(1), 3, every=2)
    add(f"GW-02{suffix}", "H", ["9"], f"{months} consecutive paid months, counting this charge.", b, DANA,
        RENEWAL + days(10),
        "We forgot to cancel and barely touched it this month. Is there anything you can do about the Sept 8 charge?",
        inv, action, cents, section)

b = WorldBuilder(created=add_months(RENEWAL, -7))
people(b)
inv = b.monthly_history(RENEWAL, 8, failed=(4,))
noise(b)
b.usage_days(MARCUS, RENEWAL + days(1), 3, every=2)
add("GW-03", "H", ["9"], "Eight months of history with a failed payment four months back.", b, DANA, RENEWAL + days(10),
    "We've been customers for most of a year and barely used it this month. Can you refund the Sept 8 charge?",
    inv, DENY, section="5")

REQUEST = RENEWAL + days(10)
for suffix, ago, basis, action, cents, section, intent in (
        ("4a", 300, "goodwill", DENY, 0, "5", "A goodwill refund 300 days ago."),
        ("4b", 400, "goodwill", REFUND, 8000, "9", "A goodwill refund 400 days ago.")):
    b, inv = team(months=15)
    refunded_at = REQUEST - days(ago)
    earlier = [i for i in b.world.invoices if i.created <= refunded_at][-1]
    b.refund(earlier.id, refunded_at, basis=basis)
    b.usage_days(MARCUS, RENEWAL + days(1), 3, every=2)
    add(f"GW-0{suffix}", "M", ["9"], intent, b, DANA, REQUEST,
        "We forgot to cancel and barely touched it this month. Is there anything you can do about the Sept 8 charge?",
        inv, action, cents, section)

# Fifteen months of history. June was billed for a fifth Seat in error, and $20 of it was refunded,
# so June is still a paid month and the only prior refund is a Billing Error refund.
b = WorldBuilder(created=add_months(RENEWAL, -14))
people(b)
for back in range(14, -1, -1):
    inv = b.charge(add_months(RENEWAL, -back), reason="subscription_create" if back == 14 else "subscription_cycle",
                   seats=5 if back == 3 else None)
    if back == 3:
        june = inv
noise(b)
b.refund(june, add_months(RENEWAL, -3) + days(2), amount=2000, basis="billing_error")
b.usage_days(MARCUS, RENEWAL + days(1), 3, every=2)
add("GW-05", "H", ["4", "9"], "The only prior refund was a partial one, for a Billing Error.", b, DANA, REQUEST,
    "We forgot to cancel and barely touched it this month. Is there anything you can do about the Sept 8 charge?",
    inv, REFUND, 8000, "9")

for suffix, action, cents, section in (("a", REFUND, 8000, "9"), ("b", DENY, 0, "5")):
    b, inv = team(months=11)
    for day in (1, 2, 3, 5, 6, 7, 8, 9, 10, 11):
        b.session(MARCUS, T("2026-09-08 18:00") + days(day))
    if suffix == "b":
        b.session(TOM, T("2026-09-12 06:30"))  # 23:30 on Sept 11 in Pacific time
    add(f"GW-06{suffix}", "H", ["2", "9"], f"Usage on {10 if suffix == 'a' else 11} distinct UTC days.", b, DANA,
        RENEWAL + days(13),
        "We only used it on a handful of days this month and meant to cancel. Can you refund the Sept 8 charge?",
        inv, action, cents, section)

b, inv = team(months=11)
august = b.world.invoices[-2].id
add("GW-07", "H", ["9"], "The request is about last month's charge, not the latest one.", b, DANA, RENEWAL + days(2),
    "We barely used Quillstack in August. Can you refund the charge from August 8?",
    august, DENY, section="5")

b, inv = team(months=11)
b.usage_days(MARCUS, RENEWAL + days(1), 3, every=2)
b.event("cancellation_requested", RENEWAL + days(12), DANA)
b.event("cancellation_completed", RENEWAL + days(12) + hours(0.02))
b.stripe_cancel(RENEWAL + days(12) + hours(0.02))
add("GW-08", "M", ["9"], "Request 31 days after the charge.", b, DANA, RENEWAL + days(31),
    "We cancelled a few weeks ago and hardly used it in September. Could you refund that last charge?",
    inv, DENY, section="5")
