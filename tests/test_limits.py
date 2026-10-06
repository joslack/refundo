"""A rate limit, an overloaded provider or an empty account is told from what the agent did, and LangSmith's
refusals are kept and sent later, without any of it reaching a provider or LangSmith.

The text in these tests is what the providers and LangSmith wrote into real job folders on 2026-10-05, with the
account names and ids replaced. If a trial that a provider stopped were read as a result, it would be scored as a
wrong answer; if an agent's own failure were read as the provider's, it would be run again until it passed.
"""

import json
import signal
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import limits  # noqa: E402

FRAMES = """Traceback (most recent call last):
  File "/installed-agent/langgraph_runner.py", line 355, in <module>
    asyncio.run(main())
  File "/opt/harbor-langgraph-venv/lib/python3.12/site-packages/openai/_base_client.py", line 1838, in request
    raise self._make_status_error_from_response(err.response) from None
"""
OVERLOADED = ("Retrying langchain_fireworks.chat_models._acompletion_with_retry.<locals>._call in 4 seconds as it raised "
              "InternalServerError: Error code: 503 - {'error': {'object': 'error', 'type': 'invalid_request_error', "
              "'code': 'invalid_request_error', 'message': 'service overloaded, please try again later'}}.\n")
TIMED_OUT = ("Retrying langchain_fireworks.chat_models._acompletion_with_retry.<locals>._call in 4 seconds as it raised "
             "APITimeoutError: Request timed out..\n")
# OpenAI's client says this, and only with its request log on (OPENAI_LOG=info).
OPENAI_AGAIN = ('[2026-10-05 19:38:01 - httpx:1740 - INFO] HTTP Request: POST https://api.openai.com/v1/responses "HTTP/1.1 200 OK"\n'
                "[2026-10-05 19:38:09 - openai._base_client:1172 - INFO] Retrying request to /responses in 0.439000 seconds\n")
TOKENS_PER_MINUTE = (
    FRAMES + "openai.RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for gpt-5.4-nano in "
    "organization org-example on tokens per min (TPM): Limit 200000, Used 197168, Requested 5194. Please try again in "
    "708ms.', 'type': 'tokens', 'param': None, 'code': 'rate_limit_exceeded'}}\n\n"
    "The above exception was the direct cause of the following exception:\n\n" + FRAMES
    + "langchain_openai.chat_models.base.OpenAIRateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached "
    "for gpt-5.4-nano in organization org-example on tokens per min (TPM): Limit 200000, Used 197168, Requested 5194.', "
    "'type': 'tokens', 'param': None, 'code': 'rate_limit_exceeded'}}\n"
    "During task with name 'model' and id '00000000-0000-0000-0000-000000000000'\n")
NO_CREDITS = (FRAMES + "openai.RateLimitError: Error code: 429 - {'error': {'message': 'You have no credits remaining. Add "
              "credits to continue using the API.', 'type': 'insufficient_quota', 'param': None, 'code': "
              "'credit_balance_exhausted'}}\nDuring task with name 'model' and id '00000000-0000-0000-0000-000000000000'\n")
SUSPENDED = (FRAMES + "fireworks.APIStatusError: Error code: 412 - {'error': {'message': 'Account example-account is "
             "suspended, possibly due to reaching the monthly spending limit or failure to pay past invoices.', 'param': "
             "None, 'code': 'PRECONDITION_FAILED', 'type': 'error'}, 'request_id': 'chatcmpl-0000'}\n"
             "During task with name 'model' and id '00000000-0000-0000-0000-000000000000'\n")
