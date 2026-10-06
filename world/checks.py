"""Checks: what the records show about one question, in a sentence, with the records behind it.

A rule in world/graph.py names the checks it made, and they become the label's evidence. A check that found
something cites those records. One that found nothing cites the sources it searched, as "source:<name>". A
check with nothing to report returns None.

A hand label is scored against the claims that produced the outcome, so each check says which kind it is:
`shown` for a claim the outcome rests on, `ruled_out` for a look that found nothing to change it.
"""

from __future__ import annotations

from world.facts import Facts
from world.scenario import Evidence


def shown(claim: str, records: list[str], need: str = "all") -> Evidence:
    return Evidence(claim=claim, records=records, need=need)


def ruled_out(claim: str, records: list[str]) -> Evidence:
    return Evidence(claim=claim, records=records, kind="rule_out")


def searched(*sources: str) -> list[str]:
    return [f"source:{s}" for s in sources]


def routine(check: Evidence) -> Evidence:
    """The same check, as one that did not decide the outcome."""
    return check.model_copy(update={"kind": "rule_out"})


# --- §3 -------------------------------------------------------------------


def requester(f: Facts) -> Evidence:
    if f.authorized:
        return shown("The requester is the Owner or a Billing Admin at the Request Time.", f.support["requester"])
    return shown("The requester is not the Owner or a Billing Admin at the Request Time.",
                 f.support["requester"] or searched("members"))


def charge(f: Facts) -> Evidence:
    amount = f"${f.amount_paid / 100:,.2f}"
    return shown(f"The charge in question is {f.invoice_id}, with Amount Paid of {amount} before tax.", [f.invoice_id])


def dispute(f: Facts) -> Evidence:
    if f.dispute == "none":
        return ruled_out("No dispute exists on this charge.", searched("disputes"))
    return shown(f"A dispute on this charge is {f.dispute.replace('_', ' ')}.", f.support["disputes"])


def refunded(f: Facts) -> Evidence:
    if f.already_refunded:
        return shown(f"${f.already_refunded / 100:,.2f} of this charge was already refunded.", f.support["refunds"])
    return ruled_out("Nothing has been refunded on this charge.", f.support["refunds"] or searched("refunds"))


# --- §4 -------------------------------------------------------------------


def duplicate(f: Facts) -> Evidence:
    if f.duplicate_of:
        return shown(f"It duplicates {f.duplicate_of} for the same Billing Period.", f.support["duplicate"])
    return ruled_out("No other charge covers the same Billing Period.", searched("invoices"))


def cancellation(f: Facts) -> Evidence:
    if f.cancelled_before_charge:
        by = "cancel_request" if f.cancel_requested_before_charge else "cancel_completed"
        return shown("A Confirmed Cancellation came before the charge.", f.support[by], need="any")
    if f.cancellation_records:
        return shown("No Confirmed Cancellation came before the charge.", f.cancellation_records)
    return ruled_out("No Confirmed Cancellation came before the charge.", searched("app_events", "tickets"))


def seat_count(f: Facts) -> Evidence | None:
    if f.overcharge:
        return shown("The invoice bills more Seats than the Workspace settings at the charge.", f.support["workspace"])
    if f.undercharged:
        return ruled_out("The invoice bills fewer Seats than the Workspace settings; an undercharge is not a Billing Error.",
                         f.support["workspace"])
    return None


def written_promise(f: Facts) -> Evidence:
    if f.written_promise is None:
        return ruled_out("No ticket contains a written refund promise.", searched("tickets"))
    if f.promise_unpaid > 0 and f.owed > 0:
        promised = f"${f.written_promise / 100:,.2f}"
        return shown(f"A support representative promised {promised} in a ticket.", f.support["promise"])
    return shown("The refund promised in a ticket has already been paid.",
                 f.support["promise_refunds"] or searched("refunds"))


# --- §5 and §6 ------------------------------------------------------------


def continues_annual_term(f: Facts) -> Evidence | None:
    if f.kind != "annual_renewal":
        return None
    return shown("The charge continues an annual term that was already running.", [f.invoice_id])


def usage_since_charge(f: Facts) -> Evidence:
    if f.usage_since_charge:
        return shown("The Workspace had Usage since the charge.", f.support["usage_since"], need="any")
    return shown("The Workspace had no Usage since the charge.", searched("sessions"))


# --- §9 -------------------------------------------------------------------


def paid_months(f: Facts) -> Evidence:
    return shown(f"The Workspace has {f.consecutive_paid_months} consecutive paid monthly charges.", searched("invoices"))


def past_goodwill(f: Facts) -> Evidence:
    if f.goodwill_in_last_365_days:
        return shown("A Goodwill Refund was issued in the last 365 days.", f.support["goodwill_refunds"])
    return shown("No Goodwill Refund was issued in the last 365 days.", searched("refunds"))


def usage_days(f: Facts) -> Evidence:
    return shown(f"The Workspace had Usage on {f.usage_days_in_period} distinct UTC days in the Billing Period.",
                 searched("sessions"))


# --- §11 ------------------------------------------------------------------


def suspension(f: Facts) -> Evidence:
    if f.tos_suspended:
        return shown("The Workspace is suspended for a terms-of-service violation.", f.support["suspension"], need="any")
    return ruled_out("The Workspace is not suspended for a terms-of-service violation.", f.support["workspace"])


def legal_mention(f: Facts) -> Evidence | None:
    return shown("The customer mentions legal action, a lawyer, or a regulator.", ["request"]) if f.mentions_legal else None


def offrecord_promise(f: Facts) -> Evidence | None:
    if not f.reports_offrecord_promise:
        return None
    return shown("The customer reports a promise made outside a support ticket.", ["request"])


def cancellation_conflict(f: Facts) -> Evidence | None:
    if not f.cancellation_records_conflict:
        return None
    return shown("Two systems record the cancellation on opposite sides of the charge.", f.support["cancel_completed"])
