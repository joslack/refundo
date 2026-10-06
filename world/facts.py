"""Facts: one charge and its Workspace, read from the records the way §2 defines things.

    extract_facts(world, request) -> Facts

Everything a decision depends on is a field of Facts. world/oracle.py applies the policy to them and reads no
record itself, so a rule can be checked against the policy without knowing how the records are laid out.

Each helper below answers one question the policy asks of the records, and returns the ids of the records behind
its answer. extract_facts gathers those ids in `support`, so a decision can say what it rests on.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from world.scenario import Request
from world.schema import Interval, Invoice, Role, Workspace, World

OPEN_DISPUTE = {"warning_needs_response", "needs_response", "under_review"}
ACTIVE_ACTIONS = {"create", "edit", "export"}
BILLING_ROLES = {Role.OWNER, Role.BILLING_ADMIN}
Stamped = list[tuple[datetime, str]]  # times, each with the id of the record that holds it


class Facts(BaseModel):
    """Everything the decision depends on, in the policy's own terms. Money is cents, pre-tax."""

    now: datetime
    authorized: bool
    invoice_id: str
    charged_at: datetime
    amount_paid: int  # §2: after discounts and credits, excluding tax
    already_refunded: int
    kind: Literal["monthly", "annual_first", "annual_renewal"]
    period_days: int
    # §3 / §11
    dispute: Literal["none", "open", "customer_won", "quillstack_won"] = "none"
    tos_suspended: bool = False
    mentions_legal: bool = False
    reports_offrecord_promise: bool = False
    # §2 Usage
    usage_since_charge: bool = False
    usage_days_in_period: int = 0
    # §4
    duplicate_of: str | None = None
    cancel_requested_at: datetime | None = None  # earliest request by an Owner or Billing Admin, app or ticket
    cancel_completed_at: list[datetime] = []  # one per system that recorded a completed cancellation
    overcharge: int = 0
    undercharged: bool = False  # billed for fewer Seats than the settings; not a Billing Error
    written_promise: int | None = None
    promise_paid: int = 0  # refunds issued since the promise was written
    # §9
    consecutive_paid_months: int = 0
    goodwill_in_last_365_days: bool = False
    is_most_recent_charge: bool = True
    on_monthly_plan: bool = True  # the Workspace's plan at the Request Time
    support: dict[str, list[str]] = {}  # fact name -> ids of the records behind it


def within(f: Facts, n_days: int) -> bool:
    """§2 Within N days: no more than N x 24 hours from the charge to the Request Time."""
    return timedelta(0) <= f.now - f.charged_at <= timedelta(days=n_days)


def extract_facts(world: World, request: Request) -> Facts:
    """The facts about the charge the request names, as of the Request Time."""
    now = request.received_at
    ws = next(w for w in world.workspaces if w.id == request.workspace_id)
    inv = next(i for i in world.invoices if i.id == request.invoice_id)
    refunds = [r for r in world.refunds if r.charge_id == inv.charge_id and r.created <= now]
    dispute, disputes = _dispute(world, inv, now)
    sessions_since, usage_days = _usage(world, inv, now)
    duplicate_of = _duplicate_of(world, inv)
    cancel_requests, cancel_completions = _cancellations(world, ws, inv)
    overcharge, undercharged = _seat_count(ws, inv)
    promise, promise_ticket, promise_refunds = _written_promise(world, now)
    paid_months, goodwill_refunds, is_latest = _goodwill_history(world, inv, now)
    return Facts(
        now=now,
        authorized=_role_at(world, request.requester_user_id, now) in BILLING_ROLES,
        invoice_id=inv.id, charged_at=inv.created, kind=_kind(inv),
        amount_paid=inv.amount_paid - inv.tax, already_refunded=sum(r.amount for r in refunds),
        period_days=(inv.period_end - inv.period_start).days,
        dispute=dispute,
        tos_suspended=ws.status == "suspended" and ws.suspension_reason == "tos_violation",
        mentions_legal=request.mentions_legal, reports_offrecord_promise=request.reports_offrecord_promise,
        usage_since_charge=bool(sessions_since), usage_days_in_period=usage_days,
        duplicate_of=duplicate_of,
        cancel_requested_at=min((at for at, _ in cancel_requests), default=None),
        cancel_completed_at=[at for at, _ in cancel_completions],
        overcharge=overcharge, undercharged=undercharged,
        written_promise=promise, promise_paid=sum(r.amount for r in promise_refunds),
        consecutive_paid_months=paid_months, goodwill_in_last_365_days=bool(goodwill_refunds),
        is_most_recent_charge=is_latest,
        on_monthly_plan=[p for p in ws.plan_history if p.effective_at <= now][-1].interval is Interval.MONTH,
        support={
            "requester": [m.id for m in world.members if m.id == request.requester_user_id],
            "workspace": [ws.id],
            "refunds": [r.id for r in refunds],
            "disputes": disputes,
            # The suspension shows on the workspace record and in the event log; either one supports the claim
            "suspension": [ws.id] + [e.id for e in world.app_events if e.type == "workspace_suspended" and e.at <= now],
            "usage_since": sessions_since,
            "duplicate": [duplicate_of] if duplicate_of else [],
            "cancel_request": [record for _, record in cancel_requests],
            "cancel_completed": [record for _, record in cancel_completions],
            "promise": [promise_ticket] if promise_ticket else [],
            "promise_refunds": [r.id for r in promise_refunds],
            "goodwill_refunds": goodwill_refunds,
        },
    )


