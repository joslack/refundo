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
- In the job's folder, langsmith.json: the experiment the job is recorded in and these settings. A job that
  is resumed reads it and records into the same experiment.

It also changes what Harbor's plugin does:

- Tokens are counted once. Harbor adds a model run to each trial holding the trial's token total, for
  agents that leave no trace. This agent's trace already carries every call's tokens, and LangSmith
  adds the two together.
- A trial Harbor runs again gets a run of its own. Harbor reuses the trial's name, the plugin makes the
  run's id from that name, and LangSmith accepts one result per run, so the second result was lost.
- A trial that was not run (evals/limits.py: the provider refused or never answered, the account was out of
  credits, the job was stopped) gets no scores. Its run carries the error and a not_run score that names the
  reason, so the experiment's reward is over the trials that were run.
- A trial with a model call sent again, or with its trace refused, gets no agent_seconds score: the waits are
  inside the time.
- A write LangSmith refuses for a usage limit is not sent again. Harbor's plugin sends a refused request five
  more times over 31 seconds, and a trial waits on that at its start, its agent's start and its grader's start;
  a monthly limit does not lift in that time. The write is kept in the job's folder (langsmith-unsent.jsonl),
  and so is every later one, unsent and in order, and the job's log says so. evals/run.py stops a job that has
  that file, and its `resume` sends the file before it runs the rest. A write that fails after Harbor's retries
  for another passing reason (a 5xx, no connection) is kept the same way. What the file cannot hold is the
  agent's own trace under the trial: the agent's container sends that itself.
- Anything else that goes wrong while a trial is being recorded is written to the job's log with the trial's
  name. Harbor's plugin drops such an error without a word.

None of this is inside a measured time. Harbor calls the plugin before it starts the agent's clock and after
it stops it, and the plugin reads a trial's files only once the trial has ended.

