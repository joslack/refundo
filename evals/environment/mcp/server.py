"""MCP server over the scenario's database: the tools every agent design draws from.

There are two sets for reading records. One is a schema listing and read-only SQL. The other is a tool per kind
of record, each returning it with the policy's own terms worked out. Every agent finds out who is asking, and
records its decision, through the same two tools. Each agent graph lists the tools it uses.

The database holds a single scenario, so nothing here scopes queries to a workspace. With more than one
workspace in the database, it has to.
"""

import json
import os
from datetime import datetime
from typing import Literal

import asyncpg
from fastmcp import FastMCP

mcp = FastMCP("quillstack")
HIDDEN_TABLES = {"proposals"}
Action = Literal["cash_refund", "account_credit", "deny", "escalate", "no_action"]


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
async def submit_proposal(action: Action, amount_cents: int, sections: list[str], rationale: str) -> str:
    """Record your decision. Call this once, when you have decided.

    action:
      cash_refund     money back to the original payment method, in full or in part
      account_credit  credit on the account
      deny            the requester may ask, and no section of the policy grants a refund or credit
      escalate        a mandatory escalation condition holds
      no_action       the requester is not the Owner or a Billing Admin
    amount_cents is before tax. For an escalation it is the amount you would have proposed, or 0 if you would have
    proposed none.
    sections are the policy sections the decision rests on, such as ["5"] or ["4.2", "11"].
    """
    await rows("INSERT INTO proposals (action, amount_cents, sections, rationale) VALUES ($1, $2, $3, $4)",
               action, amount_cents, json.dumps(sections), rationale, readonly=False)
    return "Recorded."


# --- Structured tools -------------------------------------------------------
#
# Each returns one kind of record with the policy's own terms worked out: Amount Paid, Usage, and the role a
# person held at a given time. They state facts. Which rule applies, and what it grants, is left to the agent.


def when(value: str) -> datetime:
    return datetime.fromisoformat(value)


def parsed(value) -> dict:
    """A JSON column, which the database driver hands over as text."""
    return json.loads(value) if isinstance(value, str) else (value or {})


def role_then(member: dict | None, changes: list[dict], at: str) -> str | None:
    """The role a person held at a time: the current role, unwound through any later role changes. None if they
    were not a member then."""
    if member is None or when(at) < when(member["joined_at"]):
        return None
    if member["removed_at"] and when(at) >= when(member["removed_at"]):
        return None
    later = sorted((c for c in changes if parsed(c["data"]).get("user") == member["id"] and when(c["at"]) > when(at)
                    and "old_role" in parsed(c["data"])), key=lambda c: when(c["at"]))
    return parsed(later[0]["data"])["old_role"] if later else member["role"]


def charge_views(invoices: list[dict], lines: list[dict], refunds: list[dict], disputes: list[dict]) -> list[dict]:
    """Every charge, newest first, with its refunds and disputes."""
    out = []
    for inv in sorted(invoices, key=lambda i: when(i["created"]), reverse=True):
        refunded = [r for r in refunds if inv["charge_id"] and r["charge_id"] == inv["charge_id"]]
        out.append({
            "invoice_id": inv["id"], "charge_id": inv["charge_id"], "status": inv["status"],
            "charged_at": inv["created"],
            "kind": {"subscription_create": "first charge of a subscription", "subscription_cycle": "renewal",
                     "subscription_update": "plan change starting a new term", "manual": "manual"}[inv["billing_reason"]],
            "billing_period_start": inv["period_start"], "billing_period_end": inv["period_end"],
            "billing_period_days": (when(inv["period_end"]) - when(inv["period_start"])).days,
            "lines": [{"description": line["description"], "tier": line["tier"], "interval": line["interval"],
                       "seats": line["seats"], "list_price_cents": line["amount"]}
                      for line in lines if line["invoice_id"] == inv["id"]],
            "list_price_cents": inv["subtotal"], "discount_cents": inv["discount"],
            "account_credit_applied_cents": inv["credit_applied"],
            "amount_paid_cents": inv["amount_paid"] - inv["tax"],
            "tax_cents": inv["tax"], "charged_to_card_cents": inv["amount_paid"],
            "refunds": [{"refund_id": r["id"], "amount_cents": r["amount"], "refunded_at": r["created"], "reason": r["reason"],
                         "basis": parsed(r["metadata"]).get("quillstack_basis"),
                         "for_ticket": parsed(r["metadata"]).get("quillstack_ticket")} for r in refunded],
            "refunded_cents": sum(r["amount"] for r in refunded),
            "disputes": [{"dispute_id": d["id"], "status": d["status"], "amount_cents": d["amount"], "opened_at": d["created"],
                          "reason": d["reason"]} for d in disputes if inv["charge_id"] and d["charge_id"] == inv["charge_id"]],
        })
    return out


def usage_view(events: list[dict], since: str, until: str) -> dict:
    """Usage between two times, as §2 defines it: a session with a login followed by a create, edit or export."""
    sessions: dict[str, list[dict]] = {}
    for e in events:
        sessions.setdefault(e["session_id"], []).append(e)
    found, quiet = [], 0
    for session_id, in_session in sessions.items():
        logins = [when(e["at"]) for e in in_session if e["action"] == "login"]
        active = sorted(when(e["at"]) for e in in_session if e["action"] in {"create", "edit", "export"}
                        and logins and when(e["at"]) >= min(logins) and when(since) <= when(e["at"]) <= when(until))
        if active:
            found.append({"session_id": session_id, "user_id": in_session[0]["user_id"], "first_usage_at": str(active[0]),
                          "last_usage_at": str(active[-1])})
        elif any(when(since) <= when(e["at"]) <= when(until) for e in in_session):
            quiet += 1
    days = sorted({str(when(e["at"]).date()) for e in events if e["action"] in {"create", "edit", "export"}
                   and when(since) <= when(e["at"]) <= when(until)
                   and any(s["session_id"] == e["session_id"] for s in found)})
    return {"since": since, "until": until, "sessions_with_usage": sorted(found, key=lambda s: s["first_usage_at"]),
            "days_with_usage": days, "sessions_without_usage": quiet}


