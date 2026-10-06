"""A Reading is everything a decision needs beyond the records an agent can reach.

The oracle takes its Reading from a scenario's annotations. These tests take the annotations away after that:
the records as they come back from the database, which holds no `truth`, and a request with its ground-truth
fields blanked. If any step after the reading looked at an annotation, the label would change.
"""

import json

import pytest

from world.oracle import decide_request, label
from world.reading import Reading, annotated
from world.scenario import Request
from world.scenarios import ALL
from world.sql import seed_sql, tables, world_from_rows

by_scenario = pytest.mark.parametrize("s", ALL, ids=lambda s: s.id)
# What seed_sql gives an agent of the request, and the message, which is the task's instruction.
GIVEN_TO_THE_AGENT = {"workspace_id", "requester_user_id", "received_at", "channel", "message"}
BLANK = {"invoice_id": None, "also_invoice_ids": [], "mentions_legal": False, "reports_offrecord_promise": False}


def as_an_agent_sees_it(s):
    """The scenario's records read back from SQL rows, and its request without the ground truth."""
    rows = {table: [json.loads(json.dumps(dict(zip(columns, row)), default=str)) for row in rows]
            for table, (columns, rows) in tables(s.world).items()}
    return world_from_rows(rows), s.request.model_copy(update=BLANK)


@by_scenario
def test_the_records_and_a_reading_are_enough_to_decide(s):
    world, request = as_an_agent_sees_it(s)
    assert not any(m.truth for t in world.tickets for m in t.messages)
    assert decide_request(world, request, annotated(s.world, s.request)) == label(s.world, s.request)


def test_a_reading_covers_every_annotation():
    """Each ground-truth field of a request and each `truth` key in use has its place in a Reading. An annotation
    added without one would be a judgment the process takes from nowhere."""
    assert set(Request.model_fields) - GIVEN_TO_THE_AGENT == set(BLANK)
    assert {key for s in ALL for t in s.world.tickets for m in t.messages for key in m.truth} == {
        "promise_cents", "cancellation_request"}
    assert set(Reading.model_fields) == {"charge", "other_charges", "mentions_legal", "reports_offrecord_promise",
                                         "written_promises", "cancellation_requests"}


@by_scenario
def test_the_seed_gives_the_agent_none_of_the_reading(s):
    sql = seed_sql(s.world, s.request)
    assert "CREATE TABLE request_context (workspace_id text, requester_user_id text, received_at timestamptz, channel text);" in sql
    assert "truth" not in sql.split("CREATE TABLE ticket_messages")[1].split(";")[0]
