"""The records survive the trip into SQL tables and back, and nothing hidden goes with them."""

import json

import pytest

from world.oracle import label
from world.scenarios import ALL
from world.sql import seed_sql, tables, truth_of, with_truth, world_from_rows

by_scenario = pytest.mark.parametrize("s", ALL, ids=lambda s: s.id)


def rows_as_read_back(world) -> dict[str, list[dict]]:
    """Rows the way a client gets them from the database: JSON values, with jsonb columns as text."""
    out = {}
    for table, (columns, rows) in tables(world).items():
        out[table] = []
        for row in rows:
            values = [json.dumps(v) if isinstance(v, dict) else v for v in row]
            out[table].append(json.loads(json.dumps(dict(zip(columns, values)), default=str)))
    return out


@by_scenario
def test_records_round_trip(s):
    rebuilt = world_from_rows(rows_as_read_back(s.world))
    assert with_truth(rebuilt, truth_of(s.world)) == s.world


@by_scenario
def test_oracle_gives_the_same_outcome_on_the_rebuilt_records(s):
    rebuilt = with_truth(world_from_rows(rows_as_read_back(s.world)), truth_of(s.world))
    assert label(rebuilt, s.request) == label(s.world, s.request)


@by_scenario
def test_seed_holds_no_answers(s):
    sql = seed_sql(s.world, s.request)
    assert s.id not in sql and s.intent not in sql
    assert all("truth" not in columns for columns, _ in tables(s.world).values())
    assert '"promise_cents"' not in sql and '"cancellation_request"' not in sql  # the annotation keys
