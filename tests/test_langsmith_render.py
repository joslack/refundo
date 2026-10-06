"""The page LangSmith loads to draw a trial, and the script that writes its test page.

The page is served where LangSmith's frame can load it, which may be a public address, so it and the files beside
it in Git hold no data, keys, ids or paths of this machine. The script's own logic is checked without Harbor: the
parts that need Harbor are imported only when it builds payloads.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parent.parent / "evals"
sys.path.insert(0, str(EVALS))
import langsmith_render_harness as harness  # noqa: E402

PAGES = ["langsmith_render.html", "langsmith_render_harness.html"]
NOT_IN_A_PUBLIC_FILE = {
    "a LangSmith or run id": r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    "an API key": r"lsv2_|\bsk-[A-Za-z0-9_-]{16,}|\bfw_[A-Za-z0-9]{16,}",
    "a home folder": r"/(?:Users|home)/",
    "an email address": r"[\w.+-]+@[\w-]+\.[a-z]{2,}",
    "a link into a LangSmith workspace": r"smith\.langchain\.com/o/",
}


@pytest.mark.parametrize("name", [*PAGES, "langsmith_render_harness.py"])
def test_holds_nothing_private(name):
    text = (EVALS / name).read_text()
    for what, pattern in NOT_IN_A_PUBLIC_FILE.items():
        assert not re.search(pattern, text), f"{name} holds {what}"


def test_the_renderer_carries_no_payload():
    page = (EVALS / "langsmith_render.html").read_text()
    assert "__DATA__" not in page and "application/json" not in page
    assert 'addEventListener("message"' in page


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("name", PAGES)
def test_scripts_parse(name, tmp_path):
    scripts = re.findall(r"<script(?![^>]*application/json)[^>]*>(.*?)</script>", (EVALS / name).read_text(), flags=re.S)
    assert scripts
    for i, code in enumerate(scripts):
        path = tmp_path / f"{i}.js"
        path.write_text(code)
        checked = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
        assert checked.returncode == 0, checked.stderr


def test_job_names():
    named = harness.JOB.match("case_file-gpt-6-luna-high-6e71349-1005-172003")
    assert (named["graph"], named["model"], named["effort"], named["commit"]) == ("case_file", "gpt-6-luna", "high", "6e71349")
    assert harness.JOB.match("sql-deepseek-v4p1-flash-medium-08e7875+-1005-101500")["commit"] == "08e7875+"
    assert harness.JOB.match("sql-gpt-6-luna-high-6e71349-1005-172003.log") is None
    assert harness.JOB.match("explore-6e71349.html") is None


def test_steps_are_the_tools_of_each_model_message():
    messages = [
        {"type": "human", "content": "Please refund me."},
        {"type": "ai", "tool_calls": [{"name": "get_request_context", "args": {}}]},
        {"type": "tool", "content": "{}"},
        {"type": "ai", "tool_calls": [{"name": "run_sql", "args": {"query": "SELECT 2"}}, {"name": "list_tables", "args": {}},
                                      {"name": "run_sql", "args": {"query": "SELECT 1"}}]},
        {"type": "tool", "content": "[]"}, {"type": "tool", "content": "[]"}, {"type": "tool", "content": "[]"},
        {"type": "ai", "tool_calls": [{"name": "submit_proposal", "args": {"action": "deny"}}]},
        {"type": "tool", "content": "Recorded."},
        {"type": "ai", "content": "I cannot refund this.", "tool_calls": []},
    ]
    assert harness.steps(messages) == [["get_request_context"], ["list_tables", "run_sql"], ["submit_proposal"]]
    assert harness.steps([]) == []


def test_chosen_cases():
    def trial(case: str, right: bool) -> dict:
        return {"case": case, "right": right}

    trials = [trial("b-02", False), trial("b-02", False), trial("a-01", True), trial("c-03", False), trial("c-03", True),
              trial("d-04", True), trial("d-04", True)]
    assert harness.chosen(trials) == ["b-02", "c-03", "a-01"]  # most wrong first, then one case that was always right
    many = [trial(f"x-{i:02}", False) for i in range(harness.MOST_WRONG + 3)]
    assert harness.chosen(many) == [f"x-{i:02}" for i in range(harness.MOST_WRONG)]


def test_trials_and_journeys_from_job_folders(tmp_path):
    job = tmp_path / "sql-gpt-6-luna-high-abc1234-1005-172003"
    for name, reward, calls in [("a-01__one", 1.0, ["list_tables"]), ("a-01__two", 0.0, [])]:
        folder = job / name
        (folder / "agent").mkdir(parents=True)
        (folder / "result.json").write_text(json.dumps({"task_name": "refundo/a-01", "verifier_result": {"rewards": {"reward": reward}}}))
        (folder / "agent/result.json").write_text(json.dumps({"messages": [{"type": "ai", "tool_calls": [{"name": c} for c in calls]}]}))
    (job / "a-02__unfinished").mkdir()  # no result yet
    (tmp_path / "sql-gpt-6-luna-high-fffffff-1005-172003" / "a-01__other").mkdir(parents=True)  # another commit
    (tmp_path / "_agents").mkdir()

    trials = harness.trials_of(tmp_path, "abc1234")
    assert [(t["folder"].name, t["case"], t["right"]) for t in trials] == [("a-01__one", "a-01", True), ("a-01__two", "a-01", False)]
    assert {(t["graph"], t["model"], t["effort"]) for t in trials} == {("sql", "gpt-6-luna", "high")}

    written = harness.journeys("abc1234", trials)
    assert written["commit"] == "abc1234" and "Sample data" in written["note"]
    assert written["trials"] == {
        "a-01__one": {"case": "a-01", "graph": "sql", "model": "gpt-6-luna", "effort": "high", "right": True, "steps": [["list_tables"]]},
        "a-01__two": {"case": "a-01", "graph": "sql", "model": "gpt-6-luna", "effort": "high", "right": False, "steps": []},
    }


def test_take():
    args = ["6e71349", "--jobs", "elsewhere", "-o", "page.html"]
    assert harness.take(args, "--jobs") == "elsewhere" and harness.take(args, "--renderer") is None
    assert args == ["6e71349", "-o", "page.html"]
