"""Scenarios where the right answer depends on records from long ago.

The scenarios in world/scenarios.py are decided by the last few weeks of an account. These are decided by its
history, under the four rules of the policy that look back:

    RE  a Billing Error that repeats on consecutive charges (§4)
    GL  three Goodwill Refunds in the life of a Workspace (§9)
    FP  the first-purchase refund, available once per Workspace (§6)
    WP  a written promise, which stands until it is paid (§4 case 4)

Each is built like a row of the catalog and then given an earlier subscription by world/history.py, so the
records that decide it sit among several years of others. GL-02 and GL-03 count the Goodwill Refund in that
earlier subscription: it is part of what they test.
"""

from __future__ import annotations

from world import scenarios as catalog
from world.builders import WorldBuilder, add_months, days
from world.history import with_history
from world.scenario import Scenario
from world.scenarios import DANA, DENY, ESCALATE, MARCUS, PARTIAL, CREDIT, REFUND, RENEWAL, noise, people, team
from world.schema import Interval

HISTORY: list[Scenario] = []
REQUEST = RENEWAL + days(5)


def long_accounts() -> list[Scenario]:
    """The dataset of long accounts: every catalog scenario with the earlier years added, and the scenarios here."""
    return [with_history(s) for s in catalog.ALL] + HISTORY


def add(*args, **kwargs) -> None:
    """A scenario as world/scenarios.py writes one, kept here and not in its list, with the earlier years added."""
    catalog.add(*args, **kwargs)
    HISTORY.append(with_history(catalog.ALL.pop()))


def paid_before(b: WorldBuilder, at) -> str:
    """The most recent paid charge at a time: the one a promise made then belongs to."""
    return [i for i in b.world.invoices if i.status == "paid" and i.created <= at][-1].id


# --- RE: a Billing Error that repeats (§4) ---------------------------------


def billed_for_more_seats(billed: int, wrong_until: int = 0) -> tuple[WorldBuilder, dict[int, str]]:
    """Team monthly for 18 months. The Owner cut the Seats to 4 before the renewal 13 months back, and every
    charge from then until `wrong_until` months back still bills `billed`. Returns each charge by months back."""
    b = WorldBuilder(created=add_months(RENEWAL, -17), seats=billed)
    people(b)
    charges = {}
    for back in range(17, -1, -1):
        if back == 13:
            b.plan_change(add_months(RENEWAL, -13) - days(3), seats=4, actor=DANA)
        charges[back] = b.charge(add_months(RENEWAL, -back), seats=billed if back >= wrong_until else 4,
                                 reason="subscription_create" if back == 17 else "subscription_cycle")
    noise(b)
    return b, charges


b, charges = billed_for_more_seats(6)
add("RE-01", "H", ["4", "4.3", "10"], "Billed for 6 Seats for 14 months after a cut to 4; the last 12 are refunded.", b, DANA,
    REQUEST, "We dropped to 4 seats last summer and I think you've been billing us for 6 ever since. Please fix it and "
    "refund the difference.", charges[0], PARTIAL, 12 * 4000, "4.3")

b, charges = billed_for_more_seats(7)
add("RE-02", "H", ["4", "4.3", "10", "11"], "The same with 7 Seats billed: the last 12 months come to over $500.", b, DANA,
    REQUEST, "We dropped to 4 seats last summer and I think you've been billing us for 7 ever since. Please fix it and "
    "refund the difference.", charges[0], ESCALATE, section="11", proposed=12 * 6000)

b, charges = billed_for_more_seats(6, wrong_until=5)
add("RE-03", "H", ["4", "4.3", "11"], "The overbilling stopped five months ago, so it is reported after 90 days.", b, DANA,
    REQUEST, "Going through last year's invoices, we were billed for 6 seats for months after we cut to 4. It looks right "
    "now, but we'd like the difference back.", charges[5], ESCALATE, section="11", proposed=7 * 4000)

b, inv = team(months=8)
cancelled = add_months(RENEWAL, -2) - days(3)
b.event("cancellation_requested", cancelled, DANA)
b.event("cancellation_completed", cancelled + days(0.001))
b.stripe_cancel(cancelled + days(0.001))
add("RE-04", "M", ["4", "4.2"], "Charged three times after a cancellation; the customer asks about the last.", b, DANA,
    REQUEST, "We cancelled at the start of July and you charged us again on September 8. Please refund it.",
    inv, REFUND, 3 * 8000, "4.2")


# --- GL: three Goodwill Refunds in the life of a Workspace (§9) ---------------


def with_goodwill_refunds(months_back: tuple[int, ...]) -> tuple[WorldBuilder, str]:
    """Team monthly for 40 months with a Goodwill Refund in each of the months given. Every other goodwill
    condition holds for the latest charge."""
    b, inv = team(months=40)
    for back in months_back:
        at = add_months(RENEWAL, -back)
        b.refund(paid_before(b, at), at + days(10), basis="goodwill")
    b.usage_days(MARCUS, RENEWAL + days(1), 3, every=2)
    return b, inv


