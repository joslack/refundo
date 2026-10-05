"""Build the Harbor dataset: three shared images, and one small task folder per scenario.

    uv run python evals/build.py          the scenarios that have a hand label
    uv run python evals/build.py --all    every scenario

The dataset is the hand-labeled scenarios by default: for those, a person and the oracle agree on the
answer. The rest are checked only by tests written alongside the oracle.

Everything a trial runs is defined once, in evals/environment/:
    refundo-main        the agent's container
    refundo-mcp         the MCP server, the same for every task
    refundo-verifier    the oracle and the scoring, with the only copy of world/ a trial uses
    docker-compose.yaml Postgres and the MCP server beside the agent's container
    solve.py            the reference solution

A task folder in evals/tasks/ holds only what differs between scenarios:
    task.toml               which scenario it is, for the verifier and the reference solution
    instruction.md          the customer's message, which is all the agent is given
    environment/seed.sql    the scenario's records, and who is asking
    solution/solve.sh       starts the reference solution; Harbor wants one in every task

Nothing the agent can reach holds the answer. The seed leaves out the scenario id, the ground-truth
annotations on ticket messages, and the request fields that say which charge is meant. world/ is in the
verifier's image, and is mounted into the agent's container only when the oracle agent runs.

Run the tasks with evals/run.py.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from world.labeling import LABELS, load_labels
from world.scenarios import ALL
from world.sql import seed_sql

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ENVIRONMENT = HERE / "environment"
TASKS = HERE / "tasks"
SOLVE_SH = "#!/bin/bash\n# evals/run.py mounts the reference solution here for oracle runs.\npython /reference/solve.py\n"


def build_images() -> None:
    for image, context, dockerfile in [
        ("refundo-main", ENVIRONMENT / "main", ENVIRONMENT / "main/Dockerfile"),
        ("refundo-mcp", ENVIRONMENT / "mcp", ENVIRONMENT / "mcp/Dockerfile"),
        ("refundo-verifier", ROOT, ENVIRONMENT / "verifier/Dockerfile"),
    ]:
        subprocess.run(["docker", "build", "-q", "-t", image, "-f", str(dockerfile), str(context)], check=True)


def build_task(scenario, config: str) -> None:
    task = TASKS / scenario.id.lower()
    (task / "environment").mkdir(parents=True)
    (task / "solution").mkdir()
    for token, value in {"__NAME__": scenario.id.lower(), "__SCENARIO__": scenario.id,
                         "__DIFFICULTY__": scenario.difficulty, "__ARCHETYPE__": scenario.archetype}.items():
        config = config.replace(token, value)
    (task / "task.toml").write_text(config)
    (task / "instruction.md").write_text(scenario.request.message + "\n")
    (task / "environment/seed.sql").write_text(seed_sql(scenario.world, scenario.request))
    (task / "solution/solve.sh").write_text(SOLVE_SH)


if __name__ == "__main__":
    labeled = load_labels(LABELS / "jonah.jsonl")
    scenarios = ALL if "--all" in sys.argv else [s for s in ALL if s.id in labeled]
    build_images()
    shutil.rmtree(TASKS, ignore_errors=True)
    for scenario in scenarios:
        build_task(scenario, (HERE / "task.toml").read_text())
    # Harbor copies only agents/ into the agent's container, so the policy has to be inside it.
    (ROOT / "agents/policy.md").write_text((ROOT / "docs/policy.md").read_text())
    print(f"built 3 images and {len(scenarios)} of {len(ALL)} scenarios as tasks in {TASKS.relative_to(ROOT)}/")
