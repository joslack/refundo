"""The tables and the results page count a trial that was not run as not run, and take seconds only from trials
with nothing else inside their time.

The job folders are written here in the shape Harbor leaves them. The charts need matplotlib, which is not one of
the project's dependencies (`uv run --with matplotlib`), and the tables do not, so where it is absent a stand-in
lets evals/pareto.py be imported.
"""

import csv
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

try:
    import matplotlib  # noqa: F401
except ImportError:
    for name in ("matplotlib", "matplotlib.pyplot", "matplotlib.patheffects", "matplotlib.ticker", "matplotlib.transforms"):
        sys.modules[name] = MagicMock()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import explore  # noqa: E402
import latency  # noqa: E402
import pareto  # noqa: E402

from world.scenarios import ALL  # noqa: E402

CASES = [s.id.lower() for s in ALL[:6]]
ACCOUNT = "example-account"  # stands for the account name a provider puts in its error
OVERLOADED = ("Retrying langchain_fireworks.chat_models._acompletion_with_retry.<locals>._call in 4 seconds as it raised "
              "InternalServerError: Error code: 503 - {'error': {'message': 'service overloaded, please try again later'}}.\n")
SUSPENDED = (f"fireworks.APIStatusError: Error code: 412 - {{'error': {{'message': 'Account {ACCOUNT} is suspended, possibly "
             "due to reaching the monthly spending limit or failure to pay past invoices.'}}\n")
RATE_LIMITED = "openai.RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached', 'code': 'rate_limit_exceeded'}}\n"
TRACE_REFUSED = ("Failed to multipart ingest runs: langsmith.utils.LangSmithRateLimitError: Rate limit exceeded. HTTPError('429', "
                 "'{\"error\":\"Too many requests: tenant exceeded usage limits: Monthly unique traces usage limit exceeded\"}')\n")
REQUEST_LOG = '[2026-10-05 19:38:01 - httpx:1740 - INFO] HTTP Request: POST https://api.openai.com/v1/responses "HTTP/1.1 200 OK"\n'


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(pareto, "JOBS", tmp_path)
    monkeypatch.setattr(explore, "JOBS", tmp_path)
    monkeypatch.setattr(explore, "links", lambda commit: ({}, {}))  # the page's links come from LangSmith
    return tmp_path


def job_folder(jobs: Path, name: str, model: str, paced: bool = False) -> Path:
    job = jobs / name
    job.mkdir()
    kwargs = {"graph": name.split("-")[0]} | ({"configurable": {"calls_per_minute": 9.8}} if paced else {})
    (job / "config.json").write_text(json.dumps({"agents": [{"model_name": model, "kwargs": kwargs}]}))
    (job / "result.json").write_text(json.dumps({"started_at": "2026-10-05T17:20:03"}))
    return job


def trial(job: Path, case: str, tag: str, seconds: float, right: bool = True, error: str | None = None, log: str = "") -> None:
    """One trial as Harbor leaves it: it proposed, was graded and took this long, and the agent's log holds `log`."""
    folder = job / f"{case}__{tag}"
    (folder / "agent").mkdir(parents=True)
    (folder / "artifacts/tmp").mkdir(parents=True)
    score = 1.0 if right else 0.0
    (folder / "result.json").write_text(json.dumps({
        "task_name": f"refundo/{case}", "trial_name": folder.name,
        "verifier_result": {"rewards": {"reward": score, "action": score, "amount": score, "sections": score, "proposals": 1}},
        "agent_result": {"n_input_tokens": 1000, "n_cache_tokens": 0, "n_output_tokens": 100},
        "exception_info": error and {"exception_type": error, "exception_message": "Command failed (exit 1)"},
        "agent_execution": {"started_at": "2026-10-05T22:00:00.000000Z", "finished_at": f"2026-10-05T22:{int(seconds // 60):02}:{seconds % 60:09.6f}Z"}}))
    (folder / "agent/result.json").write_text(json.dumps({"messages": [
        {"type": "ai", "usage_metadata": {"input_tokens": 1000}, "content": "Refunded."}, {"type": "tool", "content": "ok"}]}))
    (folder / "artifacts/tmp/proposals.json").write_text(json.dumps([{"action": "approve_refund", "amount_cents": 400, "sections": "[]"}]))
    (folder / "agent/langgraph-run.log").write_text(log)


