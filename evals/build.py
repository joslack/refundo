"""Build the Harbor spike task from one scenario.

    uv run python evals/build.py
    harbor run -p evals/task -a langgraph -m openai/gpt-6-luna \
        --ak project_path=agents --ak graph=sql --env-file .env \
        --plugin langsmith --pk dataset_name=refundo-harbor-spike -o "$PWD/evals/jobs"

The jobs folder is given as an absolute path on purpose. Harbor copies files into the containers with
`docker compose cp`, which reads a relative path from the task's environment folder, so a relative
jobs folder makes every copy fail and fall back to a slower retry.

The first command writes the parts of the task that come from the scenario:
    task/instruction.md                   the customer's message, which is all the agent is given
    task/environment/postgres/seed.sql    the scenario's records, and who is asking
    task/tests/expected.json              the oracle's outcome; Harbor copies tests/ in only after the agent has finished
    ../agents/policy.md                   the policy the agents work from; Harbor copies only agents/ into the container

Nothing the agent can reach holds the answer: the seed leaves out the scenario id, the ground-truth
annotations on ticket messages, and the request fields that say which charge is meant.
"""

import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import Union, get_args, get_origin

from pydantic import BaseModel

from world.labeling import ui_action
from world.oracle import label
from world.scenarios import ALL

SCENARIO = "MO-01"
HERE = Path(__file__).resolve().parent
# Lists of records inside a record become their own table, pointing back at the parent.
CHILD_TABLES = {
    ("workspaces", "plan_history"): ("plan_history", "workspace_id"),
    ("invoices", "lines"): ("invoice_lines", "invoice_id"),
    ("tickets", "messages"): ("ticket_messages", "ticket_id"),
}
HIDDEN = {"truth"}  # ground truth for facts that exist only in free text


def column_type(annotation) -> str:
    optional = get_origin(annotation) in (Union, UnionType)
    kind = next(a for a in get_args(annotation) if a is not type(None)) if optional else annotation
    if get_origin(kind) is dict:
        return "jsonb"
    by_name = {"AwareDatetime": "timestamptz", "bool": "boolean", "int": "bigint"}
    return by_name.get(getattr(kind, "__name__", ""), "text")


def literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, datetime):
        value = value.isoformat()
    elif isinstance(value, Enum):
        value = value.value
    elif isinstance(value, dict):
        value = json.dumps(value)
    return "'" + str(value).replace("'", "''") + "'"


def add_table(out: dict, name: str, model: type[BaseModel], rows: list, parent: tuple[str, str] | None = None) -> None:
    """Flatten records into out[table] = (columns and their SQL types, rows). An empty list still makes a table."""
    children = {field: child for (table, field), child in CHILD_TABLES.items() if table == name}
    fields = [f for f in model.model_fields if f not in HIDDEN and f not in children]
    columns = {f: column_type(model.model_fields[f].annotation) for f in fields}
    key, parent_id = parent or (None, None)
    out.setdefault(name, (({key: "text"} if key else {}) | columns, []))
    for row in rows:
        out[name][1].append(([parent_id] if key else []) + [getattr(row, f) for f in fields])
    for field, (child, child_key) in children.items():
        child_model = get_args(model.model_fields[field].annotation)[0]
        add_table(out, child, child_model, [], (child_key, None))
        for row in rows:
            add_table(out, child, child_model, getattr(row, field), (child_key, row.id))


def seed_sql(scenario) -> str:
    request, out, sql = scenario.request, {}, []
    for name, field in type(scenario.world).model_fields.items():
        add_table(out, name, get_args(field.annotation)[0], getattr(scenario.world, name))
    for table, (columns, rows) in out.items():
        sql.append(f"CREATE TABLE {table} ({', '.join(f'{c} {t}' for c, t in columns.items())});")
        sql += [f"INSERT INTO {table} VALUES ({', '.join(literal(v) for v in row)});" for row in rows]
    sql.append("CREATE TABLE request_context (workspace_id text, requester_user_id text, received_at timestamptz, "
               "channel text);")
    sql.append(f"INSERT INTO request_context VALUES ({literal(request.workspace_id)}, "
               f"{literal(request.requester_user_id)}, {literal(request.received_at)}, {literal(request.channel)});")
    sql.append("CREATE TABLE proposals (id serial PRIMARY KEY, action text, amount_cents bigint, sections jsonb, "
               "rationale text, created_at timestamptz DEFAULT now());")
    return "\n".join(sql) + "\n"


def expected(scenario) -> dict:
    oracle = label(scenario.world, scenario.request)
    owed = oracle.proposed.amount_cents if oracle.proposed else oracle.amount_cents
    path = set(oracle.considered) | {"2"} | ({"10"} if owed else set())
    return {"scenario": scenario.id, "action": ui_action(oracle.action.value), "amount_cents": owed,
            "must_cite": oracle.must_cite, "path": sorted(path)}


if __name__ == "__main__":
    scenario = next(s for s in ALL if s.id == SCENARIO)
    (HERE / "task/instruction.md").write_text(scenario.request.message + "\n")
    (HERE / "task/environment/postgres/seed.sql").write_text(seed_sql(scenario))
    (HERE / "task/tests/expected.json").write_text(json.dumps(expected(scenario), indent=2) + "\n")
    (HERE.parent / "agents/policy.md").write_text((HERE.parent / "docs/policy.md").read_text())
    print(f"built {SCENARIO}: {expected(scenario)}")