async def request_time() -> str:
    return (await rows("SELECT received_at FROM request_context"))[0]["received_at"]


@mcp.tool
async def get_requester() -> dict:
    """The person asking and the role they hold in the workspace at the time of the request."""
    context = (await rows("SELECT * FROM request_context"))[0]
    found = await rows("SELECT * FROM members WHERE id = $1", context["requester_user_id"])
    changes = await rows("SELECT * FROM app_events WHERE type = 'member_role_changed'")
    member = found[0] if found else None
    if member is None:
        return {"user_id": context["requester_user_id"], "member_of_workspace": False, "role": None}
    return {"user_id": member["id"], "name": member["name"], "email": member["email"],
            "member_of_workspace": role_then(member, changes, context["received_at"]) is not None,
            "role": role_then(member, changes, context["received_at"]),
            "joined_at": member["joined_at"], "removed_at": member["removed_at"]}


@mcp.tool
async def list_charges() -> list[dict]:
    """Every charge on the account, newest first, each with the refunds and disputes against it.

    amount_paid_cents is the policy's Amount Paid: what was charged after discounts and applied account credit,
    before tax. charged_to_card_cents includes tax. All amounts are in cents.
    """
    return charge_views(await rows("SELECT * FROM invoices"), await rows("SELECT * FROM invoice_lines"),
                        await rows("SELECT * FROM refunds"), await rows("SELECT * FROM disputes"))


@mcp.tool
async def get_subscription() -> dict:
    """The workspace, its plan over time, the payment processor's subscription record, and the app's log of
    cancellations, plan changes, seat changes and suspensions.

    A cancellation can be recorded in two places: the app's event log and the processor's `canceled_at`. Each
    event that a person caused carries the role that person held at that moment.
    """
    workspace = (await rows("SELECT * FROM workspaces"))[0]
    members = {m["id"]: m for m in await rows("SELECT * FROM members")}
    events = await rows("SELECT * FROM app_events ORDER BY at")
    changes = [e for e in events if e["type"] == "member_role_changed"]
    subscription = await rows("SELECT * FROM subscriptions WHERE customer = $1", workspace["stripe_customer_id"])
    return {
        "workspace": {k: workspace[k] for k in ("id", "name", "status", "suspension_reason", "created_at")},
        "plan_history": await rows("SELECT tier, interval, seats, effective_at FROM plan_history ORDER BY effective_at"),
        "processor_subscription": {k: subscription[0][k] for k in ("status", "current_period_start", "current_period_end",
                                                                   "canceled_at")} if subscription else None,
        "app_events": [{"event_id": e["id"], "type": e["type"], "at": e["at"], "by_user_id": e["actor_user_id"],
                        "by_role_then": role_then(members.get(e["actor_user_id"]), changes, e["at"]),
                        "details": parsed(e["data"])} for e in events if e["type"] != "member_role_changed"],
    }


@mcp.tool
async def get_usage(since: str, until: str | None = None) -> dict | str:
    """Usage between two times, as the policy defines it: a session with a login followed by at least one create,
    edit or export. Views alone are not Usage.

    since and until are timestamps such as 2026-03-01T00:00:00+00:00. until defaults to the time of the request.
    days_with_usage lists each calendar day that had Usage.
    """
    until = until or await request_time()
    try:
        when(since), when(until)
    except ValueError:
        return "since and until must be timestamps such as 2026-03-01T00:00:00+00:00."
    return usage_view(await rows("SELECT * FROM session_events"), since, until)


@mcp.tool
async def list_tickets() -> list[dict]:
    """Every support ticket with its messages, oldest first. A message from a customer carries the role its
    author held in the workspace when they wrote it."""
    members = {m["id"]: m for m in await rows("SELECT * FROM members")}
    changes = await rows("SELECT * FROM app_events WHERE type = 'member_role_changed'")
    messages = await rows("SELECT * FROM ticket_messages ORDER BY at")
    return [{"ticket_id": t["id"], "subject": t["subject"], "opened_by_user_id": t["opened_by"], "opened_at": t["opened_at"],
             "status": t["status"],
             "messages": [{"from": m["author_type"], "author_id": m["author_id"], "author_name": m["author_name"],
                           "author_role_then": role_then(members.get(m["author_id"]), changes, m["at"])
                           if m["author_type"] == "customer" else None,
                           "at": m["at"], "body": m["body"]} for m in messages if m["ticket_id"] == t["id"]]}
            for t in await rows("SELECT * FROM tickets ORDER BY opened_at")]


@mcp.tool
async def list_account_credits() -> list[dict]:
    """Every change to the customer's account credit balance, oldest first. A negative amount is credit given to
    the customer."""
    return [{"credit_id": c["id"], "amount_cents": c["amount"], "at": c["created"], "description": c["description"],
             "details": parsed(c["metadata"])} for c in await rows("SELECT * FROM credits ORDER BY created")]


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
