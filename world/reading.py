"""A reading: the few inputs to a decision that are judgments about text, and not fields of any record.

    which charge, or charges, the request is about
    whether the customer mentions legal action, a lawyer or a regulator
    whether the customer reports a promise made outside a support ticket
    which ticket messages promise a refund, and of how much
    which ticket messages ask to cancel

Everything else a decision needs is computed from the records by code: world/facts.py takes a Reading beside
the records and never looks at an annotation or at free text. So a Reading is all that has to be supplied to
decide a request. The oracle takes it from what a scenario's author wrote down (`annotated`). Anything that
could read the customer's message and the tickets and fill in the same six fields would get the same process.
"""

from __future__ import annotations

from pydantic import BaseModel

from world.scenario import Request
from world.schema import World


class WrittenPromise(BaseModel):
    """A ticket message that promises a refund of a stated amount (§4 case 4). Who wrote it and when are record
    fields, so code decides whether it counts: a promise counts when a support representative made it."""

    ticket_id: str
    message: int  # the message's position in the ticket
    cents: int


class CancellationRequest(BaseModel):
    """A ticket message that asks to cancel (§2 Confirmed Cancellation). Code decides whether it counts, from who
    wrote it, their role at that moment, and when."""

    ticket_id: str
    message: int  # the message's position in the ticket


class Reading(BaseModel):
    charge: str | None  # the invoice the request is about
    other_charges: list[str] = []  # further invoices it describes; each is decided separately (§4)
    mentions_legal: bool = False  # §11: legal action, a lawyer or a regulator, in the current conversation
    reports_offrecord_promise: bool = False  # §11: a promise or commitment said to be made outside a ticket
    written_promises: list[WrittenPromise] = []
    cancellation_requests: list[CancellationRequest] = []


def annotated(world: World, request: Request) -> Reading:
    """The reading a scenario's author wrote down: the request's ground-truth fields, and `truth` on ticket messages."""
    messages = [(t.id, position, m.truth) for t in world.tickets for position, m in enumerate(t.messages)]
    return Reading(
        charge=request.invoice_id,
        other_charges=request.also_invoice_ids,
        mentions_legal=request.mentions_legal,
        reports_offrecord_promise=request.reports_offrecord_promise,
        written_promises=[WrittenPromise(ticket_id=ticket, message=position, cents=truth["promise_cents"])
                          for ticket, position, truth in messages if "promise_cents" in truth],
        cancellation_requests=[CancellationRequest(ticket_id=ticket, message=position)
                               for ticket, position, truth in messages if truth.get("cancellation_request")],
    )
