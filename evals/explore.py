"""A page for reading one run's results: each experiment's measures, and every case that got a wrong answer.

    uv run --with matplotlib --env-file .env python evals/explore.py <commit>
    uv run --with matplotlib --env-file .env python evals/explore.py <commit> --planned 12 --note "A fact about the run."
    uv run --with matplotlib --env-file .env python evals/explore.py <commit> --bare -o page.html

Reads the same job folders as evals/pareto.py and writes evals/jobs/explore-<commit>.html, one file with its data
inside. The page has three parts:

    Experiments              one row per agent and model: accuracy, seconds, tokens, cost and calls per case
    Cases with a wrong answer    which models missed a case with one agent, or which agents missed it at all
    One case                 what was expected, and for every trial what it proposed, its rationale, its reply to
                             the customer and, for a wrong one, its tool calls in order

It reports what happened and draws no conclusion. --planned is the number of experiments the run was started
with; while fewer are complete, the page says the run is still going. With LANGSMITH_API_KEY set, each experiment and trial links to
LangSmith. Those links name a LangSmith workspace, so the page is written beside the job folders, which Git
ignores, and not under results/. --bare writes the page without the outer document, the form a claude.ai artifact
takes.
"""

import json
import sys
import warnings
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pareto  # noqa: E402
from run import DATASET, JOBS  # noqa: E402

from world.labeling import ui_action  # noqa: E402
from world.oracle import label  # noqa: E402
from world.scenarios import ALL  # noqa: E402

DOCUMENT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{margin:0}}[hidden]{{display:none!important}}</style></head><body>
{page}
</body></html>
"""


def text(content) -> str:
    """A message's content as text, whichever way the model's API shapes it."""
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content or ""


def conversation(folder: Path) -> dict:
    """What a trial said beyond its proposal: the reply to the customer and the tool calls the model chose."""
    messages = (pareto.read_json(folder / "agent/result.json") or {}).get("messages") or []
    reply = next((text(m.get("content")) for m in reversed(messages) if m.get("type") == "ai"), "")
    chosen = [f"{call['name']}({json.dumps(call.get('args') or {}, ensure_ascii=False)[1:-1][:500]})"
              for m in messages if m.get("type") == "ai" for call in m.get("tool_calls") or []]
    # Where code made the calls, as in a fixed graph, the model's messages do not list them.
    made = sum(m.get("type") == "tool" for m in messages)
    return {"reply": reply, "calls": chosen if chosen and len(chosen) == made else None}


def retried(job: Path) -> int:
    """How many of a job's trials had a model call that the client had to send again."""
    count = 0
    for log in job.glob("*__*/agent/langgraph-run.log"):
        try:
            count += "Retrying" in log.read_text(errors="replace")
        except OSError:
            pass
    return count


def links(commit: str) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """Each experiment's page and each trial's trace in LangSmith, by job name and by job and trial. Empty when
    LangSmith cannot be reached."""
    try:
        from langsmith import Client
        client = Client()
        dataset = client.read_dataset(dataset_name=DATASET)
        experiments, traces = {}, {}
        for project in client.list_projects(reference_dataset_id=dataset.id):
            job = ((project.extra or {}).get("metadata") or {}).get("harbor_job_name", "")
            if f"-{commit}-" not in job:
                continue
            host = "https://smith.langchain.com"
            experiments[job] = f"{host}/o/{project.tenant_id}/datasets/{dataset.id}/compare?selectedSessions={project.id}"
            latest: dict[str, tuple] = {}
            for run in client.list_runs(project_id=project.id, is_root=True, select=["id", "extra", "start_time"]):
                trial = ((run.extra or {}).get("metadata") or {}).get("trial")
                if trial and (trial not in latest or run.start_time > latest[trial][0]):  # a retried trial has two runs
                    latest[trial] = (run.start_time, f"{host}/o/{project.tenant_id}/projects/p/{project.id}/r/{run.id}")
            traces.update({(job, trial): url for trial, (_, url) in latest.items()})
        return experiments, traces
    except Exception as e:  # no key, no network, or a dataset that does not exist yet
        print(f"no LangSmith links: {type(e).__name__}: {e}", file=sys.stderr)
        return {}, {}


