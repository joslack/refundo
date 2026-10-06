"""Running the oracle sends nothing anywhere, whatever the environment asks for.

The policy graph is a LangGraph graph, and LangGraph traces a run to LangSmith when the environment switches
tracing on. The oracle runs inside the grader, the LangSmith plugin and the result tables, where tracing is on
for the agents. An answer key that sent a trace each time it was read would spend the account's allowance and
could fail on a network error, so world.graph.walk switches tracing off for its own run.

Each test here sets the environment to ask for tracing, with an API key, then records and blocks the three ways
out: building a LangSmith tracer, building a LangSmith client, and opening a socket. Nothing can leave the
process while they run, including in the test that shows an unguarded run does reach for a tracer.
"""

import socket

import pytest
from langchain_core.tracers.langchain import LangChainTracer
from langsmith import Client
from langsmith import utils as langsmith_utils

from world.graph import POLICY
from world.oracle import decide, extract_facts, label
from world.scenarios import ALL


def forget_the_environment():
    """langsmith remembers the environment variables it has read."""
    langsmith_utils.get_env_var.cache_clear()
    langsmith_utils.get_tracer_project.cache_clear()


@pytest.fixture
def reached(monkeypatch):
    """What tried to leave the process, in an environment that asks for tracing."""
    attempts = []

    def blocked(what):
        def record(*args, **kwargs):
            attempts.append(what)
            raise RuntimeError(f"{what} was reached")
        return record

    with monkeypatch.context() as m:
        m.setattr(socket.socket, "connect", blocked("a connection"))
        m.setattr(socket.socket, "connect_ex", blocked("a connection"))
        m.setattr(socket, "getaddrinfo", blocked("a name lookup"))
        m.setattr(Client, "__init__", blocked("a LangSmith client"))
        m.setattr(LangChainTracer, "__init__", blocked("a LangSmith tracer"))
        m.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:9")  # nowhere, should all of the above fail to hold
        m.setenv("LANGSMITH_API_KEY", "not-a-key")
        m.setenv("LANGSMITH_TRACING", "true")
        m.setenv("LANGCHAIN_TRACING_V2", "true")
        forget_the_environment()
        yield attempts
    forget_the_environment()


def test_the_environment_here_does_ask_for_tracing(reached):
    assert langsmith_utils.tracing_is_enabled() is True


def test_the_oracle_reaches_no_tracer_and_opens_no_connection(reached):
    for s in ALL:
        label(s.world, s.request)
    decide(extract_facts(ALL[0].world, ALL[0].request))
    assert reached == []


def test_the_same_graph_run_without_the_guard_reaches_for_a_tracer(reached):
    """What the test above would record if walk() left tracing to the environment."""
    POLICY.invoke({"facts": extract_facts(ALL[0].world, ALL[0].request)})
    assert reached == ["a LangSmith tracer"]