@pytest.fixture
def experiment(jobs):
    """Six trials on a Fireworks model: two clean results, one with a call sent again, one with a refused trace, a
    wrong one, and three that were not run."""
    job = job_folder(jobs, "sql-glm-5p3-flash-high-abc1234-1005-172003", "fireworks/accounts/fireworks/models/glm-5p3-flash")
    trial(job, CASES[0], "aaa", 10)
    trial(job, CASES[1], "bbb", 20)
    trial(job, CASES[2], "ccc", 30, right=False)
    trial(job, CASES[3], "ddd", 400, log=OVERLOADED)
    trial(job, CASES[4], "eee", 26, log=TRACE_REFUSED)
    trial(job, CASES[5], "fff", 1200, right=False, error="AgentTimeoutError", log=OVERLOADED)
    trial(job, CASES[0], "ggg", 5, right=False, error="NonZeroAgentExitCodeError", log=SUSPENDED)
    trial(job, CASES[1], "hhh", 9, right=False, error="CancelledError")
    return job


def test_a_trial_that_was_not_run_is_in_no_score_and_is_counted(experiment):
    row, = pareto.experiments("abc1234")
    assert (row["trials"], row["not_run"]) == (5, 3)
    assert row["not_run_reasons"] == "1 out of credits; 1 job stopped; 1 provider not answering"
    assert row["reward"] == 80.0  # four of the five results, not four of eight
    assert (row["errored"], row["no_proposal"], row["timed_out"]) == (0, 0, 0)
    assert row["cost_per_case_usd"] == round((1000 * 0.15 + 100 * 0.50) / 1e6, 6)
    assert not row["complete"]
    assert [(t["case"], t["error"], t["reason"]) for t in row["not_run_trials"]] == [
        (CASES[0], "NonZeroAgentExitCodeError", "out of credits"), (CASES[1], "CancelledError", "job stopped"),
        (CASES[5], "AgentTimeoutError", "provider not answering")]


def test_seconds_are_taken_from_trials_with_nothing_else_inside_their_time(experiment):
    row, = pareto.experiments("abc1234")
    assert row["agent_seconds_per_case"] == 20.0  # the median of 10, 20 and 30
    assert row["seconds_from_trials"] == 3
    assert (row["trials_with_calls_sent_again"], row["trials_with_trace_refused"]) == (1, 1)
    assert row["agent_seconds_per_case_left_out"] == 213.0  # the median of 400 and 26, shown beside it
    assert row["retries_logged"] is True  # langchain-fireworks writes every retry down


def test_a_time_out_with_no_sign_of_the_provider_stays_a_result_and_is_counted(jobs):
    job = job_folder(jobs, "sql-glm-5p3-flash-high-abc1234-1005-172003", "fireworks/accounts/fireworks/models/glm-5p3-flash")
    trial(job, CASES[0], "aaa", 10)
    trial(job, CASES[1], "bbb", 1200, right=False, error="AgentTimeoutError")
    row, = pareto.experiments("abc1234")
    assert (row["trials"], row["not_run"], row["timed_out"], row["reward"]) == (2, 0, 1, 50.0)


def test_an_experiment_says_whether_a_call_sent_again_would_show(jobs):
    """OpenAI's client sends a call again without a word unless its request log is on."""
    silent = job_folder(jobs, "sql-gpt-6-luna-high-abc1234-1005-172003", "openai/gpt-6-luna")
    trial(silent, CASES[0], "aaa", 10)
    logged = job_folder(jobs, "structured-gpt-6-luna-high-abc1234-1005-172003", "openai/gpt-6-luna")
    trial(logged, CASES[0], "aaa", 10, log=REQUEST_LOG)
    rows = {r["graph"]: r for r in pareto.experiments("abc1234")}
    assert rows["sql"]["retries_logged"] is False and rows["structured"]["retries_logged"] is True
    assert rows["sql"]["agent_seconds_per_case"] == 10.0  # still given; the column says it cannot be checked


def test_a_paced_experiment_has_no_seconds(jobs):
    job = job_folder(jobs, "sql-gpt-5.4-mini-high-abc1234-1005-172003", "openai/gpt-5.4-mini", paced=True)
    trial(job, CASES[0], "aaa", 10)
    trial(job, CASES[1], "bbb", 30, log=OVERLOADED)
    row, = pareto.experiments("abc1234")
    assert (row["paced"], row["agent_seconds_per_case"], row["seconds_from_trials"], row["agent_seconds_per_case_left_out"]) == (True, "", "", "")


