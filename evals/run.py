"""Run the dataset under Harbor.

    uv run python evals/run.py oracle                        the reference solution on every task
    uv run python evals/run.py sql                           the agent graph `sql`, default model, default settings
    uv run python evals/run.py sql -m mini,deepseek          chosen models, by short name below or full id
    uv run python evals/run.py sql -m all                    every model below
    uv run python evals/run.py sql -m all --efforts all      every model at each reasoning effort it accepts
    uv run python evals/run.py sql -m luna --efforts default,low,high    "default" is the model with no effort set
    uv run python evals/run.py sql,structured -m deepseek:medium,luna:high    two graphs, each model at one effort
    uv run python evals/run.py sql -m luna -k 5              each case five times, in the one experiment
    uv run python evals/run.py sql --dataset scratch         record in another LangSmith dataset, for a trial run
    uv run python evals/run.py sql -i an-07 -n 2             anything else goes to `harbor run`
    uv run python evals/run.py resume <job> [<job> ...]      run the trials a job still lacks, into the same experiment
    uv run python evals/run.py resume <commit>               the same for every job named with that commit

Each model and reasoning effort is its own Harbor job, recorded in LangSmith as its own experiment on the
dataset below and named graph-model-effort-commit-time. Several jobs run at once, never two on the same
model, because providers limit tokens per minute per model.

Before any job starts, each model is asked one sum, so an account without credits or a broken endpoint stops
the run and not the first experiment, and LangSmith is asked whether it is taking traces and how much of the
workspace's monthly limits is left, so a run it could not record is not started. `--no-langsmith-check` skips
the second.

While a job runs its folder is read every few seconds, and the job is stopped, with the reason printed, when:
    LangSmith stops taking its records or the agent's traces      no other job is started
    the provider's account is out of credits                      that provider's other jobs are dropped
    its last few cases all came to nothing                        that model's other jobs are dropped
    every running case has had a model call sent again and none has finished for some minutes    the same
Lines marked `notice` say when a case had a model call sent again or its trace refused. evals/limits.py holds
how each of these is read from a trial's files.

A trial that a rate limit, an overloaded provider, an empty account or a stopped job kept from running is
counted as not run, never as a wrong answer. `resume` runs those trials again, with any the job never
reached, in the same job folder and the same experiment: it first sends LangSmith the records it refused
earlier, moves the folders of the trials that were not run to jobs/_not_run/, and then has Harbor continue the
job from its own config. A job is resumed with the agent code and settings it started with.

None of this waits or retries inside a trial, so none of it is inside a measured time. A provider that fails is
answered by stopping and by running whole trials again later. The model client's own retries stay as the agent
sets them; a job on an OpenAI model is started with that client's request log on (OPENAI_LOG=info in the
agent's container; `--no-request-log` leaves it off), because the client otherwise sends a call again without a
word and the wait cannot be told from a slow answer. A trial whose log shows a call sent again keeps its
answer and is left out of the seconds.

The agent code is copied when the command starts and every job uses that copy, so the repo can change
while jobs run without changing what they test.
"""

import ast
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import limits  # noqa: E402

JOBS = HERE / "jobs"
NOT_RUN = JOBS / "_not_run"  # where `resume` moves the folders of trials that were not run, by job
# The name carries a version. Every experiment in a dataset is graded against one answer key and sees one
# description of the submit tool, so a change to either gets a new name.
DATASET = "quillstack-refund-requests-v2"
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
# OpenAI allows 200,000 tokens a minute per model on this account (read from its API on 2026-10-05). These two
# models make 15 to 20 calls a case and go past it with three trials at once, so each of their trials is told how
# many calls a minute it may make. A paced trial can wait before a call, and the wait counts as model time, so no
# other model is paced and a paced experiment says so in its LangSmith metadata.
TOKENS_PER_MINUTE = {"openai/gpt-5.4-mini": 200_000, "openai/gpt-5.4-nano": 200_000}
TOKENS_PER_CALL = {None: 5_500, "high": 8_000, "xhigh": 12_000, "max": 16_000}  # a rough size, larger with more reasoning
REQUEST_TIMEOUT = 180  # seconds for one model call at the other efforts, as in the agent
# Where a provider's models answer a plain chat request, the key that pays for it, and the name of its token cap.
PROVIDERS = {"openai/": ("https://api.openai.com/v1/chat/completions", "OPENAI_API_KEY", "max_completion_tokens"),
             "fireworks/": ("https://api.fireworks.ai/inference/v1/chat/completions", "FIREWORKS_API_KEY", "max_tokens")}
