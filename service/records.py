"""What has been issued on an account: the refunds and credits that followed a recorded decision.

A proposal is a decision. Issuing is the act that follows a decision to refund or to credit, and here it is one
row in the account's database in place of a call to a payment processor. The row says which conversation it came
from and who approved it, which is the agent when the decision did not wait for a person.
"""

import asyncpg

from accounts import database_url

ISSUED = ("CREATE TABLE IF NOT EXISTS issued (id serial PRIMARY KEY, thread_id text, kind text, amount_cents bigint, "
          "approved_by text, issued_at timestamptz DEFAULT now());")
KINDS = {"cash_refund", "account_credit"}  # the actions that move money; nothing is issued on the others
AGENT = "the agent, with no review"


async def issue(account: str, thread_id: str, kind: str, amount_cents: int, approved_by: str) -> None:
    conn = await asyncpg.connect(database_url(account))
    try:
        await conn.execute("INSERT INTO issued (thread_id, kind, amount_cents, approved_by) VALUES ($1, $2, $3, $4)",
                           thread_id, kind, amount_cents, approved_by)
    finally:
        await conn.close()


async def issued(account: str, thread_id: str) -> list[dict]:
    """What was issued in one conversation, oldest first."""
    conn = await asyncpg.connect(database_url(account))
    try:
        found = await conn.fetch("SELECT kind, amount_cents, approved_by, issued_at FROM issued WHERE thread_id = $1 "
                                 "ORDER BY id", thread_id)
    finally:
        await conn.close()
    return [dict(r) | {"issued_at": r["issued_at"].isoformat(timespec="seconds")} for r in found]
