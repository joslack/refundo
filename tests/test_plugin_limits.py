"""The LangSmith plugin does not wait on a usage limit, keeps what LangSmith refuses, records a resumed job in the
same experiment, and gives no scores to a trial that was not run. LangSmith is a stand-in session throughout.

Harbor and its LangSmith plugin are installed as a tool with an environment of their own, so `uv run pytest`
cannot import them. Where they are absent, the stand-in below takes their place: the parts of harbor-langsmith
0.3.1 that evals/langsmith_plugin.py builds on, written out from its source. The same tests run against the
real classes under Harbor's interpreter, with pytest borrowed from this project's environment:

    "$(uv tool dir)/harbor/bin/python" -c "import sys; sys.path.append('.venv/lib/python3.12/site-packages'); import pytest; pytest.main(['tests/test_plugin_limits.py'])"
"""

import asyncio
import json
import logging
import signal
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
import requests

from world.scenarios import ALL

try:
    import harbor_langsmith.plugin  # noqa: F401
except ImportError:
    class TrialEvent(Enum):
        START, ENVIRONMENT_START, AGENT_START, AGENT_END = "start", "environment-start", "agent-start", "agent-end"
        VERIFICATION_START, END, CANCEL = "verification-start", "end", "cancel"

    class LangSmithPlugin:
        def __init__(self, *, dataset_name=None, experiment_name=None, experiment_id=None, api_key=None, sync_dataset=None):
            self.dataset_name, self.experiment_name, self.experiment_id, self.api_key = dataset_name, experiment_name, experiment_id, api_key
            self.sync_dataset = True if sync_dataset is None else sync_dataset
            self.fail_fast, self.request_timeout, self.request_retries, self.request_retry_delay = False, 120.0, 5, 1.0
            self._session = requests.Session()
            self._base_url = ""
            self._dataset_id = self._experiment_id = self._experiment_session_name = None
            self._owns_experiment = False

        async def on_job_start(self, job):
            await asyncio.to_thread(self._setup, job)

        def _setup(self, job):
            self._base_url = "https://langsmith.invalid/api/v1"
            self._session.headers.update({"x-api-key": self.api_key})
            if self.sync_dataset:
                listed = self._request("GET", "/datasets", params={"name": self.dataset_name}, ok_statuses={200, 404}).json()
                self._dataset_id = next(d["id"] for d in listed if d.get("name") == self.dataset_name)
            if self.experiment_id is not None:
                self._experiment_id, self._experiment_session_name = self.experiment_id, self.experiment_name
                if self._dataset_id is not None:
                    self._request("PATCH", f"/sessions/{self._experiment_id}", json={"reference_dataset_id": self._dataset_id},
                                  ok_statuses={200, 202, 204})
                return
            experiment_id, name = str(uuid4()), f"{job.config.job_name}-{str(job.id)[:8]}"
            payload = {"id": experiment_id, "name": name, "extra": {"metadata": {}}}
            if self._dataset_id is not None:
                payload["reference_dataset_id"] = self._dataset_id
            self._request("POST", "/sessions", json=payload, ok_statuses={200, 201, 409})
            self._experiment_id, self._experiment_session_name = experiment_id, name
            self._owns_experiment = self.experiment_name is None

        def _request(self, method, path, *, ok_statuses, **kwargs):
            for attempt in range(self.request_retries + 1):
                try:
                    response = self._session.request(method, f"{self._base_url}{path}", timeout=self.request_timeout, **kwargs)
                except requests.RequestException:
                    if attempt < self.request_retries:
                        self._sleep_before_retry(attempt)
                        continue
                    raise
                if response.status_code in ok_statuses:
                    return response
                if response.status_code in {408, 429, *range(500, 600)} and attempt < self.request_retries:
                    self._sleep_before_retry(attempt)
                    continue
                response.raise_for_status()
                return response

        def _sleep_before_retry(self, attempt):
            time.sleep(self.request_retry_delay * (2 ** attempt))

        def _create_feedback(self, run_id, result):
            if result.verifier_result is not None:
                for key, score in result.verifier_result.rewards.items():
                    self._request("POST", "/feedback", ok_statuses={200, 201, 409}, json={
                        "id": self._stable_uuid(run_id, "feedback", key), "run_id": run_id, "key": key, "score": score,
                        "feedback_source_type": "api"})
            if result.exception_info is not None:
                self._request("POST", "/feedback", ok_statuses={200, 201, 409}, json={
                    "id": self._stable_uuid(run_id, "feedback", "harbor_error"), "run_id": run_id, "key": "harbor_error",
                    "score": 1, "value": result.exception_info.exception_type, "feedback_source_type": "api"})

        def _trial_outputs(self, result):
            return {} if result is None else {"task_name": result.task_name, "trial_name": result.trial_name}

        @staticmethod
        def _stable_uuid(*parts):
            return str(uuid5(NAMESPACE_URL, "harbor-langsmith:" + ":".join(str(p) for p in parts if p is not None)))

    for name, contents in {"harbor": {}, "harbor.trial": {}, "harbor.utils": {}, "harbor_langsmith": {},
                           "harbor.trial.hooks": {"TrialEvent": TrialEvent},
                           "harbor.utils.logger": {"logger": logging.getLogger("harbor.utils.logger")},
                           "harbor_langsmith.plugin": {"LangSmithPlugin": LangSmithPlugin}}.items():
        sys.modules[name] = types.ModuleType(name)
        vars(sys.modules[name]).update(contents)