LANGSMITH = "https://api.smith.langchain.com"
BROKEN_AFTER = 6  # a job whose last cases all came to nothing, this many in a row, is stopped
# Seconds, times the job's allowance for its effort. A job is stopped when every case it is running has had a
# model call sent again and none has finished for this long. A healthy job finishes a case every minute or two.
STALLED_AFTER = 300
LOOK_EVERY = 20  # seconds between two readings of a running job's folder


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=ROOT).stdout.strip()


def clock() -> str:
    return datetime.now().strftime("%H:%M:%S")


def take(extra: list[str], flag: str) -> str | None:
    """Remove `flag value` from the arguments and return the value."""
    if flag not in extra:
        return None
    at = extra.index(flag)
    value = extra[at + 1]
    del extra[at:at + 2]
    return value


def switch(extra: list[str], flag: str) -> bool:
    """Remove `flag` from the arguments and return whether it was there."""
    found = flag in extra
    if found:
        extra.remove(flag)
    return found


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


def agent_command(graph: str, model: str, effort: str | None, agents: Path, commit: str, extra: list[str],
                  dataset: str = DATASET, request_log: bool = True) -> tuple[str, str, list[str]]:
    """One job: the model it runs on, its name, and the command that starts it."""
    short = model.split("/")[-1]
    stamp = datetime.now().strftime("%m%d-%H%M%S")
    name = f"{graph}-{short}-{effort or 'default'}-{commit}-{stamp}"
    model_kwargs = {"reasoning_effort": effort} | ({"timeout": REQUEST_TIMEOUT * SLOW[effort]} if effort in SLOW else {})
    settings = ["--ak", "model_kwargs=" + json.dumps(model_kwargs)] if effort else []
    patience = ["--agent-timeout-multiplier", str(SLOW[effort])] if effort in SLOW else []
    pace = calls_per_minute(model, effort, extra)
    settings += ["--ak", "configurable=" + json.dumps({"calls_per_minute": pace})] if pace else []
    # OpenAI's client sends a failed call again without writing anything unless its request log is on. With the log,
    # a call sent again shows in the agent's log as it does for the Fireworks models, and the trial's seconds are
    # not used. It adds one line per request and changes nothing the agent does.
    settings += ["--ae", "OPENAI_LOG=info"] if request_log and model.startswith("openai/") else []
    return model, name, harbor(extra) + [
        "-a", "langgraph", "-m", model, "--ak", f"project_path={agents}", "--ak", f"graph={graph}", *settings,
        "--env-file", str(ROOT / ".env"), "--max-retries", "2", *patience,
        "--plugin", "evals.langsmith_plugin:Annotated", *plugin_settings(
            dataset_name=dataset, graph=graph, model=short, reasoning_effort=effort or "default", commit=commit,
            tools=tools_of(graph, agents), paced=bool(pace)),
        "--job-name", name] + extra


def env_file() -> dict[str, str]:
    """The keys in .env, the file Harbor is also given."""
    pairs = (line.split("=", 1) for line in (ROOT / ".env").read_text().splitlines() if "=" in line and not line.startswith("#"))
    return {key.strip(): value.strip().strip('"\'') for key, value in pairs}