SERVER_ERROR = FRAMES + "openai.InternalServerError: Error code: 503 - {'error': {'message': 'The server is overloaded.'}}\n"
AGENTS_OWN = FRAMES + "RuntimeError: The model's answer was not a decision: 1 validation error for decision\n"
IN_A_TASK_GROUP = """  + Exception Group Traceback (most recent call last):
  |   File "/installed-agent/langgraph_runner.py", line 355, in <module>
  |     asyncio.run(main())
  | ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)
  +-+---------------- 1 ----------------
    | Traceback (most recent call last):
    |   File "/opt/harbor-langgraph-venv/lib/python3.12/site-packages/openai/_base_client.py", line 1838, in request
    |     raise self._make_status_error_from_response(err.response) from None
    | openai.RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached', 'code': 'rate_limit_exceeded'}}
    +------------------------------------
"""
# What LangSmith's client in the agent's container wrote when the workspace was over its monthly traces.
TRACE_REFUSED = (
    "Failed to multipart ingest runs: langsmith.utils.LangSmithRateLimitError: Rate limit exceeded for "
    "https://api.smith.langchain.com/runs/multipart. HTTPError('429 Client Error: Too Many Requests for url: "
    "https://api.smith.langchain.com/runs/multipart', '{\"error\":\"Too many requests: tenant exceeded usage limits: "
    "Monthly unique traces usage limit exceeded\"}\\n')trace=00000000-0000-0000-0000-000000000000,"
    "id=00000000-0000-0000-0000-000000000000\n")
MONTHLY_TRACES = '{"error":"Too many requests: tenant exceeded usage limits: Monthly unique traces usage limit exceeded"}\n'
EXTENDED_RETENTION = '{"error":"usage limit monthly_longlived_traces of 5000 exceeded"}'


@pytest.mark.parametrize("error, log, reason", [
    (None, "", ""),
    (None, OVERLOADED + TRACE_REFUSED, ""),  # it answered: a result, whatever else was in its time
    ("CancelledError", "", "job stopped"),
    ("VerifierTimeoutError", "", "grader cut off"),
    ("ApiUsageLimitError", NO_CREDITS, limits.CREDITS),
    ("ApiRateLimitError", TOKENS_PER_MINUTE, limits.RATE),
    ("NonZeroAgentExitCodeError", NO_CREDITS, limits.CREDITS),  # OpenAI reports an empty account as a 429
    ("NonZeroAgentExitCodeError", SUSPENDED, limits.CREDITS),  # and Fireworks as a 412, which Harbor has no type for
    ("NonZeroAgentExitCodeError", SERVER_ERROR, limits.SILENT),
    ("NonZeroAgentExitCodeError", IN_A_TASK_GROUP, limits.RATE),
    ("NonZeroAgentExitCodeError", AGENTS_OWN, ""),
    ("NonZeroAgentExitCodeError", OVERLOADED + AGENTS_OWN, ""),  # the call was sent again and answered; the failure came later
    # Harbor files a failed run under a rate limit when "rate limit" is anywhere in its output, and LangSmith's
    # refusal says it. The run's own error decides.
    ("ApiRateLimitError", TRACE_REFUSED + AGENTS_OWN + TRACE_REFUSED, ""),
    ("AgentTimeoutError", OVERLOADED, limits.SILENT),
    ("AgentTimeoutError", TIMED_OUT + TIMED_OUT, limits.SILENT),
    ("AgentTimeoutError", OPENAI_AGAIN, limits.SILENT),
    ("AgentTimeoutError", "", ""),  # nothing shows the provider held it up, so the agent may not have finished
    ("AgentTimeoutError", TRACE_REFUSED, ""),
])
def test_a_trial_is_not_run_only_when_the_provider_or_the_job_stopped_it(error, log, reason):
    assert limits.not_run(error, "Command failed (exit 1)", log) == reason


def test_the_error_is_read_from_harbors_message_where_the_agent_left_no_log():
    message = "Command failed (exit 1): /opt/harbor-langgraph-venv/bin/python runner.py\nstdout: " + SUSPENDED + "\nstderr: None"
    assert limits.not_run("NonZeroAgentExitCodeError", message, "") == limits.CREDITS
    assert limits.not_run("NonZeroAgentExitCodeError", "Command failed (exit 1)\nstdout: " + AGENTS_OWN + "\nstderr: None") == ""


def trial_folder(tmp_path: Path, name: str, error: str | None, log: str, reward: float = 0.0) -> Path:
    folder = tmp_path / name
    (folder / "agent").mkdir(parents=True)
    (folder / "agent/langgraph-run.log").write_text(log)
    (folder / "result.json").write_text(json.dumps({
        "exception_info": error and {"exception_type": error, "exception_message": "Agent execution timed out"},
        "verifier_result": {"rewards": {"reward": reward, "proposals": int(reward)}}}))
    return folder


