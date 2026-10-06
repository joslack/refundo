"""The scheduler stops a job that a limit has made pointless, says why, and can continue it later, and it asks
LangSmith before it starts. Nothing here starts Harbor or reaches LangSmith or a provider: the job folders are
written by the tests, and the processes and the network are stand-ins.
"""

import io
import json
import sys
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import limits  # noqa: E402
import run  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
KEYS = {"LANGSMITH_API_KEY": "test-key"}
LUNA, GLM = "openai/gpt-6-luna", run.FIREWORKS + "glm-5p3-flash"
OVERLOADED = ("Retrying langchain_fireworks.chat_models._acompletion_with_retry.<locals>._call in 4 seconds as it raised "
              "InternalServerError: Error code: 503 - {'error': {'message': 'service overloaded, please try again later'}}.\n")
NO_CREDITS = ("openai.RateLimitError: Error code: 429 - {'error': {'message': 'You have no credits remaining.', "
              "'type': 'insufficient_quota', 'code': 'credit_balance_exhausted'}}\n")
RATE_LIMITED = ("openai.RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for gpt-6-luna on "
                "tokens per min (TPM)', 'code': 'rate_limit_exceeded'}}\n")
TRACE_REFUSED = ("Failed to multipart ingest runs: langsmith.utils.LangSmithRateLimitError: Rate limit exceeded. HTTPError('429', "
                 "'{\"error\":\"Too many requests: tenant exceeded usage limits: Monthly unique traces usage limit exceeded\"}')\n")
LIMIT = "usage limit monthly_longlived_traces of 5000 exceeded"


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    """An empty jobs folder in place of evals/jobs."""
    folder = tmp_path / "evals/jobs"
    folder.mkdir(parents=True)
    monkeypatch.setattr(run, "ROOT", tmp_path)
    monkeypatch.setattr(run, "JOBS", folder)
    monkeypatch.setattr(run, "NOT_RUN", folder / "_not_run")
    return folder


def job_folder(jobs: Path, name: str, model: str = LUNA, total: int = 6, patience: float | None = None, record: bool = False) -> Path:
    job = jobs / name
    job.mkdir()
    graph, effort = name.split("-")[0], "high"
    (job / "config.json").write_text(json.dumps({"job_name": name, "agent_timeout_multiplier": patience, "agents": [{
        "name": "langgraph", "model_name": model,
        "kwargs": {"project_path": str(REPO / "agents"), "graph": graph, "model_kwargs": {"reasoning_effort": effort}}}]}))
    (job / "result.json").write_text(json.dumps({"id": "job-id", "n_total_trials": total}))
    if record:
        (job / limits.RECORD).write_text(json.dumps({"experiment_id": "exp", "experiment_name": name + "-1234", "owned": True, "settings": {
            "dataset_name": "kept-dataset", "graph": graph, "model": model.split("/")[-1], "reasoning_effort": effort,
            "commit": "abc1234", "tools": ["submit_proposal"], "paced": False}}))
    return job


def trial(job: Path, name: str, error: str | None = None, log: str = "", proposals: int = 1, finished: float | None = 0.0) -> Path:
    """A trial's folder as Harbor leaves it. `finished` is seconds from now; None is a trial still running."""
    folder = job / name
    (folder / "agent").mkdir(parents=True)
    (folder / "agent/langgraph-run.log").write_text(log)
    if finished is not None:
        at = datetime.fromtimestamp(time.time() + finished, timezone.utc).isoformat().replace("+00:00", "Z")
        (folder / "result.json").write_text(json.dumps({
            "trial_name": name, "task_name": "refundo/" + name.split("__")[0], "finished_at": at,
            "exception_info": error and {"exception_type": error, "exception_message": "Command failed (exit 1)"},
            "verifier_result": None if error == "CancelledError" else {"rewards": {"reward": float(bool(proposals)), "proposals": proposals}}}))
    return folder


# ---- what a job is started with


def test_an_openai_job_is_started_with_its_clients_request_log_on(jobs):
    """Without it that client sends a call again in silence, and the wait is taken for the model's time."""
    model, name, command = run.agent_command("sql", LUNA, "high", REPO / "agents", "abc1234", [])
    assert (model, name.rsplit("-", 2)[0]) == (LUNA, "sql-gpt-6-luna-high-abc1234")
    assert command[command.index("--ae") + 1] == "OPENAI_LOG=info"
    assert command[command.index("--job-name") + 1] == name
    assert "--ae" not in run.agent_command("sql", LUNA, "high", REPO / "agents", "abc1234", [], request_log=False)[2]
    assert "--ae" not in run.agent_command("sql", GLM, "high", REPO / "agents", "abc1234", [])[2]  # its client writes retries itself