def unsound(model: str, keys: dict[str, str]) -> str | None:
    """Ask the model one sum. Returns what is wrong, or None: it answered, and the answer holds the number."""
    known = next((v for k, v in PROVIDERS.items() if model.startswith(k)), None)
    if known is None:
        return None  # a provider this does not know how to ask
    url, key, cap = known
    body = {"model": model.split("/", 1)[1], cap: 3000,
            "messages": [{"role": "user", "content": "What is 17 times 3? Reply with the number only."}]}
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Authorization": f"Bearer {keys.get(key, '')}", "Content-Type": "application/json"})
    try:
        answer = json.load(urllib.request.urlopen(request, timeout=120))["choices"][0]["message"].get("content") or ""
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"
    except (OSError, ValueError, KeyError, IndexError) as e:
        return f"{type(e).__name__}: {e}"
    return None if "51" in answer and len(answer) < 200 else f"answered {answer[:80]!r} to 17 times 3"


def langsmith(method: str, path: str, keys: dict[str, str], body=None, query: dict | None = None) -> tuple[int, str]:
    """One request to LangSmith with the key in .env, the way the plugin makes them. Returns the status and the
    text of the answer, with status 0 where LangSmith could not be reached."""
    endpoint = (keys.get("LANGSMITH_ENDPOINT") or LANGSMITH).rstrip("/")
    url = (endpoint if endpoint.endswith("/api/v1") else endpoint + "/api/v1") + path
    headers = {"x-api-key": keys.get("LANGSMITH_API_KEY", ""), "Content-Type": "application/json"}
    if keys.get("LANGSMITH_WORKSPACE_ID"):
        headers["X-Tenant-Id"] = keys["LANGSMITH_WORKSPACE_ID"]
    request = urllib.request.Request(url + ("?" + urllib.parse.urlencode(query) if query else ""), method=method,
                                     data=None if body is None else json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as answer:
            return answer.status, answer.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except OSError as e:
        return 0, f"{type(e).__name__}: {e}"


def used_this_month(keys: dict[str, str], tier: str | None, now: datetime) -> int | None:
    """The traces LangSmith has counted since the month began, in UTC; of one retention tier where one is named.
    None where it does not say: the figure is the organisation's billing usage, which a key may not be let read."""
    query = {"start_time": now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(),
             "end_time": now.isoformat(), "group_by": "workspace"}
    query |= {"trace_tier": tier} if tier else {}
    query |= {"workspace_ids": keys["LANGSMITH_WORKSPACE_ID"]} if keys.get("LANGSMITH_WORKSPACE_ID") else {}
    status, text = langsmith("GET", "/orgs/current/billing/granular-usage", keys, query=query)
    try:
        return sum(int(record["traces"]) for record in json.loads(text)["usage"]) if status == 200 else None
    except (ValueError, KeyError, TypeError):
        return None


def unrecorded(traces: int, keys: dict[str, str], now: datetime | None = None) -> tuple[str | None, list[str]]:
    """Ask LangSmith whether it will record a run of this many trials. Returns what is wrong, or None, and what it
    said about the workspace's limits.

    A trial is one trace. It is tied to a dataset example, which LangSmith keeps under extended retention, so it
    counts against a limit on those traces as well as against a limit on all traces. LangSmith answers plainly
    whether it is refusing traces now. How many are left this month is the limit less the billing usage it reports,
    which is an estimate: the two may not be counted over the same moments."""
    def answer(path: str):
        status, text = langsmith("GET", path, keys)
        try:
            return status, text, json.loads(text) if status == 200 else None
        except ValueError:
            return status, text, None

    now = now or datetime.now(timezone.utc)
    if not keys.get("LANGSMITH_API_KEY"):
        return "there is no LANGSMITH_API_KEY in .env", []
    status, text, state = answer("/workspaces/current/usage_limits")
    if not isinstance(state, dict):
        return f"it did not say whether it is taking traces (HTTP {status}: {text.strip()[:200]})", []
    if state.get("in_reject_set"):
        return (f"it is refusing traces now: the workspace is over its {state.get('usage_limit_type')} limit"
                f" of {state.get('tenant_limit')}"), []
    status, text, listed = answer("/usage-limits")
    if not isinstance(listed, list):
        return None, [f"it did not list the workspace's limits (HTTP {status}), so only its answer that it is taking traces was checked"]
    notes = []
    for limit in listed:
        kind, most = limit.get("limit_type"), limit.get("limit_value")
        if limit.get("scope", "workspace") != "workspace" or not isinstance(most, int):
            notes.append(f"a limit this does not check is set: {kind} of {most} for one {limit.get('scope')}")
            continue
        used = used_this_month(keys, "longlived" if kind == "monthly_longlived_traces" else None, now)
        if used is None:
            notes.append(f"{kind} is limited to {most}; it did not say how many are used")
        elif used + traces > most:
            return (f"this run records about {traces} traces, and the workspace's {kind} limit of {most} has about"
                    f" {max(most - used, 0)} left this month"), notes
        else:
            notes.append(f"{kind}: about {used} of {most} used this month")
    return None, notes


def planned(extra: list[str]) -> int:
    """About how many trials one job of this command runs: the tasks it names, or all of them, times -k."""
    names = [p.name for p in (HERE / "tasks").iterdir() if p.is_dir()] if (HERE / "tasks").is_dir() else []
    wanted = [extra[i + 1] for i, arg in enumerate(extra[:-1]) if arg in ("-i", "--include-task-name")]
    tasks = {name for pattern in wanted for name in fnmatch.filter(names, pattern)} if wanted else names
    times = next((int(extra[i + 1]) for i, arg in enumerate(extra[:-1]) if arg in ("-k", "--n-attempts")), 1)
    return len(tasks) * times


def trials_of(job: Path, seen: dict | None = None) -> list[dict]:
    """Every trial folder of a job as limits.standing reads it, with when the trial finished (None while it runs)
    and whether it proposed anything. `seen` keeps what was read, so a finished trial's files are read once."""
    rows = []
    for folder in sorted(p for p in job.glob("*__*") if p.is_dir()):
        try:
            stamp = tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in (folder / "result.json", folder / "agent/langgraph-run.log"))
        except OSError:
            continue  # Harbor removes the folder of a trial it is about to run again
        if seen is not None and seen.get(folder.name, (None, None))[0] == stamp:
            rows.append(seen[folder.name][1])
            continue
        trial = limits.read_json(folder / "result.json") or {}
        proposals = ((trial.get("verifier_result") or {}).get("rewards") or {}).get("proposals")
        finished = trial.get("finished_at")
        row = limits.standing(folder, trial) | {
            "name": folder.name, "proposed": bool(proposals),
            "finished": datetime.fromisoformat(finished).timestamp() if finished else None}
        if seen is not None:
            seen[folder.name] = (stamp, row)
        rows.append(row)
    return rows


