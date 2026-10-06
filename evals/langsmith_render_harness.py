"""A page for checking evals/langsmith_render.html without LangSmith.

    "$(uv tool dir)/harbor/bin/python" evals/langsmith_render_harness.py <commit>
    "$(uv tool dir)/harbor/bin/python" evals/langsmith_render_harness.py <commit>:be-16,an-03 <another commit>:auth-08
    ... --jobs <dir> -o <page.html> --renderer <address> --results <address> --journeys <address>

It runs under Harbor's own Python because it builds each payload with the code that recorded it: Harbor's model of a
trial's result, and evals/langsmith_plugin.py. For each commit it reads the job folders evals/run.py wrote and puts
into one page what LangSmith holds for a few cases: the dataset example with its reference outputs, and every trial's
inputs and outputs. The cases are the ones named after the commit, or else the eight with the most wrong trials and
one that was right every time.

It writes two files beside the job folders, which Git ignores, because the payloads name folders on this machine:

    langsmith-render-harness.html    evals/langsmith_render_harness.html with the payloads inside. It loads the
                                     renderer in frames sized like LangSmith's and posts each payload the way
                                     LangSmith does, with the variations LangSmith has shown
    langsmith-render-sample-journeys-<commit>.json
                                     the first commit's trials in the shape of the renderer's ?journeys= file, each
                                     step being the names of the tools one model message called. It is sample data
                                     for the harness; evals/journeys.py writes the file the renderer is meant for

The frames read files and pass the answer key between themselves, which a page opened from disk may not do, so the
harness is opened over HTTP from the folder that holds both pages:

    python3 -m http.server 8765 --bind 127.0.0.1 --directory evals
    http://localhost:8765/jobs/langsmith-render-harness.html

--renderer is the renderer's address as the harness page sees it. --results, the folder of result tables, and
--journeys, the journeys file, are addresses as the renderer sees them. Without --results the renderer reads
results/<commit>/ beside itself, where evals/pareto.py writes.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
JOB = re.compile(r"^(?P<graph>[a-z_]+)-(?P<model>.+)-(?P<effort>[a-z]+)-(?P<commit>[0-9a-f]{7,40}\+?)-\d{4}-\d{6}$")
MOST_WRONG = 8  # cases a commit brings to the page when none are named


def take(args: list[str], flag: str) -> str | None:
    """Remove `flag value` from the arguments and return the value."""
    if flag not in args:
        return None
    at = args.index(flag)
    value = args[at + 1]
    del args[at:at + 2]
    return value


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def steps(messages: list[dict]) -> list[list[str]]:
    """A trial's tool calls as the journeys file holds them: one step per model message that called a tool, each
    the sorted names of the tools it called, a name once."""
    return [sorted({call["name"] for call in m["tool_calls"]}) for m in messages if m.get("type") == "ai" and m.get("tool_calls")]


def trials_of(jobs: Path, commit: str) -> list[dict]:
    """Every finished trial of a commit's jobs: where it is, its experiment, its case and whether it was right."""
    found = []
    for job in sorted(p for p in jobs.iterdir() if p.is_dir()):
        named = JOB.match(job.name)
        if not named or named["commit"] != commit:
            continue
        for path in sorted(job.glob("*__*/result.json")):
            result = read_json(path)
            if not result:
                continue
            rewards = (result.get("verifier_result") or {}).get("rewards") or {}
            found.append({"folder": path.parent, "job": job.name, "graph": named["graph"], "model": named["model"],
                          "effort": named["effort"], "case": result["task_name"].split("/")[-1],
                          "right": rewards.get("reward") == 1.0})
    return found


def chosen(trials: list[dict]) -> list[str]:
    """The cases with the most wrong trials, most first, and the first case that was right every time."""
    wrong: dict[str, int] = {}
    for t in trials:
        wrong[t["case"]] = wrong.get(t["case"], 0) + (not t["right"])
    missed = sorted((case for case, n in wrong.items() if n), key=lambda case: (-wrong[case], case))[:MOST_WRONG]
    return missed + sorted(case for case, n in wrong.items() if not n)[:1]


