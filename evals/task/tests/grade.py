"""Verifier for the spike: compare the agent's proposal with the oracle's outcome.

The section check repeats world.labeling.sections_agree. The real verifier should import it instead,
from an image that has world/ in it.
"""

import asyncio
import json
import os
from pathlib import Path

import asyncpg


async def proposals() -> list[dict]:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        return [dict(r) for r in await conn.fetch("SELECT * FROM proposals ORDER BY id")]
    finally:
        await conn.close()


expected = json.loads(Path("/tests/expected.json").read_text())
submitted = asyncio.run(proposals())
last = submitted[-1] if submitted else {"action": None, "amount_cents": None, "sections": "[]"}
picked = json.loads(last["sections"])

action = last["action"] == expected["action"]
amount = last["amount_cents"] == expected["amount_cents"]
named = all(s in picked or s.split(".")[0] in picked for s in expected["must_cite"])
sections = bool(picked) and named and all(p in expected["path"] for p in picked)

rewards = {"reward": float(action and amount), "action": float(action), "amount": float(amount),
           "sections": float(sections), "proposals": len(submitted)}
Path("/logs/verifier/reward.json").write_text(json.dumps(rewards))
print(json.dumps({"expected": expected, "submitted": submitted, "rewards": rewards}, default=str, indent=2))
