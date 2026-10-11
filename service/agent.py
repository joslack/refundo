"""The agent that serves refund requests: the structured-tools agent that was evaluated, in a live conversation,
with a person's review before a decision is recorded.

agents/ holds the designs that the offline evaluation compares on one thing, the policy decision, and nothing in it
stops for a person. This is the one design that is served. It takes its prompt, its tools and its model settings
from agents/structured.py and agents/shared.py, so the decision a customer gets is made the way it was measured.
What it adds is what a live conversation needs, and those parts are checked on its traces, not offline:

    review     the agent stops before a submit_proposal call and waits for a person
    issuing    a recorded refund or credit is issued, with who approved it (service/records.py)
    follow-up  the conversation goes on after the decision, and the agent reads what was issued before it says so
    the wait   with its decision the agent writes a line for the customer to read while a person reviews it

A run's config sets "review":
    "all"          the agent stops before every submit_proposal call
    "escalations"  it stops only when the proposed action is escalate, and records and issues every other decision
                   itself
The person approves the call, edits its arguments, or rejects it with a note the model reads. The graph needs a
checkpointer to wait in, which a LangGraph server gives it. A run that resumes a stop names the person in
"reviewer". The account, the review setting and the reviewer are in the run's config, so they are on its trace.
"""

import sys
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware, wrap_tool_call
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))
import records  # noqa: E402
from shared import chat_model, policy, tools_for  # noqa: E402
from structured import PROMPT, TOOLS  # noqa: E402

# The model and effort that got every case right on each of three repeats (evals/results/6e71349/results.csv).
MODEL = {"model": "fireworks:accounts/fireworks/models/deepseek-v4p1-flash", "model_kwargs": {"reasoning_effort": "medium"}}

REVIEW = {"allowed_decisions": ["approve", "edit", "reject"],
          "description": "The agent's decision on this refund request. Nothing is recorded until a person approves it."}
WHEN = {"all": lambda request: True,
        "escalations": lambda request: request.tool_call["args"].get("action") == "escalate"}

# Added after the evaluated prompt. {review} is one of the two texts below.
LIVE = """

<live_chat>
You are in a live support chat, and the customer can write again after your decision.

{review}

submit_proposal returns what was recorded and whether a refund or credit was issued. Tell the customer that a
refund or credit was issued only when it says so.

When the customer writes again about a request you have decided, do not call submit_proposal for it again. If they
ask what happened to it, or whether a refund or credit has been issued, call get_request_status and answer from what
it returns. Decide a request about a different charge or a different problem as you decided the first.
</live_chat>"""
REVIEW_TEXT = {
    "all": "A support person reviews every decision before it is recorded. submit_proposal returns after they have "
           "approved it, changed it, or sent it back with a note. Nothing is issued before they approve.",
    "escalations": "You record every decision yourself, and a refund or credit you record is issued at once. The one "
                   "exception is an escalation, which a support person reviews before it is recorded.",
}


# An argument the served agent's submit_proposal takes and the evaluated one does not. The chat shows it to the
# customer while the call waits for a person, so the customer hears from the agent before the wait and not only
# after it. It is taken off the call before the records server sees it.
WHILE_WAITING = "message_while_waiting"
WHILE_WAITING_IS = ("One or two sentences the customer reads if this decision waits for a support person's review. Say "
                    "that you have looked at the account and that a specialist is checking the request before you "
                    "answer. Do not state the decision, an amount, or how long the review takes.")


def with_message_while_waiting(submit):
    """submit_proposal as the records server offers it, with the one argument more."""
    schema = submit.args_schema
    return submit.model_copy(update={"args_schema": schema | {
        "properties": schema["properties"] | {WHILE_WAITING: {"type": "string", "description": WHILE_WAITING_IS}},
        "required": [*schema["required"], WHILE_WAITING]}})


def dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


@tool
async def get_request_status(config: RunnableConfig) -> dict | str:
    """What has been issued in this conversation: each refund or credit, its amount, when, and whether a support
    person approved it. Call it when the customer asks what happened to a request you have decided."""
    configurable = config["configurable"]
    found = await records.issued(configurable["account"], configurable["thread_id"])
    if not found:
        return "No refund or credit has been issued in this conversation."
    return {"issued": [{"kind": r["kind"], "amount_cents": r["amount_cents"], "issued_at": r["issued_at"],
                        "approved_by_a_support_person": r["approved_by"] != records.AGENT} for r in found]}


@wrap_tool_call
async def issue_what_was_recorded(request, handler):
    """Once submit_proposal has recorded a decision, issue the refund or credit if it is one, and say in the tool's
    result what happened: what was issued, and whether a support person reviewed the decision. The agent writes its
    reply from this result, so a reply that follows a review says that the review is over. The call reaches here
    only after the review step let it through, with the reviewer's changes if there were any."""
    call, configurable = request.tool_call, request.runtime.config["configurable"]
    if call["name"] != "submit_proposal":
        return await handler(request)
    recorded = {name: value for name, value in call["args"].items() if name != WHILE_WAITING}
    result = await handler(request.override(tool_call=call | {"args": recorded}))
    # A run that resumes a stop names its reviewer. With review on every decision, a call that got here was approved
    # by someone even if the page they used gave no name. The record names the reviewer; the model is told only
    # that a person approved, so the name cannot reach the customer.
    args = call["args"]
    approved_by = configurable.get("reviewer") or ("a support person" if configurable.get("review", "all") == "all"
                                                   else records.AGENT)
    reviewed = approved_by != records.AGENT
    if args.get("action") in records.KINDS:
        await records.issue(configurable["account"], configurable["thread_id"], args["action"], args["amount_cents"], approved_by)
        kind = "cash refund to the original payment method" if args["action"] == "cash_refund" else "account credit"
        how = "a support person has reviewed and approved it" if reviewed else "issued by you with no review"
        result.content = f"Recorded. Issued: {dollars(args['amount_cents'])} {kind}; {how}."
    elif reviewed and args.get("action") == "escalate":
        result.content = ("Recorded. A support person has seen this escalation and has left it escalated: the outcome is "
                          "not decided yet, and nothing is issued. Tell the customer it is with a specialist.")
    elif reviewed:
        result.content = "Recorded. A support person has reviewed this decision and approved it. The review is over."
    return result


async def make_graph(config):
    configurable = MODEL | config["configurable"]
    review = configurable.get("review", "all")
    if review not in WHEN:
        raise ValueError(f'"review" is {review!r}; it can be {" or ".join(map(repr, WHEN))}.')
    tools = [with_message_while_waiting(t) if t.name == "submit_proposal" else t for t in await tools_for(configurable, TOOLS)]
    tools.append(get_request_status)
    return create_agent(chat_model(configurable), tools=tools,
                        system_prompt=PROMPT.format(policy=policy()) + LIVE.format(review=REVIEW_TEXT[review]),
                        middleware=[HumanInTheLoopMiddleware(interrupt_on={"submit_proposal": REVIEW | {"when": WHEN[review]}}),
                                    issue_what_was_recorded])
