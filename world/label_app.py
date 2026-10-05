"""Labeling UI: decide each scenario from its records, blind, then compare with the oracle.

    uv run streamlit run world/label_app.py

Labels are appended to labels/<labeler>.jsonl. Nothing that gives the answer away is
shown while labeling: no scenario id, intent, difficulty, or ground-truth annotations.
"""

import json
import random
import time
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

import streamlit as st

from world.labeling import (ACTIONS, LABELS, SECTIONS, SOURCES, agrees, load_labels, original, ui_action,
                            uncovered)
from world.oracle import label as oracle_label
from world.scenarios import ALL

POLICY = Path(__file__).resolve().parent.parent / "docs" / "policy.md"
# The last round: one case for each kind of oracle outcome that no hand label had covered.
# ESC-14 was written after that round, so it is queued here and has no label yet.
FINAL_ROUND = ["GW-01", "GW-04a", "GW-06b", "GW-07", "AN-08", "AN-09b", "BE-10", "ESC-03a", "ESC-09a", "ESC-14"]
ACTION_LABELS = {"cash_refund": "cash refund", "account_credit": "account credit", "deny": "deny",
                 "escalate": "escalate", "no_action": "no action (requester not authorized)"}
SEED = 7  # fixes the case order, so a session can be resumed

st.set_page_config(page_title="Refundo labeling", layout="wide")


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def esc(text: str) -> str:
    """Stop Streamlit from reading dollar amounts as LaTeX."""
    return text.replace("$", "\\$")