def test_no_setting_touches_how_the_agent_calls_the_model(jobs):
    """Tolerance is outside the trial: the model's own retries and time limit are whatever the agent sets."""
    command = run.agent_command("sql", LUNA, None, REPO / "agents", "abc1234", [])[2]
    settings = [command[i + 1] for i, arg in enumerate(command) if arg == "--ak"]
    assert settings == [f"project_path={REPO / 'agents'}", "graph=sql"]


def test_trials_a_command_will_run(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "HERE", tmp_path)
    for name in ("an-01", "an-02", "be-01", "be-02", "mo-01"):
        (tmp_path / "tasks" / name).mkdir(parents=True)
    assert run.planned([]) == 5
    assert run.planned(["-k", "3"]) == 15
    assert run.planned(["-i", "an-*", "-i", "mo-01", "-k", "2", "-n", "4"]) == 6


# ---- asking LangSmith before a run


def langsmith_saying(monkeypatch, answers: dict, asked: list | None = None):
    """Stand in for LangSmith: `answers` gives the status and the body for each path."""
    def answer(method, path, keys, body=None, query=None):
        if asked is not None:
            asked.append((method, path, query))
        status, said = answers.get(path, (404, {"detail": "Not found"}))
        return status, said if isinstance(said, str) else json.dumps(said)
    monkeypatch.setattr(run, "langsmith", answer)


TAKING = {"/workspaces/current/usage_limits": (200, {"in_reject_set": False, "usage_limit_type": None, "tenant_limit": None})}
LIMITS = {"/usage-limits": (200, [{"limit_type": "monthly_traces", "limit_value": 20000},
                                  {"limit_type": "monthly_longlived_traces", "limit_value": 5000}])}


def test_a_run_is_not_started_without_a_langsmith_key(monkeypatch):
    langsmith_saying(monkeypatch, {})
    assert run.unrecorded(240, {}) == ("there is no LANGSMITH_API_KEY in .env", [])


def test_a_run_is_not_started_while_langsmith_is_refusing_traces(monkeypatch):
    langsmith_saying(monkeypatch, {"/workspaces/current/usage_limits": (200, {
        "in_reject_set": True, "usage_limit_type": "user_defined_monthly_longlived_traces", "tenant_limit": 5000})})
    problem, notes = run.unrecorded(240, KEYS)
    assert problem == "it is refusing traces now: the workspace is over its user_defined_monthly_longlived_traces limit of 5000"


def test_a_run_is_not_started_where_langsmith_does_not_answer(monkeypatch):
    langsmith_saying(monkeypatch, {"/workspaces/current/usage_limits": (0, "URLError: no route to host")})
    assert run.unrecorded(240, KEYS)[0] == "it did not say whether it is taking traces (HTTP 0: URLError: no route to host)"
    langsmith_saying(monkeypatch, {"/workspaces/current/usage_limits": (401, {"detail": "Invalid token"})})
    assert "HTTP 401" in run.unrecorded(240, KEYS)[0]


def test_a_run_that_does_not_fit_the_months_limit_is_not_started(monkeypatch):
    """12 experiments of 240 trials against 5,000 extended-retention traces a month with 2,600 used."""
    asked = []
    langsmith_saying(monkeypatch, TAKING | LIMITS | {"/orgs/current/billing/granular-usage": (200, {"usage": [
        {"dimensions": {}, "traces": 2000}, {"dimensions": {}, "traces": 600}]})}, asked)
    problem, notes = run.unrecorded(2880, KEYS, now=datetime(2026, 10, 5, 21, 0, tzinfo=timezone.utc))
    assert problem == ("this run records about 2880 traces, and the workspace's monthly_longlived_traces limit of 5000 has "
                       "about 2400 left this month")
    assert notes == ["monthly_traces: about 2600 of 20000 used this month"]
    usage = [query for _, path, query in asked if path == "/orgs/current/billing/granular-usage"]
    assert [query.get("trace_tier") for query in usage] == [None, "longlived"]  # all traces, then extended retention
    assert usage[0]["start_time"] == "2026-10-01T00:00:00+00:00" and usage[0]["end_time"] == "2026-10-05T21:00:00+00:00"
    assert run.unrecorded(2400, KEYS)[0] is None