def journeys(commit: str, trials: list[dict]) -> dict:
    """The trials in the shape of the renderer's journeys file, with each trial's steps read from its messages."""
    def said(t: dict) -> list[dict]:
        return (read_json(t["folder"] / "agent/result.json") or {}).get("messages") or []

    return {"commit": commit,
            "note": "Sample data written by evals/langsmith_render_harness.py for its own page. A step is the names "
                    "of the tools one model message called. evals/journeys.py writes the file the renderer is meant for.",
            "trials": {t["folder"].name: {k: t[k] for k in ("case", "graph", "model", "effort", "right")} | {"steps": steps(said(t))}
                       for t in trials}}


def sample(case: str, commit: str, trials: list[dict]) -> dict:
    """What LangSmith holds for one case: the example, and each trial's run as the plugin recorded it."""
    from harbor.models.trial.result import TrialResult

    from evals.langsmith_plugin import SCENARIOS, Annotated, reference

    plugin, runs, example = Annotated(), [], None
    for t in trials:
        result = TrialResult.model_validate_json((t["folder"] / "result.json").read_text())
        result = result.model_copy(update={"trial_uri": t["folder"].as_uri()})  # where the folder is now
        task = result.config.task
        instruction = plugin._read_instruction(task) or (t["folder"] / "agent/instruction.txt").read_text()
        asked = {"task_name": result.task_name, "instruction": instruction, "task_id": task.get_task_id().model_dump(mode="json")}
        agent = result.config.agent
        runs.append({"experiment": t["job"], "trial": result.trial_name, "right": t["right"],
                     "inputs": asked | {"trial_name": result.config.trial_name, "agent": agent.name or agent.import_path,
                                        "model": agent.model_name},
                     "outputs": json.loads(json.dumps(plugin._trial_outputs(result), default=str))})
        example = example or {"inputs": asked | {"task_name": case}, "outputs": reference(SCENARIOS[case])}
    scenario = SCENARIOS[case]
    return {"name": case, "commit": commit, "about": f"{scenario.archetype}, {scenario.difficulty}: {scenario.intent}",
            "example": example, "runs": runs}


if __name__ == "__main__":
    args = sys.argv[1:]
    jobs = Path(take(args, "--jobs") or HERE / "jobs").resolve()
    out = Path(take(args, "-o") or jobs / "langsmith-render-harness.html").resolve()
    renderer = take(args, "--renderer") or os.path.relpath(HERE / "langsmith_render.html", out.parent)
    results, given = take(args, "--results"), take(args, "--journeys")
    if not args:
        sys.exit(__doc__)
    sys.path.insert(0, str(ROOT))  # Harbor's Python does not have the repo on its path

    samples, written = [], None
    for spec in args:
        commit, _, named = spec.partition(":")
        trials = trials_of(jobs, commit)
        if not trials:
            sys.exit(f"No finished trial of commit {commit} in {jobs}.")
        if written is None:
            written = out.with_name(f"langsmith-render-sample-journeys-{commit}.json")
            written.write_text(json.dumps(journeys(commit, trials), ensure_ascii=False))
        for case in named.split(",") if named else chosen(trials):
            mine = [t for t in trials if t["case"] == case]
            if not mine:
                sys.exit(f"No finished trial of {case} at commit {commit}.")
            samples.append(sample(case, commit, mine))
    if not given and "://" not in renderer:  # the renderer resolves the file's address against its own
        given = os.path.relpath(written, (out.parent / renderer).resolve().parent)
    data = {"written": datetime.now().strftime("%Y-%m-%d %H:%M"), "renderer": renderer, "results": results, "journeys": given,
            "samples": samples}
    page = (HERE / "langsmith_render_harness.html").read_text().replace(
        "__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    out.write_text(page)
    runs = sum(len(s["runs"]) for s in samples)
    print(f"{out}: {len(samples)} cases, {runs} runs, {out.stat().st_size / 1e6:.1f} MB; sample journeys in {written.name}")
