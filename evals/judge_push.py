"""Put the judge's answers on each trial's run in LangSmith, as scores beside the grader's.

    uv run --with matplotlib --with "openai>=3.26" --env-file .env python evals/judge_push.py <commit>          # match trials to runs, send nothing
    uv run --with matplotlib --with "openai>=3.26" --env-file .env python evals/judge_push.py <commit> --push
    ... --only <job name>    one experiment
    ... --dataset <name>     a dataset other than the one evals/run.py records in

evals/judge.py leaves one line per trial in evals/jobs/judge-<commit>-w<wording>.jsonl. This reads that file and
writes one score per question to the trial's run: the probability that the fault is present, from 0 to 1. The two
checks made in code are written as 0 or 1. Every key starts with "reply_", so the scores sort together in an
experiment's columns, and a lower average is better on each.

A score's id is made from the run, the key and the wording, so a second push of the same answers writes over the
first. It reads each experiment's runs to find a trial's run, because a trial that Harbor ran again has two runs
and the later one holds the result. It adds scores and no runs, and asks LangSmith not to keep a trace longer
because of them.
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from langsmith import Client

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import judge  # noqa: E402
import pareto  # noqa: E402
from run import DATASET  # noqa: E402

AT_ONCE = 8  # scores being written at one time


def scores(commit: str) -> dict[str, dict[str, float]]:
    """For each trial with answers on file: a score for each question asked of it, and for each check in code."""
    lines = judge.answered(pareto.JOBS / f"judge-{commit}-w{judge.WORDING}.jsonl")
    out = {}
    for b in judge.requests(commit):
        line = lines.get(b.trial)
        if line is None or not line.answers:
            continue
        found = {f"reply_{a.name}": judge.fault(a) for a in line.answers}
        found |= {f"reply_{check}": float(test(b)) for check, test in judge.IN_CODE.items()}
        out[b.trial] = {key: value for key, value in found.items() if value is not None}
    return out


def runs_by_trial(client: Client, experiment) -> dict[str, str]:
    """The id of each trial's run in one experiment: the latest, where a trial has more than one."""
    latest = {}
    for run in client.list_runs(project_id=experiment.id, is_root=True, select=["id", "start_time", "extra"]):
        trial = ((run.extra or {}).get("metadata") or {}).get("trial")
        if trial and (trial not in latest or run.start_time > latest[trial].start_time):
            latest[trial] = run
    return {trial: str(run.id) for trial, run in latest.items()}


if __name__ == "__main__":
    args = sys.argv[1:]
    commit = args[0]
    take = lambda flag: args[args.index(flag) + 1] if flag in args else None  # noqa: E731
    only, push = take("--only"), "--push" in args
    found = scores(commit)
    jobs = {r["job"]: [t["trial"] for t in r["cases"]] for r in pareto.experiments(commit)}
    client = Client()
    dataset = client.read_dataset(dataset_name=take("--dataset") or DATASET)
    written = 0
    for experiment in sorted(client.list_projects(reference_dataset_id=dataset.id), key=lambda p: p.name):
        job = ((experiment.extra or {}).get("metadata") or {}).get("harbor_job_name", "")
        if job not in jobs or (only and job != only):
            continue
        runs = runs_by_trial(client, experiment)
        judged = [t for t in jobs[job] if t in found]
        matched = [t for t in judged if t in runs]
        count = sum(len(found[t]) for t in matched)
        print(f"{job}: {len(jobs[job])} trials, {len(judged)} with answers, {len(matched)} with a run, {count} scores", flush=True)
        if not push:
            continue
        def write(score: tuple[str, str, float]) -> None:
            run, key, value = score
            client.create_feedback(run, key, score=value, feedback_source_type="api", extend_trace_retention=False,
                                   feedback_id=uuid5(NAMESPACE_URL, f"refundo-judge:{run}:{key}:w{judge.WORDING}"))

        with ThreadPoolExecutor(AT_ONCE) as pool:  # each score is one request that waits for its answer
            list(pool.map(write, [(runs[t], key, value) for t in matched for key, value in found[t].items()]))
        client.flush()
        written += count
    print(f"{written} scores written." if push else "Nothing sent. Add --push to write the scores.")
