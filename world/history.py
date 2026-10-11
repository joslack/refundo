"""Years of earlier history for a scenario's account, added so that the right answer stays the same.

A scenario's account is a few months old and holds little beside the records its case is about. A long-standing
customer's account is not like that. with_history() puts an earlier subscription in front of the scenario's own
records: years of monthly charges that ended in a cancellation, a gap, and then the subscription the scenario
describes. The earlier years hold what such an account collects:

    seat changes, each followed by charges at the new Seat count
    a month where the payment failed
    a referral credit, applied to a later invoice
    a duplicate charge, refunded at the time
    a Goodwill Refund, more than 365 days before the request
    a refund promised in a ticket and paid against that ticket
    a dispute the customer won
    a member who joined and left, and a role change
    support tickets about other things, two of which mention refunds or a lawyer
    ordinary Usage

Nothing in those years is owed anything, and nothing in them bears on the request. tests/test_history.py checks
both with the oracle on every scenario: the answer is the one the scenario had without the history, and no charge
from the earlier years would be refunded or credited if the request were about it.

Every record is numbered again in order of time, so an id does not show which records were added.
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta

from world.builders import add_months, list_price
from world.scenario import Scenario
from world.schema import (
    AppEvent,
    CreditGrant,
    Dispute,
    Interval,
    Invoice,
    InvoiceLine,
    Member,
    PlanPeriod,
    Refund,
    Role,
    SessionEvent,
    Ticket,
    TicketMessage,
    Tier,
)

GAP_MONTHS = 4  # between the end of the earlier subscription and the scenario's first record
SUPPORT = [("agt_lena", "Lena Fischer"), ("agt_omar", "Omar Haddad"), ("agt_wei", "Wei Chen")]
# (subject, what the customer wrote, what support answered). None of them promises anything.
TICKETS = [
    ("Can't invite a new teammate", "The invite button does nothing for addresses on our new domain. Two people are waiting to get in.",
     "Your workspace had the old domain on its allow list. I've added the new one; invites should go through now."),
    ("Export to DOCX loses comments", "When we export a long document to DOCX the review comments are gone. PDF keeps them.",
     "Confirmed on our side. The fix shipped today, so comments now come through as Word comments."),
    ("Login loop on Safari", "Safari sends two of us back to the login page after signing in. Chrome is fine.",
     "This was a cookie setting in Safari 17. We changed how the session is stored; please sign in once more."),
    ("Invoice shows our old address", "Our invoices still carry the address we moved out of. Accounting needs the new one on them.",
     "I've updated the billing address. New invoices will carry it; earlier ones can be downloaded again with the new address."),
    ("Template gallery won't load", "The template gallery spins forever for everyone on the team since this morning.",
     "A bad deploy caused this and was rolled back within the hour. The gallery loads again."),
    ("How do we transfer ownership?", "Our founder is stepping back. How do we make someone else the Owner of the workspace?",
     "The Owner can do this under Settings, Members: choose the person and pick Make owner. It takes effect at once."),
    ("Shared link shows a 404", "A client says the shared link we sent last week now shows a 404.",
     "The link had a 7-day expiry set. You can turn expiry off under Share, Link settings, and the same link will work again."),
    ("Do you refund unused seats?", "If we drop two seats in the middle of the month, do we get a refund for them?",
     "Seat changes take effect at the next renewal, and monthly plans aren't prorated, so there is no refund for the current month."),
    ("Copy of the data processing agreement", "Our lawyer has asked for a signed copy of your data processing agreement for our files.",
     "Attached is the countersigned agreement. Let me know if your counsel needs the list of subprocessors too."),
    ("Comment notifications arrive late", "Email notifications for comments reach us hours later, which defeats the point.",
     "Our mail provider had a backlog on Tuesday. It has cleared, and we've added a second provider for these emails."),
    ("Can we get an invoice per quarter?", "Finance would like one invoice per quarter and not one per month. Is that possible?",
     "Invoices follow the billing interval, so a monthly plan has monthly invoices. An annual plan has one a year."),
    ("Search misses recent documents", "Documents created this week don't show up in search until the next day.",
     "Indexing was running behind. It is caught up, and new documents should be searchable within a minute."),
]


def with_history(scenario: Scenario, years: int = 5, tickets_per_year: int = 24) -> Scenario:
    """The scenario with an earlier subscription of `years` years in front of its own records."""
    if years < 2:
        raise ValueError("The earlier Goodwill Refund has to be more than 365 days old, so the history needs two years or more.")
    rng = random.Random(int(hashlib.sha256(scenario.id.encode()).hexdigest(), 16))
    world, request = scenario.world.model_copy(deep=True), scenario.request.model_copy(deep=True)
    ws = world.workspaces[0]
    times = [ws.created_at, *(i.created for i in world.invoices), *(t.opened_at for t in world.tickets),
             *(e.at for e in world.app_events), *(e.at for e in world.session_events)]
    first = min(times)
    months = 12 * years
    start = add_months(first.replace(day=min(first.day, 28)), -(months + GAP_MONTHS))
    month = [add_months(start, k) for k in range(months + 1)]  # month[k] is the k-th renewal; month[months] the end

    # The account and the people who were there from its first day begin with the earlier subscription.
    for m in world.members:
        if m.joined_at <= ws.created_at + timedelta(days=1):
            m.joined_at = start + (m.joined_at - ws.created_at)
    ws.created_at = start
    people = [m.id for m in world.members if m.joined_at <= start + timedelta(days=1) and m.removed_at is None]
    names = {m.id: m.name for m in world.members}
    owner = next((m.id for m in world.members if m.role is Role.OWNER and m.id in people), people[0])
    left = Member(id="usr_ines", workspace_id=ws.id, name="Ines Varga", email="ines@example.com", role=Role.MEMBER,
                  joined_at=start + timedelta(days=30), removed_at=month[months // 2] + timedelta(days=9))
    world.members.append(left)
    names[left.id] = left.name

    def event(type: str, at: datetime, actor: str | None = None, **data) -> None:
        world.app_events.append(AppEvent(id=f"ev_h{len(world.app_events)}", workspace_id=ws.id, at=at, type=type,
                                         actor_user_id=actor, data=data))

    # Seats: two, then three, then four, each change made two days before the renewal that first bills it.
    grew = [0, months // 3, 2 * months // 3]
    for seats, k in zip((2, 3, 4), grew):
        at = start if k == 0 else month[k] - timedelta(days=2)
        ws.plan_history.append(PlanPeriod(tier=Tier.TEAM, interval=Interval.MONTH, seats=seats, effective_at=at))
        if k:
            event("seats_changed", at, owner, tier="team", interval="month", seats=seats)
    ws.plan_history.sort(key=lambda p: p.effective_at)

    def seats_at(k: int) -> int:
        return 2 + sum(k >= change for change in grew[1:])

    def charge(k: int, at: datetime, reason: str, paid: bool = True, credit: int = 0) -> Invoice:
        seats = seats_at(k)
        price = list_price(Tier.TEAM, Interval.MONTH, seats)
        n = len(world.invoices)
        invoice = Invoice(
            id=f"in_h{n}", customer=ws.stripe_customer_id, billing_reason=reason, created=at, period_start=month[k],
            period_end=month[k + 1], lines=[InvoiceLine(description=f"Quillstack Team (monthly) x {seats}", tier=Tier.TEAM,
                                                        interval=Interval.MONTH, seats=seats, amount=price)],
            subtotal=price, discount=0, credit_applied=credit, tax=0, amount_paid=price - credit if paid else 0,
            status="paid" if paid else "uncollectible", charge_id=f"ch_h{n}" if paid else None, payment_method="pm_001")
        world.invoices.append(invoice)
        return invoice

    def refund(invoice: Invoice, at: datetime, amount: int, reason: str = "requested_by_customer", **metadata: str) -> None:
        world.refunds.append(Refund(id=f"re_h{len(world.refunds)}", charge_id=invoice.charge_id, amount=amount, created=at,
                                    reason=reason, metadata=metadata))

    def ticket(subject: str, by: str, at: datetime, said: list[tuple[str, str, dict]]) -> Ticket:
        """`said` is (author, text, ground truth); an author is a member's id or "support"."""
        agent = rng.choice(SUPPORT)
        messages = [TicketMessage(author_type="support_agent" if who == "support" else "customer",
                                  author_id=agent[0] if who == "support" else who,
                                  author_name=agent[1] if who == "support" else names[who],
                                  at=at + timedelta(hours=3 * i), body=text, truth=truth)
                    for i, (who, text, truth) in enumerate(said)]
        made = Ticket(id=f"tkt_h{len(world.tickets)}", workspace_id=ws.id, subject=subject, opened_by=by, opened_at=at,
                      status="solved", messages=messages)
        world.tickets.append(made)
        return made

    # One month each for the things a long account collects, none of them in the first or last two months.
    failed, credited, doubled, goodwill, promised, disputed = rng.sample(range(2, months - 2), 6)
    by_month: dict[int, Invoice] = {}
    for k in range(months):
        by_month[k] = charge(k, month[k], "subscription_create" if k == 0 else "subscription_cycle", paid=k != failed,
                             credit=2000 if k == credited else 0)

    world.credits.append(CreditGrant(id="cbtxn_h0", customer=ws.stripe_customer_id, amount=-2000,
                                     created=month[credited] - timedelta(days=10), description="Referral credit"))
    world.credits.append(CreditGrant(id="cbtxn_h1", customer=ws.stripe_customer_id, amount=2000, created=month[credited],
                                     description="Applied to invoice", metadata={"invoice": by_month[credited].id}))

    twice = charge(doubled, month[doubled] + timedelta(minutes=3), "subscription_cycle")
    ticket("Charged twice this month", owner, month[doubled] + timedelta(days=1), [
        (owner, "Our card shows two charges for this month's renewal.", {}),
        ("support", "You're right, the renewal ran twice. I've refunded the second charge in full.", {})])
    refund(twice, month[doubled] + timedelta(days=2), twice.amount_paid, "duplicate", quillstack_basis="billing_error")

    ticket("We barely used it this month", owner, month[goodwill] + timedelta(days=11), [
        (owner, "Most of the team was away and we hardly opened Quillstack this month. Is there anything you can do?", {}),
        ("support", "Given how long you've been with us, I've refunded this month's charge as a one-time courtesy.", {})])
    refund(by_month[goodwill], month[goodwill] + timedelta(days=12), by_month[goodwill].amount_paid, quillstack_basis="goodwill")

    outage = ticket("Outage during our launch week", owner, month[promised] + timedelta(days=4), [
        (owner, "The editor was down for most of Tuesday, in the week of our launch. We'd like compensation.", {}),
        ("support", "I'm sorry about Tuesday. I've approved a refund of $20 for the outage.", {"promise_cents": 2000})])
    refund(by_month[promised], month[promised] + timedelta(days=5), 2000, quillstack_basis="billing_error",
           quillstack_ticket=outage.id)

    world.disputes.append(Dispute(id="dp_h0", charge_id=by_month[disputed].charge_id, amount=by_month[disputed].amount_paid,
                                  created=month[disputed] + timedelta(days=6), reason="unrecognized", status="lost"))

    billing_admin = next((m.id for m in world.members if m.role is Role.BILLING_ADMIN and m.id in people), None)
    if billing_admin:
        event("member_role_changed", start + timedelta(days=60), owner, user=billing_admin, old_role="member",
              new_role="billing_admin")

    # The earlier subscription ends: the Owner cancels a few days before a renewal, and that renewal never happens.
    asked = month[months] - timedelta(days=6)
    event("cancellation_requested", asked, owner)
    event("cancellation_completed", asked + timedelta(minutes=1))

    def present(at: datetime) -> list[str]:
        return people + ([left.id] if left.joined_at <= at < left.removed_at else [])

    for _ in range(tickets_per_year * years):
        at = start + (month[months] - start) * rng.random()
        subject, wrote, answered = rng.choice(TICKETS)
        who = rng.choice(present(at))
        ticket(subject, who, at, [(who, wrote, {}), ("support", answered, {}), (who, "Thanks, that sorted it.", {})])

    for k in range(months):
        for _ in range(rng.randint(2, 6)):
            at = month[k] + (month[k + 1] - month[k]) * rng.random()
            session, who = f"sess_h{len(world.session_events)}", rng.choice(present(at))
            for i, action in enumerate(("login", *rng.choice([("edit",), ("view", "edit"), ("create", "export"), ("view",)]))):
                world.session_events.append(SessionEvent(id=f"se_h{len(world.session_events)}", workspace_id=ws.id, user_id=who,
                                                         session_id=session, at=at + timedelta(minutes=4 * i), action=action))

    _number_again(world, request)
    return scenario.model_copy(update={"world": world, "request": request})


