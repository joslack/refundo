"""A scenario: one world, one incoming request, and the correct outcome.

Layers, each built on the one before:
1. World (schema.py): platform records.
2. Scenario (this file): a request against that world, labeled with the
   outcome the policy requires. This is what gets hand-labeled.
3. Conversation test case (later): a scenario plus customer behavior such as
   prompt injection, abuse, persistence, or new claims, true or false.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel

from world.schema import World


class Action(StrEnum):
    REFUND = "refund"
    PARTIAL_REFUND = "partial_refund"
    CREDIT = "credit"
    DENY = "deny"
    ESCALATE = "escalate"
    NO_ACTION = "no_action"


class Request(BaseModel):
    """What arrives at support. Who sent it is platform data; what it says is untrusted."""

    workspace_id: str
    requester_user_id: str  # the authenticated user, not whoever the message claims to be
    received_at: AwareDatetime  # the policy's Request Time
    channel: Literal["chat", "email"]
    message: str
    invoice_id: str | None  # ground truth for which charge the customer means; never shown to the agent
    also_invoice_ids: list[str] = []  # ground truth: other charges the request describes, each decided separately
    mentions_legal: bool = False  # ground truth for §11's legal trigger; never shown to the agent
    reports_offrecord_promise: bool = False  # ground truth: claims a promise made outside a ticket (§11)


class Evidence(BaseModel):
    """One assertion the decision rests on, and the records that support it.

    `records` holds record ids, "request" for the customer's message, or "source:<name>" when the
    assertion is that a source holds nothing relevant (e.g. no Usage means the session log was checked).
    """

    claim: str
    records: list[str]
    need: Literal["all", "any"] = "all"  # "any": one of the records is enough to support the claim
    # "decisive" claims produce this outcome; "rule_out" claims are checks that found nothing to change it.
    kind: Literal["decisive", "rule_out"] = "decisive"


class Outcome(BaseModel):
    action: Action
    amount_cents: int = 0
    form: Literal["cash", "credit"] | None = None
    section: str  # governing policy section, e.g. "4.2", "6", "11"
    considered: list[str] = []  # every section the decision had to consult, in order
    must_cite: list[str] = []  # the sections a correct label has to name: usually just `section`
    rationale: str  # one sentence a reviewer can check against the records
    records: list[str] = []  # ids of the records the decision rests on
    evidence: list[Evidence] = []  # the assertions behind the decision, each with its support
    proposed: Outcome | None = None  # for escalations: what would have been done (§11 note)


class Scenario(BaseModel):
    id: str  # e.g. cancel_charged_017
    archetype: str
    difficulty: Literal["easy", "medium", "hard"]
    sections: list[str]  # policy sections the scenario is built to exercise
    intent: str  # what the author meant it to test, in a sentence
    world: World
    request: Request
    expected: Outcome | None = None  # from the oracle; hidden from the labeler until reveal
