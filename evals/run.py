"""Run the dataset under Harbor.

    uv run python evals/run.py oracle                        the reference solution on every task
    uv run python evals/run.py sql                           the agent graph `sql`, default model, default settings
    uv run python evals/run.py sql -m mini,deepseek          chosen models, by short name below or full id
    uv run python evals/run.py sql -m all                    every model below
    uv run python evals/run.py sql -m all --efforts all      every model at each reasoning effort it accepts
    uv run python evals/run.py sql -m luna --efforts default,low,high    "default" is the model with no effort set
    uv run python evals/run.py sql -i an-07 -n 2             anything else goes to `harbor run`

Each model and reasoning effort is its own Harbor job, recorded in LangSmith as its own experiment on the
dataset below and named graph-model-effort-commit-time. Several jobs run at once, never two on the same
model, because providers limit tokens per minute per model.

The agent code is copied when the command starts and every job uses that copy, so the repo can change
while jobs run without changing what they test.
"""

import ast
import json
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
JOBS = HERE / "jobs"
DATASET = "quillstack-refund-requests"
FIREWORKS = "fireworks/accounts/fireworks/models/"
# Short name -> (model id as Harbor expects it, the reasoning efforts the model accepts). Checked 2026-10-05.
MODELS = {
    "luna": ("openai/gpt-6-luna", ["none", "low", "medium", "high", "xhigh", "max"]),
    "luna-5.6": ("openai/gpt-5.6-luna", ["none", "low", "medium", "high", "xhigh", "max"]),
    "mini": ("openai/gpt-5.4-mini", ["none", "low", "medium", "high", "xhigh"]),
    "nano": ("openai/gpt-5.4-nano", ["none", "low", "medium", "high", "xhigh"]),
    "deepseek": (FIREWORKS + "deepseek-v4p1-flash", ["none", "low", "medium", "high"]),
    "glm": (FIREWORKS + "glm-5p3-flash", ["low", "medium", "high"]),
    "nemotron": (FIREWORKS + "nemotron-lightning-3p5-30b-a3b", ["none", "low", "medium", "high"]),
    "oss": (FIREWORKS + "gpt-oss-120b", ["low", "medium", "high"]),
}
DEFAULT_MODEL = "luna"
EFFORT_ORDER = ["low", "high", "none", "medium", "xhigh", "max"]  # the order a sweep runs them in
JOBS_AT_ONCE = 6
TRIALS_AT_ONCE = 3  # per job; 18 trials in all. Jobs paced by a token limit are mostly waiting, so more of them fit
SLOW = {"high": 2, "xhigh": 3, "max": 4}  # how much longer a model call and a trial may take at these efforts
# OpenAI allows this many tokens a minute per model on this account (read from its API on 2026-10-05). A fast
# model with four trials at once goes well past it, so each trial is told how many calls a minute it may make.
TOKENS_PER_MINUTE = {"openai/gpt-5.6-luna": 500_000, "openai/": 200_000}
TOKENS_PER_CALL = {None: 5_500, "high": 8_000, "xhigh": 12_000, "max": 16_000}  # a rough size, larger with more reasoning
REQUEST_TIMEOUT = 180  # seconds for one model call at the other efforts, as in the agent


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=ROOT).stdout.strip()


def take(extra: list[str], flag: str) -> str | None:
    """Remove `flag value` from the arguments and return the value."""
    if flag not in extra:
        return None
    at = extra.index(flag)
    value = extra[at + 1]
    del extra[at:at + 2]
    return value


def tools_of(graph: str, agents: Path) -> list[str]:
    """The tools a graph allows itself: the TOOLS set in its source file."""
    path = json.loads((agents / "langgraph.json").read_text())["graphs"][graph].split(":")[0]
    for node in ast.parse((agents / path).read_text()).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "TOOLS":
            return sorted(ast.literal_eval(node.value))
    return []


def harbor(extra: list[str]) -> list[str]:
    concurrency = [] if "-n" in extra else ["-n", str(TRIALS_AT_ONCE)]
    return ["harbor", "run", "-p", str(HERE / "tasks"), "-o", str(JOBS), "-y",
            "--extra-docker-compose", str(HERE / "environment/docker-compose.yaml"), *concurrency]


def oracle_command(extra: list[str], stamp: str) -> list[str]:
    # The reference solution needs the oracle. Only these runs get world/ in the agent's container.
    mounts = [{"type": "bind", "source": str(ROOT / "world"), "target": "/reference/world", "read_only": True},
              {"type": "bind", "source": str(HERE / "environment/solve.py"), "target": "/reference/solve.py",
               "read_only": True}]
    return harbor(extra) + ["-a", "oracle", "--mounts", json.dumps(mounts), "--job-name", f"oracle-{stamp}"] + extra


