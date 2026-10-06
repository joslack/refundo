"""What a rate limit, an overloaded provider or an account out of credits does to a run, read from the files
Harbor leaves, so that none of it is taken for the agent's doing or for the model's speed.

Three things are read from a trial's folder:

    not run          The trial says nothing about the agent: the provider refused or never answered, the account
                     was out of credits, the grader was cut off or the job was stopped. Such a trial is not a wrong
                     answer and not a result. evals/run.py can run it again into the same experiment.
    calls sent again How many model calls the client had to repeat. The trial's answer stands, but the waits are
                     inside its seconds, so it is left out of every time that is reported.
    trace refused    LangSmith's client in the agent's container had a request refused. The answer stands; the
                     client's own retries are inside the trial's seconds, and its trace in LangSmith is incomplete.

The rule for "not run" never looks at the score. It reads Harbor's error type and the provider's own words in the
agent's log, so a trial is not kept because it was right or run again because it was wrong.

Tolerance here means stopping, saying so, and running a whole trial again. Nothing in this file or its callers
waits or retries inside a trial, so nothing it does is inside a measured time.

The second half is LangSmith's side: telling a usage limit from a passing refusal, and the file a job keeps the
writes LangSmith would not take in. evals/langsmith_plugin.py writes that file and evals/run.py sends it later.
Only the standard library is imported, so the plugin (in Harbor's environment), the scheduler and the tables can
all use it.
"""

import json
import re
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path

CREDITS, RATE, SILENT = "out of credits", "rate limited", "provider not answering"
PROVIDER = {CREDITS, RATE, SILENT}  # the reasons that are the model provider's doing
# Harbor's error types that settle it by themselves. Its other Api* types are decided by patterns written for
# other agents' output, and one of them (`rate.?limit`) also matches LangSmith's refusals in this agent's log, so
# for every other type the provider's own error line is read.
BY_TYPE = {"CancelledError": "job stopped", "VerifierTimeoutError": "grader cut off", "ApiUsageLimitError": CREDITS}
# What a provider's error says, most specific first: OpenAI reports an empty account as a 429 as well.
SAYS = [
    (CREDITS, re.compile(r"insufficient_quota|credit_balance_exhausted|no credits remaining|exceeded your current quota"
                         r"|is suspended|spending limit|unpaid invoice|Error code: 402", re.I)),
    (RATE, re.compile(r"Error code: 429|RateLimitError|rate limit|too many requests", re.I)),
    (SILENT, re.compile(r"Error code: 5\d\d|InternalServerError|overloaded|APITimeoutError|APIConnectionError"
                        r"|Request timed out|Connection error", re.I)),
]
LANGSMITH_LINE = re.compile(r"langsmith|smith\.langchain\.com", re.I)
# A model call sent again. langchain-fireworks writes the first form at every retry. OpenAI's client writes the
# second, and only where its request log is on (OPENAI_LOG=info, which evals/run.py sets for new jobs).
SENT_AGAIN = re.compile(r"Retrying .+? in [\d.]+ seconds")
REQUEST_LOG = "HTTP Request: "  # what that request log writes for every request, so its presence shows the log is on
# The line a Python traceback ends on: a dotted class name, or a built-in exception, then the message.
ERROR_LINE = re.compile(r"(?:(?:[A-Za-z_]\w*\.)+[A-Za-z_]\w*|[A-Z]\w*(?:Error|Exception|Exit|Interrupt|Group))(?::|$)")
GROUP_MARGIN = re.compile(r"^(?:\s*[|+])+-* ?")  # what Python draws left of an error raised inside a task group
USAGE_LIMIT = re.compile(r"usage limit", re.I)  # in both of LangSmith's refusals: the plan's traces and a set limit

UNSENT = "langsmith-unsent.jsonl"  # in a job's folder: why LangSmith stopped taking writes, then each write, in order
REJECTED = "langsmith-rejected.jsonl"  # in a job's folder: writes LangSmith turned down for a reason that will not pass
RECORD = "langsmith.json"  # in a job's folder: the experiment it is recorded in, and the plugin's settings
RETRYABLE = {408, 429, *range(500, 600)}  # the statuses Harbor's plugin sends a request again for


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def log_of(folder: Path) -> str:
    """What the agent's process wrote while a trial ran: warnings from its clients, and the traceback if it failed."""
    try:
        return (folder / "agent/langgraph-run.log").read_text(errors="replace")
    except OSError:
        return ""


def provider_lines(text: str) -> tuple[list[str], str]:
    """From an agent's log: each notice of a model call sent again, and the error the run ended on ("" if none).
    LangSmith's lines are left out, so a refused trace is never read as a refused model call."""
    notices, last = [], ""
    for line in text.splitlines():
        line = GROUP_MARGIN.sub("", line)
        if not line or line[0].isspace() or LANGSMITH_LINE.search(line):
            continue
        if SENT_AGAIN.search(line):
            notices.append(line)
        elif ERROR_LINE.match(line):
            last = line
    return notices, last


def not_run(error: str | None, message: str = "", log: str = "") -> str:
    """Why a trial that ended in this error says nothing about the agent, or "" where it is a result.

    A run that failed is read by the error it ended on. A run cut off at the time limit ended on no error of its
    own, so it is read by the calls it was sending again: with any, the provider was holding it up. A time-out
    with none stays a result, because the agent may simply not have finished; evals/pareto.py counts those."""
    if not error:
        return ""
    if error in BY_TYPE:
        return BY_TYPE[error]
    notices, last = provider_lines(log if log.strip() else message)
    said = notices if error == "AgentTimeoutError" else [last]
    for reason, pattern in SAYS:
        if any(pattern.search(line) for line in said):
            return reason
    return SILENT if error == "AgentTimeoutError" and notices else ""


