"""The oracle: reads a World the way the policy defines things, then applies the policy.

Two steps, kept apart on purpose:
    extract_facts(world, request) -> Facts     §2 definitions applied to raw records (world/facts.py)
    decide(facts) -> Outcome                   §1's three steps over those facts

It implements every decision rule in docs/policy.md: §3, 4, 5, 6, 9, 10 and 11. §12 governs how
replies are written, which is judged on the conversation and not here.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from math import ceil

from world.facts import Facts, extract_facts
from world.facts import within as _within
from world.scenario import Action, Evidence, Outcome, Request
from world.schema import World

ESCALATION_CAP_CENTS = 50_000


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
    # §1 step 3 names the suspension beside the open dispute: escalate, and the note carries the outcome from step 2.
    if f.tos_suspended:
        reasons.append("the Workspace is suspended for a terms-of-service violation")
        c.add("The Workspace is suspended for a terms-of-service violation.", "suspension", need="any")
    else:
        c.add("The Workspace is not suspended for a terms-of-service violation.", "workspace", rule_out=True)
    if f.mentions_legal:
        reasons.append("the customer mentions legal action or a regulator")
        c.add("The customer mentions legal action, a lawyer, or a regulator.", records=["request"])
    if f.reports_offrecord_promise:
        reasons.append("the customer reports a promise made outside a ticket, which the records can't confirm or rule out")
        c.add("The customer reports a promise made outside a support ticket.", records=["request"])
    if proposal.section.startswith("4") and proposal.action is not Action.DENY and not _within(f, 90):
        reasons.append("a Billing Error was reported more than 90 days after the charge")
    if proposal.amount_cents > ESCALATION_CAP_CENTS:
        reasons.append("the amount is over $500")
    if proposal.section == "4.4" and f.written_promise > f.amount_paid:
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
    if cancel_records:
        c.consider("4.2")
    if f.undercharged:
        c.consider("4.3")
        c.add("The invoice bills fewer Seats than the Workspace settings; an undercharge is not a Billing Error.",
              "workspace", rule_out=True)
    if f.overcharge:
        c.add("The invoice bills more Seats than the Workspace settings at the charge.", "workspace")
        return _cash(c, f.overcharge, "4.3", "Charged for more Seats than the Workspace settings at renewal.")
    if f.written_promise is not None or f.reports_offrecord_promise:
        c.consider("4.4")
    if f.written_promise is None:
        c.add("No ticket contains a written refund promise.", records=["source:tickets"], rule_out=True)
    elif f.written_promise > f.promise_paid and owed > 0:
        c.add(f"A support representative promised ${f.written_promise / 100:,.2f} in a ticket.", "promise")
        return _cash(c, min(f.written_promise - f.promise_paid, owed), "4.4",
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
    denied = not (months_ok and usage_ok and not f.goodwill_in_last_365_days and _within(f, 30)
                  and f.is_most_recent_charge and f.on_monthly_plan)
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
        "the Workspace is no longer on a monthly plan": f.on_monthly_plan,
        "more than 30 days since the charge": _within(f, 30),
        "Usage on more than 10 days": f.usage_days_in_period <= 10,
        "not the most recent monthly charge": f.is_most_recent_charge,
    }.items() if not ok]
    if not failed:
        return _cash(c, owed, "9", "§5 grants nothing, and every goodwill condition holds.")
    out = _out(c, Action.DENY, 0, "5", "§5 grants nothing (window or Usage); goodwill fails: " + ", ".join(failed) + ".")
    out.must_cite = ["5", "9"]      # the denial rests on both sections granting nothing
    return out


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
    return Outcome(action=action, amount_cents=amount, section=section, rationale=why, must_cite=[section],
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
        merged.must_cite += [s for s in o.must_cite if s not in merged.must_cite]
        merged.evidence += [e for e in o.evidence if e not in merged.evidence]
        merged.records = sorted(set(merged.records) | set(o.records))
    merged.rationale = " ".join(dict.fromkeys(o.rationale for o in outs))
    escalated = [o for o in outs if o.action is Action.ESCALATE]
    if escalated or total > ESCALATION_CAP_CENTS:
        why = merged.rationale if escalated else "Escalate: the amounts for the charges in this request total over $500."
        merged.considered += [s for s in ["11"] if s not in merged.considered]
        return merged.model_copy(update={
            "action": Action.ESCALATE, "amount_cents": 0, "form": None, "section": "11", "rationale": why,
            "must_cite": ["11"],
            "proposed": Outcome(action=Action.REFUND if total else Action.DENY, amount_cents=total,
                                section=primary.section, rationale="Total across the charges in the request."),
        })
    if total:
        full = all(o.action is Action.REFUND for o in outs)
        merged.action, merged.amount_cents = (Action.REFUND if full else Action.PARTIAL_REFUND), total
    return merged
