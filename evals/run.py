"""Run the dataset under Harbor.

    uv run python evals/run.py oracle                    the reference solution on every task
    uv run python evals/run.py sql                       the agent graph `sql`, on the default model
    uv run python evals/run.py sql -m haiku              one model, by its short name below or its full id
    uv run python evals/run.py sql -m luna,haiku,glm     several models, one run each
    uv run python evals/run.py sql -m all -i an-07       anything else goes to `harbor run`

Each agent run is recorded in LangSmith as its own experiment on the dataset refundo-scenarios, named
after the graph, the model and the commit, so the models can be compared there and a score can be traced
to the code that produced it.

This wraps `harbor run` because the command needs absolute paths in three places. It prints each command
it runs.
"""

import json
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# Short names for -m. A full id works too, written provider/model as Harbor expects.
MODELS = {
    "luna": "openai/gpt-6-luna",
    "haiku": "anthropic/claude-haiku-4-5",
    "deepseek": "fireworks/accounts/fireworks/models/deepseek-v4p1-flash",
    "glm": "fireworks/accounts/fireworks/models/glm-5p3-flash",
}
DEFAULT_MODEL = "luna"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=ROOT).stdout.strip()


def command(name: str, model: str | None, extra: list[str]) -> list[str]:
    stamp = datetime.now().strftime("%m%d-%H%M%S")
    cmd = ["harbor", "run", "-p", str(HERE / "tasks"), "-o", str(HERE / "jobs"), "-y",
           "--extra-docker-compose", str(HERE / "environment/docker-compose.yaml")]
    if name == "oracle":
        # The reference solution needs the oracle. Only these runs get world/ in the agent's container.
        mounts = [{"type": "bind", "source": str(ROOT / "world"), "target": "/reference/world", "read_only": True},
                  {"type": "bind", "source": str(HERE / "environment/solve.py"), "target": "/reference/solve.py",
                   "read_only": True}]
        return cmd + ["-a", "oracle", "--mounts", json.dumps(mounts), "--job-name", f"oracle-{stamp}"] + extra
    commit = git("rev-parse", "--short", "HEAD") + ("+" if git("status", "--porcelain") else "")
    return cmd + ["-a", "langgraph", "-m", model, "--ak", f"project_path={ROOT / 'agents'}", "--ak", f"graph={name}",
                  "--env-file", str(ROOT / ".env"), "--plugin", "langsmith", "--pk", "dataset_name=refundo-scenarios",
                  "--job-name", f"{name}-{model.split('/')[-1]}-{commit}-{stamp}"] + extra


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    name, extra = sys.argv[1], sys.argv[2:]
    chosen = DEFAULT_MODEL
    if "-m" in extra:
        at = extra.index("-m")
        chosen = extra[at + 1]
        del extra[at:at + 2]
    names = list(MODELS) if chosen == "all" else chosen.split(",")
    models = [None] if name == "oracle" else [MODELS.get(m, m) for m in names]
    failed = 0
    for model in models:
        cmd = command(name, model, extra)
        print(shlex.join(cmd), flush=True)
        failed += subprocess.run(cmd, cwd=ROOT).returncode != 0
    sys.exit(failed)
