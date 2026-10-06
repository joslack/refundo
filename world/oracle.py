"""The oracle: the answer key for a scenario, worked out by code from its records and its annotations.

Three steps, kept apart on purpose:
    annotated(world, request) -> Reading             the judgments about text, as the scenario's author wrote them down
    read_records(world, request, reading) -> Facts   §2 definitions applied to raw records (world/facts.py)
    the policy graph over those facts -> Outcome     §1's three steps, one node per rule (world/graph.py)

Only the first is particular to the oracle. decide_request takes any Reading, and from there on no step reads an
annotation or free text.

It implements every decision rule in docs/policy.md: §3, 4, 5, 6, 9, 10 and 11. §12 governs how
replies are written, which is judged on the conversation and not here.
"""

from __future__ import annotations

from world.facts import Facts, read_records
from world.facts import within as _within  # noqa: F401  tests/test_tools.py checks the tools' windows against it
from world.graph import ESCALATION_CAP_CENTS, walk
from world.reading import Reading, annotated
from world.scenario import Action, Outcome, Request
from world.schema import World

GRANTS = (Action.REFUND, Action.PARTIAL_REFUND, Action.CREDIT)


def label(world: World, request: Request) -> Outcome:
    """The answer key for a scenario. Its annotations stand in for reading the text; code does the rest."""
    return decide_request(world, request, annotated(world, request))


def extract_facts(world: World, request: Request) -> Facts:
    """The facts about the charge a scenario's request names, read with the scenario's annotations."""
    return read_records(world, request, annotated(world, request))


def decide(f: Facts) -> Outcome:
    """Decide one charge from its facts, by walking the policy graph."""
    return walk({"facts": f})["outcome"]


def decide_request(world: World, request: Request, reading: Reading) -> Outcome:
    """Decide a request from its records and a reading of its text. Each charge it describes is decided
    separately (§4), then the amounts are added."""
    def decide_charge(charge: str | None) -> Outcome:
        return walk({"world": world, "request": request, "reading": reading, "charge": charge})["outcome"]

    primary = decide_charge(reading.charge)
    if not reading.other_charges or primary.action is Action.NO_ACTION:
        return primary
    return _combine(primary, [decide_charge(charge) for charge in reading.other_charges])


def _combine(primary: Outcome, others: list[Outcome]) -> Outcome:
    """One label for several charges: the sum of what each is owed, resting on everything each decision rests on.
    It escalates if any charge does, or if the total is over §11's limit."""
    outcomes = [primary, *others]
    owed = [o.proposed if o.action is Action.ESCALATE else o for o in outcomes]
    total = sum(o.amount_cents for o in owed if o and o.action in GRANTS)
    merged = primary.model_copy(deep=True)
    for o in others:
        merged.considered += [s for s in o.considered if s not in merged.considered]
        merged.must_cite += [s for s in o.must_cite if s not in merged.must_cite]
        merged.evidence += [e for e in o.evidence if e not in merged.evidence]
        merged.records = sorted(set(merged.records) | set(o.records))
    merged.rationale = " ".join(dict.fromkeys(o.rationale for o in outcomes))

    escalated = any(o.action is Action.ESCALATE for o in outcomes)
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
        full = all(o.action is Action.REFUND for o in outcomes)
        merged.action, merged.amount_cents = (Action.REFUND if full else Action.PARTIAL_REFUND), total
    return merged