def test_a_run_goes_ahead_where_langsmith_takes_traces_and_says_no_more(monkeypatch):
    langsmith_saying(monkeypatch, TAKING)
    problem, notes = run.unrecorded(240, KEYS)
    assert problem is None and "only its answer that it is taking traces was checked" in notes[0]
    langsmith_saying(monkeypatch, TAKING | LIMITS | {"/orgs/current/billing/granular-usage": (403, {"detail": "Forbidden"})})
    assert run.unrecorded(240, KEYS) == (None, ["monthly_traces is limited to 20000; it did not say how many are used",
                                                "monthly_longlived_traces is limited to 5000; it did not say how many are used"])
    langsmith_saying(monkeypatch, TAKING | {"/usage-limits": (200, [])})  # no limit set on the workspace
    assert run.unrecorded(240, KEYS) == (None, [])
    langsmith_saying(monkeypatch, TAKING | {"/usage-limits": (200, [{"limit_type": "monthly_traces", "limit_value": 50, "scope": "project"}])})
    assert run.unrecorded(240, KEYS) == (None, ["a limit this does not check is set: monthly_traces of 50 for one project"])


def test_the_stop_names_the_reason_and_the_way_past_it(monkeypatch, capsys):
    monkeypatch.setattr(run, "unrecorded", lambda traces, keys: (
        "it is refusing traces now: the workspace is over its total_unique_traces limit of 5000", ["monthly_traces: about 9 of 10 used this month"]))
    with pytest.raises(SystemExit) as stop:
        run.asked(240, KEYS)
    assert "Nothing was started. LangSmith would not record this run: it is refusing traces now" in str(stop.value)
    assert "--no-langsmith-check" in str(stop.value)
    monkeypatch.setattr(run, "unrecorded", lambda traces, keys: (None, ["monthly_traces: about 9 of 10 used this month"]))
    run.asked(1, KEYS)
    assert capsys.readouterr().out == "LangSmith: monthly_traces: about 9 of 10 used this month\n"


class Reply(io.BytesIO):
    status = 202

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_request_to_langsmith_goes_where_the_plugin_sends_its_own(monkeypatch):
    sent = []

    def urlopen(request, timeout):
        sent.append(request)
        return Reply(b'{"ok": true}')

    monkeypatch.setattr(run.urllib.request, "urlopen", urlopen)
    keys = KEYS | {"LANGSMITH_ENDPOINT": "https://langsmith.example/", "LANGSMITH_WORKSPACE_ID": "workspace"}
    assert run.langsmith("POST", "/runs", keys, body={"id": "a"}, query={"x": "1"}) == (202, '{"ok": true}')
    request = sent[0]
    assert (request.full_url, request.get_method(), request.data) == ("https://langsmith.example/api/v1/runs?x=1", "POST", b'{"id": "a"}')
    assert (request.get_header("X-api-key"), request.get_header("X-tenant-id")) == ("test-key", "workspace")
    run.langsmith("GET", "/usage-limits", KEYS)
    assert sent[1].full_url == "https://api.smith.langchain.com/api/v1/usage-limits" and sent[1].data is None


def test_a_refusal_and_an_unreachable_langsmith_come_back_as_answers(monkeypatch):
    def refused(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, io.BytesIO(b'{"error":"usage limit"}'))

    def unreachable(request, timeout):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(run.urllib.request, "urlopen", refused)
    assert run.langsmith("POST", "/runs", KEYS, body={}) == (429, '{"error":"usage limit"}')
    monkeypatch.setattr(run.urllib.request, "urlopen", unreachable)
    assert run.langsmith("POST", "/runs", KEYS, body={}) == (0, "URLError: <urlopen error no route to host>")


# ---- reading a running job


def test_a_jobs_folder_is_read_trial_by_trial_and_a_finished_trial_once(jobs, monkeypatch):
    job = job_folder(jobs, "sql-gpt-6-luna-high-abc1234-1005-120000")
    trial(job, "an-01__aaa")
    trial(job, "an-02__bbb", "ApiRateLimitError", RATE_LIMITED, proposals=0)
    trial(job, "be-01__ccc", log=OVERLOADED, finished=None)
    seen = {}
    rows = {r["name"]: r for r in run.trials_of(job, seen)}
    assert [(r["not_run"], r["proposed"], bool(r["finished"])) for r in rows.values()] == [
        ("", True, True), (limits.RATE, False, True), ("", False, False)]
    assert rows["be-01__ccc"]["retried_calls"] == 1
    monkeypatch.setattr(limits, "standing", lambda *a: pytest.fail("a trial that has not changed was read again"))
    assert run.trials_of(job, seen) == list(rows.values())


