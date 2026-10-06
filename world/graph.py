"""The policy as a graph: each node is one rule of docs/policy.md, and the edges are §1's order of precedence.

A rule reads the facts and reports a Step: the checks it made, the decision if it reached one, and the edge to
follow. EDGES says where each edge leads. The end of this file hands both to LangGraph: POLICY is a StateGraph
whose nodes are the rules and whose conditional edges are EDGES, with one node before them that reads the
records. So the graph that LangGraph draws and steps through is the table as written here.

No rule keeps track of evidence, records or sections, and none reads a record or any text. A case enters with
its records and a Reading (world/reading.py), and everything from there to the outcome is code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from math import ceil
from operator import itemgetter
from typing import Callable, NamedTuple, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langsmith.run_helpers import tracing_context

from world import checks as check
from world.facts import Facts, read_records, within
from world.reading import Reading
from world.scenario import Action, Evidence, Outcome, Request
from world.schema import World

ESCALATION_CAP_CENTS = 50_000

# The edges a rule can send a case along.
NOTHING = "nothing"    # this rule decides nothing: go on to the next
SETTLED = "settled"    # step 2 has its outcome: go to §11
STOP = "stop"          # the request ends here, with no escalation check
ESCALATE = "escalate"  # §11 replaces the outcome with an escalation


@dataclass(frozen=True)
class Step:
    """What one rule reports."""

    edge: str
    checks: tuple[Evidence, ...] = ()
    action: Action | None = None  # set when the rule reaches a decision
    amount_cents: int = 0
    form: str | None = None
    rationale: str = ""
    rests_on: tuple[str, ...] = ()  # the sections a correct label has to name, when not just the rule's own
    on_path: bool = True  # False when the records gave no reason to look at this case of §4
    formula: bool = False  # the amount came from one of §10's formulas


def _made(checks) -> tuple[Evidence, ...]:
    return tuple(c for c in checks if c is not None)


def nothing(*checks: Evidence | None, on_path: bool = True) -> Step:
    return Step(NOTHING, _made(checks), on_path=on_path)


def stop(action: Action, why: str, *checks: Evidence | None) -> Step:
    return Step(STOP, _made(checks), action, rationale=why)


def cash(f: Facts, amount: int, why: str, *checks: Evidence | None, formula: bool = False) -> Step:
    """A cash refund, never more than the charge's Amount Paid (§10). It is a full refund only at that amount."""
    amount = min(amount, f.amount_paid)
    action = Action.REFUND if amount == f.amount_paid else Action.PARTIAL_REFUND
    return Step(SETTLED, _made(checks), action, amount, "cash", why, formula=formula)


def credit(amount: int, why: str, *checks: Evidence | None) -> Step:
    return Step(SETTLED, _made(checks), Action.CREDIT, amount, "credit", why, formula=True)


def deny(why: str, *checks: Evidence | None, rests_on: tuple[str, ...] = ()) -> Step:
    return Step(SETTLED, _made(checks), Action.DENY, rationale=why, rests_on=rests_on)


def escalate(why: str, *checks: Evidence | None) -> Step:
    return Step(ESCALATE, _made(checks), Action.ESCALATE, rationale=why)


class Condition(NamedTuple):
    """One line of a list of conditions (§9, §11): whether it holds, the words for the rationale, and its check."""

    holds: bool
    words: str
    check: Evidence | None = None


def rule(section: str | None):
    """Mark a function as the rule for one policy section."""
    def mark(function):
        function.section = section
        return function
    return mark


# --- Step 1: authorize (§3) ------------------------------------------------


@rule("3")
def authorize(f: Facts) -> Step:
    """Only the Owner or a Billing Admin may ask, by their role in the records at the Request Time."""
    if not f.authorized:
        return stop(Action.NO_ACTION, "The requester is not the Owner or a Billing Admin.", check.requester(f))
    return nothing(check.requester(f))


