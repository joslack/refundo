# How this was built

Records of the work behind this repo, for anyone who wants to see how it was done.

| File | What it is |
|---|---|
| `earlier-chat.md` | The conversation in the Claude app in which the problem was chosen |
| `claude-session.md` | The Claude Code session in which the policy, scenarios, oracle and labels were built |
| `labeling-sessions/` | Verbatim transcripts of the four recorded hand-labeling sessions, transcribed locally |
| `export_session.py` | The script that produced `claude-session.md`, so the edits can be inspected |

The two conversation records are edited. Each one says at the top what was removed: tool output,
personal identifiers, and the parts about presenting the work rather than doing it. The labeling
transcripts are not edited.