def test_a_job_is_summed_up_in_one_line(jobs):
    job = job_folder(jobs, "sql-gpt-6-luna-high-abc1234-1005-120000")
    for n in range(4):
        trial(job, f"an-0{n}__aaa", log=OVERLOADED if n == 0 else TRACE_REFUSED if n == 1 else "")
    trial(job, "be-01__bbb", "ApiRateLimitError", RATE_LIMITED, proposals=0)
    trial(job, "be-02__ccc", "CancelledError", proposals=0)
    trial(job, "be-03__ddd", "CancelledError", proposals=0)
    assert run.summary(run.trials_of(job)) == ("4 results, 3 not run (1 rate limited, 2 job stopped); seconds from 2 of them "
                                               "(1 had a model call sent again, 1 a refused trace)")
    assert run.summary([]) == "0 results"


def stop(job: Path, began: float | None = None, now: float | None = None):
    return run.why_stop(job, run.trials_of(job), time.time() - 3600 if began is None else began, now or time.time())


def test_a_healthy_job_is_left_running(jobs):
    job = job_folder(jobs, "sql-gpt-6-luna-high-abc1234-1005-120000")
    for n in range(8):
        trial(job, f"an-0{n}__aaa", proposals=n % 2, finished=-60 * n)  # every other case has no proposal, none in a row
    trial(job, "be-01__bbb", "ApiRateLimitError", RATE_LIMITED, proposals=0, finished=-1)
    trial(job, "be-02__ccc", log=OVERLOADED, finished=None)  # one of two running cases has had a call sent again
    trial(job, "be-03__ddd", finished=None)
    assert stop(job, now=time.time() + 7200) is None


def test_a_job_langsmith_has_stopped_recording_is_stopped_with_every_other(jobs):
    job = job_folder(jobs, "sql-gpt-6-luna-high-abc1234-1005-120000")
    trial(job, "an-01__aaa")
    kept = limits.Unsent(job / limits.UNSENT)
    kept.close(LIMIT)
    assert stop(job) == ("every", f"LangSmith is not taking its records: {LIMIT}")


def test_a_job_whose_agent_traces_are_refused_for_a_limit_is_stopped(jobs):
    job = job_folder(jobs, "sql-gpt-6-luna-high-abc1234-1005-120000")
    trial(job, "an-01__aaa", log=TRACE_REFUSED, finished=None)
    scope, why = stop(job)
    assert scope == "every" and why.endswith("Monthly unique traces usage limit exceeded")
    other = job_folder(jobs, "sql-gpt-6-luna-low-abc1234-1005-120000")
    trial(other, "an-01__aaa", log="Failed to get info from https://api.smith.langchain.com: LangSmithConnectionError('reset')\n")
    assert stop(other) is None  # a dropped connection is not a limit


def test_a_job_on_an_empty_account_is_stopped_with_the_providers_others(jobs):
    job = job_folder(jobs, "sql-gpt-6-luna-high-abc1234-1005-120000")
    trial(job, "an-01__aaa")
    trial(job, "an-02__bbb", "ApiUsageLimitError", NO_CREDITS, proposals=0)
    assert stop(job) == ("provider", "the provider's account is out of credits")


def test_a_job_whose_last_cases_came_to_nothing_is_stopped_with_the_models_others(jobs):
    job = job_folder(jobs, "sql-gpt-6-luna-high-abc1234-1005-120000")
    trial(job, "an-00__aaa", finished=-700)
    for n in range(1, 4):
        trial(job, f"an-0{n}__aaa", proposals=0, finished=-100 * n)
    for n in range(4, 6):
        trial(job, f"an-0{n}__aaa", "ApiRateLimitError", RATE_LIMITED, proposals=0, finished=-100 * n)
    assert stop(job) is None  # five
    # A case cut off after it had proposed is as lost as one cut off before.
    trial(job, "an-06__aaa", "AgentTimeoutError", OVERLOADED, proposals=1, finished=-1)
    assert stop(job) == ("model", "its last 6 cases came to nothing: 1 provider not answering, 3 no proposal, 2 rate limited")


