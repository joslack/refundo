"""Estimate the seconds a case spent in model calls, for every finished experiment of one sweep.

    uv run --env-file .env python evals/latency.py <commit> [dataset]

The dataset is the LangSmith dataset the sweep was recorded in, where that is not the one evals/run.py names now.

Wall time from a sweep cannot be compared across experiments: the machine's load changes, and for OpenAI models
the agent waits before a call so that calls are at least `interval` seconds apart (60 / calls_per_minute, from
the job's settings; the first call of a case waits a full interval). That wait sits inside the model call that
LangSmith records. This takes each call's start and end from the LangSmith traces and removes the wait: a call
was let through at
    released[i] = max(started[i], released[i-1] + interval)
and took ended[i] - released[i]. Models with no pacing are used as recorded.

The result is a reconstruction, not a measurement, and still holds whatever a loaded machine added on the client
side. It ranks models; it is not a latency figure to quote.

Only trials with nothing else inside their model calls are used: a result (evals/limits.py), with no model call
that the client sent again, since the wait and the failed attempt are inside the call LangSmith records, and
with no refused trace, since that trace is missing calls. Where Harbor ran a trial again, the trace of the last
attempt is the one used. trials_left_out says how many trials that set aside.

Writes evals/results/<commit>/latency.csv, one row per job. Jobs already in the file are skipped.
"""

import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from langsmith import Client

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import limits  # noqa: E402
from run import DATASET, JOBS  # noqa: E402

COLUMNS = ["job", "cases", "wait_interval_seconds", "model_seconds_per_case_estimated", "model_seconds_per_call",
           "waited_seconds_per_case", "trials_left_out"]


def timed(roots: list[tuple], job: Path) -> set:
    """The traces whose model calls are timed, from each trial's trace as (trace id, trial name, start): for each
    trial the last trace under its name, where the trial's folder shows a result with nothing else in its time."""
    latest: dict[str, tuple] = {}
    for trace, trial, started in roots:
        if trial not in latest or started > latest[trial][0]:
            latest[trial] = (started, trace)
    return {trace for trial, (_, trace) in latest.items()
            if (job / trial / "result.json").exists() and limits.clean(limits.standing(job / trial))}


def estimate(calls: dict, interval: float) -> dict:
    """Medians over cases, from each case's model calls as (started, ended) in seconds."""
    per_case, per_call, waited = [], [], []
    for runs in calls.values():
        released, total, wait = None, 0.0, 0.0
        for started, ended in sorted(runs):
            released = started if not interval else started + interval if released is None else max(started, released + interval)
            took = max(0.0, ended - released)
            total += took
            wait += released - started
            per_call.append(took)
        per_case.append(total)
        waited.append(wait)
    return {"cases": len(per_case), "wait_interval_seconds": round(interval, 2),
            "model_seconds_per_case_estimated": round(statistics.median(per_case), 1),
            "model_seconds_per_call": round(statistics.median(per_call), 2),
            "waited_seconds_per_case": round(statistics.median(waited), 1)}


if __name__ == "__main__":
    commit = sys.argv[1]
    out = ROOT / "evals/results" / commit / "latency.csv"
    rows = list(csv.DictReader(out.open())) if out.exists() else []
    have = {r["job"] for r in rows}
    client = Client()
    dataset = client.read_dataset(dataset_name=sys.argv[2] if len(sys.argv) > 2 else DATASET)
    for experiment in sorted(client.list_projects(reference_dataset_id=dataset.id), key=lambda p: p.name):
        name = ((experiment.extra or {}).get("metadata") or {}).get("harbor_job_name", "")
        job = JOBS / name
        if f"-{commit}-" not in name or name in have or not (job / "result.json").exists():
            continue
        if not json.loads((job / "result.json").read_text()).get("finished_at"):
            continue
        settings = json.loads((job / "config.json").read_text())["agents"][0].get("kwargs") or {}
        pace = (settings.get("configurable") or {}).get("calls_per_minute")
        # A trial's own run is named after the trial and is the root of its trace.
        roots = [(run.trace_id, run.name, run.start_time.timestamp())
                 for run in client.list_runs(project_id=experiment.id, is_root=True, select=["trace_id", "name", "start_time"])]
        keep = timed(roots, job)
        calls = defaultdict(list)
        for run in client.list_runs(project_id=experiment.id, run_type="llm",
                                    select=["trace_id", "name", "start_time", "end_time"]):
            # The agent's own calls are named after the chat model class. Harbor's plugin adds one more per case,
            # named after the model, which is a token total and not a call.
            if run.name.startswith("Chat") and run.end_time and run.trace_id in keep:
                calls[run.trace_id].append((run.start_time.timestamp(), run.end_time.timestamp()))
        if not calls:
            continue
        rows.append({"job": name} | estimate(calls, 60 / pace if pace else 0.0)
                    | {"trials_left_out": len({trial for _, trial, _ in roots}) - len(keep)})
        print(f"{name}  {rows[-1]['model_seconds_per_case_estimated']} s a case", flush=True)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(sorted(rows, key=lambda r: r["job"]))