def data(commit: str, notes: list[str], planned: int | None = None) -> dict:
    rows = pareto.experiments(commit)
    listed = list(json.loads((HERE.parent / "agents/langgraph.json").read_text())["graphs"])
    graphs = [g for g in listed if any(r["graph"] == g for r in rows)]
    models = [m for m in pareto.PRICES if any(r["model"] == m for r in rows)]
    rows.sort(key=lambda r: (graphs.index(r["graph"]), models.index(r["model"])))
    experiment_url, trace_url = links(commit)

    def number(value):
        return None if value == "" else value

    experiments = []
    for r in rows:
        started = (pareto.read_json(JOBS / r["job"] / "result.json") or {}).get("started_at")
        experiments.append({
            "graph": r["graph"], "model": r["model"], "model_name": pareto.NAMES[r["model"]], "effort": r["effort"],
            "trials": r["trials"], "complete": r["complete"], "reward": r["reward"], "reward_sd": number(r["reward_sd"]),
            "always": number(r["cases_always_right"]), "sometimes": number(r["cases_sometimes_right"]),
            "never": number(r["cases_never_right"]), "seconds": number(r["agent_seconds_per_case"]),
            "cost": r["cost_per_case_usd"], "input": r["input_tokens_per_case"], "cached": r["cached_tokens_per_case"],
            "output": r["output_tokens_per_case"], "model_calls": r["model_calls_per_case"],
            "tool_calls": r["tool_calls_per_case"], "no_proposal": r["no_proposal"], "retried": retried(JOBS / r["job"]),
            "started": datetime.fromisoformat(started).astimezone().strftime("%H:%M") if started else "",
            "url": experiment_url.get(r["job"]),
        })

    hand_labeled = pareto.HAND_LABELED
    cases = {}
    for scenario in ALL:
        oracle = label(scenario.world, scenario.request)
        owed = oracle.proposed.amount_cents if oracle.proposed else oracle.amount_cents
        cases[scenario.id.lower()] = {
            "id": scenario.id, "area": scenario.archetype, "tier": scenario.difficulty, "intent": scenario.intent,
            "message": scenario.request.message, "hand_labeled": scenario.id.lower() in hand_labeled,
            "expected": {"action": ui_action(oracle.action.value), "amount_cents": owed or 0, "sections": oracle.must_cite,
                         "rationale": oracle.rationale},
        }

    # The words of a trial are kept for every case that some experiment got wrong, right trials included, so a
    # wrong answer can be read beside a right one. Cases nobody missed carry their scores only.
    missed = {t["case"] for r in rows for t in r["cases"] if not t["right"]}
    trials = []
    for r in rows:
        for t in r["cases"]:
            proposal = t["proposal"]
            sections = proposal.get("sections") if proposal else None
            entry = {
                "c": t["case"], "g": r["graph"], "m": r["model"], "right": t["right"], "a": t["action"], "am": t["amount"],
                "s": t["sections"], "secs": number(t["agent_seconds"]), "mc": t["model_calls"], "tc": t["tool_calls"],
                "cost": round(t["cost_usd"], 6), "err": t["error"] or None, "url": trace_url.get((r["job"], t["trial"])),
                "p": proposal and {"action": proposal.get("action"), "amount_cents": proposal.get("amount_cents"),
                                   "sections": json.loads(sections) if isinstance(sections, str) else sections},
            }
            if t["case"] in missed:
                said = conversation(JOBS / r["job"] / t["trial"])
                entry["reply"] = said["reply"]
                if proposal:
                    entry["p"]["rationale"] = proposal.get("rationale")
                if not t["right"]:
                    entry["calls"] = said["calls"]
            trials.append(entry)

    return {
        "commit": commit, "dataset": DATASET, "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "cases_count": len(ALL), "repeats": max((r["repeats"] for r in rows), default=1), "graphs": graphs, "models": models,
        "planned": planned or len(rows),
        "model_names": {m: pareto.NAMES[m] for m in models}, "notes": notes, "experiments": experiments, "cases": cases,
        "trials": trials,
    }


if __name__ == "__main__":
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    args = sys.argv[1:]
    bare = "--bare" in args
    notes = [args[i + 1] for i, a in enumerate(args) if a == "--note"]
    commit = args[0]
    out = Path(args[args.index("-o") + 1]) if "-o" in args else JOBS / f"explore-{commit}.html"
    found = data(commit, notes, int(args[args.index("--planned") + 1]) if "--planned" in args else None)
    page = (HERE / "explore_page.html").read_text().replace(
        "__DATA__", json.dumps(found, ensure_ascii=False).replace("</", "<\\/"))
    out.write_text(page if bare else DOCUMENT.format(page=page))
    wrong = sum(not t["right"] for t in found["trials"])
    print(f"{out}: {len(found['experiments'])} experiments, {len(found['trials'])} trials, {wrong} wrong, "
          f"{sum(1 for t in found['trials'] if t['url'])} with a LangSmith link, {out.stat().st_size / 1e6:.1f} MB")