def test_a_job_whose_provider_has_gone_quiet_is_stopped_before_its_cases_time_out(jobs):
    """Fireworks answered 503 and then nothing: three cases sat for twenty minutes each before Harbor cut them off."""
    job = job_folder(jobs, "sql-glm-5p3-flash-high-abc1234-1005-120000", GLM, patience=2.0)
    for name in ("be-01__aaa", "be-06__bbb", "esc-09a__ccc"):
        trial(job, name, log=OVERLOADED, finished=None)
    began = time.time()
    assert stop(job, began, began + 599) is None  # ten minutes at this effort
    scope, why = stop(job, began, began + 601)
    assert scope == "model"
    assert why == ("the provider is not answering: every running case has had a model call sent again and none has finished "
                   "for 10 minutes (InternalServerError: Error code: 503 - {'error': {'message': 'service overloaded, please "
                   "try again later'}}.)")
    trial(job, "mo-03a__ddd", finished=-30)  # a case that finishes shows the provider is answering
    assert stop(job, began - 900, time.time()) is None


def test_a_resumed_job_is_not_stopped_for_what_stopped_it_before(jobs):
    job = job_folder(jobs, "sql-gpt-6-luna-high-abc1234-1005-120000")
    for n in range(7):
        trial(job, f"an-0{n}__aaa", proposals=0, log=TRACE_REFUSED, finished=-3600 - n)
    assert stop(job, began=time.time() - 7200)[0] == "every"
    assert stop(job, began=time.time()) is None


def test_trouble_is_told_when_it_starts_and_when_it_has_doubled(jobs):
    job = job_folder(jobs, "sql-glm-5p3-flash-high-abc1234-1005-120000", GLM)
    told = {}
    assert run.news(run.trials_of(job), told) == []
    trial(job, "an-01__aaa", log=OVERLOADED)
    assert run.news(run.trials_of(job), told) == [
        "cases with a model call sent again: 1 (InternalServerError: Error code: 503 - {'error': {'message': 'service "
        "overloaded, please try again later'}}.); their seconds are not used"]
    assert run.news(run.trials_of(job), told) == []
    trial(job, "an-02__bbb", log=OVERLOADED)
    trial(job, "an-03__ccc", "ApiRateLimitError", RATE_LIMITED, proposals=0)
    lines = run.news(run.trials_of(job), told)
    assert lines[0].startswith("cases with a model call sent again: 2 (") and lines[1] == "cases not run: 1 rate limited"
    trial(job, "an-04__ddd", log=OVERLOADED)
    assert run.news(run.trials_of(job), told) == []  # three is not yet twice two


# ---- running jobs and stopping them


class Process:
    """A Harbor process that does nothing. `acts` is called at every look, and may write into the job's folder."""

    started: list = []
    code = 0  # what it exits with when nobody stops it

    def __init__(self, command, cwd, stdout, stderr, env):
        self.command, self.env, self.output, self.looks, self.terminated = command, env, stdout, 0, False
        Process.started.append(self)

    def wait(self, timeout):
        """It runs for three looks unless it is stopped first."""
        if self.terminated:
            return 143
        self.looks += 1
        self.acts(self)
        if self.looks > 3:
            return self.code
        raise run.subprocess.TimeoutExpired(self.command, timeout)

    def terminate(self):
        self.terminated = True

    acts = staticmethod(lambda process: None)


@pytest.fixture
def harbor(jobs, monkeypatch):
    Process.started = []
    monkeypatch.setattr(run.subprocess, "Popen", Process)
    monkeypatch.setattr(run, "LOOK_EVERY", 0.01)
    return Process


def test_when_langsmith_stops_recording_one_job_no_other_is_started(jobs, harbor, monkeypatch, capsys):
    first = job_folder(jobs, "sql-gpt-6-luna-high-abc1234-1005-120000")
    trial(first, "an-01__aaa", finished=60)

    def refuse(process):
        if process.looks == 2:
            limits.Unsent(first / limits.UNSENT).close(LIMIT)

    monkeypatch.setattr(harbor, "acts", staticmethod(refuse))
    monkeypatch.setattr(run, "JOBS_AT_ONCE", 1)
    commands = [(LUNA, first.name, ["harbor", "run", "--job-name", first.name]),
                (GLM, "sql-glm-5p3-flash-high-abc1234-1005-120000", ["harbor", "run"]),
                (LUNA, "structured-gpt-6-luna-high-abc1234-1005-120000", ["harbor", "run"])]
    assert run.run_all(commands, KEYS) == 3
    out = capsys.readouterr().out
    assert len(harbor.started) == 1 and harbor.started[0].terminated
    assert harbor.started[0].env["LANGSMITH_API_KEY"] == "test-key"  # `harbor jobs resume` reads no --env-file
    assert f"finish {first.name} (stopped: LangSmith is not taking its records: {LIMIT}): 1 results" in out
    assert "not run sql-glm-5p3-flash-high-abc1234-1005-120000" in out and "not run structured-gpt-6-luna-high-abc1234-1005-120000" in out
    # The waiting jobs are taken off the list when the stop is decided, not when Harbor has finished stopping.
    assert out.index("not run structured-gpt-6-luna-high") < out.index(f"finish {first.name}")
    assert f"  uv run python evals/run.py resume {first.name}\n" in out
    assert out.rstrip().endswith("naming their graphs and models:\n  sql-glm-5p3-flash-high-abc1234-1005-120000 "
                                 "structured-gpt-6-luna-high-abc1234-1005-120000")