def calls_per_minute(model: str, effort: str | None, extra: list[str]) -> float | None:
    """A trial's share of the model's token limit, as calls a minute; None where no limit binds."""
    limit = next((v for k, v in TOKENS_PER_MINUTE.items() if model.startswith(k)), None)
    if limit is None:
        return None
    trials = int(extra[extra.index("-n") + 1]) if "-n" in extra else TRIALS_AT_ONCE
    return round(0.9 * limit / TOKENS_PER_CALL.get(effort, TOKENS_PER_CALL[None]) / trials, 1)


def plugin_settings(**settings) -> list[str]:
    """Settings for the plugin, each written as JSON. Harbor reads a bare value as a number where it can, and
    the commit 61e1870 came out as infinity."""
    return [arg for key, value in settings.items() for arg in ("--pk", f"{key}={json.dumps(value)}")]


def agent_command(graph: str, model: str, effort: str | None, agents: Path, commit: str, extra: list[str]) -> list[str]:
    short = model.split("/")[-1]
    stamp = datetime.now().strftime("%m%d-%H%M%S")
    model_kwargs = {"reasoning_effort": effort} | ({"timeout": REQUEST_TIMEOUT * SLOW[effort]} if effort in SLOW else {})
    settings = ["--ak", "model_kwargs=" + json.dumps(model_kwargs)] if effort else []
    patience = ["--agent-timeout-multiplier", str(SLOW[effort])] if effort in SLOW else []
    pace = calls_per_minute(model, effort, extra)
    settings += ["--ak", "configurable=" + json.dumps({"calls_per_minute": pace})] if pace else []
    return harbor(extra) + [
        "-a", "langgraph", "-m", model, "--ak", f"project_path={agents}", "--ak", f"graph={graph}", *settings,
        "--env-file", str(ROOT / ".env"), "--max-retries", "2", *patience,
        "--plugin", "evals.langsmith_plugin:Annotated", *plugin_settings(
            dataset_name=DATASET, graph=graph, model=short, reasoning_effort=effort or "default", commit=commit,
            tools=tools_of(graph, agents)),
        "--job-name", f"{graph}-{short}-{effort or 'default'}-{commit}-{stamp}"] + extra


def run_all(commands: list[tuple[str, list[str]]]) -> int:
    """Run the jobs a few at a time, in order, skipping past any whose model already has a job running."""
    lock, busy, failed = threading.Lock(), set(), []

    def worker() -> None:
        while True:
            with lock:
                pick = next((c for c in commands if c[0] not in busy), None)
                if pick is None:
                    if not commands:
                        return
                else:
                    commands.remove(pick)
                    busy.add(pick[0])
            if pick is None:  # every job left is for a model that is running; wait for one to finish
                threading.Event().wait(5)
                continue
            model, cmd = pick
            name = cmd[cmd.index("--job-name") + 1]
            print(f"{datetime.now():%H:%M:%S} start  {name}", flush=True)
            with (JOBS / f"{name}.log").open("w") as log:
                # Harbor imports the plugin, and the plugin imports world/, so both need the repo on the path.
                code = subprocess.run(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                      env={**__import__("os").environ, "PYTHONPATH": str(ROOT)}).returncode
            print(f"{datetime.now():%H:%M:%S} finish {name}{'' if code == 0 else f' (exit {code})'}", flush=True)
            with lock:
                busy.discard(model)
                if code:
                    failed.append(name)

    threads = [threading.Thread(target=worker) for _ in range(JOBS_AT_ONCE)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return len(failed)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    graph, extra = sys.argv[1], sys.argv[2:]
    JOBS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%m%d-%H%M%S")
    if graph == "oracle":
        cmd = oracle_command(extra, stamp)
        print(" ".join(cmd), flush=True)
        sys.exit(subprocess.run(cmd, cwd=ROOT).returncode)

    chosen = take(extra, "-m") or DEFAULT_MODEL
    efforts = take(extra, "--efforts")
    names = list(MODELS) if chosen == "all" else chosen.split(",")
    commit = git("rev-parse", "--short", "HEAD") + ("+" if git("status", "--porcelain") else "")
    agents = JOBS / "_agents" / f"{commit}-{stamp}"
    shutil.copytree(ROOT / "agents", agents, ignore=shutil.ignore_patterns("__pycache__"))

    plan: list[tuple[str | None, str]] = []  # (effort, model name), in the order to run
    if efforts is None:
        plan = [(None, n) for n in names]
    else:
        wanted = EFFORT_ORDER if efforts == "all" else efforts.split(",")
        plan = [(None if e == "default" else e, n) for e in wanted for n in names
                if e == "default" or n not in MODELS or e in MODELS[n][1]]
    commands = [(MODELS.get(n, (n,))[0], agent_command(graph, MODELS.get(n, (n,))[0], e, agents, commit, list(extra)))
                for e, n in plan]
    print(f"{len(commands)} jobs, {JOBS_AT_ONCE} at a time; each job's output is in {JOBS.relative_to(ROOT)}/<job name>.log", flush=True)
    sys.exit(run_all(commands))