ASK = "Most of the team was out and we barely used it this month. Could you refund the September 8 charge as a courtesy?"
for id, given, action, cents, section, intent in (
        ("GL-01", (14, 22, 30), DENY, 0, "5", "Three Goodwill Refunds on this subscription, the latest 14 months ago."),
        ("GL-02", (14, 22), DENY, 0, "5", "Two on this subscription and one on the subscription before it."),
        ("GL-03", (14,), REFUND, 8000, "9", "One on this subscription and one on the subscription before it.")):
    b, inv = with_goodwill_refunds(given)
    add(id, "H", ["5", "9"], intent, b, DANA, RENEWAL + days(10), ASK, inv, action, cents, section)


# --- FP: the first-purchase refund, once per Workspace (§6) -------------------


def bought_annual_again(refunded: bool) -> tuple[WorldBuilder, str]:
    """An annual plan bought 30 months ago and cancelled within weeks, with or without the first-purchase refund,
    and an annual plan bought again at RENEWAL."""
    then = add_months(RENEWAL, -30)
    b = WorldBuilder(created=then, seats=2, interval=Interval.YEAR)
    people(b)
    first = b.charge(then, reason="subscription_create")
    left = then + days(9 if refunded else 40)
    b.event("cancellation_requested", left, DANA)
    b.event("cancellation_completed", left + days(0.001))
    if refunded:
        b.refund(first, left + days(1), basis="first_purchase")
    return b, b.charge(RENEWAL, reason="subscription_create")


b, inv = bought_annual_again(refunded=True)
add("FP-01", "H", ["6"], "A second first purchase after the refund was used: day 20, no Usage, so the 14 days apply.", b, DANA,
    RENEWAL + days(20), "We signed up for the annual plan again this month and it isn't going to work out. Nobody has "
    "used it. Can we get our money back?", inv, DENY, section="6")

b, inv = bought_annual_again(refunded=True)
b.session(MARCUS, RENEWAL + days(2))
add("FP-02", "H", ["6", "10"], "The same on day 10 with Usage: Account Credit, as for a renewal.", b, DANA,
    RENEWAL + days(10), "We signed up for the annual plan again this month and it isn't going to work out. Can we get "
    "our money back?", inv, CREDIT, 40000 * 11 // 12, "6")

b, inv = bought_annual_again(refunded=False)
add("FP-03", "M", ["6"], "The earlier annual term ended with no refund, so this is a first purchase: day 20, no Usage.", b,
    DANA, RENEWAL + days(20), "We signed up for the annual plan again this month and it isn't going to work out. Nobody "
    "has used it. Can we get our money back?", inv, REFUND, 40000, "6")


# --- WP: a written promise stands until it is paid (§4 case 4) ---------------


def promised(months_back: int, dollars: int, paid_dollars: int = 0) -> tuple[WorldBuilder, str]:
    """Team monthly for a year. Two days after the renewal `months_back` months ago, support promised a refund in
    a ticket, and `paid_dollars` of it was refunded against that ticket."""
    b, _ = team(months=12)
    at = add_months(RENEWAL, -months_back) + days(2)
    charge = paid_before(b, at)
    ticket = b.ticket("Editor outage", DANA, at - days(1), [
        (DANA, "The editor was down for most of yesterday and we lost a working day. We'd like compensation."),
        ("support:Lena Fischer", f"I'm sorry about that. I've approved a refund of ${dollars} for the outage.",
         {"promise_cents": dollars * 100}),
    ])
    if paid_dollars:
        b.refund(charge, at + days(3), amount=paid_dollars * 100, basis="billing_error", ticket=ticket)
    return b, charge


b, inv = promised(8, 40)
add("WP-01", "H", ["4", "4.4", "11"], "A promise from eight months ago that was never paid.", b, DANA, REQUEST,
    "Back in January Lena promised us $40 for the outage and it never arrived.", inv, ESCALATE, section="11", proposed=4000)

b, inv = promised(2, 40)
add("WP-02", "M", ["4", "4.4"], "A promise from two months ago, never paid, on a charge that is not the latest.", b, DANA,
    REQUEST, "In July Lena promised us $40 for the outage and it never arrived.", inv, PARTIAL, 4000, "4.4")

b, inv = promised(2, 60, paid_dollars=20)
add("WP-03", "H", ["4", "4.4"], "A promise of $60 from two months ago, of which $20 was paid.", b, DANA, REQUEST,
    "In July Lena promised us $60 for the outage and only $20 ever arrived.", inv, PARTIAL, 4000, "4.4")

b, inv = promised(10, 40, paid_dollars=40)
add("WP-04", "M", ["4", "4.4", "5"], "A promise from ten months ago that was paid in full at the time.", b, DANA, REQUEST,
    "Last November Lena promised us $40 for the outage and it never arrived.", inv, DENY, section="5")