@rule("3")
def disputed_charge(f: Facts) -> Step:
    """A dispute that closed in the customer's favor has already returned the money, so the request is denied.
    An open dispute, or one Quillstack won, is weighed in §11."""
    if f.dispute == "customer_won":
        why = "A dispute on this charge closed in the customer's favor; the bank already returned the money."
        return stop(Action.DENY, why, check.charge(f), check.dispute(f))
    return nothing(check.charge(f), check.dispute(f), check.refunded(f))


# --- Step 2: find the outcome (§4, then §5 and §6, then §9) ------------------


@rule("4.1")
def duplicate_charge(f: Facts) -> Step:
    """Duplicate charge for the same Billing Period: refund the duplicate, not the original."""
    if not f.duplicate_of:
        return nothing(check.duplicate(f), on_path=False)
    if f.owed <= 0:
        return deny("The duplicate charge was already refunded.", check.duplicate(f))
    return cash(f, f.owed, f"Duplicate of {f.duplicate_of} for the same Billing Period.", check.duplicate(f))


@rule("4.2")
def charged_after_cancellation(f: Facts) -> Step:
    """Charge after a Confirmed Cancellation: refund the full charge, even if the Workspace had Usage afterward."""
    if f.cancelled_before_charge:
        return cash(f, f.owed, "Charged after a Confirmed Cancellation.", check.cancellation(f))
    return nothing(check.cancellation(f), on_path=bool(f.cancellation_records))


@rule("4.3")
def wrong_seat_count(f: Facts) -> Step:
    """Wrong Seat count: refund the difference when the charge was too high. An undercharge is not a Billing Error."""
    if f.overcharge:
        return cash(f, f.overcharge, "Charged for more Seats than the Workspace settings at renewal.", check.seat_count(f))
    return nothing(check.seat_count(f), on_path=f.undercharged)


@rule("4.4")
def written_promise(f: Facts) -> Step:
    """Refund promised in writing in a ticket: refund what is still unpaid of it, up to what is left of the charge."""
    if f.promise_unpaid > 0 and f.owed > 0:
        why = "A support representative promised this refund in a ticket."
        return cash(f, min(f.promise_unpaid, f.owed), why, check.written_promise(f))
    return nothing(check.written_promise(f), on_path=f.written_promise is not None or f.reports_offrecord_promise)


@rule(None)
def kind_of_charge(f: Facts) -> Step:
    """§5 governs a monthly charge. §6 treats a first annual purchase and an annual renewal differently."""
    return Step(f.kind, _made([check.continues_annual_term(f)]))


@rule("5")
def monthly_renewal(f: Facts) -> Step:
    """Refunded in full within 7 days of the Renewal Timestamp with no Usage since. Otherwise §9 is next."""
    if not within(f, 7):
        return nothing()
    if f.usage_since_charge:
        return nothing(check.usage_since_charge(f))
    return cash(f, f.owed, "Monthly renewal, within 7 days, no Usage since.", check.usage_since_charge(f))


@rule("6")
def first_annual_purchase(f: Facts) -> Step:
    """Within 30 days: a full refund with no Usage since, a prorated one with Usage. After 30 days, nothing."""
    if not within(f, 30):
        return deny("First annual purchase, more than 30 days ago.")
    if not f.usage_since_charge:
        return cash(f, f.owed, "First annual purchase, within 30 days, no Usage since.", check.usage_since_charge(f))
    elapsed = ceil((f.now - f.charged_at) / timedelta(days=1))  # §10: whole days since the charge, rounded up
    return cash(f, f.amount_paid * (f.period_days - elapsed) // f.period_days,
                f"First annual purchase with Usage: prorated, {elapsed} of {f.period_days} days elapsed.",
                check.usage_since_charge(f), formula=True)


@rule("6")
def annual_renewal(f: Facts) -> Step:
    """Within 14 days: a full refund with no Usage since, Account Credit for the unused months with Usage, never
    cash. After 14 days, nothing."""
    if not within(f, 14):
        return deny("Annual renewal, more than 14 days ago.")
    if not f.usage_since_charge:
        return cash(f, f.owed, "Annual renewal, within 14 days, no Usage since.", check.usage_since_charge(f))
    started = (f.now - f.charged_at).days // 30 + 1  # §10: 30-day months started since the renewal
    return credit(f.amount_paid * (12 - started) // 12,
                  f"Annual renewal with Usage: Account Credit for {12 - started} unused months, never cash.",
                  check.usage_since_charge(f))


