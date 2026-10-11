"""The review page: send a refund request to the served agent, and decide on what waits for a person.

service/run.py starts it. It talks to the agent's LangGraph server and to nothing else.

    Accounts       each demo account with the message its customer wrote. Send it as it is or changed, or open the
                   account in LangChain's Agent Chat UI and write there.
    Review queue   every request where the agent has stopped before it records a decision. Approve it, change it
                   and approve, or send it back with a note.
    Requests       every request, with the tools the agent called and what it told the customer.

The setting at the side says which decisions wait for a person. It is kept on each account's assistant, so it
holds for requests from this page and from the chat alike.
"""

import sys
from pathlib import Path

import streamlit as st
from langgraph_sdk import get_sync_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
from accounts import ACCOUNTS, GRAPH, REVIEW, SCENARIOS, SERVER, assistant_id, chat_url, configurable  # noqa: E402

from world.oracle import label  # noqa: E402
from world.labeling import ui_action  # noqa: E402

ACTIONS = ["cash_refund", "account_credit", "deny", "escalate", "no_action"]
ACCOUNT_OF = {assistant_id(a): a for a in ACCOUNTS}
client = get_sync_client(url=SERVER)


def text(content) -> str:
    """A message's text, whichever way the model's API shapes it."""
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content or ""


def dollars(cents: int | None) -> str:
    return f"${(cents or 0) / 100:,.2f}"


def account_of(thread: dict) -> str:
    return ACCOUNT_OF.get((thread.get("metadata") or {}).get("assistant_id"), "?")


def messages(thread: dict) -> list[dict]:
    return (thread.get("values") or {}).get("messages") or []


def customer_message(thread: dict) -> str:
    return next((text(m["content"]) for m in messages(thread) if m["type"] == "human"), "")


def waiting(thread: dict) -> tuple[str, dict] | None:
    """The stop a thread is at, if it is at one: the stop's id and the submit_proposal call it holds."""
    for interrupts in (thread.get("interrupts") or {}).values():
        for interrupt in interrupts:
            requests = (interrupt.get("value") or {}).get("action_requests") or []
            if requests:
                return interrupt["id"], requests[0]
    return None


def resume(thread: dict, decision: dict) -> None:
    client.runs.wait(thread["thread_id"], assistant_id(account_of(thread)), command={"resume": {"decisions": [decision]}},
                     config={"configurable": {"reviewer": st.session_state["reviewer"]}})


def review_setting() -> str:
    found = client.assistants.get(assistant_id(ACCOUNTS[0]))
    return found["config"]["configurable"]["review"]


def set_review(review: str) -> None:
    for account in ACCOUNTS:
        client.assistants.update(assistant_id(account), graph_id=GRAPH, config={"configurable": configurable(account, review)})


def show_accounts() -> None:
    for account in ACCOUNTS:
        scenario = SCENARIOS[account]
        with st.container(border=True):
            st.subheader(account)
            message = st.text_area("The customer's message", scenario.request.message, key=f"message-{account}")
            send, chat, _ = st.columns([1, 1, 4])
            if send.button("Send", key=f"send-{account}", type="primary"):
                thread = client.threads.create()
                client.runs.create(thread["thread_id"], assistant_id(account), input={"messages": [{"role": "user", "content": message}]})
                st.toast(f"{account}: the request is with the agent. See Requests.")
            chat.link_button("Open in chat", chat_url(account))
            with st.expander("What the answer key says"):
                key = label(scenario.world, scenario.request)
                owed = key.proposed.amount_cents if key.proposed else key.amount_cents
                st.write(f"{scenario.intent}  \n**{ui_action(key.action.value)}**, {dollars(owed)}, "
                         f"sections {', '.join(key.must_cite) or 'none'}")


