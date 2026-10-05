"""A scenario's records as SQL tables, and back again.

seed_sql() writes what an agent is allowed to see. The ground-truth annotations on ticket messages are
left out; truth_of() returns them on their own, so a reference solution can put them back with
with_truth() after reading the records from the database.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from types import UnionType
from typing import Union, get_args, get_origin

from pydantic import BaseModel

from world.scenario import Request
from world.schema import World

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


def _columns(model: type[BaseModel], table: str) -> dict[str, str]:
    children = {field for (parent, field) in CHILD_TABLES if parent == table}
    return {f: column_type(info.annotation) for f, info in model.model_fields.items()
            if f not in HIDDEN and f not in children}


def _add(out: dict, table: str, model: type[BaseModel], records: list, parent: tuple[str, str | None] = ("", None)) -> None:
    key, parent_id = parent
    columns = _columns(model, table)
    out.setdefault(table, (({key: "text"} if key else {}) | columns, []))
    for record in records:
        out[table][1].append(([parent_id] if key else []) + [getattr(record, f) for f in columns])
    for (parent_table, field), (child, child_key) in CHILD_TABLES.items():
        if parent_table == table:
            child_model = get_args(model.model_fields[field].annotation)[0]
            _add(out, child, child_model, [], (child_key, None))
            for record in records:
                _add(out, child, child_model, getattr(record, field), (child_key, record.id))


def tables(world: World) -> dict[str, tuple[dict[str, str], list[list]]]:
    """Every table as (columns and their SQL types, rows). An empty list of records still makes a table."""
    out: dict = {}
    for name, field in World.model_fields.items():
        _add(out, name, get_args(field.annotation)[0], getattr(world, name))
    return out


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


def seed_sql(world: World, request: Request) -> str:
    """The records, who is asking, and an empty table for the agent's proposal."""
    sql = []
    for table, (columns, rows) in tables(world).items():
        sql.append(f"CREATE TABLE {table} ({', '.join(f'{c} {t}' for c, t in columns.items())});")
        sql += [f"INSERT INTO {table} VALUES ({', '.join(literal(v) for v in row)});" for row in rows]
    sql.append("CREATE TABLE request_context (workspace_id text, requester_user_id text, received_at timestamptz, "
               "channel text);")
    sql.append(f"INSERT INTO request_context VALUES ({literal(request.workspace_id)}, "
               f"{literal(request.requester_user_id)}, {literal(request.received_at)}, {literal(request.channel)});")
    sql.append("CREATE TABLE proposals (id serial PRIMARY KEY, action text, amount_cents bigint, sections jsonb, "
               "rationale text, created_at timestamptz DEFAULT now());")
    return "\n".join(sql) + "\n"


def world_from_rows(rows: dict[str, list[dict]]) -> World:
    """Rebuild the records from rows read out of the database, in the order they were inserted."""

    def record(model: type[BaseModel], table: str, row: dict) -> dict:
        types = _columns(model, table)
        return {c: json.loads(v) if types[c] == "jsonb" and isinstance(v, str) else v
                for c, v in row.items() if c in types}

    data = {}
    for name, field in World.model_fields.items():
        model = get_args(field.annotation)[0]
        data[name] = []
        for row in rows.get(name, []):
            item = record(model, name, row)
            for (parent_table, child_field), (child, key) in CHILD_TABLES.items():
                if parent_table == name:
                    child_model = get_args(model.model_fields[child_field].annotation)[0]
                    item[child_field] = [record(child_model, child, r) for r in rows.get(child, [])
                                         if r[key] == row["id"]]
            data[name].append(item)
    return World.model_validate(data)


def truth_of(world: World) -> dict[str, list[dict]]:
    """The annotations seed_sql leaves out: for each ticket, one entry per message."""
    return {t.id: [m.truth for m in t.messages] for t in world.tickets}


def with_truth(world: World, truth: dict[str, list[dict]]) -> World:
    out = world.model_copy(deep=True)
    for ticket in out.tickets:
        for message, annotation in zip(ticket.messages, truth.get(ticket.id, []), strict=True):
            message.truth = annotation
    return out
