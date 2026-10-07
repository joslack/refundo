# Refundo

A refund agent for Quillstack, a fictional subscription software company, and the test world it is evaluated on.

## Layout

| Path | What it holds |
|---|---|
| `world/` | The test world: record schema, scenario builders, 80 scenarios, the oracle, the labeling UI and the hand labels |
| `agents/` | The agent implementations, one graph each, listed in `agents/langgraph.json`: `sql` reads the records with SQL, `structured` through a tool per kind of record, and `case_file` through one tool that returns the whole account. `pipeline` is a fixed graph with no agent loop: code reads the account, one model call decides, code submits. `shared.py` holds what they do the same way. They import nothing from `world/`; `tests/test_boundaries.py` enforces that |
| `evals/` | The Harbor dataset. `environment/` defines the one environment every task runs in: the MCP server with every agent's tools, the verifier and the reference solution. `build.py` writes a small task folder per scenario, `run.py` runs them, and `langsmith_plugin.py` records each run in LangSmith with the scenario's tier, area and reference answer. `pareto.py` and `latency.py` turn a sweep's job folders into the tables and charts in `results/<commit>/`, and `explore.py` into one page for reading each experiment's measures and every case that got a wrong answer |
| `docs/` | The refund policy and the scenario catalog |
| `deck/` | The presentation, as Markdown slides |
| `process/` | How the work was done: edited transcripts of the working sessions with Claude, and the labeling session transcripts |

## Commands

```
uv sync                                    # install
uv run pytest                              # scenarios, policy counterexamples, hand labels, and the structured tools and case file against the oracle
uv run python -m world.labeling            # how the hand labels compare with the oracle
uv run streamlit run world/label_app.py    # labeling UI
uv run python evals/build.py               # build the shared images and the Harbor tasks in evals/tasks/
uv run python evals/run.py oracle          # the reference solution on every task; it should score 1.0 everywhere
uv run python evals/run.py sql             # the agent graph `sql` on the default model, recorded in LangSmith as an experiment
uv run python evals/run.py sql -m all --efforts all   # every model in run.py at each reasoning effort it accepts
uv run python evals/run.py sql,structured,case_file,pipeline -m deepseek:medium,luna:high,glm:high -k 3   # every graph, each model at one reasoning effort, each case three times, so a score does not rest on one run
uv run --with matplotlib python evals/pareto.py a3dd364   # that sweep's tables and charts, in evals/results/a3dd364/
uv run --env-file .env python evals/latency.py a3dd364 quillstack-refund-requests   # estimated seconds in model calls per case, from the LangSmith traces
uv run --with matplotlib --env-file .env python evals/explore.py <commit>   # a page for reading a run: each experiment's measures, the cases it got wrong, and what each trial proposed
```

The dataset is all 80 scenarios. In LangSmith each one is marked hand-labeled or unlabeled, so results can be read for either group.

## What the answer key covers

- The oracle (`world/oracle.py`) implements every decision rule in `docs/policy.md`: §3, 4, 5, 6, 9, 10 and 11. §12, which governs how replies are written, is not evaluated yet.
- All 80 scenarios are checked against the oracle by tests. The scenarios and the oracle were written together, so that shows they are consistent, not that they are right.
- 41 of the 80 scenarios were also labeled by hand, 32 of them blind. The other 39 have no hand label.
- `tests/test_policy_invariants.py` holds cases written from the policy text alone, independent of the scenarios.
