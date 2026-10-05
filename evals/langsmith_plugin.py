"""Harbor's LangSmith plugin, with what it leaves out.

evals/run.py loads this as `--plugin evals.langsmith_plugin:Annotated`. Harbor's plugin records each
trial as a run in a LangSmith experiment, with the agent's own trace nested under it. This adds:

- On each dataset example, before any trial runs: the oracle's outcome as the reference output, and the
  scenario's tier, area and whether its answer was checked by hand. LangSmith copies an example's
  metadata onto a run when the run is created, and Group by and the filters read that copy, so it has
  to be on the example first. A run cannot be given it afterwards.
- On the experiment: the graph, model, reasoning effort, commit and tools.
- On each trial's run: what the agent proposed and what it told the customer, at the top of the output.
- On a missed case: a comment on the reward score saying what was expected.
- The experiment's end time in UTC. Harbor hands the plugin a local time with no zone.

It overrides methods of harbor-langsmith 0.3.1 that are not a public interface, so check it when Harbor
is updated.
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from harbor_langsmith.plugin import LangSmithPlugin

from world.labeling import LABELS, load_labels, ui_action
from world.oracle import label
from world.scenarios import ALL

SCENARIOS = {s.id.lower(): s for s in ALL}  # a task is named after its scenario, as in "mo-01"
HAND_LABELED = set(load_labels(LABELS / "jonah.jsonl"))


def reference(scenario) -> dict:
    oracle = label(scenario.world, scenario.request)
    owed = oracle.proposed.amount_cents if oracle.proposed else oracle.amount_cents
    return {"action": ui_action(oracle.action.value), "amount_cents": owed, "sections": oracle.must_cite,
            "rationale": oracle.rationale}


def describe(outcome: dict | None) -> str:
    if not outcome:
        return "nothing"
    return f"{outcome['action']} ${(outcome['amount_cents'] or 0) / 100:,.2f} §{', '.join(outcome['sections'] or [])}"


def trial_folder(result) -> Path:
    return Path(unquote(urlparse(result.trial_uri).path))


def proposal(result) -> dict | None:
    """The agent's last proposal, from the file Harbor copied out of the database."""
    try:
        submitted = json.loads((trial_folder(result) / "artifacts/tmp/proposals.json").read_text())
    except (OSError, ValueError):
        return None
    return {k: submitted[-1].get(k) for k in ("action", "amount_cents", "sections", "rationale")} if submitted else None


def reply(result) -> str | None:
    """The agent's last message to the customer, as text whichever way the model's API shapes it."""
    try:
        messages = json.loads((trial_folder(result) / "agent/result.json").read_text())["messages"]
    except (OSError, ValueError, KeyError):
        return None
    content = next((m.get("content") for m in reversed(messages) if m.get("type") == "ai"), None)
    if isinstance(content, list):
        content = "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content or None


class Annotated(LangSmithPlugin):
    def __init__(self, *, graph: str | None = None, model: str | None = None, reasoning_effort: str | None = None,
                 commit: str | None = None, tools: list[str] | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        # LangSmith's experiment table has Models and Tools columns that read the plural keys.
        self._about = {"graph": graph, "model": model, "models": [model], "reasoning_effort": reasoning_effort,
                       "commit": commit, "tools": tools or []}
        self._trial = threading.local()  # trials finish on separate threads

    def _request(self, method: str, path: str, *, ok_statuses: set[int], **kwargs: Any):
        body = kwargs.get("json")
        if isinstance(body, dict):
            if method == "POST" and path == "/sessions":
                body.setdefault("extra", {}).setdefault("metadata", {}).update(self._about)
            elif path.startswith("/examples"):
                self._annotate_example(body)
            elif path == "/feedback" and body.get("key") == "reward" and body.get("score") != 1:
                body["comment"] = getattr(self._trial, "comment", None)
        return super()._request(method, path, ok_statuses=ok_statuses, **kwargs)

    @staticmethod
    def _annotate_example(body: dict) -> None:
        scenario = SCENARIOS.get((body.get("inputs") or {}).get("task_name"))
        if scenario is None:
            return
        checked = "hand-labeled" if scenario.id in HAND_LABELED else "unlabeled"
        body["outputs"] = reference(scenario)
        body["metadata"] = (body.get("metadata") or {}) | {
            "scenario": scenario.id, "difficulty": scenario.difficulty, "area": scenario.archetype,
            "answer_checked_by": checked, "summary": scenario.intent}
        body["split"] = ["base", scenario.difficulty, checked]

    def _trial_outputs(self, result) -> dict[str, Any]:
        outputs = super()._trial_outputs(result)
        if result is None:
            return outputs
        proposed = proposal(result)
        scenario = SCENARIOS.get(result.task_name.split("/")[-1])
        if scenario is not None:  # read by _request when the reward score for this trial is written
            self._trial.comment = f"expected {describe(reference(scenario))}; submitted {describe(proposed)}"
        return (proposed or {"action": None}) | {"reply": reply(result)} | outputs

    @staticmethod
    def _format_time(value: datetime) -> str:
        if value.tzinfo is None:  # Harbor's own timestamps are local time
            value = value.astimezone()
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