def test_the_score_has_no_say_in_whether_a_trial_was_run(tmp_path):
    """A trial cut off after it submitted the right answer is as much not run as one cut off before."""
    right = limits.standing(trial_folder(tmp_path, "right", "AgentTimeoutError", OVERLOADED, reward=1.0))
    wrong = limits.standing(trial_folder(tmp_path, "wrong", "AgentTimeoutError", OVERLOADED, reward=0.0))
    assert right == wrong
    assert right["not_run"] == limits.SILENT


def test_what_was_inside_a_results_seconds(tmp_path):
    quiet = limits.standing(trial_folder(tmp_path, "quiet", None, ""))
    again = limits.standing(trial_folder(tmp_path, "again", None, TIMED_OUT + OVERLOADED))
    refused = limits.standing(trial_folder(tmp_path, "refused", None, TRACE_REFUSED + TRACE_REFUSED))
    assert (quiet["retried_calls"], quiet["trace_refused"], limits.clean(quiet)) == (0, "", True)
    assert (again["retried_calls"], again["trace_refused"], limits.clean(again)) == (2, "", False)
    assert "service overloaded" in limits.short(again["provider_said"])
    assert refused["retried_calls"] == 0 and not limits.clean(refused)
    assert refused["trace_refused"] == "Too many requests: tenant exceeded usage limits: Monthly unique traces usage limit exceeded"
    assert not limits.clean(limits.standing(trial_folder(tmp_path, "stopped", "CancelledError", "")))


def test_openais_request_log_shows_a_call_sent_again(tmp_path):
    silent = limits.standing(trial_folder(tmp_path, "silent", None, ""))
    logged = limits.standing(trial_folder(tmp_path, "logged", None, OPENAI_AGAIN))
    assert (silent["request_log"], silent["retried_calls"]) == (False, 0)
    assert (logged["request_log"], logged["retried_calls"]) == (True, 1)


def test_a_trial_still_running_has_no_error_yet(tmp_path):
    folder = tmp_path / "running"
    (folder / "agent").mkdir(parents=True)
    (folder / "agent/langgraph-run.log").write_text(OVERLOADED)
    state = limits.standing(folder)
    assert (state["error"], state["not_run"], state["retried_calls"]) == ("", "", 1)


@pytest.mark.parametrize("status, body, reason", [
    (429, MONTHLY_TRACES, "Too many requests: tenant exceeded usage limits: Monthly unique traces usage limit exceeded"),
    (429, EXTENDED_RETENTION, "usage limit monthly_longlived_traces of 5000 exceeded"),
    (429, "usage limit monthly_traces exceeded", "usage limit monthly_traces exceeded"),  # not JSON: said as it came
    (429, '{"detail":"Too many requests"}', None),  # too many requests a second: gone in a moment, worth sending again
    (503, "usage limit", None),
    (202, "", None),
])
def test_a_usage_limit_is_told_from_a_passing_refusal(status, body, reason):
    assert limits.refusal(status, body) == reason


class Answer:
    def __init__(self, status: int, text: str = "{}"):
        self.status_code, self.text = status, text


class Session:
    """A `requests` session that answers from a list and remembers what it was asked."""

    def __init__(self, *answers: Answer):
        self.answers, self.asked, self.headers = list(answers), [], {}

    def request(self, method, url, **kwargs):
        self.asked.append((method, url))
        return self.answers.pop(0)


def test_a_usage_limit_is_raised_and_everything_else_goes_through():
    inner = Session(Answer(202), Answer(429, '{"detail":"Too many requests"}'), Answer(429, EXTENDED_RETENTION))
    session = limits.Guarded(inner)
    session.headers["x-api-key"] = "k"  # Harbor's plugin sets its headers on whatever session it holds
    assert inner.headers == {"x-api-key": "k"}
    assert session.request("POST", "/runs").status_code == 202
    assert session.request("POST", "/runs").status_code == 429
    with pytest.raises(limits.Refused, match="monthly_longlived_traces of 5000"):
        session.request("POST", "/runs")


