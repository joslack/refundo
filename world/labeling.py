"""Hand labels: the file format, and how a label is compared with the oracle.

Labels live in labels/<labeler>.jsonl, one JSON object per line. Every row has these fields:

    case_id, action, amount_cents, sections, confidence, hard_to_call, records, sources,
    rationale, seconds, labeled_at

A row that was corrected after labeling also has `corrected_from` (the label as first recorded:
action, amount_cents, sections, records, sources, rationale), `correction_reason` and `corrected_at`.
Its own fields describe the corrected label, so the rationale and evidence support what it says now.

    uv run python -m world.labeling        prints the agreement numbers for a label file
"""

import json
import re
import sys
from pathlib import Path
from statistics import median

from world.oracle import label as oracle_label
from world.scenario import Action, Evidence, Outcome

LABELS = Path(__file__).resolve().parent / "labels"
SECTIONS = ["2", "3", "4", "4.1", "4.2", "4.3", "4.4", "5", "6", "9", "10", "11"]
ACTIONS = ["cash_refund", "account_credit", "deny", "escalate", "no_action"]
# A cash refund is full or partial by its amount, so the two oracle actions are one choice here.
TO_UI_ACTION = {"refund": "cash_refund", "partial_refund": "cash_refund", "credit": "account_credit"}
SOURCES = ["members", "workspace", "invoices", "refunds", "disputes", "subscription", "sessions", "app_events",
           "tickets"]
SOURCE_OF = {"usr": "members", "ws": "workspace", "in": "invoices", "re": "refunds", "cbtxn": "refunds",
             "dp": "disputes", "sub": "subscription", "sess": "sessions", "ev": "app_events", "tkt": "tickets"}
FIELDS = {"case_id", "action", "amount_cents", "sections", "confidence", "hard_to_call", "records", "sources",
          "rationale", "seconds", "labeled_at"}
CORRECTION_FIELDS = {"corrected_from", "correction_reason", "corrected_at"}
ORIGINAL_FIELDS = {"action", "amount_cents", "sections", "records", "sources", "rationale"}


def ui_action(action: str) -> str:
    return TO_UI_ACTION.get(action, action)


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_labels(path: Path) -> dict[str, dict]:
    return {row["case_id"]: row for row in read_rows(path)}  # a later label for the same case replaces the earlier one


def original(row: dict) -> dict:
    """The label as first recorded, before any correction."""
    return row | row["corrected_from"] if "corrected_from" in row else row


def outcome_agrees(row: dict, oracle: Outcome) -> bool:
    """Same action and amount. For an escalation the amount is what the label would have proposed."""
    amount = oracle.proposed.amount_cents if oracle.proposed else oracle.amount_cents
    return (ui_action(row["action"]), row["amount_cents"]) == (ui_action(oracle.action.value), amount)


def sections_agree(picked: list[str], oracle: Outcome) -> bool:
    """Every section the outcome rests on is named, and nothing named is off the oracle's path.

    The sections an outcome rests on are `oracle.must_cite`: the governing section, and for a monthly
    denial both §5 and §9, since each had to grant nothing. "4" may stand for one of its cases, so "4"
    covers "4.2". The reverse does not hold: a case of §4 is on the path only when the oracle looked at
    that case. The path also includes the definitions in §2, and §10 when an amount is owed.
    """
    path = set(oracle.considered) | {"2"}
    if oracle.amount_cents or (oracle.proposed and oracle.proposed.amount_cents):
        path.add("10")
    named = all(s in picked or s.split(".")[0] in picked for s in oracle.must_cite)
    return bool(picked) and named and all(p in path for p in picked)


def agrees(row: dict, oracle: Outcome) -> bool:
    return outcome_agrees(row, oracle) and sections_agree(row["sections"], oracle)


def is_covered(claim: Evidence, records: list[str], sources: list[str]) -> bool:
    """A claim is covered if the labeler cited its records, or checked the source it says is empty."""
    checked = set(sources) | {SOURCE_OF[r.split("_")[0]] for r in records if r != "request"}
    hits = [r[len("source:"):] in checked if r.startswith("source:") else r in records for r in claim.records]
    return any(hits) if claim.need == "any" else all(hits)


def uncovered(row: dict, oracle: Outcome) -> list[Evidence]:
    """The decisive claims this label's evidence does not cover."""
    return [e for e in oracle.evidence if e.kind == "decisive" and not is_covered(e, row["records"], row["sources"])]


def outcome_kind(oracle: Outcome) -> str:
    """The rule that produced an outcome: its rationale with the ids and numbers taken out."""
    why = oracle.rationale + (" -> " + oracle.proposed.rationale if oracle.proposed else "")
    return re.sub(r"in_\d+|\d+ of \d+|\d+ unused|\$[\d,.]+", "N", why)


def summary(labels: dict[str, dict], scenarios: list) -> dict:
    """The numbers reported about a label file, recomputed against the oracle as it is now."""
    cases = {s.id: oracle_label(s.world, s.request) for s in scenarios if s.id in labels}
    kinds: dict[str, list[str]] = {}
    for s in scenarios:
        kinds.setdefault(outcome_kind(cases.get(s.id) or oracle_label(s.world, s.request)), []).append(s.id)
    unlabeled = [ids for ids in kinds.values() if not set(ids) & set(labels)]
    first = {cid: original(labels[cid]) for cid in cases}
    corrected = [cid for cid in cases if "corrected_from" in labels[cid]]
    return {
        "scenarios": len(scenarios),
        "labeled": len(cases),
        "first pass, same action and amount": sum(outcome_agrees(first[c], o) for c, o in cases.items()),
        "first pass, sections also agree": sum(agrees(first[c], o) for c, o in cases.items()),
        "agree now": sum(agrees(labels[c], o) for c, o in cases.items()),
        "corrected": len(corrected),
        "corrected on action or amount": [c for c in corrected if not outcome_agrees(first[c], cases[c])],
        "corrected on sections only": [c for c in corrected if outcome_agrees(first[c], cases[c])],
        "first pass, every decisive claim cited": sum(not uncovered(first[c], o) for c, o in cases.items()),
        "first pass, a decisive claim not cited": [c for c, o in cases.items() if uncovered(first[c], o)],
        "median seconds per case": median(labels[c]["seconds"] for c in cases),
        "kinds of oracle outcome": len(kinds),
        "kinds with a hand-labeled case": len(kinds) - len(unlabeled),
        "kinds with no hand-labeled case": ["/".join(ids) for ids in unlabeled],
    }


if __name__ == "__main__":
    from world.scenarios import ALL

    labeler = sys.argv[1] if len(sys.argv) > 1 else "jonah"
    for name, value in summary(load_labels(LABELS / f"{labeler}.jsonl"), ALL).items():
        print(f"{name}: {', '.join(value) if isinstance(value, list) else value}"
              + (f" ({len(value)})" if isinstance(value, list) else ""))
