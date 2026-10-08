"""Verifier: run the oracle on the task's scenario and compare the agent's proposal with its outcome.

The comparison is world.labeling's, the one the hand labels are scored with. The task says which
scenario it is through SCENARIO_ID, and Harbor copies the proposals out of the database into
/tmp/proposals.json before this runs.
"""

import json
import os
from pathlib import Path

from world.labeling import outcome_agrees, sections_agree, ui_action
from world.oracle import label
from world.history_scenarios import long_accounts
from world.scenarios import ALL

# A task built with long accounts says so, and is graded on the long account's own records.
scenarios = long_accounts() if os.environ.get("LONG_ACCOUNTS") == "1" else ALL
scenario = next(s for s in scenarios if s.id == os.environ["SCENARIO_ID"])
oracle = label(scenario.world, scenario.request)
owed = oracle.proposed.amount_cents if oracle.proposed else oracle.amount_cents

submitted = json.loads(Path("/tmp/proposals.json").read_text())
rewards = {"reward": 0.0, "action": 0.0, "amount": 0.0, "sections": 0.0, "proposals": len(submitted)}
if submitted:  # the last proposal is the one that counts
    row = submitted[-1]
    rewards |= {
        "reward": float(outcome_agrees(row, oracle)),
        "action": float(row["action"] == ui_action(oracle.action.value)),
        "amount": float(row["amount_cents"] == owed),
        "sections": float(sections_agree(row["sections"], oracle)),
    }

Path("/logs/verifier/reward.json").write_text(json.dumps(rewards))
print(json.dumps({
    "oracle": {"action": ui_action(oracle.action.value), "amount_cents": owed, "must_cite": oracle.must_cite,
               "rationale": oracle.rationale},
    "submitted": submitted, "rewards": rewards,
}, indent=2))
