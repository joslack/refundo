"""Reference solution: read the records through the MCP server, run the oracle on them, submit its outcome.

It reads every table with the same SQL tool an agent has, so it only reaches the right answer if the
records the server exposes are enough to decide the case. Two things come from the scenario and not
from the database, because they exist only as annotations: which charge the request is about, and the
facts written in free text in tickets. An agent has to work those out by reading.

evals/run.py mounts this file and world/ into the agent's container for oracle runs only.
"""

import asyncio
import os

from fastmcp import Client

from world.labeling import ui_action
from world.oracle import label
from world.scenarios import ALL
from world.sql import truth_of, with_truth, world_from_rows


async def main() -> None:
    scenario = next(s for s in ALL if s.id == os.environ["SCENARIO_ID"])
    async with Client("http://mcp:8000/mcp") as mcp:
        tables = (await mcp.call_tool("list_tables", {})).data
        rows = {t: (await mcp.call_tool("run_sql", {"query": f"SELECT * FROM {t} ORDER BY ctid"})).data
                for t in tables}
        world = with_truth(world_from_rows(rows), truth_of(scenario.world))
        outcome = label(world, scenario.request)
        owed = outcome.proposed.amount_cents if outcome.proposed else outcome.amount_cents
        await mcp.call_tool("submit_proposal", {
            "action": ui_action(outcome.action.value), "amount_cents": owed,
            "sections": outcome.must_cite, "rationale": outcome.rationale,
        })
        print(f"submitted {ui_action(outcome.action.value)} {owed} {outcome.must_cite}")


asyncio.run(main())