def lost(rows: list[dict]) -> list[dict]:
    """A job's most recently finished trials, counting back from the last, that came to nothing: they ended
    without a proposal, or were not run because of the provider."""
    streak = []
    for row in sorted((r for r in rows if r["finished"]), key=lambda r: r["finished"], reverse=True):
        if row["proposed"] and row["not_run"] not in limits.PROVIDER:
            break
        streak.append(row)
    return streak


def why_stop(job: Path, rows: list[dict], began: float, now: float) -> tuple[str, str] | None:
    """Whether a running job should be stopped. Returns whose waiting jobs the reason also settles ("every" job,
    the "provider"'s or the "model"'s) and the reason, or None. Only trials that ran since `began` are read, so a
    resumed job is not stopped again for what stopped it before."""
    rows = [r for r in rows if not r["finished"] or r["finished"] >= began]
    refused = limits.unsent(job / limits.UNSENT)[0]
    if refused:
        return "every", f"LangSmith is not taking its records: {refused}"
    refused = next((r["trace_refused"] for r in rows if limits.USAGE_LIMIT.search(r["trace_refused"])), "")
    if refused:
        return "every", f"LangSmith is refusing the agent's traces: {refused}"
    if any(r["not_run"] == limits.CREDITS for r in rows):
        return "provider", "the provider's account is out of credits"
    streak = lost(rows)
    if len(streak) >= BROKEN_AFTER:
        why = Counter(r["not_run"] or "no proposal" for r in streak[:BROKEN_AFTER])
        return "model", f"its last {BROKEN_AFTER} cases came to nothing: " + ", ".join(f"{n} {reason}" for reason, n in why.items())
    running = [r for r in rows if not r["finished"]]
    last = max([began, *(r["finished"] for r in rows if r["finished"])])
    patience = STALLED_AFTER * ((limits.read_json(job / "config.json") or {}).get("agent_timeout_multiplier") or 1)
    if running and all(r["retried_calls"] for r in running) and now - last >= patience:
        return "model", (f"the provider is not answering: every running case has had a model call sent again and none has "
                         f"finished for {(now - last) / 60:.0f} minutes ({limits.short(running[-1]['provider_said'])})")
    return None