def show_queue() -> None:
    threads = client.threads.search(status="interrupted", limit=50)
    stopped = [(t, waiting(t)) for t in threads]
    stopped = sorted(((t, at) for t, at in stopped if at), key=lambda found: found[0]["created_at"])
    if not stopped:
        st.info("Nothing waits for a person.")
    # A field is keyed by the stop, not the thread: a request that was sent back stops again with a new proposal,
    # and its fields then start from that proposal.
    for thread, (key, call) in stopped:
        args = call["args"]
        with st.container(border=True):
            st.subheader(f"{account_of(thread)}: {args.get('action')} {dollars(args.get('amount_cents'))}")
            st.caption("The customer wrote")
            st.write(customer_message(thread))
            if args.get("message_while_waiting"):
                st.caption("The customer is reading")
                st.write(args["message_while_waiting"])
            if args.get("action") == "escalate":
                st.info(f"The agent escalated this and would have proposed {dollars(args.get('amount_cents'))}. To decide "
                        "it now, change Action to the outcome and approve: the customer is told the outcome. Approved as "
                        "it is, it stays escalated and the customer is told a specialist will follow up.")
            action = st.selectbox("Action", ACTIONS, index=ACTIONS.index(args["action"]) if args.get("action") in ACTIONS else 0,
                                  key=f"action-{key}")
            amount = st.number_input("Amount in dollars", value=(args.get("amount_cents") or 0) / 100, min_value=0.0, step=1.0,
                                     key=f"amount-{key}")
            sections = st.text_input("Policy sections", ", ".join(args.get("sections") or []), key=f"sections-{key}")
            rationale = st.text_area("Rationale", args.get("rationale", ""), key=f"rationale-{key}")
            edited = {"action": action, "amount_cents": round(amount * 100),
                      "sections": [s.strip() for s in sections.split(",") if s.strip()], "rationale": rationale}
            changed = edited != {k: args.get(k) for k in edited}
            edited = args | edited  # the call keeps what this form does not show, such as the line the customer is reading
            approve, back = st.columns(2)
            if approve.button("Approve with these changes" if changed else "Approve", key=f"approve-{key}", type="primary"):
                with st.spinner("The agent records the decision and writes its reply..."):
                    resume(thread, {"type": "edit", "edited_action": {"name": call["name"], "args": edited}} if changed
                           else {"type": "approve"})
                st.rerun()
            note = back.text_input("A note to the agent", key=f"note-{key}", placeholder="What to look at again")
            if back.button("Send back", key=f"reject-{key}", disabled=not note):
                with st.spinner("The agent reads the note..."):
                    resume(thread, {"type": "reject", "message": note})
                st.rerun()


@st.fragment(run_every=2)
def show_requests() -> None:
    threads = client.threads.search(limit=50)
    if not threads:
        st.info("No request has been sent.")
    for thread in threads:
        said = messages(thread)
        calls = [call for m in said if m["type"] == "ai" for call in m.get("tool_calls") or []]
        proposals = [call["args"] for call in calls if call["name"] == "submit_proposal"]
        state = {"busy": "the agent is working", "interrupted": "waits for a person", "idle": "done",
                 "error": "stopped on an error"}.get(thread["status"], thread["status"])
        with st.container(border=True):
            st.markdown(f"**{account_of(thread)}**: {state}")
            st.caption(customer_message(thread))
            if calls:
                st.write("Tools called: " + ", ".join(f"`{call['name']}`" for call in calls))
            if proposals:
                last = proposals[-1]
                st.write(f"The agent proposed: **{last.get('action')}**, {dollars(last.get('amount_cents'))}, "
                         f"sections {', '.join(last.get('sections') or []) or 'none'}")
            if any(m["type"] == "tool" and "reviewer replaced" in text(m["content"]) for m in said):
                st.write("A person changed the proposal before it was recorded.")
            if thread["status"] == "idle" and said and said[-1]["type"] == "ai":
                st.write("Reply: " + text(said[-1]["content"]))


st.set_page_config(page_title="Refund requests", layout="wide")
st.title("Refund requests")
with st.sidebar:
    st.text_input("Reviewing as", "Sam Ortiz", key="reviewer", help="Recorded on what you approve.")
    now = review_setting()
    chosen = st.radio("Decisions that wait for a person", list(REVIEW), index=list(REVIEW).index(now), format_func=REVIEW.get)
    if chosen != now:
        set_review(chosen)
        st.toast(f"Set: {REVIEW[chosen].lower()}.")
    st.caption("With escalations only, the agent records every other decision itself.")
accounts, queue, requests = st.tabs(["Accounts", "Review queue", "Requests"])
with accounts:
    show_accounts()
with queue:
    if st.button("Look again"):
        st.rerun()
    show_queue()
with requests:
    show_requests()
