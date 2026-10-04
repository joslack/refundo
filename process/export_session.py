"""Turn the Claude Code session log into a readable, de-identified transcript.

    uv run python process/export_session.py [--report]

Reads the raw session log from ~/.claude and writes process/claude-session.md. The header written at
the top of that file says exactly what is changed. In short: tool output, images and Claude's private
reasoning are dropped; turns about the presentation, recording and repository housekeeping are left
out and marked; and in Claude's replies, sentences about the interview or the presentation are removed.

Two inputs are not in git, because they quote what they remove. process/redactions.local.txt lists
identifiers: one `pattern => replacement` per line, and lines starting with `sentence:` name patterns
whose whole sentence is removed from Claude's replies. process/export_rules.local.json lists which
turns and sections to leave out.
"""

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = next((Path.home() / ".claude" / "projects").glob("*/cf3d26da-*.jsonl"))
OUT = HERE / "claude-session.md"

# Which turns to leave out is decided per message, by how the message starts. Those rules quote the
# omitted messages, so they live in process/export_rules.local.json, which is not in git. The kinds of rule:
#   DROP            turn left out, with a marker giving the reason
#   STOP_AT         the record ends at this message
#   USER_ONLY       the message is kept and Claude's reply is left out, with a marker
#   REPLY_FROM      show another turn's reply under this message (for messages sent mid-task)
#   CUT_SECTIONS    headings removed from Claude's reply, with everything under them
#   CUT_PARAGRAPHS  paragraphs removed from Claude's reply, by how they start
#   CUT_LINES       single lines removed from Claude's reply, by how they start
#   USER_CUT        the off-topic start of a mixed message, replaced by a marker
RULES = json.loads((HERE / "export_rules.local.json").read_text())
DROP, STOP_AT, USER_ONLY, REPLY_FROM = RULES["DROP"], RULES["STOP_AT"], RULES["USER_ONLY"], RULES["REPLY_FROM"]
CUT_SECTIONS, CUT_PARAGRAPHS, CUT_LINES, USER_CUT = (RULES[k] for k in ("CUT_SECTIONS", "CUT_PARAGRAPHS", "CUT_LINES", "USER_CUT"))
# In Claude's replies, a sentence that matches is removed.
ABOUT_THE_INTERVIEW = (r"\binterview\w*|pushback answers?|a pushback answer|their pushback|\bthe rubric\b"
                       r"|\bdecks?\b|\bslides?\b|look bad|rehears\w*|presentation|prep (notes|session)")
DANGLING = re.compile(r"^(It|That|This|These|Those|They|So|Then|Either|Both)\b")
removed: list[str] = []


def local_rules():
    replace, sentence = [], []
    local = HERE / "redactions.local.txt"
    for line in local.read_text().splitlines() if local.exists() else []:
        if line.startswith("sentence:"):
            sentence.append(line.split(":", 1)[1].strip())
        elif "=>" in line and not line.startswith("#"):
            pattern, replacement = (part.strip() for part in line.split("=>", 1))
            replace.append((re.compile(pattern), replacement))
    return replace, sentence


REPLACE_LOCAL, SENTENCE_LOCAL = local_rules()
SENTENCE = re.compile("|".join([ABOUT_THE_INTERVIEW] + SENTENCE_LOCAL), re.I)
REPLACE = REPLACE_LOCAL + [(re.compile(p), r) for p, r in [
    (r"</?pasted_content[^>]*>", ""),
    (r"[\w.+-]+@[\w-]+(\.[\w-]+)+", "[email removed]"),
    (r"/private/tmp/claude-\d+/[^\s`'\"]*scratchpad", "[scratch]"),
    (r"/Users/[a-z]+", "~"),
    (r"https://claude\.ai/[^\s)\]]+", "[private Claude link]"),
]]


def redact(text: str) -> str:
    for pattern, replacement in REPLACE:
        text = pattern.sub(replacement, text)
    return text


def clean_reply(text: str) -> str:
    """Remove sentences about the interview from one of Claude's replies. Code blocks are left alone."""
    out, in_code, skip_level = [], False, None
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_code = not in_code
        heading = re.match(r"(#+) ", line)
        if skip_level and heading and len(heading.group(1)) <= skip_level:
            skip_level = None
        if skip_level:
            continue
        if in_code or not line.strip():
            out.append(line)
            continue
        if heading and SENTENCE.search(line):
            skip_level = len(heading.group(1))
            removed.append(line + "  (whole section)")
            continue
        if line.lstrip().startswith("|"):
            if SENTENCE.search(line) and not re.fullmatch(r"[|\s:-]+", line):
                removed.append(line)
                continue
            out.append(line)
            continue
        lead = re.match(r"\s*(?:[-*>]|\d+\.)\s+", line)
        prefix, body = (lead.group(0), line[lead.end():]) if lead else ("", line)
        sentences = re.split(r"(?<=[.!?:])\s+(?=[A-Z\"“\[`*(])", body)
        kept, dropping = [], False
        for s in sentences:
            if SENTENCE.search(s) or (dropping and DANGLING.match(s)):
                removed.append(s)
                dropping = True
            else:
                kept.append(s)
                dropping = False
        orphaned_label = len(kept) < len(sentences) and re.fullmatch(r"\**[\w\s]+:\**", " ".join(kept))
        if kept and not orphaned_label:
            out.append(prefix + " ".join(kept))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def cut_sections(text: str, headings: list[str]) -> str:
    for heading in headings:
        text = re.sub(rf"#+ {re.escape(heading)}\n.*?(?=\n#+ |\Z)", "", text, flags=re.S)
    return text