from evals import langsmith_plugin  # noqa: E402

limits = langsmith_plugin.limits  # the module object the plugin itself holds
CASE = ALL[0].id.lower()
MONTHLY_TRACES = '{"error":"Too many requests: tenant exceeded usage limits: Monthly unique traces usage limit exceeded"}\n'
RATE_LIMITED = "openai.RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached', 'code': 'rate_limit_exceeded'}}\n"
OVERLOADED = ("Retrying langchain_fireworks.chat_models._acompletion_with_retry.<locals>._call in 4 seconds as it raised "
              "InternalServerError: Error code: 503 - {'error': {'message': 'service overloaded'}}.\n")


class Answer:
    def __init__(self, status: int = 200, text: str = "{}"):
        self.status_code, self.text = status, text

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} for url", response=self)


class LangSmith:
    """What stands in for LangSmith: it answers 200 until told what to say, and remembers every request."""

    def __init__(self):
        self.headers, self.asked, self.says = {}, [], lambda method, path: Answer()

    def request(self, method, url, timeout=None, **kwargs):
        path = url.split("/api/v1", 1)[-1]
        self.asked.append((method, path, kwargs.get("json")))
        return self.says(method, path)

    def refuse(self, status: int, text: str) -> None:
        self.says = lambda method, path: Answer(status, text)


@pytest.fixture
def waits(monkeypatch):
    """Every wait before a request is sent again, in seconds, without the waiting."""
    waited = []
    monkeypatch.setattr(time, "sleep", waited.append)
    return waited


def job(tmp_path: Path):
    hooks = {f"on_{event}": lambda callback: None for event in (
        "trial_started", "environment_started", "agent_started", "verification_started", "trial_ended", "trial_cancelled")}
    return types.SimpleNamespace(job_dir=tmp_path, id=uuid4(), _task_configs=[], **hooks,
                                 config=types.SimpleNamespace(job_name="sql-gpt-6-luna-high-abc1234-1005-120000", datasets=[], tasks=[]))


def started(tmp_path: Path, a_job=None):
    """A plugin whose experiment exists, and the stand-in it talks to."""
    plugin = langsmith_plugin.Annotated(dataset_name="scratch", api_key="test-key", sync_dataset=False, graph="sql", model="gpt-6-luna",
                                        reasoning_effort="high", commit="abc1234", tools=["submit_proposal"], paced=False)
    there = LangSmith()
    plugin._session = limits.Guarded(there)
    plugin._setup(a_job or job(tmp_path))
    return plugin, there