@rule("9")
def goodwill(f: Facts) -> Step:
    """A Goodwill Refund of the most recent monthly charge, when no earlier section grants one and every
    condition holds. When one fails, the denial rests on §5 and §9 together."""
    conditions = [
        Condition(f.consecutive_paid_months >= 6, "fewer than 6 consecutive paid months", check.paid_months(f)),
        Condition(not f.goodwill_in_last_365_days, "a goodwill refund in the last 365 days", check.past_goodwill(f)),
        Condition(f.on_monthly_plan, "the Workspace is no longer on a monthly plan"),
        Condition(within(f, 30), "more than 30 days since the charge"),
        Condition(f.usage_days_in_period <= 10, "Usage on more than 10 days", check.usage_days(f)),
        Condition(f.is_most_recent_charge, "not the most recent monthly charge"),
    ]
    failed = [c.words for c in conditions if not c.holds]
    if not failed:
        return cash(f, f.owed, "§5 grants nothing, and every goodwill condition holds.", *(c.check for c in conditions))
    # Only the conditions that failed decide a denial; the ones that held are routine checks.
    made = [check.routine(c.check) if c.holds else c.check for c in conditions if c.check]
    why = "§5 grants nothing (window or Usage); goodwill fails: " + ", ".join(failed) + "."
    return deny(why, *made, rests_on=("5", "9"))


# --- Step 3: check escalation (§11) ----------------------------------------


@rule("11")
def escalation(f: Facts, found: Outcome) -> Step:
    """Escalate, and take no billing action, when any condition holds. `found` is the outcome of step 2, which
    the escalation note carries."""
    billing_error = found.section.startswith("4") and found.action is not Action.DENY
    conditions = [
        Condition(f.dispute == "open", "a dispute is open on the charge"),
        Condition(f.dispute == "quillstack_won", "a dispute on the charge closed in Quillstack's favor"),
        Condition(f.tos_suspended, "the Workspace is suspended for a terms-of-service violation", check.suspension(f)),
        Condition(f.mentions_legal, "the customer mentions legal action or a regulator", check.legal_mention(f)),
        Condition(f.reports_offrecord_promise,
                  "the customer reports a promise made outside a ticket, which the records can't confirm or rule out",
                  check.offrecord_promise(f)),
        Condition(billing_error and not within(f, 90), "a Billing Error was reported more than 90 days after the charge"),
        Condition(found.amount_cents > ESCALATION_CAP_CENTS, "the amount is over $500"),
        Condition(found.section == "4.4" and f.written_promise > f.amount_paid,
                  "the refund promised in writing is larger than the Amount Paid for the charge"),
        Condition(f.cancellation_records_conflict,
                  "the app event log and the billing system put the cancellation on opposite sides of the charge",
                  check.cancellation_conflict(f)),
    ]
    reasons = [c.words for c in conditions if c.holds]
    made = [c.check for c in conditions]
    if reasons:
        return escalate("Escalate: " + "; ".join(reasons) + ".", *made)
    return nothing(*made)


# --- The graph ---------------------------------------------------------------

Rule = Callable[..., Step]

# §1 as edges: for each rule, where each edge it can report leads.
EDGES: dict[Rule, dict[str, Rule | str]] = {
    # Step 1: authorize. A requester who may not ask stops the request.
    authorize:                  {STOP: END, NOTHING: disputed_charge},
    disputed_charge:            {STOP: END, NOTHING: duplicate_charge},
    # Step 2: find the outcome. §4's cases in the order listed, then the plan's rule, then goodwill.
    duplicate_charge:           {SETTLED: escalation, NOTHING: charged_after_cancellation},
    charged_after_cancellation: {SETTLED: escalation, NOTHING: wrong_seat_count},
    wrong_seat_count:           {SETTLED: escalation, NOTHING: written_promise},
    written_promise:            {SETTLED: escalation, NOTHING: kind_of_charge},
    kind_of_charge:             {"monthly": monthly_renewal, "annual_first": first_annual_purchase,
                                 "annual_renewal": annual_renewal},
    monthly_renewal:            {SETTLED: escalation, NOTHING: goodwill},
    first_annual_purchase:      {SETTLED: escalation},
    annual_renewal:             {SETTLED: escalation},
    goodwill:                   {SETTLED: escalation},
    # Step 3: check escalation over the outcome of step 2.
    escalation:                 {ESCALATE: END, NOTHING: END},
}