def _role_at(world: World, user_id: str | None, at: datetime) -> Role | None:
    """A person's role at a point in time: the current role, unwound through any later role changes."""
    m = {m.id: m for m in world.members}.get(user_id)
    if not m or at < m.joined_at or (m.removed_at is not None and at >= m.removed_at):
        return None
    later = sorted((e for e in world.app_events if e.type == "member_role_changed"
                    and e.data.get("user") == user_id and e.at > at and "old_role" in e.data), key=lambda e: e.at)
    return Role(later[0].data["old_role"]) if later else m.role


def _kind(inv: Invoice) -> str:
    """§6: an annual charge that continues a term already running is a renewal; any other is a first purchase."""
    if inv.lines[0].interval is Interval.MONTH:
        return "monthly"
    return "annual_renewal" if inv.billing_reason == "subscription_cycle" else "annual_first"


def _dispute(world: World, inv: Invoice, now: datetime) -> tuple[str, list[str]]:
    """Where the latest dispute on this charge stands. Disputes on other charges say nothing about it."""
    on_charge = [d for d in world.disputes if d.charge_id == inv.charge_id and d.created <= now]
    if not on_charge:
        return "none", []
    status = on_charge[-1].status
    state = "open" if status in OPEN_DISPUTE else "customer_won" if status == "lost" else "quillstack_won"
    return state, [d.id for d in on_charge]


def _usage(world: World, inv: Invoice, now: datetime) -> tuple[list[str], int]:
    """§2 Usage: a login followed, in the same session, by at least one create, edit or export. Returns the
    sessions with Usage since the charge, and the number of UTC days with Usage in the Billing Period so far."""
    sessions: dict[str, list] = {}
    for e in world.session_events:
        sessions.setdefault(e.session_id, []).append(e)
    active = []  # (time, session) of each action that counts
    for sid, events in sessions.items():
        login = min((e.at for e in events if e.action == "login"), default=None)
        active += [(e.at, sid) for e in events if login is not None and e.action in ACTIVE_ACTIONS and e.at >= login]
    since = sorted({sid for at, sid in active if inv.created <= at <= now})
    days = {at.date() for at, _ in active if inv.period_start <= at <= min(inv.period_end, now)}
    return since, len(days)


def _duplicate_of(world: World, inv: Invoice) -> str | None:
    """§4.1: another paid invoice for the same Billing Period, created earlier. The later one is the duplicate."""
    return next((o.id for o in world.invoices
                 if o.id != inv.id and o.status == "paid" and o.period_start == inv.period_start
                 and o.period_end == inv.period_end and o.created <= inv.created), None)