def test_a_usage_limit_is_not_waited_on_and_every_write_from_then_on_is_kept(tmp_path, waits, caplog):
    plugin, there = started(tmp_path)
    before = len(there.asked)
    there.refuse(429, MONTHLY_TRACES)
    with caplog.at_level(logging.WARNING):
        answer = plugin._request("POST", "/runs", json={"id": "run-1"}, ok_statuses={200, 201, 409})
        plugin._request("PATCH", "/runs/run-1", json={"end_time": "t"}, ok_statuses={200, 202, 204})
        plugin._request("POST", "/feedback", json={"key": "tool_calls", "score": 3}, ok_statuses={200, 201, 409})
    assert answer.status_code == 202  # Harbor's plugin goes on as if the write had been taken
    assert waits == []  # Harbor's plugin would have waited 1, 2, 4, 8 and 16 seconds, for each of the three
    assert len(there.asked) == before + 1  # the refused write was sent once and the two after it not at all
    why, writes = limits.unsent(tmp_path / limits.UNSENT)
    assert why == "Too many requests: tenant exceeded usage limits: Monthly unique traces usage limit exceeded"
    assert [(w["method"], w["path"], w["json"]) for w in writes] == [
        ("POST", "/runs", {"id": "run-1"}), ("PATCH", "/runs/run-1", {"end_time": "t"}),
        ("POST", "/feedback", {"key": "tool_calls", "score": 3})]
    said = [record.getMessage() for record in caplog.records]
    assert len(said) == 1 and said[0].startswith("LangSmith is not taking this job's records: Too many requests: tenant exceeded")


def test_a_passing_refusal_gets_harbors_retries_once_and_is_then_kept(tmp_path, waits):
    plugin, there = started(tmp_path)
    before = len(there.asked)
    there.refuse(429, '{"detail":"Too many requests"}')
    assert plugin._request("POST", "/runs", json={"id": "run-1"}, ok_statuses={200, 201, 409}).status_code == 202
    assert (waits, len(there.asked) - before) == ([1.0, 2.0, 4.0, 8.0, 16.0], 6)
    plugin._request("POST", "/runs", json={"id": "run-2"}, ok_statuses={200, 201, 409})  # no second half minute
    assert (waits, len(there.asked) - before) == ([1.0, 2.0, 4.0, 8.0, 16.0], 6)
    why, writes = limits.unsent(tmp_path / limits.UNSENT)
    assert why.startswith("HTTPError: 429") and [w["json"]["id"] for w in writes] == ["run-1", "run-2"]


def test_a_refusal_that_passes_loses_nothing(tmp_path, waits):
    plugin, there = started(tmp_path)
    answers = [Answer(503, "unavailable"), Answer(429, '{"detail":"Too many requests"}'), Answer(202)]
    there.says = lambda method, path: answers.pop(0)
    assert plugin._request("POST", "/runs", json={"id": "run-1"}, ok_statuses={200, 201, 409}).status_code == 202
    assert waits == [1.0, 2.0] and not (tmp_path / limits.UNSENT).exists()


def test_a_write_langsmith_turns_down_for_good_is_not_kept_and_is_said_with_the_trials_name(tmp_path, waits, caplog):
    """Harbor's plugin drops an error met while a trial is recorded. A record lost that way is at least named."""
    plugin, there = started(tmp_path)
    there.refuse(422, '{"detail":"run_type is required"}')
    with pytest.raises(requests.HTTPError, match='POST /runs was turned down: HTTP 422 {"detail":"run_type is required"}'):
        plugin._request("POST", "/runs", json={"id": "run-1"}, ok_statuses={200, 201, 409})
    assert not (tmp_path / limits.UNSENT).exists() and waits == []

    def record(event):
        plugin._request("POST", "/runs", json={"id": "run-1"}, ok_statuses={200, 201, 409})

    plugin._handle_event_sync = record
    event = types.SimpleNamespace(config=types.SimpleNamespace(trial_name=f"{CASE}__aaa"), event=types.SimpleNamespace(value="end"))
    with caplog.at_level(logging.WARNING):
        asyncio.run(plugin._handle_event(event))  # the trial itself goes on
    assert [r.getMessage() for r in caplog.records] == [
        f'LangSmith has no full record of {CASE}__aaa at its end: HTTPError: POST /runs was turned down: HTTP 422 '
        '{"detail":"run_type is required"}']


def test_a_limit_met_while_the_experiment_is_set_up_stops_the_job_with_langsmiths_reason(tmp_path, waits):
    plugin = langsmith_plugin.Annotated(dataset_name="scratch", api_key="test-key", sync_dataset=False)
    there = LangSmith()
    there.refuse(429, MONTHLY_TRACES)
    plugin._session = limits.Guarded(there)
    with pytest.raises(limits.Refused, match="Monthly unique traces usage limit exceeded"):
        plugin._setup(job(tmp_path))
    assert waits == [] and not (tmp_path / limits.UNSENT).exists()