def news(rows: list[dict], told: dict[str, int]) -> list[str]:
    """What is worth a line since the last look. Each kind is told when it first happens and again whenever its
    count has doubled, so a job in steady trouble does not fill the output."""
    retried = [r for r in rows if r["retried_calls"]]
    refused = [r for r in rows if r["trace_refused"]]
    missing = Counter(r["not_run"] for r in rows if r["not_run"] in limits.PROVIDER)
    found = {
        "retried": (len(retried), lambda: f"cases with a model call sent again: {len(retried)} "
                    f"({limits.short(retried[-1]['provider_said'])}); their seconds are not used"),
        "refused": (len(refused), lambda: f"cases with a request of LangSmith's client refused: {len(refused)} "
                    f"({refused[-1]['trace_refused'][:160]}); their seconds are not used and their traces are incomplete"),
        "missing": (sum(missing.values()), lambda: "cases not run: " + ", ".join(f"{n} {reason}" for reason, n in missing.items())),
    }
    lines = []
    for kind, (count, line) in found.items():
        if count and count >= 2 * told.get(kind, 0):
            told[kind] = count
            lines.append(line())
    return lines


def summary(rows: list[dict]) -> str:
    """A finished or stopped job in one line: its results, the trials not run and why, and how many results have
    seconds that can be used."""
    results = [r for r in rows if r["finished"] and not r["not_run"]]
    missing = Counter(r["not_run"] for r in rows if r["not_run"])
    out = f"{len(results)} results"
    if missing:
        out += f", {sum(missing.values())} not run (" + ", ".join(f"{n} {reason}" for reason, n in missing.items()) + ")"
    retried, refused = sum(bool(r["retried_calls"]) for r in results), sum(bool(r["trace_refused"]) for r in results)
    if retried or refused:
        out += (f"; seconds from {sum(limits.clean(r) for r in results)} of them ({retried} had a model call sent again,"
                f" {refused} a refused trace)")
    return out


def ended(job: subprocess.Popen, seconds: float) -> int | None:
    """Wait this long for a job's process. Returns its exit code, or None while it is still running."""
    try:
        return job.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return None


def last_error(log: Path) -> str:
    """The error a job's own output ends on, for a job that stopped by itself: the last line of its traceback."""
    try:
        lines = log.read_text(errors="replace")[-20_000:].splitlines()
    except OSError:
        return ""
    return next((line.strip()[:300] for line in reversed(lines) if limits.ERROR_LINE.match(line)), "")


