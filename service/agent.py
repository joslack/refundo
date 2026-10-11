"""The agent that serves refund requests: the structured-tools agent as it was evaluated, with a person's review
before a decision is recorded.

agents/ holds the designs that the evaluation compares, and nothing in it stops for a person, so a run there
finishes unattended. This is the one design that is served. It takes its prompt, its tools and its model settings
from agents/structured.py and agents/shared.py, so what a customer talks to is what was measured, and the review
step is the only thing added.

A run's config sets "review":
    "all"          the agent stops before every submit_proposal call and waits for a person
    "escalations"  it stops only when the proposed action is escalate, and records every other decision itself
The person approves the call, edits its arguments, or rejects it with a note the model reads. The graph needs a
checkpointer to wait in, which a LangGraph server gives it.
"""

import sys
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))
from shared import chat_model, policy, tools_for  # noqa: E402
from structured import PROMPT, TOOLS  # noqa: E402

# The model and effort that got every case right on each of three repeats (evals/results/6e71349/results.csv).
MODEL = {"model": "fireworks:accounts/fireworks/models/deepseek-v4p1-flash", "model_kwargs": {"reasoning_effort": "medium"}}

REVIEW = {"allowed_decisions": ["approve", "edit", "reject"],
          "description": "The agent's decision on this refund request. Nothing is recorded until a person approves it."}
WHEN = {"all": lambda request: True,
        "escalations": lambda request: request.tool_call["args"].get("action") == "escalate"}


async def make_graph(config):
    configurable = MODEL | config["configurable"]
    review = configurable.get("review", "all")
    if review not in WHEN:
        raise ValueError(f'"review" is {review!r}; it can be {" or ".join(map(repr, WHEN))}.')
    return create_agent(chat_model(configurable), tools=await tools_for(configurable, TOOLS),
                        system_prompt=PROMPT.format(policy=policy()),
                        middleware=[HumanInTheLoopMiddleware(interrupt_on={"submit_proposal": REVIEW | {"when": WHEN[review]}})])