class Case(TypedDict, total=False):
    """The graph's state: one charge under decision."""

    # What a case enters with. The Reading is the only part that takes reading text to supply.
    world: World
    request: Request  # used for the Workspace, who is asking and when
    reading: Reading
    charge: str  # which of the reading's charges this case decides; the first when left out
    # What code works out from there.
    facts: Facts
    path: list[str]  # the sections consulted so far, in order
    checks: list[Evidence]  # what each rule visited looked at and found
    outcome: Outcome | None  # the outcome in force: step 2's, until §11 replaces it with an escalation
    edge: str  # the edge the last rule reported


def records(case: Case) -> dict:
    """§2 applied to the records: the facts every rule reads. A case that enters with its facts keeps them."""
    if "facts" in case:
        return {}
    return {"facts": read_records(case["world"], case["request"], case["reading"], case.get("charge"))}


def _as_node(function: Rule) -> Callable[[Case], dict]:
    """A rule as a LangGraph node: call it on the case's facts, and add what it reports to the case."""
    def visit(case: Case) -> dict:
        f = case["facts"]
        step = function(f, case["outcome"]) if function is escalation else function(f)  # §11 weighs what step 2 found
        return _report(case, function.section, step)
    return visit


def _report(case: Case, section: str | None, step: Step) -> dict:
    """What a Step adds to the case. The outcome in force lists everything consulted up to the last rule visited;
    the `proposed` outcome inside an escalation keeps the list as it stood when step 2 ended."""
    path = case.get("path", [])
    path = path + [s for s in _sections(section, step) if s not in path]
    checks = case.get("checks", []) + list(step.checks)
    outcome = case.get("outcome")
    if step.action is not None:
        governing = step.rests_on[0] if step.rests_on else section
        outcome = Outcome(action=step.action, amount_cents=step.amount_cents, form=step.form, section=governing,
                          must_cite=list(step.rests_on) or [governing], rationale=step.rationale, proposed=outcome)
    if outcome is not None:
        cited = sorted({r for made in checks for r in made.records if not r.startswith("source:") and r != "request"})
        outcome = outcome.model_copy(update={"considered": list(path), "evidence": list(checks), "records": cited})
    return {"path": path, "checks": checks, "outcome": outcome, "edge": step.edge}


def _sections(section: str | None, step: Step) -> list[str]:
    """The sections a visit puts on the path. A case of §4 always puts §4 there, and itself only when the records
    gave a reason to look at it. §10 is there when one of its formulas set the amount."""
    if section is None:
        return []
    general = section.split(".")[0]
    return [general] + ([section] if section != general and step.on_path else []) + (["10"] if step.formula else [])


def build() -> CompiledStateGraph:
    """EDGES as a LangGraph graph: a node for each rule, and a conditional edge for each entry of the table."""
    graph = StateGraph(Case)
    graph.add_node("records", records)
    graph.add_edge(START, "records")
    graph.add_edge("records", authorize.__name__)
    for node, edges in EDGES.items():
        graph.add_node(node.__name__, _as_node(node))
        graph.add_conditional_edges(node.__name__, itemgetter("edge"), {
            edge: target if target is END else target.__name__ for edge, target in edges.items()})
    return graph.compile()


POLICY = build()


def walk(case: Case) -> Case:
    """Run a case through the graph. The answer key must have no path to the network, so LangSmith tracing is
    switched off for the run itself, whatever the environment asks for."""
    with tracing_context(enabled=False):
        return POLICY.invoke(case)
