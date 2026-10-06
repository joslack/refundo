# Refundo

A refund agent for Quillstack, a fictional subscription software company, and the test world it is evaluated on.

## Layout

| Path | What it holds |
|---|---|
| `world/` | The test world: record schema, scenario builders, 80 scenarios, the oracle, the labeling UI and the hand labels |
| `agents/` | The agent implementations, one graph each, listed in `agents/langgraph.json`: `sql` reads the records with SQL, `structured` through a tool per kind of record, and `case_file` through one tool that returns the whole account. `pipeline` is a fixed graph with no agent loop: code reads the account, one model call decides, code submits. `shared.py` holds what they do the same way. They import nothing from `world/`; `tests/test_boundaries.py` enforces that |
| `evals/` | The Harbor dataset. `environment/` defines the one environment every task runs in: the MCP server with every agent's tools, the verifier and the reference solution. `build.py` writes a small task folder per scenario, `run.py` runs them, and `langsmith_plugin.py` records each run in LangSmith with the scenario's tier, area and reference answer. `limits.py` reads from a trial's files whether a rate limit, an overloaded provider or an empty account kept it from running, and whether anything but the agent and the model was inside its seconds. `pareto.py` and `latency.py` turn a sweep's job folders into the tables and charts in `results/<commit>/`, and `explore.py` into one page for reading each experiment's measures and every case that got a wrong answer |
| `docs/` | The refund policy and the scenario catalog |
| `deck/` | The presentation, as Markdown slides |
| `process/` | How the work was done: edited transcripts of the working sessions with Claude, and the labeling session transcripts |

## Commands

```
uv sync                                    # install
uv run pytest                              # scenarios, policy counterexamples, hand labels, the structured tools and case file against the oracle, and how rate limits and refusals are handled
uv run python -m world.labeling            # how the hand labels compare with the oracle
uv run streamlit run world/label_app.py    # labeling UI
uv run python evals/build.py               # build the shared images and the Harbor tasks in evals/tasks/
uv run python evals/run.py oracle          # the reference solution on every task; it should score 1.0 everywhere
uv run python evals/run.py sql             # the agent graph `sql` on the default model, recorded in LangSmith as an experiment
uv run python evals/run.py sql -m all --efforts all   # every model in run.py at each reasoning effort it accepts
uv run python evals/run.py sql,structured,case_file,pipeline -m deepseek:medium,luna:high,glm:high -k 3   # every graph, each model at one reasoning effort, each case three times, so a score does not rest on one run
uv run python evals/run.py resume <job> [<job> ...]   # run the trials a job lacks, into the same experiment: those it never reached and those a rate limit, a silent provider, an empty account or a stop kept from running
uv run python evals/run.py resume <commit>   # the same for every job of one run
uv run --with matplotlib python evals/pareto.py a3dd364   # that sweep's tables and charts, in evals/results/a3dd364/
uv run --env-file .env python evals/latency.py a3dd364 quillstack-refund-requests   # estimated seconds in model calls per case, from the LangSmith traces
uv run --with matplotlib --env-file .env python evals/explore.py <commit>   # a page for reading a run: each experiment's measures, the cases it got wrong, and what each trial proposed
```

The dataset is all 80 scenarios. In LangSmith each one is marked hand-labeled or unlabeled, so results can be read for either group.

## When a provider or LangSmith refuses

A run meets three kinds of limit: a model provider that refuses calls or stops answering, an account that runs out of credits, and LangSmith's monthly limits on traces. `evals/run.py` handles all three the same way: it stops, says why in its own output, and leaves the job so that `resume` can finish it.

- Before a run starts, each model is asked one question and LangSmith is asked whether it is taking traces and how much of the month's limits is left. A run that would not be recorded is not started. `--no-langsmith-check` starts it anyway.
- While a job runs, it is stopped when LangSmith refuses its records or the agent's traces, when the provider's account is out of credits, when its last six cases came to nothing, or when every running case has had a model call sent again and none has finished for five minutes (longer at the higher reasoning efforts). The line that says so starts `finish <job> (stopped: ...)`; lines marked `notice` report trouble that did not stop the job.
- A trial that such a failure kept from running is counted as not run, never as a wrong answer. `results.csv` counts these per experiment, `not_run.csv` lists them, the results page shows them, and in LangSmith the trial's run carries the error and a `not_run` score and no reward. `resume` moves their folders to `evals/jobs/_not_run/<job>/` and has Harbor run those cases again in the same job and experiment.
- A record LangSmith refuses is not sent again and again. It is written to `langsmith-unsent.jsonl` in the job's folder with every record after it, and `resume` sends that file before it runs anything. The agent's own trace cannot be kept this way, because the agent's container sends it; a trial that ran while LangSmith was refusing has its scores in LangSmith after `resume` and an incomplete trace.

None of this changes a measured time. It adds no wait and no retry inside a trial: a provider that fails is answered by stopping the job and running whole trials again later. The agent's model client still sends a failed call again by itself, as `agents/shared.py` sets it. A trial in which it did, or in which LangSmith's client was refused, keeps its answer and is left out of seconds per case, and the tables say how many trials that was. OpenAI's client sends a failed call again without writing it down, so jobs on OpenAI models are started with its request log on (`OPENAI_LOG=info` in the agent's container; `--no-request-log` leaves it off). `retries_logged` in `results.csv` says for each experiment whether a call sent again would have shown.

## What the answer key covers

- The oracle (`world/oracle.py`) implements every decision rule in `docs/policy.md`: §3, 4, 5, 6, 9, 10 and 11. §12, which governs how replies are written, is not evaluated yet.
- All 80 scenarios are checked against the oracle by tests. The scenarios and the oracle were written together, so that shows they are consistent, not that they are right.
- 41 of the 80 scenarios were also labeled by hand, 32 of them blind. The other 39 have no hand label.
- `tests/test_policy_invariants.py` holds cases written from the policy text alone, independent of the scenarios.