def test_an_empty_account_drops_that_providers_jobs_and_no_others(jobs, harbor, monkeypatch, capsys):
    first = job_folder(jobs, "sql-gpt-6-luna-high-abc1234-1005-120000")
    monkeypatch.setattr(harbor, "acts", staticmethod(lambda process: process.command[-1] == first.name and process.looks == 1 and trial(
        first, "an-01__aaa", "ApiUsageLimitError", NO_CREDITS, proposals=0, finished=60)))
    monkeypatch.setattr(run, "JOBS_AT_ONCE", 1)
    commands = [(LUNA, first.name, ["harbor", "run", "--job-name", first.name]),
                ("openai/gpt-5.4-mini", "sql-gpt-5.4-mini-high-abc1234-1005-120000", ["harbor", "run", "mini"]),
                (GLM, "sql-glm-5p3-flash-high-abc1234-1005-120000", ["harbor", "run", "glm"])]
    assert run.run_all(commands, KEYS) == 2
    out = capsys.readouterr().out
    assert [process.command[-1] for process in harbor.started] == [first.name, "glm"]
    assert "(stopped: the provider's account is out of credits): 0 results, 1 not run (1 out of credits)" in out
    assert "not run sql-gpt-5.4-mini-high-abc1234-1005-120000" in out
    assert "finish sql-glm-5p3-flash-high-abc1234-1005-120000: 0 results" in out


def test_a_job_that_stops_by_itself_is_reported_with_the_error_it_ended_on(jobs, harbor, monkeypatch, capsys):
    def fail(process):
        if process.looks == 1:
            process.output.write('Traceback (most recent call last):\n  File "plugin.py", line 1, in _setup\n'
                                 "evals.limits.Refused: Too many requests: tenant exceeded usage limits\n⠋ 0:00:01 running\n")
            process.output.flush()

    monkeypatch.setattr(harbor, "acts", staticmethod(fail))
    monkeypatch.setattr(harbor, "code", 1)
    assert run.run_all([(LUNA, "sql-gpt-6-luna-high-abc1234-1005-120000", ["harbor", "run"])], KEYS) == 1
    assert ("finish sql-gpt-6-luna-high-abc1234-1005-120000 (exit 1: evals.limits.Refused: Too many requests: tenant exceeded "
            "usage limits): 0 results") in capsys.readouterr().out


def test_a_job_that_ends_with_trials_not_run_is_named_for_resuming(jobs, harbor, capsys):
    job = job_folder(jobs, "sql-gpt-6-luna-high-abc1234-1005-120000")
    trial(job, "an-01__aaa", finished=60)
    trial(job, "an-02__bbb", "ApiRateLimitError", RATE_LIMITED, proposals=0, finished=60)
    assert run.run_all([(LUNA, job.name, ["harbor", "run"])], KEYS) == 0
    out = capsys.readouterr().out
    assert f"notice {job.name}: cases not run: 1 rate limited" in out
    assert f"finish {job.name}: 1 results, 1 not run (1 rate limited)" in out
    assert f"resume {job.name}" in out


# ---- continuing a job


@pytest.fixture
def quiet(monkeypatch):
    """The models answer, LangSmith has room, and no Harbor process is alive."""
    monkeypatch.setattr(run, "unsound", lambda model, keys: None)
    monkeypatch.setattr(run, "unrecorded", lambda traces, keys: (None, []))
    monkeypatch.setattr(run, "running", lambda name: False)


def stopped_job(jobs: Path, name: str = "sql-gpt-6-luna-high-abc1234-1005-120000", record: bool = True) -> Path:
    job = job_folder(jobs, name, record=record)
    trial(job, "an-01__aaa")
    trial(job, "an-02__bbb", log=OVERLOADED)  # a result whose seconds are not used
    trial(job, "be-01__ccc", "ApiRateLimitError", RATE_LIMITED, proposals=0)
    trial(job, "be-02__ddd", "CancelledError", proposals=0)
    trial(job, "be-03__eee", "AgentTimeoutError", proposals=0)  # no sign of the provider: a result
    return job


