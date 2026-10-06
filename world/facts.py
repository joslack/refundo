"""Facts: one charge and its Workspace, read from the records the way §2 defines things.

    extract_facts(world, request) -> Facts

Everything a decision depends on is a field of Facts. world/oracle.py applies the policy to them and reads no
record itself, so a rule can be checked against the policy without knowing how the records are laid out.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from world.scenario import Request
from world.schema import Interval, Role, World

OPEN_DISPUTE = {"warning_needs_response", "needs_response", "under_review"}
ACTIVE_ACTIONS = {"create", "edit", "export"}


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


def extract_facts(world: World, request: Request) -> Facts:
    now = request.received_at
    ws = next(w for w in world.workspaces if w.id == request.workspace_id)
    members = {m.id: m for m in world.members}
    requester = members.get(request.requester_user_id)
    billing_roles = {Role.OWNER, Role.BILLING_ADMIN}

    def role_at(user_id: str | None, at: datetime) -> Role | None:
        """A person's role at a point in time: the current role, unwound through any later role changes."""
        m = members.get(user_id)
        if not m or at < m.joined_at or (m.removed_at is not None and at >= m.removed_at):
            return None
        later = sorted((e for e in world.app_events if e.type == "member_role_changed"
                        and e.data.get("user") == user_id and e.at > at and "old_role" in e.data), key=lambda e: e.at)
        return Role(later[0].data["old_role"]) if later else m.role

    authorized = role_at(request.requester_user_id, now) in billing_roles

    inv = next(i for i in world.invoices if i.id == request.invoice_id)
    line = inv.lines[0]
    support: dict[str, list[str]] = {"requester": [requester.id] if requester else [], "workspace": [ws.id]}
    amount_paid = inv.amount_paid - inv.tax
    on_charge = [r for r in world.refunds if r.charge_id == inv.charge_id and r.created <= now]
    refunded = sum(r.amount for r in on_charge)
    support["refunds"] = [r.id for r in on_charge]

    if line.interval is Interval.MONTH:
        kind = "monthly"
    elif inv.billing_reason == "subscription_cycle":
        kind = "annual_renewal"
    else:
        kind = "annual_first"

    # The suspension shows on the workspace record and in the event log; either one supports the claim
    support["suspension"] = [ws.id] + [e.id for e in world.app_events if e.type == "workspace_suspended" and e.at <= now]

    # Disputes on this charge only
    dispute = "none"
    for d in world.disputes:
        if d.charge_id == inv.charge_id and d.created <= now:
            support.setdefault("disputes", []).append(d.id)
            dispute = "open" if d.status in OPEN_DISPUTE else "customer_won" if d.status == "lost" else "quillstack_won"

    # Usage: a login followed, in the same session, by at least one create, edit or export
    sessions: dict[str, list] = {}
    for e in world.session_events:
        sessions.setdefault(e.session_id, []).append(e)
    logins = {sid: min((x.at for x in events if x.action == "login"), default=None) for sid, events in sessions.items()}
    active = [
        (e.at, sid) for sid, events in sessions.items() if logins[sid] is not None
        for e in events if e.action in ACTIVE_ACTIONS and e.at >= logins[sid]
    ]
    active_times = [t for t, _ in active]
    support["usage_since"] = sorted({sid for t, sid in active if inv.created <= t <= now})
    period_end = min(inv.period_end, now)
    usage_since = any(inv.created <= t <= now for t in active_times)
    usage_days = len({t.date() for t in active_times if inv.period_start <= t <= period_end})

    # Duplicate: another paid invoice for the same Billing Period, created earlier
    duplicate_of = next(
        (o.id for o in world.invoices
         if o.id != inv.id and o.status == "paid" and o.period_start == inv.period_start
         and o.period_end == inv.period_end and o.created <= inv.created),
        None,
    )
    support["duplicate"] = [duplicate_of] if duplicate_of else []

    # Confirmed Cancellation (§2): a completed event, or a request by someone who was the Owner or a
    # Billing Admin when they made it, before the Renewal Timestamp, in the app or a ticket, even if it failed.
    def may_cancel(user_id: str | None, at: datetime) -> bool:
        return role_at(user_id, at) in billing_roles

    # A cancellation belongs to the subscription it cancelled. One that came before the current
    # subscription's first charge (the customer signed up again) says nothing about this charge.
    sub_started = max((i.created for i in world.invoices
                       if i.billing_reason == "subscription_create" and i.created <= inv.created), default=None)

    def current(at: datetime) -> bool:
        return sub_started is None or at >= sub_started

    requested, completed = [], []
    for e in world.app_events:
        if not current(e.at):
            continue
        if e.type in {"cancellation_requested", "cancellation_failed"} and may_cancel(e.actor_user_id, e.at):
            requested.append(e.at)
            support.setdefault("cancel_request", []).append(e.id)
        elif e.type == "cancellation_completed":
            completed.append(e.at)
            support.setdefault("cancel_completed", []).append(e.id)
    for t in world.tickets:
        for m in t.messages:
            if m.truth.get("cancellation_request") and may_cancel(m.author_id, m.at) and current(m.at):
                requested.append(m.at)
                support.setdefault("cancel_request", []).append(t.id)
    sub = next((s for s in world.subscriptions if s.customer == ws.stripe_customer_id), None)
    if sub and sub.canceled_at and current(sub.canceled_at):
        completed.append(sub.canceled_at)
        support.setdefault("cancel_completed", []).append(sub.id)

    # Wrong Seat count: invoiced Seats against the settings in effect at the charge. The overcharge is
    # the price of the extra Seats after the invoice's percentage discount; a fixed credit applied to
    # the invoice would have been applied either way, so it does not change the difference (§10).
    settings = [p for p in ws.plan_history if p.effective_at <= inv.created][-1]
    overcharge = 0
    if line.seats > settings.seats:
        extra = line.amount * (line.seats - settings.seats) // line.seats
        overcharge = extra * (inv.subtotal - inv.discount) // inv.subtotal

    # Written promise by a support representative, in a ticket. Only a refund recorded against that
    # ticket counts as paying it; a refund issued for some other reason does not.
    promise, promise_paid = None, 0
    for t in world.tickets:
        for m in t.messages:
            if m.author_type == "support_agent" and "promise_cents" in m.truth and m.at <= now:
                promise = m.truth["promise_cents"]
                support["promise"] = [t.id]
                paid = [r for r in world.refunds if r.metadata.get("quillstack_ticket") == t.id and r.created <= now]
                promise_paid = sum(r.amount for r in paid)
                support["promise_refunds"] = [r.id for r in paid]

    # Goodwill history. Months are counted as Billing Periods, so a duplicate invoice is not an extra
    # month, and the run stops at a period with no successful charge or at a gap between periods.
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
    charge_to_invoice = {i.charge_id: i for i in world.invoices}
    support["goodwill_refunds"] = [
        r.id for r in world.refunds
        if r.metadata.get("quillstack_basis") == "goodwill" and r.charge_id in charge_to_invoice
        and timedelta(0) <= now - r.created <= timedelta(days=365)
    ]
    goodwill_recent = bool(support["goodwill_refunds"])
    is_latest = not any(
        i.created > inv.created and i.created <= now and i.status == "paid"
        and i.billing_reason == "subscription_cycle" for i in world.invoices
    )

    plan_now = [p for p in ws.plan_history if p.effective_at <= now][-1]

    return Facts(
        now=now, authorized=authorized, on_monthly_plan=plan_now.interval is Interval.MONTH, invoice_id=inv.id, charged_at=inv.created, amount_paid=amount_paid,
        already_refunded=refunded, kind=kind, period_days=(inv.period_end - inv.period_start).days,
        dispute=dispute, tos_suspended=ws.status == "suspended" and ws.suspension_reason == "tos_violation",
        mentions_legal=request.mentions_legal, reports_offrecord_promise=request.reports_offrecord_promise, usage_since_charge=usage_since, usage_days_in_period=usage_days,
        duplicate_of=duplicate_of, cancel_requested_at=min(requested, default=None),
        cancel_completed_at=completed, overcharge=overcharge, undercharged=line.seats < settings.seats,
        written_promise=promise, promise_paid=promise_paid,
        consecutive_paid_months=consecutive, goodwill_in_last_365_days=goodwill_recent,
        is_most_recent_charge=is_latest, support=support,
    )


def within(f: Facts, n_days: int) -> bool:
    """§2 Within N days: no more than N x 24 hours from the charge to the Request Time."""
    return timedelta(0) <= f.now - f.charged_at <= timedelta(days=n_days)