def when(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def table(rows: list[dict]) -> None:
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")
    else:
        st.caption("No records.")


def record_options(s) -> dict[str, str]:
    """Every record in the case that a decision could cite: id -> label for the picker."""
    w = s.world
    names = {m.id: m.name for m in w.members}
    opts = {"request": "request · the customer's message"}
    opts |= {m.id: f"{m.id} · {m.name} ({m.role.value})" for m in w.members}
    opts |= {x.id: f"{x.id} · workspace and plan history" for x in w.workspaces}
    opts |= {i.id: f"{i.id} · invoice {i.created:%Y-%m-%d} · {money(i.amount_paid)}" for i in w.invoices}
    opts |= {r.id: f"{r.id} · refund {money(r.amount)}" for r in w.refunds}
    opts |= {c.id: f"{c.id} · balance {money(c.amount)}" for c in w.credits}
    opts |= {d.id: f"{d.id} · dispute ({d.status})" for d in w.disputes}
    opts |= {x.id: f"{x.id} · subscription" for x in w.subscriptions}
    for e in w.session_events:
        opts.setdefault(e.session_id, f"{e.session_id} · session · {names.get(e.user_id, '?')} · {e.at:%Y-%m-%d}")
    opts |= {e.id: f"{e.id} · {e.type} · {e.at:%Y-%m-%d %H:%M}" for e in w.app_events}
    opts |= {t.id: f"{t.id} · ticket · {t.subject}" for t in w.tickets}
    # The absence of a record is evidence too: "I checked refunds and there is none for this charge."
    opts |= {f"source:{name}": f"{name.replace('_', ' ')} · checked, nothing relevant there" for name in SOURCES}
    return opts


def show_records(s) -> None:
    w = s.world
    names = {m.id: m.name for m in w.members}
    ws = w.workspaces[0]
    tabs = st.tabs(["Workspace", "Members", "Invoices", "Refunds and credits", "Disputes", "Subscription",
                    f"Sessions ({len(w.session_events)})", f"App events ({len(w.app_events)})",
                    f"Tickets ({len(w.tickets)})"])
    with tabs[0]:
        st.write(f"**{ws.name}** · `{ws.id}` · status **{ws.status}**"
                 + (f" ({ws.suspension_reason})" if ws.suspension_reason else "")
                 + f" · created {when(ws.created_at)} · Stripe customer `{ws.stripe_customer_id}`")
        st.caption("Plan history")
        table([{"effective": when(p.effective_at), "tier": p.tier.value, "interval": p.interval.value,
                "seats": p.seats}
               for p in ws.plan_history])
    with tabs[1]:
        table([{"id": m.id, "name": m.name, "email": m.email, "role": m.role.value, "joined": when(m.joined_at),
                "removed": when(m.removed_at) if m.removed_at else ""} for m in w.members])
    with tabs[2]:
        table([{"id": i.id, "created": when(i.created), "reason": i.billing_reason,
                "period": f"{i.period_start:%Y-%m-%d} to {i.period_end:%Y-%m-%d}",
                "line": i.lines[0].description, "subtotal": money(i.subtotal), "discount": money(i.discount),
                "credit applied": money(i.credit_applied), "tax": money(i.tax), "amount_paid": money(i.amount_paid),
                "status": i.status, "charge": i.charge_id or ""} for i in w.invoices])
    with tabs[3]:
        st.caption("Refunds")
        table([{"id": r.id, "charge": r.charge_id, "amount": money(r.amount), "created": when(r.created),
                "reason": r.reason or "", "metadata": json.dumps(r.metadata) if r.metadata else ""}
               for r in w.refunds])
        st.caption("Customer balance transactions (negative is credit owed to the customer)")
        table([{"id": c.id, "amount": money(c.amount), "created": when(c.created), "description": c.description}
               for c in w.credits])
    with tabs[4]:
        table([{"id": d.id, "charge": d.charge_id, "amount": money(d.amount), "created": when(d.created),
                "reason": d.reason, "status": d.status} for d in w.disputes])
    with tabs[5]:
        table([{"id": x.id, "status": x.status, "current period": f"{x.current_period_start:%Y-%m-%d} to "
                f"{x.current_period_end:%Y-%m-%d}", "canceled_at": when(x.canceled_at) if x.canceled_at else ""}
               for x in w.subscriptions])
    with tabs[6]:
        table([{"at": when(e.at), "user": f"{names.get(e.user_id, '?')} ({e.user_id})", "session": e.session_id,
                "action": e.action} for e in w.session_events])
    with tabs[7]:
        table([{"id": e.id, "at": when(e.at), "type": e.type,
                "actor": f"{names.get(e.actor_user_id, '?')} ({e.actor_user_id})" if e.actor_user_id else "system",
                "data": json.dumps(e.data) if e.data else ""} for e in w.app_events])
    with tabs[8]:
        for t in w.tickets:
            with st.expander(f"{t.id} · {t.subject} · opened {when(t.opened_at)} · {t.status}"):
                for m in t.messages:
                    st.markdown(f"**{m.author_name}** ({m.author_type}, `{m.author_id}`) · {when(m.at)}")
                    st.write(esc(m.body))
        if not w.tickets:
            st.caption("No records.")


def label_page(order: list, labels: dict, path: Path, running: bool) -> None:
    queue = [s for s in order if s.id in FINAL_ROUND]
    todo = [s for s in queue if s.id not in labels]
    st.progress(1 - len(todo) / len(queue), text=f"{len(queue) - len(todo)} of {len(queue)} in this round · "
                                                 f"{len(labels)} of {len(order)} labeled overall")
    if not todo:
        st.success("This round is done. Switch to Review in the sidebar.")
        return
    s = todo[0]
    # Time per case only counts while the timer is running; the case is hidden while paused.
    clock = st.session_state.setdefault("clock", {}).setdefault(s.id, {"spent": 0.0, "since": None})
    if not running:
        if clock["since"] is not None:
            clock["spent"] += time.time() - clock["since"]
            clock["since"] = None
        st.info("The timer is paused and the case is hidden. Turn on \"Timer running\" in the sidebar to continue.")
        return
    if clock["since"] is None:
        clock["since"] = time.time()
    r = s.request
    requester = next((m for m in s.world.members if m.id == r.requester_user_id), None)

    st.subheader(f"Case {len(labels) + 1}")
    st.markdown(f"**Request received {when(r.received_at)}** by {r.channel}, from "
                f"**{requester.name if requester else 'unknown'}** (`{r.requester_user_id}`)")
    st.info(esc(r.message))
    show_records(s)

    st.divider()
    with st.form(f"label-{s.id}"):
        left, right = st.columns(2)
        action = left.radio("Action", ACTIONS, horizontal=True, index=None, format_func=ACTION_LABELS.get)
        amount = left.number_input("Amount in dollars, pre-tax (for an escalation: what you would have proposed)",
                                   min_value=0.0, step=0.01, format="%.2f")
        sections = left.multiselect("Policy sections that apply (the governing one, plus any you had to "
                                    "consult and rule out)", SECTIONS)
        confidence = right.radio("Confidence", ["low", "medium", "high"], horizontal=True, index=2)
        hard = right.checkbox("Hard to call (two careful people could disagree)")
        opts = record_options(s)
        picked = st.multiselect(
            "Evidence: the records your decision rests on. To cite the absence of a record, pick the "
            "\"checked, nothing relevant there\" entry for that source (they are at the end of the list).",
            list(opts), default=["request"], format_func=opts.get)
        records = [x for x in picked if not x.startswith("source:")]
        sources = [x[len("source:"):] for x in picked if x.startswith("source:")]
        rationale = st.text_area("Rationale, in a sentence or two")
        if st.form_submit_button("Save and next", type="primary"):
            if action is None or not sections:
                st.error("Pick an action and at least one section.")
                st.stop()
            path.parent.mkdir(exist_ok=True)
            with path.open("a") as f:
                f.write(json.dumps({
                    "case_id": s.id, "action": action, "amount_cents": round(amount * 100), "sections": sections,
                    "confidence": confidence, "hard_to_call": hard,
                    "records": records, "sources": sources, "rationale": rationale,
                    "seconds": round(clock["spent"] + time.time() - clock["since"], 1),
                    "labeled_at": datetime.now(UTC).isoformat(),
                }) + "\n")
            st.rerun()


def review_page(order: list, labels: dict) -> None:
    done = [s for s in order if s.id in labels]
    if not done:
        st.info("Nothing labeled yet.")
        return
    rows, disagreements, gaps = [], [], []
    for s in done:
        mine, oracle = labels[s.id], oracle_label(s.world, s.request)
        decisive = [e for e in oracle.evidence if e.kind == "decisive"]
        missed = uncovered(mine, oracle)
        if missed:
            gaps.append((s, missed))
        oracle_amount = oracle.proposed.amount_cents if oracle.proposed else oracle.amount_cents
        picked = mine["sections"]
        agree = agrees(mine, oracle)
        rows.append({"case": s.id, "tier": s.difficulty, "agree": "yes" if agree else "NO",
                     "your action": ACTION_LABELS[mine["action"]],
                     "oracle action": ACTION_LABELS[ui_action(oracle.action.value)],
                     "your amount": money(mine["amount_cents"]), "oracle amount": money(oracle_amount),
                     "your §": ", ".join(picked),
                     "oracle §": f"{', '.join(oracle.must_cite)} (path: {', '.join(oracle.considered)})",
                     "decisive claims covered": f"{len(decisive) - len(missed)} of {len(decisive)}",
                     "corrected": "yes" if mine.get("corrected_from") else "",
                     "hard to call": "yes" if mine["hard_to_call"] else "", "seconds": mine["seconds"]})
        if not agree:
            disagreements.append((s, mine, oracle))
    a, b, c, d = st.columns(4)
    a.metric("Labeled", f"{len(done)} of {len(order)}")
    b.metric("Agree with oracle", f"{len(done) - len(disagreements)} of {len(done)}")
    c.metric("Median time per case", f"{median(r['seconds'] for r in rows):.0f} s")
    d.metric("Marked hard to call", sum(1 for s in done if labels[s.id]["hard_to_call"]))
    table(rows)
    st.subheader("Disagreements")
    st.caption("Each one is a labeling slip, an oracle bug, or a gap in the policy.")
    for s, mine, oracle in disagreements:
        with st.expander(f"{s.id} · {s.intent}"):
            st.markdown(esc(f"**You:** {mine['action']}, {money(mine['amount_cents'])}, §{', '.join(mine['sections'])}. {mine['rationale']}"))
            st.markdown("**Oracle's claims:**\n" + esc("\n".join(f"- {e.claim} `{', '.join(e.records)}`" + ("" if e.kind == "decisive" else " (routine check)") for e in oracle.evidence)))
            st.markdown(esc(f"**Oracle:** {oracle.action.value}, {money(oracle.amount_cents)}, "
                            f"§{', '.join(oracle.must_cite)}. {oracle.rationale}"))
            if oracle.proposed:
                st.markdown(esc(f"**Oracle would have proposed:** {oracle.proposed.action.value}, "
                                f"{money(oracle.proposed.amount_cents)}, §{oracle.proposed.section}. {oracle.proposed.rationale}"))
            st.info(esc(s.request.message))
            show_records(s)
    if not disagreements:
        st.caption("None.")
    corrected = [(s, labels[s.id]) for s in done if labels[s.id].get("corrected_from")]
    st.subheader(f"Corrected labels ({len(corrected)})")
    st.caption("Labels changed after a policy ruling or a scoring change, without relabeling. Each row keeps the "
               "label as first recorded.")
    table([{"case": s.id,
            "was": f"{ACTION_LABELS[original(m)['action']]}, {money(original(m)['amount_cents'])}, "
                   f"§{', '.join(original(m)['sections'])}",
            "now": f"{ACTION_LABELS[m['action']]}, {money(m['amount_cents'])}, §{', '.join(m['sections'])}",
            "why": m["correction_reason"]} for s, m in corrected])
    st.subheader("Evidence gaps")
    st.caption("Decisive claims your evidence doesn't cover: facts that produce this outcome. Routine checks that "
               "found nothing (no dispute, not suspended, nothing already refunded) are not scored here.")
    for s, missed in gaps:
        with st.expander(f"{s.id} · {len(missed)} uncovered"):
            for e in missed:
                st.markdown(esc(f"- {e.claim} `{', '.join(e.records)}`" + (" (any one)" if e.need == "any" else "")))
    if not gaps:
        st.caption("None.")


order = ALL[:]
random.Random(SEED).shuffle(order)

with st.sidebar:
    st.title("Refundo labeling")
    labeler = st.text_input("Labeler", "jonah")
    page = st.radio("Mode", ["Label", "Review"])
    running = st.toggle("Timer running", value=False)
    with st.expander("Policy"):
        st.markdown(esc(POLICY.read_text()))

labels_path = LABELS / f"{labeler}.jsonl"
labels = load_labels(labels_path)
if page == "Label":
    label_page(order, labels, labels_path, running)
else:
    review_page(order, labels)