def test_a_resumed_job_is_recorded_in_the_experiment_it_started_in(tmp_path):
    the_job = job(tmp_path)
    first, there = started(tmp_path, the_job)
    assert [(method, path) for method, path, _ in there.asked if path == "/sessions"] == [("POST", "/sessions")]
    record = json.loads((tmp_path / limits.RECORD).read_text())
    assert record == {"experiment_id": first._experiment_id, "experiment_name": first._experiment_session_name, "owned": True,
                      "settings": {"dataset_name": "scratch", "graph": "sql", "model": "gpt-6-luna", "reasoning_effort": "high",
                                   "commit": "abc1234", "tools": ["submit_proposal"], "paced": False}}

    again, there = started(tmp_path, the_job)  # `harbor jobs resume`: a new process, the same folder
    assert (again._experiment_id, again._experiment_session_name) == (first._experiment_id, first._experiment_session_name)
    assert not [path for method, path, _ in there.asked if (method, path) == ("POST", "/sessions")]
    assert again._owns_experiment  # so the experiment's end time is written when the resumed job ends
    assert json.loads((tmp_path / limits.RECORD).read_text()) == record


def on_a_dataset(tmp_path: Path, **settings):
    """A plugin set up as Harbor sets it up by default, with the job's dataset looked up in LangSmith."""
    plugin = langsmith_plugin.Annotated(dataset_name="scratch", api_key="test-key", **settings)
    there = LangSmith()
    there.says = lambda method, path: Answer(200, '[{"name": "scratch", "id": "0e0e0e0e-0000-0000-0000-000000000000"}]'
                                             if (method, path) == ("GET", "/datasets") else "{}")
    plugin._session = limits.Guarded(there)
    plugin._setup(job(tmp_path))
    return plugin, there


def test_a_resumed_jobs_experiment_is_left_on_the_dataset_it_was_made_on(tmp_path):
    """Handed an experiment, Harbor's plugin sends it the dataset in a field LangSmith's API does not list for an
    update. A resumed job does not need that request, so its start does not depend on how LangSmith takes it."""
    first, there = on_a_dataset(tmp_path)
    assert [body.get("reference_dataset_id") for method, path, body in there.asked if (method, path) == ("POST", "/sessions")] == [
        "0e0e0e0e-0000-0000-0000-000000000000"]
    again, there = on_a_dataset(tmp_path)
    assert again._experiment_id == first._experiment_id
    assert not [path for method, path, _ in there.asked if method in ("POST", "PATCH") and path.startswith("/sessions")]
    (tmp_path / limits.RECORD).unlink()
    handed, there = on_a_dataset(tmp_path, experiment_id="1f1f1f1f-0000-0000-0000-000000000000", experiment_name="by hand")
    assert [(method, path) for method, path, _ in there.asked if path.startswith("/sessions")] == [
        ("PATCH", "/sessions/1f1f1f1f-0000-0000-0000-000000000000")]  # Harbor's own behaviour, where the user names one


def test_a_job_that_is_stopped_cancels_its_trials_whichever_command_started_it(tmp_path):
    """evals/run.py stops a job with SIGTERM. `harbor jobs resume` sets nothing up for it, so the plugin does."""
    before = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        plugin = langsmith_plugin.Annotated(dataset_name="scratch", api_key="test-key", sync_dataset=False)
        plugin._session = limits.Guarded(LangSmith())
        asyncio.run(plugin.on_job_start(job(tmp_path)))
        assert plugin._experiment_id and plugin._unsent is not None
        with pytest.raises(KeyboardInterrupt):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, before)


def test_a_job_started_with_writes_still_unsent_adds_to_them_and_sends_nothing(tmp_path):
    limits.Unsent(tmp_path / limits.UNSENT).close("usage limit monthly_longlived_traces of 5000 exceeded")
    plugin, there = started(tmp_path)
    before = len(there.asked)
    plugin._request("POST", "/runs", json={"id": "run-9"}, ok_statuses={200, 201, 409})
    assert len(there.asked) == before
    assert [w["json"] for w in limits.unsent(tmp_path / limits.UNSENT)[1]] == [{"id": "run-9"}]