def _number_again(world, request) -> None:
    """Give every record a new id in order of time, and follow each reference to an id that changed."""
    new: dict[str, str] = {}

    def number(records: list, prefix: str, at) -> None:
        records.sort(key=at)
        width = max(3, len(str(len(records))))
        for n, record in enumerate(records, 1):
            new[record.id] = f"{prefix}_{n:0{width}d}"

    number(world.invoices, "in", lambda r: r.created)
    paid = [i for i in world.invoices if i.charge_id]
    for n, invoice in enumerate(paid, 1):
        new[invoice.charge_id] = f"ch_{n:0{max(3, len(str(len(paid))))}d}"
    number(world.refunds, "re", lambda r: r.created)
    number(world.credits, "cbtxn", lambda r: r.created)
    number(world.disputes, "dp", lambda r: r.created)
    number(world.app_events, "ev", lambda r: r.at)
    number(world.session_events, "se", lambda r: r.at)
    number(world.tickets, "tkt", lambda r: r.opened_at)
    sessions = list(dict.fromkeys(e.session_id for e in world.session_events))
    new |= {old: f"sess_{n:0{max(3, len(str(len(sessions))))}d}" for n, old in enumerate(sessions, 1)}

    def renamed(value):
        return new.get(value, value) if isinstance(value, str) else value

    for invoice in world.invoices:
        invoice.id, invoice.charge_id = new[invoice.id], renamed(invoice.charge_id)
    for record in (*world.refunds, *world.disputes):
        record.id, record.charge_id = new[record.id], new[record.charge_id]
    for record in world.credits:
        record.id = new[record.id]
    for record in (*world.refunds, *world.credits):
        record.metadata = {k: renamed(v) for k, v in record.metadata.items()}
    for e in world.app_events:
        e.id, e.data = new[e.id], {k: renamed(v) for k, v in e.data.items()}
    for e in world.session_events:
        e.id, e.session_id = new[e.id], new[e.session_id]
    for t in world.tickets:
        t.id = new[t.id]
        for m in t.messages:  # a promise may name the charge it is for
            m.truth = {k: renamed(v) for k, v in m.truth.items()}
    request.invoice_id = renamed(request.invoice_id)
    request.also_invoice_ids = [new[i] for i in request.also_invoice_ids]
