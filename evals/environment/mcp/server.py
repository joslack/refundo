"""MCP server over the scenario's database: the tools every agent design draws from.

The database holds a single scenario, so nothing here scopes queries to a workspace. With more than one
workspace in the database, it has to.
"""

import json
import os
from typing import Literal

import asyncpg
from fastmcp import FastMCP

mcp = FastMCP("quillstack")
HIDDEN_TABLES = {"proposals"}


async def rows(query: str, *args, readonly: bool = True) -> list[dict]:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        async with conn.transaction(readonly=readonly):
            found = await conn.fetch(query, *args)
    finally:
        await conn.close()
    return json.loads(json.dumps([dict(r) for r in found], default=str))


@mcp.tool
async def get_request_context() -> dict:
    """Who is asking, for which workspace, and when. This comes from the platform, not from the message."""
    context = (await rows("SELECT * FROM request_context"))[0]
    member = await rows("SELECT name FROM members WHERE id = $1", context["requester_user_id"])
    return context | {"requester_name": member[0]["name"] if member else None}


@mcp.tool
async def list_tables() -> dict[str, list[str]]:
    """The tables you can query with run_sql, and the columns of each."""
    columns = await rows("SELECT table_name, column_name || ' ' || data_type AS col FROM information_schema.columns "
                         "WHERE table_schema = 'public' ORDER BY table_name, ordinal_position")
    out: dict[str, list[str]] = {}
    for c in columns:
        if c["table_name"] not in HIDDEN_TABLES:
            out.setdefault(c["table_name"], []).append(c["col"])
    return out


@mcp.tool
async def run_sql(query: str) -> list[dict] | str:
    """Run one read-only SQL query (PostgreSQL) against the records. Returns at most 200 rows."""
    try:
        return (await rows(query))[:200]
    except asyncpg.PostgresError as e:
        return f"SQL error: {e}"


@mcp.tool
async def submit_proposal(
    action: Literal["cash_refund", "account_credit", "deny", "escalate", "no_action"],
    amount_cents: int,
    sections: list[str],
    rationale: str,
) -> str:
    """Record your decision. Call this once, when you have decided.

    amount_cents is before tax; for an escalation it is the amount you would have proposed.
    sections are the policy sections the decision rests on, such as ["5"] or ["4.2", "11"].
    """
    await rows("INSERT INTO proposals (action, amount_cents, sections, rationale) VALUES ($1, $2, $3, $4)",
               action, amount_cents, json.dumps(sections), rationale, readonly=False)
    return "Recorded."


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
