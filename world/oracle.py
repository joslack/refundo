"""The oracle: reads a World the way the policy defines things, then applies the policy.

Two steps, kept apart on purpose:
    extract_facts(world, request) -> Facts     §2 definitions applied to raw records
    decide(facts) -> Outcome                   §1's three steps over those facts

It covers the MVP sections (3, 4, 5, 6, 9, 10, 11). Trials (§7), seat and plan
changes (§8) and legacy plans (§13) are not implemented yet.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from math import ceil
from typing import Literal

from pydantic import BaseModel

from world.scenario import Action, Evidence, Outcome, Request
from world.schema import Interval, Invoice, Role, World

ESCALATION_CAP_CENTS = 50_000
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
    written_promise: int | None = None
    promise_paid: int = 0  # refunds issued since the promise was written
    # §9
    consecutive_paid_months: int = 0
    goodwill_in_last_365_days: bool = False
    is_most_recent_charge: bool = True
    support: dict[str, list[str]] = {}  # fact name -> ids of the records behind it


# --- Step 1: facts from records -------------------------------------------


def extract_facts(world: World, request: Request) -> Facts:
    now = request.received_at
    ws = next(w for w in world.workspaces if w.id == request.workspace_id)
    members = {m.id: m for m in world.members}
    requester = members.get(request.requester_user_id)
    billing_roles = {Role.OWNER, Role.BILLING_ADMIN}
    authorized = bool(
        requester and requester.role in billing_roles
        and (requester.removed_at is None or requester.removed_at > now)
    )

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

    # Usage: sessions with a login and at least one create, edit or export
    sessions: dict[str, list] = {}
    for e in world.session_events:
        sessions.setdefault(e.session_id, []).append(e)
    active = [
        (e.at, sid) for sid, events in sessions.items() if any(x.action == "login" for x in events)
        for e in events if e.action in ACTIVE_ACTIONS
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

    # Confirmed Cancellation (§2): a completed event, or a request by an Owner or Billing Admin
    # before the Renewal Timestamp, in the app or a ticket, even if it failed.
    def may_cancel(user_id: str | None) -> bool:
        m = members.get(user_id)
        return bool(m and m.role in billing_roles)

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
        if e.type in {"cancellation_requested", "cancellation_failed"} and may_cancel(e.actor_user_id):
            requested.append(e.at)
            support.setdefault("cancel_request", []).append(e.id)
        elif e.type == "cancellation_completed":
            completed.append(e.at)
            support.setdefault("cancel_completed", []).append(e.id)
    for t in world.tickets:
        for m in t.messages:
            if m.truth.get("cancellation_request") and may_cancel(m.author_id) and current(m.at):
                requested.append(m.at)
                support.setdefault("cancel_request", []).append(t.id)
    sub = next((s for s in world.subscriptions if s.customer == ws.stripe_customer_id), None)
    if sub and sub.canceled_at and current(sub.canceled_at):
        completed.append(sub.canceled_at)
        support.setdefault("cancel_completed", []).append(sub.id)

    # Wrong Seat count: invoiced Seats against the settings in effect at the charge
    settings = [p for p in ws.plan_history if p.effective_at <= inv.created][-1]
    overcharge = 0
    if line.seats > settings.seats and line.tier == settings.tier:
        overcharge = amount_paid * (line.seats - settings.seats) // line.seats

    # Written promise by a support representative, in a ticket
    promise, promise_paid = None, 0
    for t in world.tickets:
        for m in t.messages:
            if m.author_type == "support_agent" and "promise_cents" in m.truth and m.at <= now:
                promise = m.truth["promise_cents"]
                support["promise"] = [t.id]
                paid_since = [r for r in world.refunds if m.at <= r.created <= now]
                promise_paid = sum(r.amount for r in paid_since)
                support["promise_refunds"] = [r.id for r in paid_since]

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

    return Facts(
        now=now, authorized=authorized, invoice_id=inv.id, charged_at=inv.created, amount_paid=amount_paid,
        already_refunded=refunded, kind=kind, period_days=(inv.period_end - inv.period_start).days,
        dispute=dispute, tos_suspended=ws.status == "suspended" and ws.suspension_reason == "tos_violation",
        mentions_legal=request.mentions_legal, reports_offrecord_promise=request.reports_offrecord_promise, usage_since_charge=usage_since, usage_days_in_period=usage_days,
        duplicate_of=duplicate_of, cancel_requested_at=min(requested, default=None),
        cancel_completed_at=completed, overcharge=overcharge, written_promise=promise, promise_paid=promise_paid,
        consecutive_paid_months=consecutive, goodwill_in_last_365_days=goodwill_recent,
        is_most_recent_charge=is_latest, support=support,
    )


# --- Step 2: the policy over facts ----------------------------------------


class _Claims:
    """Collects the assertions a decision rests on, as it is made."""

    def __init__(self, f: Facts):
        self.f, self.items, self.sections = f, [], []

    def consider(self, *sections: str) -> None:
        self.sections += [s for s in sections if s not in self.sections]

    def add(self, claim: str, fact: str | None = None, empty: tuple[str, ...] = (), need: str = "all",
            records: list[str] | None = None, rule_out: bool = False) -> None:
        """Support a claim with the records behind `fact`, or with the sources checked if there are none."""
        found = records if records is not None else self.f.support.get(fact, [])
        self.items.append(Evidence(claim=claim, records=found or [f"source:{s}" for s in empty], need=need,
                                   kind="rule_out" if rule_out else "decisive"))


def decide(f: Facts) -> Outcome:
    c = _Claims(f)
    c.consider("3")

    # §1 step 1: authorize
    if not f.authorized:
        c.add("The requester is not the Owner or a Billing Admin at the Request Time.", "requester", ("members",))
        return _out(c, Action.NO_ACTION, 0, "3", "The requester is not the Owner or a Billing Admin.")
    c.add("The requester is the Owner or a Billing Admin at the Request Time.", "requester")
    if f.tos_suspended:  # §3: no refunds or credits, so there is nothing to work out before escalating
        c.add("The Workspace is suspended for a terms-of-service violation.", "suspension", need="any")
        out = _out(c, Action.ESCALATE, 0, "11", "Escalate: the Workspace is suspended for a terms-of-service violation.")
        out.proposed = Outcome(action=Action.DENY, section="3",
                               rationale="No refunds or credits for a Workspace suspended for a terms-of-service violation.")
        return out
    c.add(f"The charge in question is {f.invoice_id}, with Amount Paid of ${f.amount_paid / 100:,.2f} before tax.",
          records=[f.invoice_id])
    if f.dispute == "none":
        c.add("No dispute exists on this charge.", "disputes", ("disputes",), rule_out=True)
    else:
        c.add(f"A dispute on this charge is {f.dispute.replace('_', ' ')}.", "disputes")
    if f.dispute == "customer_won":
        return _out(c, Action.DENY, 0, "3",
                    "A dispute on this charge closed in the customer's favor; the bank already returned the money.")
    if f.already_refunded:
        c.add(f"${f.already_refunded / 100:,.2f} of this charge was already refunded.", "refunds")
    else:
        c.add("Nothing has been refunded on this charge.", "refunds", ("refunds",), rule_out=True)

    # §1 step 2: find the outcome. A request before the charge settles the cancellation question.
    # Otherwise the completed cancellation is read from every system that recorded one; if they put it
    # on opposite sides of the charge, §2 says to escalate, and the earliest time is used for the proposal.
    if f.cancel_requested_at and f.cancel_requested_at < f.charged_at:
        proposal, conflict = _propose(f, f.cancel_requested_at, c), False
    else:
        proposal = _propose(f, min(f.cancel_completed_at, default=None), c)
        conflict = len({t < f.charged_at for t in f.cancel_completed_at}) > 1

    # §1 step 3: escalation over that outcome
    c.consider("11")
    reasons = []
    if f.dispute == "open":
        reasons.append("a dispute is open on the charge")
    if f.dispute == "quillstack_won":
        reasons.append("a dispute on the charge closed in Quillstack's favor")
    c.add("The Workspace is not suspended for a terms-of-service violation.", "workspace", rule_out=True)
    if f.mentions_legal:
        reasons.append("the customer mentions legal action or a regulator")
        c.add("The customer mentions legal action, a lawyer, or a regulator.", records=["request"])
    if f.reports_offrecord_promise and proposal.section != "4.5":
        reasons.append("the customer reports a promise made outside a ticket, which the records can't confirm or rule out")
        c.add("The customer reports a promise made outside a support ticket.", records=["request"])
    if proposal.section.startswith("4") and proposal.action is not Action.DENY and not _within(f, 90):
        reasons.append("a Billing Error was reported more than 90 days after the charge")
    if proposal.amount_cents > ESCALATION_CAP_CENTS:
        reasons.append("the amount is over $500")
    if proposal.section == "4.5" and f.written_promise > f.amount_paid:
        reasons.append("the refund promised in writing is larger than the Amount Paid for the charge")
    if conflict:
        reasons.append("the app event log and the billing system put the cancellation on opposite sides of the charge")
        c.add("Two systems record the cancellation on opposite sides of the charge.", "cancel_completed")
    if reasons:
        out = _out(c, Action.ESCALATE, 0, "11", "Escalate: " + "; ".join(reasons) + ".")
        out.proposed = proposal
        return out
    proposal.evidence, proposal.records, proposal.considered = c.items, _flatten(c), list(c.sections)
    return proposal


def _propose(f: Facts, cancelled_at: datetime | None, c: _Claims) -> Outcome:
    owed = f.amount_paid - f.already_refunded

    # §4 billing errors, in the order listed
    c.consider("4")
    if f.duplicate_of:
        c.add(f"It duplicates {f.duplicate_of} for the same Billing Period.", "duplicate")
        if owed <= 0:
            return _out(c, Action.DENY, 0, "4.1", "The duplicate charge was already refunded.")
        return _cash(c, owed, "4.1", f"Duplicate of {f.duplicate_of} for the same Billing Period.")
    c.add("No other charge covers the same Billing Period.", records=["source:invoices"], rule_out=True)
    cancel_records = f.support.get("cancel_request", []) + f.support.get("cancel_completed", [])
    if cancelled_at and f.charged_at > cancelled_at:
        fact = "cancel_request" if f.cancel_requested_at and f.cancel_requested_at < f.charged_at else "cancel_completed"
        c.add("A Confirmed Cancellation came before the charge.", fact, need="any")
        return _cash(c, owed, "4.2", "Charged after a Confirmed Cancellation.")
    c.add("No Confirmed Cancellation came before the charge.", records=cancel_records or ["source:app_events", "source:tickets"],
          rule_out=not cancel_records)
    if f.overcharge:
        c.add("The invoice bills more Seats than the Workspace settings at the charge.", "workspace")
        return _cash(c, f.overcharge, "4.3", "Charged for more Seats than the Workspace settings at renewal.")
    if f.written_promise is None:
        c.add("No ticket contains a written refund promise.", records=["source:tickets"], rule_out=True)
    elif f.written_promise > f.promise_paid and owed > 0:
        c.add(f"A support representative promised ${f.written_promise / 100:,.2f} in a ticket.", "promise")
        return _cash(c, min(f.written_promise - f.promise_paid, owed), "4.5",
                     "A support representative promised this refund in a ticket.")
    else:
        c.add("The refund promised in a ticket has already been paid.", "promise_refunds", ("refunds",))

    def usage() -> bool:
        if f.usage_since_charge:
            c.add("The Workspace had Usage since the charge.", "usage_since", need="any")
        else:
            c.add("The Workspace had no Usage since the charge.", records=["source:sessions"])
        return f.usage_since_charge

    if f.kind == "monthly":
        c.consider("5")
        if _within(f, 7) and not usage():
            return _cash(c, owed, "5", "Monthly renewal, within 7 days, no Usage since.")
        return _goodwill(f, owed, c)

    c.consider("6")
    if f.kind == "annual_first":
        if not _within(f, 30):
            return _out(c, Action.DENY, 0, "6", "First annual purchase, more than 30 days ago.")
        if not usage():
            return _cash(c, owed, "6", "First annual purchase, within 30 days, no Usage since.")
        d = ceil((f.now - f.charged_at) / timedelta(days=1))
        c.consider("10")
        return _cash(c, f.amount_paid * (f.period_days - d) // f.period_days, "6",
                     f"First annual purchase with Usage: prorated, {d} of {f.period_days} days elapsed.")

    c.add("The charge continues an annual term that was already running.", records=[f.invoice_id])
    if not _within(f, 14):
        return _out(c, Action.DENY, 0, "6", "Annual renewal, more than 14 days ago.")
    if not usage():
        return _cash(c, owed, "6", "Annual renewal, within 14 days, no Usage since.")
    m = (f.now - f.charged_at).days // 30 + 1
    c.consider("10")
    out = _out(c, Action.CREDIT, f.amount_paid * (12 - m) // 12, "6",
               f"Annual renewal with Usage: Account Credit for {12 - m} unused months, never cash.")
    out.form = "credit"
    return out


def _goodwill(f: Facts, owed: int, c: _Claims) -> Outcome:
    c.consider("9")
    # When goodwill is denied, only the conditions that failed decide it; the ones that passed are rule-outs.
    months_ok, usage_ok = f.consecutive_paid_months >= 6, f.usage_days_in_period <= 10
    denied = not (months_ok and usage_ok and not f.goodwill_in_last_365_days and _within(f, 30) and f.is_most_recent_charge)
    c.add(f"The Workspace has {f.consecutive_paid_months} consecutive paid monthly charges.", records=["source:invoices"],
          rule_out=denied and months_ok)
    if f.goodwill_in_last_365_days:
        c.add("A Goodwill Refund was issued in the last 365 days.", "goodwill_refunds")
    else:
        c.add("No Goodwill Refund was issued in the last 365 days.", records=["source:refunds"], rule_out=denied)
    c.add(f"The Workspace had Usage on {f.usage_days_in_period} distinct UTC days in the Billing Period.",
          records=["source:sessions"], rule_out=denied and usage_ok)
    failed = [name for name, ok in {
        "fewer than 6 consecutive paid months": f.consecutive_paid_months >= 6,
        "a goodwill refund in the last 365 days": not f.goodwill_in_last_365_days,
        "more than 30 days since the charge": _within(f, 30),
        "Usage on more than 10 days": f.usage_days_in_period <= 10,
        "not the most recent monthly charge": f.is_most_recent_charge,
    }.items() if not ok]
    if not failed:
        return _cash(c, owed, "9", "§5 grants nothing, and every goodwill condition holds.")
    return _out(c, Action.DENY, 0, "5", "§5 grants nothing (window or Usage); goodwill fails: " + ", ".join(failed) + ".")


def _within(f: Facts, n_days: int) -> bool:
    return timedelta(0) <= f.now - f.charged_at <= timedelta(days=n_days)


def _cash(c: _Claims, amount: int, section: str, why: str) -> Outcome:
    amount = min(amount, c.f.amount_paid)
    action = Action.REFUND if amount == c.f.amount_paid else Action.PARTIAL_REFUND
    out = _out(c, action, amount, section, why)
    out.form = "cash"
    return out


def _flatten(c: _Claims) -> list[str]:
    return sorted({r for e in c.items for r in e.records if not r.startswith("source:") and r != "request"})


def _out(c: _Claims, action: Action, amount: int, section: str, why: str) -> Outcome:
    c.consider(section)
    return Outcome(action=action, amount_cents=amount, section=section, rationale=why,
                   records=_flatten(c), evidence=list(c.items), considered=list(c.sections))


def label(world: World, request: Request) -> Outcome:
    """Decide the request. Each charge it describes is decided separately (§4), then the amounts are added."""
    primary = decide(extract_facts(world, request))
    if not request.also_invoice_ids or primary.action is Action.NO_ACTION:
        return primary
    outs = [primary] + [decide(extract_facts(world, request.model_copy(update={"invoice_id": i})))
                        for i in request.also_invoice_ids]
    grants = (Action.REFUND, Action.PARTIAL_REFUND, Action.CREDIT)
    owed = [o.proposed if o.action is Action.ESCALATE else o for o in outs]
    total = sum(o.amount_cents for o in owed if o and o.action in grants)
    merged = primary.model_copy(deep=True)
    for o in outs[1:]:
        merged.considered += [s for s in o.considered if s not in merged.considered]
        merged.evidence += [e for e in o.evidence if e not in merged.evidence]
        merged.records = sorted(set(merged.records) | set(o.records))
    merged.rationale = " ".join(dict.fromkeys(o.rationale for o in outs))
    escalated = [o for o in outs if o.action is Action.ESCALATE]
    if escalated or total > ESCALATION_CAP_CENTS:
        why = merged.rationale if escalated else "Escalate: the amounts for the charges in this request total over $500."
        merged.considered += [s for s in ["11"] if s not in merged.considered]
        return merged.model_copy(update={
            "action": Action.ESCALATE, "amount_cents": 0, "form": None, "section": "11", "rationale": why,
            "proposed": Outcome(action=Action.REFUND if total else Action.DENY, amount_cents=total,
                                section=primary.section, rationale="Total across the charges in the request."),
        })
    if total:
        full = all(o.action is Action.REFUND for o in outs)
        merged.action, merged.amount_cents = (Action.REFUND if full else Action.PARTIAL_REFUND), total
    return merged