def test_refused_writes_are_kept_in_order_and_a_resumed_job_adds_to_them(tmp_path):
    path = tmp_path / limits.UNSENT
    kept = limits.Unsent(path)
    assert kept.why == "" and limits.unsent(path) == ("", [])
    assert kept.close("usage limit monthly_longlived_traces of 5000 exceeded") is True
    assert kept.close("something later") is False
    assert kept.keep("POST", "/runs", {"id": "a"}, {200, 201, 409}).status_code == 202
    kept.keep("PATCH", "/runs/a", {"end_time": "t"}, {200, 202, 204})
    again = limits.Unsent(path)  # the job is started again before the file has been sent
    assert again.why == "usage limit monthly_longlived_traces of 5000 exceeded"
    again.keep("POST", "/feedback", {"key": "reward"}, {200})
    why, writes = limits.unsent(path)
    assert why == "usage limit monthly_longlived_traces of 5000 exceeded"
    assert [(w["method"], w["path"], w["json"], w["ok"]) for w in writes] == [
        ("POST", "/runs", {"id": "a"}, [200, 201, 409]), ("PATCH", "/runs/a", {"end_time": "t"}, [200, 202, 204]),
        ("POST", "/feedback", {"key": "reward"}, [200])]


def kept_writes(tmp_path: Path, count: int) -> Path:
    kept = limits.Unsent(tmp_path / limits.UNSENT)
    kept.close("Monthly unique traces usage limit exceeded")
    for n in range(count):
        kept.keep("POST", "/runs", {"id": n}, {200, 201, 409})
    return kept.path


def test_kept_writes_are_sent_in_order_and_the_file_goes_when_all_are_taken(tmp_path):
    path, sent = kept_writes(tmp_path, 3), []

    def send(method, where, body):
        sent.append((method, where, body["id"]))
        return 202, ""

    assert limits.send_unsent(path, send) == (3, 0, "")
    assert sent == [("POST", "/runs", 0), ("POST", "/runs", 1), ("POST", "/runs", 2)]
    assert not path.exists()


@pytest.mark.parametrize("status, text, why", [
    (429, EXTENDED_RETENTION, "usage limit monthly_longlived_traces of 5000 exceeded"),
    (503, "upstream unavailable", "HTTP 503: upstream unavailable"),
    (0, "URLError: no route to host", "URLError: no route to host"),
])
def test_sending_stops_where_langsmith_still_refuses_and_keeps_the_rest(tmp_path, status, text, why):
    path = kept_writes(tmp_path, 3)
    answers = [(202, ""), (status, text)]
    assert limits.send_unsent(path, lambda *write: answers.pop(0)) == (1, 0, why)
    assert limits.unsent(path) == (why, limits.unsent(path)[1])
    assert [w["json"]["id"] for w in limits.unsent(path)[1]] == [1, 2]
    assert limits.send_unsent(path, lambda *write: (202, "")) == (2, 0, "")  # a later try takes up where this one stopped


def test_a_write_langsmith_will_never_take_is_set_beside_and_the_rest_go_on(tmp_path):
    path = kept_writes(tmp_path, 3)
    answers = [(202, ""), (422, '{"detail":"run not found"}'), (202, "")]
    assert limits.send_unsent(path, lambda *write: answers.pop(0)) == (2, 1, "")
    assert not path.exists()
    rejected = [json.loads(line) for line in (tmp_path / limits.REJECTED).read_text().splitlines()]
    assert [(w["json"]["id"], w["status"]) for w in rejected] == [(1, 422)]


def test_sigterm_becomes_an_interrupt_unless_something_already_handles_it():
    before = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        limits.interrupt_on_sigterm()
        with pytest.raises(KeyboardInterrupt):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        def already(signum, frame):
            pass

        signal.signal(signal.SIGTERM, already)  # as `harbor run` has set its own by the time the plugin starts
        limits.interrupt_on_sigterm()
        assert signal.getsignal(signal.SIGTERM) is already
    finally:
        signal.signal(signal.SIGTERM, before)