def run_all(commands: list[tuple[str, str, list[str]]], keys: dict[str, str]) -> int:
    """Run the jobs a few at a time, in order, skipping past any whose model already has a job running."""
    lock, busy, failed, again, unstarted = threading.Lock(), set(), [], [], []

    def drop(model: str, scope: str) -> None:
        """Take the waiting jobs that what stopped this model's job would stop too off the list."""
        with lock:
            for dropped in [c for c in commands if scope == "every" or c[0] == model
                            or scope == "provider" and c[0].split("/")[0] == model.split("/")[0]]:
                commands.remove(dropped)
                failed.append(dropped[1])
                unstarted.append(dropped[1])
                print(f"{clock()} not run {dropped[1]}", flush=True)

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
            model, name, cmd = pick
            print(f"{clock()} start  {name}", flush=True)
            seen, told, stopped, began = {}, {}, None, time.time()
            with (JOBS / f"{name}.log").open("a") as log:
                # Harbor imports the plugin, and the plugin imports world/, so both need the repo on the path.
                # `harbor jobs resume` takes no --env-file, so the keys are given to both commands here.
                job = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                       env={**os.environ, **keys, "PYTHONPATH": str(ROOT)})
                while (code := ended(job, LOOK_EVERY)) is None:
                    rows = trials_of(JOBS / name, seen)
                    for line in news([r for r in rows if not r["finished"] or r["finished"] >= began], told):
                        print(f"{clock()} notice {name}: {line}", flush=True)
                    if stopped is None and (stopped := why_stop(JOBS / name, rows, began, time.time())):
                        job.terminate()
                        drop(model, stopped[0])  # now, not when Harbor has finished cancelling its trials
            rows = trials_of(JOBS / name, seen)
            error = "" if stopped or not code else last_error(JOBS / f"{name}.log")
            note = f" (stopped: {stopped[1]})" if stopped else f" (exit {code}{': ' + error if error else ''})" if code else ""
            print(f"{clock()} finish {name}{note}: {summary(rows)}", flush=True)
            with lock:
                busy.discard(model)
                if code or stopped:
                    failed.append(name)
                if (code or stopped or any(r["not_run"] for r in rows)) and (JOBS / name / "config.json").exists():
                    again.append(name)

    threads = [threading.Thread(target=worker) for _ in range(JOBS_AT_ONCE)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if again:
        print("These jobs lack trials. Once what stopped them has passed, this runs the rest into the same experiments:\n"
              "  uv run python evals/run.py resume " + " ".join(again), flush=True)
    if unstarted:
        print("These jobs were not started, and have no folder to resume. Start them with the command that started this run, "
              "naming their graphs and models:\n  " + " ".join(unstarted), flush=True)
    return len(failed)


def running(name: str) -> bool:
    """Whether a Harbor process for this job is alive. Resuming a job that is still running would remove the
    folders of the trials it has under way."""
    processes = subprocess.run(["ps", "-Aww", "-o", "command="], capture_output=True, text=True).stdout.splitlines()
    return any(re.search(r"\bharbor\b.* (run|resume) ", line) and name in line for line in processes)


def jobs_named(names: list[str]) -> list[Path]:
    """The job folders these names mean: a job's own name, or a commit, which means every job named with it."""
    found = []
    for name in names:
        found += [JOBS / name] if (JOBS / name / "config.json").exists() else sorted(
            p for p in JOBS.glob(f"*-{name}-*") if (p / "config.json").exists())
    return list(dict.fromkeys(found))


def left(job: Path, rows: list[dict]) -> int | None:
    """How many of a job's trials have no result yet; None where the job never wrote how many it has."""
    total = (limits.read_json(job / "result.json") or {}).get("n_total_trials")
    return None if total is None else total - sum(bool(r["finished"]) and not r["not_run"] for r in rows)


def settings_of(job: Path, dataset: str) -> dict:
    """The plugin's settings for a job being resumed: as they are kept in the job's folder, or, for a job whose
    folder holds none, worked out from the job's config and name, with the dataset as given."""
    kept = (limits.read_json(job / limits.RECORD) or {}).get("settings")
    if kept:
        return kept
    agent = limits.read_json(job / "config.json")["agents"][0]
    kwargs = agent.get("kwargs") or {}
    graph, short = kwargs["graph"], agent["model_name"].split("/")[-1]
    effort = (kwargs.get("model_kwargs") or {}).get("reasoning_effort") or "default"
    return {"dataset_name": dataset, "graph": graph, "model": short, "reasoning_effort": effort,
            "commit": job.name.removeprefix(f"{graph}-{short}-{effort}-").rsplit("-", 2)[0],
            "tools": tools_of(graph, Path(kwargs["project_path"])),
            "paced": bool((kwargs.get("configurable") or {}).get("calls_per_minute"))}


def find_experiment(job: Path, dataset: str, keys: dict[str, str]) -> str | None:
    """For a job whose folder does not name its experiment: find the experiment in LangSmith under the name Harbor's
    plugin gave it, check that it is on this dataset, and write the record the plugin reads when the job is resumed.
    Returns what is wrong, or None. Without the record the plugin would make the experiment again and rely on
    LangSmith refusing the second one by its name."""
    def listed(path: str, name: str) -> dict | None:
        status, text = langsmith("GET", path, keys, query={"name": name})
        try:
            found = json.loads(text) if status == 200 else []
        except ValueError:
            found = []
        return next((item for item in found if isinstance(item, dict) and item.get("name") == name), None) if isinstance(found, list) else None

    name = f"{job.name}-{str((limits.read_json(job / 'result.json') or {}).get('id'))[:8]}"
    experiment, on = listed("/sessions", name), listed("/datasets", dataset)
    if experiment is None:
        return f"LangSmith shows no experiment named {name}"
    if on is None or experiment.get("reference_dataset_id") != on.get("id"):
        return f"the experiment {name} is not on the dataset {dataset}; name the dataset it was recorded in with --dataset"
    (job / limits.RECORD).write_text(json.dumps({"experiment_id": experiment["id"], "experiment_name": name, "owned": True,
                                                 "settings": settings_of(job, dataset)}, indent=1))
    return None


def set_aside(job: Path, rows: list[dict]) -> list[str]:
    """Move the folders of a job's trials that were not run out of the job, so Harbor runs those cases again. They
    are kept, under jobs/_not_run/<job>/, because they are the record of what stopped them."""
    moved = [r["name"] for r in rows if r["not_run"]]
    for name in moved:
        (NOT_RUN / job.name).mkdir(parents=True, exist_ok=True)
        shutil.move(str(job / name), str(NOT_RUN / job.name / name))
    return moved


def asked(traces: int, keys: dict[str, str]) -> None:
    """Stop here, with LangSmith's reason, if it would not record a run of this many trials."""
    problem, notes = unrecorded(traces, keys)
    if problem:
        sys.exit(f"Nothing was started. LangSmith would not record this run: {problem}.\n"
                 + "".join(f"  LangSmith: {note}\n" for note in notes)
                 + "A trial that runs while LangSmith refuses it cannot have its trace sent later, so the run waits for the "
                   "limit to be raised or the month to end.\n--no-langsmith-check starts it anyway; a job LangSmith then "
                   "refuses is stopped at once.")
    for note in notes:
        print(f"LangSmith: {note}", flush=True)


def resume(names: list[str], dataset: str, keys: dict[str, str], check: bool = True) -> list[tuple[str, str, list[str]]]:
    """The commands that continue these jobs, after what has to happen first: the models and LangSmith are asked,
    the records LangSmith refused are sent, and the trials that were not run are set aside. Exits with the reason
    where the jobs cannot be continued now; no trial folder has been moved by then."""
    todo = []
    for job in jobs_named(names):
        rows = trials_of(job)
        missing = left(job, rows)
        if running(job.name):
            print(f"{job.name}: still running, left alone")
        elif missing == 0 and not (job / limits.UNSENT).exists():
            print(f"{job.name}: nothing to run, it has all its results")
        else:
            todo.append((job, rows))
            print(f"{job.name}: {summary(rows)}" + ("" if missing is None else f"; to run: {missing}"))
    if not todo:
        sys.exit(f"No job to continue among those named {' '.join(names)} in {JOBS.relative_to(ROOT)}.")
    models = {job: limits.read_json(job / "config.json")["agents"][0]["model_name"] for job, _ in todo}
    wrong = {model: problem for model in dict.fromkeys(models.values()) if (problem := unsound(model, keys))}
    if wrong:
        sys.exit("Nothing was started. These models did not answer a test question:\n"
                 + "\n".join(f"  {model}: {problem}" for model, problem in wrong.items()))
    if check:
        asked(sum(left(job, rows) or 0 for job, rows in todo), keys)
    for job, _ in todo:
        if not (job / limits.RECORD).exists() and (problem := find_experiment(job, dataset, keys)):
            sys.exit(f"Nothing was started. {job.name} cannot be continued in its experiment: {problem}.")
    for job, _ in todo:
        why, writes = limits.unsent(job / limits.UNSENT)
        if not why:
            continue
        sent, rejected, still = limits.send_unsent(job / limits.UNSENT, lambda method, path, body: langsmith(method, path, keys, body))
        print(f"{job.name}: sent LangSmith {sent} of the {len(writes)} records it refused earlier ({why})"
              + (f"; it turned down {rejected} for good, kept in {limits.REJECTED}" if rejected else ""), flush=True)
        if still:
            sys.exit(f"Nothing was started. LangSmith is still not taking records: {still}\n"
                     f"The records that are left stay in {(job / limits.UNSENT).relative_to(ROOT)}.")
    commands = []
    for job, rows in todo:
        moved = set_aside(job, rows)
        if moved:
            print(f"{job.name}: trials not run, moved to {(NOT_RUN / job.name).relative_to(ROOT)}: {len(moved)}", flush=True)
        commands.append((models[job], job.name, ["harbor", "jobs", "resume", "-p", str(job), "--plugin",
                                                 "evals.langsmith_plugin:Annotated", *plugin_settings(**settings_of(job, dataset))]))
    return commands


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    graphs, extra = sys.argv[1].split(","), sys.argv[2:]
    JOBS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%m%d-%H%M%S")
    if graphs == ["oracle"]:
        cmd = oracle_command(extra, stamp)
        print(" ".join(cmd), flush=True)
        sys.exit(subprocess.run(cmd, cwd=ROOT).returncode)

    dataset = take(extra, "--dataset") or DATASET
    check = not switch(extra, "--no-langsmith-check")
    request_log = not switch(extra, "--no-request-log")
    keys = env_file()
    if graphs == ["resume"]:
        commands = resume(extra, dataset, keys, check)
        print(f"{len(commands)} jobs to continue, {JOBS_AT_ONCE} at a time; each job's output is added to "
              f"{JOBS.relative_to(ROOT)}/<job name>.log", flush=True)
        sys.exit(run_all(commands, keys))

    chosen = take(extra, "-m") or DEFAULT_MODEL
    efforts = take(extra, "--efforts")
    names = list(MODELS) if chosen == "all" else chosen.split(",")
    commit = git("rev-parse", "--short", "HEAD") + ("+" if git("status", "--porcelain") else "")
    agents = JOBS / "_agents" / f"{commit}-{stamp}"
    shutil.copytree(ROOT / "agents", agents, ignore=shutil.ignore_patterns("__pycache__"))

    plan: list[tuple[str | None, str]] = []  # (effort, model name), in the order to run
    if efforts is None:  # a model may name its own effort, as in luna:high
        plan = [(effort or None, name) for name, _, effort in (n.partition(":") for n in names)]
    else:
        wanted = EFFORT_ORDER if efforts == "all" else efforts.split(",")
        plan = [(None if e == "default" else e, n) for e in wanted for n in names
                if e == "default" or n not in MODELS or e in MODELS[n][1]]
    commands = [agent_command(graph, MODELS.get(n, (n,))[0], e, agents, commit, list(extra), dataset, request_log)
                for graph in graphs for e, n in plan]
    wrong = {model: problem for model in dict.fromkeys(c[0] for c in commands) if (problem := unsound(model, keys))}
    if wrong:
        sys.exit("Nothing was started. These models did not answer a test question:\n"
                 + "\n".join(f"  {model}: {problem}" for model, problem in wrong.items()))
    if check:
        asked(len(commands) * planned(extra), keys)
    print(f"{len(commands)} jobs, {JOBS_AT_ONCE} at a time; each job's output is in {JOBS.relative_to(ROOT)}/<job name>.log", flush=True)
    sys.exit(run_all(commands, keys))