def _cancellations(world: World, ws: Workspace, inv: Invoice) -> tuple[Stamped, Stamped]:
    """§2 Confirmed Cancellation: the requests and the completed cancellations on record, each with its time.

    A request counts when the person who made it was the Owner or a Billing Admin at that moment, in the app or
    in a ticket, even if it failed to process. A completed cancellation is read from the app event log and from
    the billing system. A cancellation belongs to the subscription it cancelled: one from before the current
    subscription's first charge (the customer signed up again) says nothing about this charge.
    """
    started = max((i.created for i in world.invoices
                   if i.billing_reason == "subscription_create" and i.created <= inv.created), default=None)

    def current(at: datetime) -> bool:
        return started is None or at >= started

    def may_cancel(user_id: str | None, at: datetime) -> bool:
        return _role_at(world, user_id, at) in BILLING_ROLES

    requests = [(e.at, e.id) for e in world.app_events
                if e.type in {"cancellation_requested", "cancellation_failed"} and current(e.at)
                and may_cancel(e.actor_user_id, e.at)]
    requests += [(m.at, t.id) for t in world.tickets for m in t.messages
                 if m.truth.get("cancellation_request") and may_cancel(m.author_id, m.at) and current(m.at)]
    completions = [(e.at, e.id) for e in world.app_events if e.type == "cancellation_completed" and current(e.at)]
    sub = next((s for s in world.subscriptions if s.customer == ws.stripe_customer_id), None)
    if sub and sub.canceled_at and current(sub.canceled_at):
        completions.append((sub.canceled_at, sub.id))
    return requests, completions


def _seat_count(ws: Workspace, inv: Invoice) -> tuple[int, bool]:
    """§4.3 and §10: the overcharge in cents, and whether the invoice bills fewer Seats than the settings.

    Invoiced Seats are compared with the settings in effect at the charge. The overcharge is the price of the
    extra Seats after the invoice's percentage discount; a fixed credit applied to the invoice would have been
    applied either way, so it does not change the difference.
    """
    line, settings = inv.lines[0], [p for p in ws.plan_history if p.effective_at <= inv.created][-1]
    if line.seats <= settings.seats:
        return 0, line.seats < settings.seats
    extra = line.amount * (line.seats - settings.seats) // line.seats
    return extra * (inv.subtotal - inv.discount) // inv.subtotal, False


def _written_promise(world: World, now: datetime) -> tuple[int | None, str | None, list]:
    """§4.4: the latest refund promise a support representative wrote in a ticket, that ticket, and the refunds
    recorded against it. A refund issued for some other reason does not pay the promise."""
    promises = [(m.truth["promise_cents"], t.id) for t in world.tickets for m in t.messages
                if m.author_type == "support_agent" and "promise_cents" in m.truth and m.at <= now]
    if not promises:
        return None, None, []
    cents, ticket = promises[-1]
    paid = [r for r in world.refunds if r.metadata.get("quillstack_ticket") == ticket and r.created <= now]
    return cents, ticket, paid


def _goodwill_history(world: World, inv: Invoice, now: datetime) -> tuple[int, list[str], bool]:
    """§9: consecutive paid months up to this charge, Goodwill Refunds in the last 365 days, and whether this is
    the most recent charge.

    Months are counted as Billing Periods, so a duplicate invoice is not an extra month, and the run stops at a
    period with no successful charge or at a gap between periods.
    """
    periods: dict[datetime, tuple[datetime, bool]] = {}
    for i in world.invoices:
        if (i.lines[0].interval is Interval.MONTH and i.period_start <= inv.period_start
                and i.billing_reason in {"subscription_cycle", "subscription_create"}):
            end, paid = periods.get(i.period_start, (i.period_end, False))
            periods[i.period_start] = (end, paid or i.status == "paid")
    consecutive, later_start = 0, None
    for start in sorted(periods, reverse=True):
        end, paid = periods[start]
        if not paid or (later_start is not None and end != later_start):
            break
        consecutive, later_start = consecutive + 1, start
    charges = {i.charge_id for i in world.invoices}
    goodwill = [r.id for r in world.refunds
                if r.metadata.get("quillstack_basis") == "goodwill" and r.charge_id in charges
                and timedelta(0) <= now - r.created <= timedelta(days=365)]
    is_latest = not any(i.created > inv.created and i.created <= now and i.status == "paid"
                        and i.billing_reason == "subscription_cycle" for i in world.invoices)
    return consecutive, goodwill, is_latest