def test_the_tables_list_the_trials_not_run_and_keep_the_providers_words_out(experiment, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    pareto.write_csv(pareto.experiments("abc1234"), out)
    with (out / "not_run.csv").open() as f:
        missing = list(csv.DictReader(f))
    assert [(m["case"], m["trial"], m["error"], m["reason"], m["job"]) for m in missing] == [
        (CASES[0], f"{CASES[0]}__ggg", "NonZeroAgentExitCodeError", "out of credits", experiment.name),
        (CASES[1], f"{CASES[1]}__hhh", "CancelledError", "job stopped", experiment.name),
        (CASES[5], f"{CASES[5]}__fff", "AgentTimeoutError", "provider not answering", experiment.name)]
    with (out / "results.csv").open() as f:
        row, = csv.DictReader(f)
    assert (row["not_run"], row["agent_seconds_per_case"], row["seconds_from_trials"], row["retries_logged"]) == ("3", "20.0", "3", "True")
    assert "cases" not in row and "not_run_trials" not in row
    with (out / "cases.csv").open() as f:
        cases = {c["trial"]: c for c in csv.DictReader(f)}
    assert len(cases) == 5 and f"{CASES[0]}__ggg" not in cases
    assert (cases[f"{CASES[3]}__ddd"]["retried_calls"], cases[f"{CASES[4]}__eee"]["trace_refused"]) == ("1", "True")
    with (out / "misses.csv").open() as f:
        assert [m["case"] for m in csv.DictReader(f)] == [CASES[2]]  # the wrong answer; no trial that was not run
    # A provider's error can name the account. These files are committed, so none of it may reach them.
    assert all(ACCOUNT not in path.read_text() for path in out.iterdir())


def test_the_page_shows_what_was_not_run_and_which_seconds_are_not_used(experiment):
    found = explore.data("abc1234", [])
    shown, = found["experiments"]
    assert (shown["trials"], shown["not_run"], shown["not_run_reasons"]) == (5, 3, "1 out of credits; 1 job stopped; 1 provider not answering")
    assert (shown["seconds"], shown["seconds_trials"], shown["seconds_left_out"]) == (20.0, 3, 213.0)
    assert (shown["retried"], shown["trace_refused"], shown["paced"], shown["retries_logged"]) == (1, 1, False, True)
    assert sorted((t["c"], t["why"], t["err"]) for t in found["not_run"]) == sorted([
        (CASES[0], "out of credits", "NonZeroAgentExitCodeError"), (CASES[1], "job stopped", "CancelledError"),
        (CASES[5], "provider not answering", "AgentTimeoutError")])
    trials = {t["c"]: t for t in found["trials"]}
    assert len(found["trials"]) == 5 and sum(not t["right"] for t in found["trials"]) == 1
    assert (trials[CASES[3]]["again"], trials[CASES[3]]["refused"]) == (1, False)
    assert (trials[CASES[4]]["again"], trials[CASES[4]]["refused"]) == (0, True)
    assert ACCOUNT not in json.dumps(found)


def test_the_page_reads_every_field_the_data_holds():
    """The page is one file of markup and script; a field it does not name is a field nobody sees."""
    page = (Path(explore.__file__).parent / "explore_page.html").read_text()
    for field in ("e.not_run", "e.not_run_reasons", "e.trace_refused", "e.seconds_trials", "e.seconds_left_out", "e.paced",
                  "e.retries_logged", "D.not_run", "t.again", "t.refused", "t.why"):
        assert field in page, field


def test_model_seconds_come_from_the_last_trace_of_each_clean_result(experiment):
    """evals/latency.py times model calls from LangSmith's traces; these are the traces it may use."""
    def name(case: int, tag: str) -> str:
        return f"{CASES[case]}__{tag}"

    roots = [("t1", name(0, "aaa"), 100.0), ("t2", name(1, "bbb"), 100.0), ("t3", name(3, "ddd"), 100.0),
             ("t4", name(4, "eee"), 100.0), ("t5", name(5, "fff"), 100.0), ("t6", name(1, "hhh"), 100.0),
             ("t7", name(0, "aaa"), 50.0),  # an earlier attempt that Harbor ran again under the same name
             ("t8", "an-99__gone", 100.0)]  # a trial whose folder is not in the job
    assert latency.timed(roots, experiment) == {"t1", "t2"}