def test_resuming_sends_what_langsmith_refused_sets_the_unrun_trials_aside_and_continues_the_job(jobs, quiet, monkeypatch, capsys):
    job = stopped_job(jobs)
    kept = limits.Unsent(job / limits.UNSENT)
    kept.close(LIMIT)
    kept.keep("PATCH", "/runs/a", {"end_time": "t"}, {200, 202, 204})
    kept.keep("POST", "/feedback", {"key": "reward", "score": 1}, {200, 201, 409})
    sent = []
    monkeypatch.setattr(run, "langsmith", lambda method, path, keys, body=None, query=None: sent.append((method, path, body)) or (202, ""))

    commands = run.resume([job.name], "another-dataset", KEYS)

    assert sent == [("PATCH", "/runs/a", {"end_time": "t"}), ("POST", "/feedback", {"key": "reward", "score": 1})]
    assert not (job / limits.UNSENT).exists()
    assert sorted(p.name for p in job.iterdir() if p.is_dir()) == ["an-01__aaa", "an-02__bbb", "be-03__eee"]
    assert sorted(p.name for p in (jobs / "_not_run" / job.name).iterdir()) == ["be-01__ccc", "be-02__ddd"]
    (model, name, command), = commands
    assert (model, name) == (LUNA, job.name)
    assert command[:7] == ["harbor", "jobs", "resume", "-p", str(job), "--plugin", "evals.langsmith_plugin:Annotated"]
    settings = dict(arg.split("=", 1) for arg in command[8::2])
    assert command[7::2] == ["--pk"] * 7
    # The job is recorded where it was first recorded, whatever dataset is named now.
    assert json.loads(settings["dataset_name"]) == "kept-dataset" and json.loads(settings["commit"]) == "abc1234"
    out = capsys.readouterr().out
    assert f"{job.name}: 3 results, 2 not run (1 rate limited, 1 job stopped); seconds from 2 of them" in out
    assert "; to run: 3" in out
    assert f"sent LangSmith 2 of the 2 records it refused earlier ({LIMIT})" in out
    assert f"trials not run, moved to evals/jobs/_not_run/{job.name}: 2" in out


def test_nothing_is_moved_or_started_while_langsmith_still_refuses(jobs, quiet, monkeypatch):
    job = stopped_job(jobs)
    kept = limits.Unsent(job / limits.UNSENT)
    kept.close(LIMIT)
    kept.keep("PATCH", "/runs/a", {"end_time": "t"}, {200, 202, 204})
    monkeypatch.setattr(run, "langsmith", lambda *a, **k: (429, json.dumps({"error": LIMIT})))
    with pytest.raises(SystemExit) as stop:
        run.resume([job.name], run.DATASET, KEYS)
    assert f"LangSmith is still not taking records: {LIMIT}" in str(stop.value)
    assert (job / "be-01__ccc").is_dir() and not (jobs / "_not_run").exists()
    assert len(limits.unsent(job / limits.UNSENT)[1]) == 1


def test_nothing_is_started_where_the_model_or_langsmith_would_stop_it_again(jobs, quiet, monkeypatch):
    job = stopped_job(jobs)
    monkeypatch.setattr(run, "unsound", lambda model, keys: "HTTP 503: service overloaded")
    with pytest.raises(SystemExit, match="did not answer a test question"):
        run.resume([job.name], run.DATASET, KEYS)
    monkeypatch.setattr(run, "unsound", lambda model, keys: None)
    asked = []
    monkeypatch.setattr(run, "unrecorded", lambda traces, keys: asked.append(traces) or ("it is refusing traces now", []))
    with pytest.raises(SystemExit, match="LangSmith would not record this run"):
        run.resume([job.name], run.DATASET, KEYS)
    assert asked == [3] and (job / "be-01__ccc").is_dir()  # six planned, three results
    run.resume([job.name], run.DATASET, KEYS, check=False)
    assert not (job / "be-01__ccc").exists()


def test_a_job_that_is_running_or_complete_is_left_alone(jobs, quiet, monkeypatch, capsys):
    alive = stopped_job(jobs)
    done = job_folder(jobs, "structured-gpt-6-luna-high-abc1234-1005-120000", total=2)
    trial(done, "an-01__aaa")
    trial(done, "an-02__bbb")
    monkeypatch.setattr(run, "running", lambda name: name == alive.name)
    with pytest.raises(SystemExit, match="No job to continue"):
        run.resume(["abc1234"], run.DATASET, KEYS)
    out = capsys.readouterr().out
    assert f"{alive.name}: still running, left alone" in out and f"{done.name}: nothing to run, it has all its results" in out
    assert (alive / "be-01__ccc").is_dir()