class Result:
    """A trial's result as Harbor hands it to the plugin when the trial ends, with its folder on disk."""

    def __init__(self, tmp_path: Path, error: str | None = None, log: str = "", reward: float = 1.0):
        folder = tmp_path / f"{CASE}__aaa"
        (folder / "agent").mkdir(parents=True, exist_ok=True)
        (folder / "agent/langgraph-run.log").write_text(log)
        (folder / "agent/result.json").write_text(json.dumps({"messages": [
            {"type": "ai", "usage_metadata": {"input_tokens": 9}, "content": "Refunded."}, {"type": "tool", "content": "ok"}]}))
        self.trial_uri, self.task_name, self.trial_name, self.agent_result = folder.as_uri(), f"refundo/{CASE}", folder.name, None
        self.exception_info = error and types.SimpleNamespace(
            exception_type=error, exception_message="Command failed (exit 1)", model_dump=lambda mode: {"exception_type": error})
        self.verifier_result = types.SimpleNamespace(rewards={"reward": reward, "action": reward, "proposals": 1})
        start = datetime(2026, 10, 5, 22, 0, tzinfo=timezone.utc)
        self.agent_execution = types.SimpleNamespace(started_at=start, finished_at=start + timedelta(seconds=17.5))

    def model_copy(self, update: dict):
        copy = types.SimpleNamespace(**vars(self))
        vars(copy).update(update)
        return copy

    def compute_token_cost_totals(self):
        return None, None, None, None


def scores(there: LangSmith) -> dict:
    return {body["key"]: body.get("value", body["score"]) for method, path, body in there.asked if path == "/feedback"}


def test_a_trial_that_was_not_run_gets_no_scores(tmp_path):
    """The grader gave it 0 for an empty database. In LangSmith that would average in as a wrong answer."""
    plugin, there = started(tmp_path)
    plugin._create_feedback("run-1", Result(tmp_path, "ApiRateLimitError", RATE_LIMITED, reward=0.0))
    assert scores(there) == {"harbor_error": "ApiRateLimitError", "not_run": "rate limited"}


def test_a_result_keeps_its_scores_and_its_time(tmp_path):
    plugin, there = started(tmp_path)
    plugin._create_feedback("run-1", Result(tmp_path))
    assert scores(there) == {"reward": 1.0, "action": 1.0, "proposals": 1, "tool_calls": 1, "model_calls": 1, "agent_seconds": 17.5}


def test_a_result_with_a_call_sent_again_or_a_refused_trace_keeps_its_scores_and_not_its_time(tmp_path):
    plugin, there = started(tmp_path)
    plugin._create_feedback("run-1", Result(tmp_path, log=OVERLOADED, reward=0.0))
    assert scores(there) == {"reward": 0.0, "action": 0.0, "proposals": 1, "tool_calls": 1, "model_calls": 1}
    plugin, there = started(tmp_path)
    plugin._create_feedback("run-2", Result(tmp_path, log="Failed to multipart ingest runs: langsmith.utils.LangSmithRateLimitError: "
                                                       + MONTHLY_TRACES))
    assert "agent_seconds" not in scores(there) and scores(there)["reward"] == 1.0


def test_a_trial_cut_off_with_no_sign_of_the_provider_is_scored(tmp_path):
    plugin, there = started(tmp_path)
    plugin._create_feedback("run-1", Result(tmp_path, "AgentTimeoutError", reward=0.0))
    assert scores(there) == {"reward": 0.0, "action": 0.0, "proposals": 1, "harbor_error": "AgentTimeoutError",
                             "tool_calls": 1, "model_calls": 1, "agent_seconds": 17.5}


def test_a_runs_output_names_what_was_unusual_about_the_trial(tmp_path):
    plugin, _ = started(tmp_path)
    plain = plugin._trial_outputs(Result(tmp_path))
    assert not {"not_run", "retried_calls", "trace_refused"} & plain.keys() and plain["reply"] == "Refunded."
    assert plugin._trial_outputs(Result(tmp_path, "ApiRateLimitError", RATE_LIMITED))["not_run"] == "rate limited"
    assert plugin._trial_outputs(Result(tmp_path, log=OVERLOADED + OVERLOADED))["retried_calls"] == 2


def test_no_score_is_lost_when_langsmith_stops_while_a_trial_is_being_recorded(tmp_path, waits):
    plugin, there = started(tmp_path)
    answers = [Answer(), Answer(429, MONTHLY_TRACES)]
    there.says = lambda method, path: answers.pop(0)
    plugin._create_feedback("run-1", Result(tmp_path))
    kept = [w["json"]["key"] for w in limits.unsent(tmp_path / limits.UNSENT)[1]]
    assert kept == ["action", "proposals", "tool_calls", "model_calls", "agent_seconds"] and waits == []