It overrides methods of harbor-langsmith 0.3.1 that are not a public interface, so check it when Harbor
is updated.
"""

import asyncio
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests
from harbor.trial.hooks import TrialEvent
from harbor.utils.logger import logger
from harbor_langsmith.plugin import LangSmithPlugin

from evals import limits
from world.labeling import LABELS, load_labels, ui_action
from world.oracle import label
from world.scenarios import ALL

log = logger.getChild("langsmith")  # under Harbor's logger, so a line reaches the job's log and its output
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


def measures(result, timed: bool = True) -> dict[str, float]:
    """What the trial cost in calls and time, as scores beside the grader's. The time is left out where waits
    that are not the agent's or the model's are inside it."""
    said = messages(result)
    out = {}
    if said:
        out = {"tool_calls": sum(m.get("type") == "tool" for m in said),
               "model_calls": sum(m.get("type") == "ai" and bool(m.get("usage_metadata")) for m in said)}
    ran = result.agent_execution
    if timed and ran and ran.started_at and ran.finished_at:
        out["agent_seconds"] = round((ran.finished_at - ran.started_at).total_seconds(), 1)
    return out


def standing(result) -> dict:
    """Whether the trial was run, and what was inside its seconds, from its error and the agent's log."""
    error = result.exception_info
    return limits.standing(trial_folder(result), {"exception_info": error and {
        "exception_type": error.exception_type, "exception_message": error.exception_message}})


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
        self._settings = {"dataset_name": self.dataset_name, "graph": graph, "model": model,
                          "reasoning_effort": reasoning_effort, "commit": commit, "tools": tools or [], "paced": paced}
        self._trial = threading.local()  # trials finish on separate threads
        self._lock = threading.Lock()
        self._ended: set[str] = set()  # trials that have finished once
        self._again: dict[str, int] = {}  # how many times Harbor has started each of them again
        self._session = limits.Guarded(self._session)  # a usage limit is raised at once, not waited on
        self._unsent: limits.Unsent | None = None  # once the experiment exists: where refused writes are kept
        self._resumed = False  # whether the experiment is one the job's folder already names

    async def on_job_start(self, job) -> None:
        limits.interrupt_on_sigterm()
        await super().on_job_start(job)

    def _setup(self, job) -> None:
        record = job.job_dir / limits.RECORD
        kept = limits.read_json(record) or {}
        self._resumed = bool(kept.get("experiment_id")) and self.experiment_id is None
        if self._resumed:
            self.experiment_id, self.experiment_name = kept["experiment_id"], kept["experiment_name"]
        super()._setup(job)
        # Harbor's plugin writes an experiment's end time only where it made the experiment in this process.
        self._owns_experiment = self._owns_experiment or bool(kept.get("owned"))
        record.write_text(json.dumps({"experiment_id": self._experiment_id, "experiment_name": self._experiment_session_name,
                                      "owned": self._owns_experiment, "settings": self._settings}, indent=1))
        self._unsent = limits.Unsent(job.job_dir / limits.UNSENT)

    async def _handle_event(self, event) -> None:
        """Harbor's plugin drops whatever goes wrong while a trial is being recorded, so that a trial never fails
        for LangSmith's sake. This does the same and says so in the job's log first."""
        try:
            await asyncio.to_thread(self._handle_event_sync, event)
        except Exception as e:
            log.warning("LangSmith has no full record of %s at its %s: %s: %s", event.config.trial_name, event.event.value,
                        type(e).__name__, str(e)[:400])
            if self.fail_fast:
                raise

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
        state = standing(result)
        if state["not_run"]:
            # The grader scored whatever was in the database when the run was cut short. That is not the agent's
            # answer, so only the error is recorded, with the reason as a score to filter and count by.
            super()._create_feedback(run_id, result.model_copy(update={"verifier_result": None}))
            self._request("POST", "/feedback", ok_statuses={200, 201, 409}, json={
                "id": self._stable_uuid(run_id, "feedback", "not_run"), "run_id": run_id, "key": "not_run", "score": 1,
                "value": state["not_run"], "feedback_source_type": "api"})
            return
        super()._create_feedback(run_id, result)
        for key, score in measures(result, timed=limits.clean(state)).items():
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
        if self._unsent is None or method == "GET":
            if self._resumed and method == "PATCH" and isinstance(body, dict) and set(body) == {"reference_dataset_id"}:
                # Harbor's plugin points an experiment it is handed at the job's dataset. A resumed job's experiment
                # was made on that dataset, and LangSmith lists no such field among those an experiment can be given.
                return limits.Kept()
            # The experiment is still being set up. A failure here stops the job before any trial has started.
            return super()._request(method, path, ok_statuses=ok_statuses, **kwargs)
        if not self._unsent.why:
            try:
                return super()._request(method, path, ok_statuses=ok_statuses, **kwargs)
            except limits.Refused as e:
                why = str(e)
            except requests.RequestException as e:
                status = getattr(e.response, "status_code", None)
                if status is not None and status not in limits.RETRYABLE:
                    # LangSmith will never take this write as it is, so it is not kept. _handle_event says it was lost.
                    raise requests.HTTPError(f"{method} {path} was turned down: HTTP {status} {e.response.text[:300]}",
                                             response=e.response) from e
                why = f"{type(e).__name__}: {e}"[:300]
            if self._unsent.close(why):
                log.warning("LangSmith is not taking this job's records: %s. Every record from here on is kept in %s and "
                            "nothing more is sent. `evals/run.py resume` sends them and runs the trials that are left.",
                            why, self._unsent.path)
        return self._unsent.keep(method, path, body, ok_statuses)

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
        state = standing(result)
        unusual = {key: state[key] for key in ("not_run", "retried_calls", "trace_refused") if state[key]}
        return (proposed or {"action": None}) | {"reply": reply(result)} | unusual | outputs

    @staticmethod
    def _format_time(value: datetime) -> str:
        if value.tzinfo is None:  # Harbor's own timestamps are local time
            value = value.astimezone()
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
