"""Harbor's LangSmith plugin, with what it leaves out.

evals/run.py loads this as `--plugin evals.langsmith_plugin:Annotated`. Harbor's plugin records each
trial as a run in a LangSmith experiment, with the agent's own trace nested under it. This adds:

- On each trial's run: the scenario's tier, area and whether its answer was checked by hand, with the
  graph, model, reasoning effort and commit. These are what Group by and the filters in an experiment
  read. Group by offers only the first ten metadata keys in alphabetical order, and Harbor's own keys
  would fill them, so a run carries these ten and no others. A run cannot be given metadata afterwards.
- On each dataset example: the oracle's outcome as the reference output, and the same labels.
- On the experiment: the graph, model, reasoning effort, commit and tools, and whether its model calls were paced.
- On each trial's run: what the agent proposed and what it told the customer, at the top of the output.
- On a missed case: a comment on the reward score saying what was expected.
- Three more scores per trial: tool_calls, model_calls and agent_seconds.
- The experiment's end time in UTC. Harbor hands the plugin a local time with no zone.

It also changes two things Harbor's plugin does:

- Tokens are counted once. Harbor adds a model run to each trial holding the trial's token total, for
  agents that leave no trace. This agent's trace already carries every call's tokens, and LangSmith
  adds the two together.
- A trial Harbor runs again gets a run of its own. Harbor reuses the trial's name, the plugin makes the
  run's id from that name, and LangSmith accepts one result per run, so the second result was lost.

It overrides methods of harbor-langsmith 0.3.1 that are not a public interface, so check it when Harbor
is updated.
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from harbor.trial.hooks import TrialEvent
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


def messages(result) -> list[dict]:
    """The conversation the agent left behind: one "tool" message per tool call, and one "ai" message per model
    call, carrying that call's token usage. A reply that code wrote is an "ai" message with no usage."""
    try:
        return json.loads((trial_folder(result) / "agent/result.json").read_text())["messages"]
    except (OSError, ValueError, KeyError):
        return []


def measures(result) -> dict[str, float]:
    """What the trial cost in calls and time, as scores beside the grader's."""
    said = messages(result)
    out = {}
    if said:
        out = {"tool_calls": sum(m.get("type") == "tool" for m in said),
               "model_calls": sum(m.get("type") == "ai" and bool(m.get("usage_metadata")) for m in said)}
    ran = result.agent_execution
    if ran and ran.started_at and ran.finished_at:
        out["agent_seconds"] = round((ran.finished_at - ran.started_at).total_seconds(), 1)
    return out


def reply(result) -> str | None:
    """The agent's last message to the customer, as text whichever way the model's API shapes it."""
    content = next((m.get("content") for m in reversed(messages(result)) if m.get("type") == "ai"), None)
    if isinstance(content, list):
        content = "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content or None


class Annotated(LangSmithPlugin):
    def __init__(self, *, graph: str | None = None, model: str | None = None, reasoning_effort: str | None = None,
                 commit: str | None = None, tools: list[str] | None = None, paced: bool = False, **kwargs: Any):
        super().__init__(**kwargs)
        # LangSmith's experiment table has Models and Tools columns that read the plural keys.
        self._about = {"graph": graph, "model": model, "models": [model], "reasoning_effort": reasoning_effort,
                       "commit": commit, "tools": tools or [], "paced": paced}
        self._trial = threading.local()  # trials finish on separate threads
        self._lock = threading.Lock()
        self._ended: set[str] = set()  # trials that have finished once
        self._again: dict[str, int] = {}  # how many times Harbor has started each of them again

    def _handle_event_sync(self, event) -> None:
        name = event.config.trial_name
        with self._lock:
            if event.event == TrialEvent.START and name in self._ended:
                self._again[name] = self._again.get(name, 0) + 1
        super()._handle_event_sync(event)
        if event.event in {TrialEvent.END, TrialEvent.CANCEL}:
            with self._lock:
                self._ended.add(name)

    def _stable_uuid(self, *parts: Any) -> str:
        """Harbor's ids, with the retry number added for a trial that is being run again."""
        with self._lock:
            again = next((self._again[p] for p in parts if isinstance(p, str) and p in self._again), 0)
        return LangSmithPlugin._stable_uuid(*parts, *([f"retry-{again}"] if again else []))

    def _emit_usage_run(self, event, parent_run_id: str, usage_metadata: dict[str, Any]) -> None:
        """Nothing: the agent's trace under the trial already holds every call's tokens."""

    def _create_feedback(self, run_id: str, result: Any) -> None:
        super()._create_feedback(run_id, result)
        for key, score in measures(result).items():
            self._request("POST", "/feedback", ok_statuses={200, 201, 409}, json={
                "id": self._stable_uuid(run_id, "feedback", key), "run_id": run_id, "key": key, "score": score,
                "feedback_source_type": "api"})

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

    def _trial_metadata(self, event) -> dict[str, Any]:
        internal = {k: v for k, v in super()._trial_metadata(event).items() if k.startswith("ls_")}
        task = event.task_name.split("/")[-1]
        scenario = SCENARIOS.get(task)
        labels = {} if scenario is None else {
            "answer_checked_by": "hand-labeled" if scenario.id in HAND_LABELED else "unlabeled",
            "area": scenario.archetype, "difficulty": scenario.difficulty, "scenario": scenario.id}
        about = {k: self._about[k] for k in ("commit", "graph", "model", "reasoning_effort")}
        return internal | labels | about | {"task": task, "trial": event.config.trial_name}

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