def cut_paragraphs(text: str, starts: list[str]) -> str:
    return "\n\n".join(p for p in text.split("\n\n") if not any(p.strip().startswith(s) for s in starts))


def read_turns():
    turns, current = [], None
    for line in LOG.read_text(errors="ignore").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (entry.get("message") or {}).get("content")
        texts = []
        if entry.get("type") == "user" and not entry.get("isMeta"):
            texts = [content] if isinstance(content, str) else [
                b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
        elif entry.get("type") == "attachment" and (entry.get("attachment") or {}).get("humanTurn"):
            texts = [entry["attachment"].get("prompt", "")]     # a message sent while Claude was mid-task
        for text in (re.sub(r"</?pasted_content[^>]*>", "", t).strip() for t in texts if isinstance(t, str)):
            if not text or text.startswith(("<system-reminder>", "[SYSTEM NOTIFICATION", "<command-name>",
                                             "<local-command", "Here is a note offered by a side agent")):
                continue
            if turns and turns[-1]["user"] == text:
                continue
            current = {"user": text, "when": entry.get("timestamp", "")[:10], "texts": [], "tools": 0}
            turns.append(current)
        if entry.get("type") == "assistant" and current is not None and isinstance(content, list):
            for block in content:
                if block.get("type") == "text" and block.get("text", "").strip():
                    current["texts"].append(block["text"].strip())
                elif block.get("type") == "tool_use":
                    current["tools"] += 1
    return turns


def match(table: dict, user: str):
    return next((value for start, value in table.items() if user.startswith(start)), None)


def reply_for(turn: dict, key_turn: dict) -> str:
    user = key_turn["user"]
    parts = []
    for text in key_turn["texts"]:
        text = cut_sections(text, match(CUT_SECTIONS, user) or [])
        text = cut_paragraphs(text, match(CUT_PARAGRAPHS, user) or [])
        starts = match(CUT_LINES, user) or []
        text = "\n".join(l for l in text.split("\n") if not any(l.strip().startswith(x) for x in starts))
        text = clean_reply(text)
        if text:
            parts.append(text)
    note = f"> *Claude used tools {key_turn['tools']} times here (commands, file edits, web lookups).*\n\n" if key_turn["tools"] else ""
    return note + "\n\n".join(parts)


def main():
    turns = read_turns()
    by_start = lambda start: next((t for t in turns if t["user"].startswith(start)), None)
    out, kept, pending = [HEADER], 0, []

    def flush():
        if pending:
            reasons = list(dict.fromkeys(pending))
            count = "One turn" if len(pending) == 1 else f"{len(pending)} turns"
            out.append(f"\n---\n\n*[{count} left out: {'; '.join(reasons)}.]*\n")
            pending.clear()

    for turn in turns:
        user = turn["user"]
        if "--turns" in sys.argv:
            status = "STOP" if user.startswith(STOP_AT) else "drop" if match(DROP, user) else "user-only" if match(USER_ONLY, user) else "keep"
            print(f"  {status:9} {user[:88].replace(chr(10), ' ')}")
        if user.startswith(STOP_AT):
            pending.append("preparing this record")
            break
        reason = match(DROP, user)
        if reason and not any(user.startswith(source) for source in REPLY_FROM.values()):
            pending.append(reason)
            continue
        if reason:      # dropped, but its reply is shown under another turn
            continue
        flush()
        kept += 1
        if user.startswith("alright i have a langchain take home"):
            user = user.split("\n")[0].strip() + "\n\n*[the take-home brief was pasted here; it is not reproduced]*"
        if match(USER_CUT, user):
            pattern, about = match(USER_CUT, user)
            user = f"*[the start of this message, about {about}, is left out]*\n\n" + re.sub(pattern, "", user, flags=re.S)
        out.append(f"\n---\n\n### Jonah · {turn['when']}\n\n{user}\n")
        if match(USER_ONLY, turn["user"]):
            out.append(f"\n*[Claude's reply, about {match(USER_ONLY, turn['user'])}, is left out.]*\n")
            continue
        source = by_start(match(REPLY_FROM, turn["user"])) if match(REPLY_FROM, turn["user"]) else turn
        reply = reply_for(turn, source)
        if reply.strip():
            out.append(f"\n### Claude\n\n{reply}\n")
    flush()
    text = redact("".join(out))
    OUT.write_text(text)
    print(f"{kept} turns kept of {len(turns)}; {len(text.split()):,} words; {len(removed)} sentences or rows removed from Claude's replies")
    if "--report" in sys.argv:
        for s in removed:
            print("  -", redact(s)[:170])


HEADER = """# Working session with Claude Code

This is an edited record of the Claude Code session in which the test world for this project was built:
the choice of problem, the refund policy and its rulings, the scenarios and the oracle, the hand-labeling
and what it found, and the plan for evaluating the agent.

What was changed from the raw session:

- Tool output, images and Claude's private reasoning are removed. Where Claude used tools, a line says
  how many times.
- Email addresses, personal file paths and references to my employer or unrelated projects are removed.
- The take-home brief is not reproduced.
- Turns about the presentation, recording the labeling sessions, and repository housekeeping are left
  out. Each omission is marked where it occurs.
- In Claude's replies, sentences about the interview or the presentation are removed, without a marker.

My own messages are as typed or dictated, apart from the marked omissions.
"""

if __name__ == "__main__":
    main()
