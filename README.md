# Refundo

A refund agent for Quillstack, a fictional subscription software company, and the test world it is evaluated on.

## Layout

| Path | What it holds |
|---|---|
| `world/` | The test world: record schema, scenario builders, 79 scenarios, the oracle, the labeling UI, hand labels and session transcripts |
| `agent/` | The agent (not built yet). It imports nothing from `world/`; `tests/test_boundaries.py` enforces that |
| `mcp_server/` | The tools the agent uses to read records (not built yet) |
| `evals/` | Harbor tasks and the verifier (not built yet) |
| `docs/` | The refund policy and the scenario catalog |
| `deck/` | The presentation, as Markdown slides |

## Commands

```
uv sync                                    # install
uv run pytest                              # scenarios against the oracle, plus regression tests
uv run streamlit run world/label_app.py    # labeling UI
```