def test_a_commit_names_every_job_of_a_run(jobs, quiet):
    first = stopped_job(jobs)
    second = stopped_job(jobs, "structured-gpt-6-luna-high-abc1234-1005-120000")
    job_folder(jobs, "sql-gpt-6-luna-high-fff9999-1005-120000", record=True)
    commands = run.resume(["abc1234"], run.DATASET, KEYS)
    assert [(model, name) for model, name, _ in commands] == [(LUNA, first.name), (LUNA, second.name)]


def unrecorded_job(jobs: Path) -> Path:
    """A stopped job whose folder does not name its experiment, as every job started before the plugin wrote that."""
    job = stopped_job(jobs, "structured-glm-5p3-flash-high-abc1234-1005-120000", record=False)
    (job / "config.json").write_text((job / "config.json").read_text().replace(LUNA, GLM))
    return job


def test_a_job_that_does_not_name_its_experiment_has_it_found_in_langsmith_before_it_is_resumed(jobs, quiet, monkeypatch):
    job = unrecorded_job(jobs)
    name, asked = job.name + "-job-id", []  # Harbor's plugin names an experiment after the job and the start of its id
    langsmith_saying(monkeypatch, {"/sessions": (200, [{"id": "other", "name": name + "-2"}, {"id": "exp-1", "name": name, "reference_dataset_id": "ds-1"}]),
                                   "/datasets": (200, [{"id": "ds-1", "name": "the-dataset"}])}, asked)
    (model, _, command), = run.resume([job.name], "the-dataset", KEYS)
    assert model == GLM and asked == [("GET", "/sessions", {"name": name}), ("GET", "/datasets", {"name": "the-dataset"})]
    settings = {"dataset_name": "the-dataset", "graph": "structured", "model": "glm-5p3-flash", "reasoning_effort": "high",
                "commit": "abc1234", "tools": run.tools_of("structured", REPO / "agents"), "paced": False}
    assert json.loads((job / limits.RECORD).read_text()) == {"experiment_id": "exp-1", "experiment_name": name, "owned": True, "settings": settings}
    assert {key: json.loads(value) for key, value in (arg.split("=", 1) for arg in command[8::2])} == settings


def test_a_job_whose_experiment_cannot_be_found_or_is_on_another_dataset_is_not_resumed(jobs, quiet, monkeypatch):
    job = unrecorded_job(jobs)
    name = job.name + "-job-id"
    langsmith_saying(monkeypatch, {"/sessions": (200, []), "/datasets": (200, [{"id": "ds-1", "name": run.DATASET}])})
    with pytest.raises(SystemExit, match=f"cannot be continued in its experiment: LangSmith shows no experiment named {name}"):
        run.resume([job.name], run.DATASET, KEYS)
    langsmith_saying(monkeypatch, {"/sessions": (200, [{"id": "exp-1", "name": name, "reference_dataset_id": "ds-0"}]),
                                   "/datasets": (200, [{"id": "ds-1", "name": run.DATASET}])})
    with pytest.raises(SystemExit, match="is not on the dataset quillstack-refund-requests-v2; name the dataset it was recorded in"):
        run.resume([job.name], run.DATASET, KEYS)
    assert not (job / limits.RECORD).exists() and (job / "be-01__ccc").is_dir()


def test_a_live_harbor_process_is_found_by_the_jobs_name(monkeypatch):
    listing = ("/Users/x/.local/share/uv/tools/harbor/bin/python /Users/x/.local/bin/harbor run -p /repo/evals/tasks -o /repo/evals/jobs "
               "--job-name sql-gpt-6-luna-high-abc1234-1005-120000\n"
               "/Users/x/.local/share/uv/tools/harbor/bin/python /Users/x/.local/bin/harbor jobs resume -p "
               "/repo/evals/jobs/sql-glm-5p3-flash-high-abc1234-1005-120000 --plugin evals.langsmith_plugin:Annotated\n"
               "python evals/run.py resume structured-gpt-6-luna-high-abc1234-1005-120000\n")
    monkeypatch.setattr(run.subprocess, "run", lambda *a, **k: type("Done", (), {"stdout": listing})())
    assert run.running("sql-gpt-6-luna-high-abc1234-1005-120000")
    assert run.running("sql-glm-5p3-flash-high-abc1234-1005-120000")
    assert not run.running("structured-gpt-6-luna-high-abc1234-1005-120000")  # this scheduler itself is not a job