def trace_refusal(log: str) -> str:
    """What LangSmith's client in the agent's container said when a request of its failed, or ""."""
    line = next((line for line in log.splitlines() if "Failed to" in line and LANGSMITH_LINE.search(line)), "")
    said = re.search(r'"error":\s*"([^"]+)"', line)
    return said[1] if said else line[:200]


def short(line: str) -> str:
    """A provider's error as it reads in one line of the run's output."""
    return line.split(" as it raised ", 1)[-1].strip()[:200]


def standing(folder: Path, trial: dict | None = None) -> dict:
    """One trial's folder: the error it ended in, why it was not run (or ""), how many model calls were sent again,
    what LangSmith's client was refused (or ""), the provider's last words, and whether the model client's
    request log is in the agent's log. A trial still running has no error yet."""
    trial = trial if trial is not None else read_json(folder / "result.json") or {}
    error = trial.get("exception_info") or {}
    log = log_of(folder)
    notices, last = provider_lines(log)
    return {"error": error.get("exception_type") or "",
            "not_run": not_run(error.get("exception_type"), error.get("exception_message") or "", log),
            "retried_calls": len(notices), "trace_refused": trace_refusal(log),
            "provider_said": (notices[-1] if notices else last)[:300], "request_log": REQUEST_LOG in log}


def clean(row: dict) -> bool:
    """Whether a trial's seconds are the agent's and the model's alone: it is a result, no model call was sent
    again and LangSmith's client was refused nothing."""
    return not (row["not_run"] or row["retried_calls"] or row["trace_refused"])


# ---- LangSmith


class Refused(Exception):
    """LangSmith answered that a usage limit is reached. It is not one of `requests`' exceptions, so Harbor's
    plugin, which sends a failed request again five times over half a minute, lets it straight through."""


def refusal(status: int, body: str) -> str | None:
    """LangSmith's reason, where a response says a usage limit is reached. Such a limit lasts until the month ends
    or someone raises it, so sending the request again is a wait for nothing. Any other 429 passes in seconds."""
    if status != 429 or not USAGE_LIMIT.search(body or ""):
        return None
    try:
        return str(json.loads(body)["error"])
    except (ValueError, KeyError, TypeError):
        return body.strip()[:300]


class Guarded:
    """A `requests` session that raises Refused on a usage limit and is otherwise the session it wraps."""

    def __init__(self, session):
        self._session = session

    def request(self, method: str, url: str, **kwargs):
        response = self._session.request(method, url, **kwargs)
        if why := refusal(response.status_code, response.text):
            raise Refused(why)
        return response

    def __getattr__(self, name: str):
        return getattr(self._session, name)


class Kept:
    """Stands in for LangSmith's answer to a write that went to the file instead."""

    status_code = 202
    text = ""

    def json(self) -> dict:
        return {}


class Unsent:
    """The writes of one job that LangSmith would not take. Once one write is refused every later one is kept
    too, without being sent, so the file holds them in the order they were made and no trial waits on LangSmith."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.why = unsent(path)[0]  # a job resumed with writes still unsent adds to them, so they stay in order

    def close(self, why: str) -> bool:
        """Stop sending. Returns whether this call was the one that stopped it."""
        with self._lock:
            if self.why:
                return False
            self.why = why or "no reason given"
            with self.path.open("a") as f:
                f.write(json.dumps({"closed": self.why, "at": now()}) + "\n")
            return True

    def keep(self, method: str, path: str, body, ok_statuses) -> Kept:
        with self._lock, self.path.open("a") as f:
            f.write(json.dumps({"method": method, "path": path, "json": body, "ok": sorted(ok_statuses), "at": now()}) + "\n")
        return Kept()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def unsent(path: Path) -> tuple[str, list[dict]]:
    """Why a job's file of unsent writes was started ("" if there is none), and the writes."""
    try:
        lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        return "", []
    return next((line["closed"] for line in lines if "closed" in line), ""), [line for line in lines if "method" in line]


def send_unsent(path: Path, send) -> tuple[int, int, str]:
    """Send a job's kept writes in order. `send(method, path, body)` returns LangSmith's status and text, with
    status 0 where LangSmith could not be reached.

    Returns how many were sent, how many LangSmith turned down for good, and why sending stopped ("" at the end
    of the file). A write refused for a limit or by a failing server stops it, and that write and the rest stay in
    the file. A write turned down for another reason would be turned down every time, so it is moved to a file
    beside it and the rest go on."""
    _, writes = unsent(path)
    sent = rejected = 0
    why = ""
    for at, write in enumerate(writes):
        status, text = send(write["method"], write["path"], write["json"])
        if status in write["ok"] or 200 <= status < 300:
            sent += 1
        elif status == 0 or status in RETRYABLE:
            why = refusal(status, text) or (f"HTTP {status}: " if status else "") + text.strip()[:200]
            path.write_text("".join(json.dumps(line) + "\n" for line in [{"closed": why, "at": now()}, *writes[at:]]))
            return sent, rejected, why
        else:
            rejected += 1
            with path.with_name(REJECTED).open("a") as f:
                f.write(json.dumps(write | {"status": status, "said": text.strip()[:300]}) + "\n")
    path.unlink(missing_ok=True)
    return sent, rejected, why


def interrupt_on_sigterm() -> None:
    """Make SIGTERM cancel a job's trials and remove their containers, as an interrupt does. `harbor run` sets this
    up itself; `harbor jobs resume` does not, and a resumed job that is stopped would leave its containers running."""

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread() and signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
        signal.signal(signal.SIGTERM, interrupt)
