"""Cost against reward for every experiment of one sweep, as tables and charts.

    uv run --with matplotlib python evals/pareto.py <commit>

Reads the Harbor job folders in evals/jobs/ whose names carry the given commit (the one evals/run.py put in
each job's name) and writes, to evals/results/<commit>/:
    results.csv      one row per experiment: scores, calls, seconds, tokens, cost per case
    cases.csv        one row per trial: what was proposed, how it scored, calls, seconds, tokens, cost
    misses.csv       one row per case and experiment with a wrong trial: what was expected and what was given
    pareto.png       reward against cost per case, one line per agent and model across its reasoning efforts
    pareto-top.png   the same, zoomed on the experiments that are cheap and at least 80% right
    index.png        reward against the Artificial Analysis Intelligence Index for the same model and effort

An experiment is one agent graph, model and reasoning effort. Reward is the share of its trials where the action
and the amount were both right. Where each case was run more than once (`run.py -k`), the reward is over all the
trials, and results.csv also says how far a single run's score would stray from it (reward_sd) and how many cases
were right every time, some of the time, and never. Cost per case is the experiment's tokens at the provider's
list price, divided by the number of trials. The charts show experiments that have every case, the same number of
times.

Seconds per case is the median time the agent took, from its first model call to its reply, as Harbor timed it.
It compares experiments that ran under the same load, and is left blank for a job whose model calls were paced,
because the waits are in it. If evals/latency.py has written latency.csv beside the tables, its estimate of the
seconds a case spent in model calls is added to results.csv.
"""

import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import median

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patheffects import withStroke  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402
from matplotlib.transforms import Bbox  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from world.labeling import LABELS, load_labels, ui_action  # noqa: E402
from world.oracle import label  # noqa: E402
from world.scenarios import ALL  # noqa: E402

JOBS = ROOT / "evals/jobs"
OUT = ROOT / "evals/results"
CASES = 80
HAND_LABELED = {k.lower() for k in load_labels(LABELS / "jonah.jsonl")}
EFFORTS = ["none", "low", "medium", "high", "xhigh", "max"]
HALO = [withStroke(linewidth=2.5, foreground="white")]  # keeps a label readable where it crosses a line
# USD per million tokens (input, cached input, cache write, output), read on 2026-10-05 from
# developers.openai.com/api/docs/pricing and docs.fireworks.ai/serverless/pricing. Reasoning tokens are billed as
# output. Only the two Luna models charge for writing to the cache, at 1.25 times the input price.
PRICES = {
    "gpt-6-luna": (0.10, 0.01, 0.125, 0.50), "gpt-5.6-luna": (0.20, 0.02, 0.25, 1.20),
    "gpt-5.4-mini": (0.75, 0.075, 0.75, 4.50), "gpt-5.4-nano": (0.20, 0.02, 0.20, 1.25),
    "deepseek-v4p1-flash": (0.30, 0.006, 0.30, 1.20), "glm-5p3-flash": (0.15, 0.03, 0.15, 0.50),
    "nemotron-lightning-3p5-30b-a3b": (0.05, 0.01, 0.05, 0.20), "gpt-oss-120b": (0.15, 0.015, 0.15, 0.60),
}
# What a model does when no effort is set. OpenAI's API reports it. The Fireworks models reason by default, and
# their API does not say at what effort.
DEFAULT_EFFORT = {"gpt-6-luna": "medium", "gpt-5.6-luna": "medium", "gpt-5.4-mini": "none", "gpt-5.4-nano": "none"}
# Artificial Analysis Intelligence Index v4.3.2, read on 2026-10-05 from artificialanalysis.ai/models/<model>,
# by our name for the effort.
AA_INDEX = {
    "gpt-6-luna": {"max": 38.12, "xhigh": 34.56, "high": 32.93, "medium": 29.93, "low": 21.53, "none": 18.47},
    "gpt-5.6-luna": {"max": 37.32, "xhigh": 34.56, "high": 32.12, "medium": 25.04, "low": 21.01, "none": 15.53},
    "gpt-5.4-mini": {"xhigh": 24.07, "medium": 19.75, "none": 11.14},
    "gpt-5.4-nano": {"xhigh": 20.72, "medium": 20.01, "none": 11.69},
    "deepseek-v4p1-flash": {"none": 24.67},
    "gpt-oss-120b": {"high": 11.6, "low": 10.21},
}
AA_ESTIMATED = {("gpt-5.4-mini", "medium"), ("gpt-5.4-mini", "none"), ("gpt-5.4-nano", "medium"),
                ("gpt-5.4-nano", "none"), ("gpt-oss-120b", "low")}  # scores Artificial Analysis marks as estimated
# Artificial Analysis lists these at one effort that Fireworks does not offer by that name. Each is paired with
# the nearest experiment here: the highest effort offered, or the only one.
AA_NEAREST = {("deepseek-v4p1-flash", "high"): 39.46, ("glm-5p3-flash", "high"): 41.81,
              ("nemotron-lightning-3p5-30b-a3b", "default"): 12.86}
# Fireworks' endpoint for this model returned nonsense for stretches of 2026-10-05, in the experiments below and
# in three more that were stopped, so its scores here understate it.
UNRELIABLE = {"nemotron-lightning-3p5-30b-a3b"}
NAMES = {"gpt-6-luna": "GPT-6 Luna", "gpt-5.6-luna": "GPT-5.6 Luna", "gpt-5.4-mini": "GPT-5.4 mini",
         "gpt-5.4-nano": "GPT-5.4 nano", "deepseek-v4p1-flash": "DeepSeek V4.1 Flash", "glm-5p3-flash": "GLM-5.3 Flash",
         "nemotron-lightning-3p5-30b-a3b": "Nemotron Lightning 3.5", "gpt-oss-120b": "gpt-oss-120b"}
COLORS = {"gpt-6-luna": "#111111", "gpt-5.6-luna": "#6b6b6b", "gpt-5.4-mini": "#c2410c", "gpt-5.4-nano": "#e0a100",
          "deepseek-v4p1-flash": "#3b3bd6", "glm-5p3-flash": "#2d7ff9", "nemotron-lightning-3p5-30b-a3b": "#3f9142",
          "gpt-oss-120b": "#a23b72"}


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


# A trial that ended this way says nothing about the agent: the provider refused the call because the account was
# out of credits, the grader was cut off, or the job was stopped. The case counts as not run.
NOT_A_RESULT = {"ApiUsageLimitError", "VerifierTimeoutError", "CancelledError"}


def trials_of(job: Path, model: str) -> list[dict]:
    """One row per trial in a job folder that produced a result."""
    price_in, price_read, price_write, price_out = PRICES[model]
    rows = []
    for path in sorted(job.glob("*/result.json")):
        trial = read_json(path)
        if not trial or (trial.get("exception_info") or {}).get("exception_type") in NOT_A_RESULT:
            continue
        scores = (trial.get("verifier_result") or {}).get("rewards") or {}
        used = trial.get("agent_result") or {}
        tokens_in, read, out = (used.get(k) or 0 for k in ("n_input_tokens", "n_cache_tokens", "n_output_tokens"))
        # Harbor does not record tokens written to the cache, so they come from the agent's own messages.
        messages = (read_json(path.parent / "agent/result.json") or {}).get("messages") or []
        write = sum(((m.get("usage_metadata") or {}).get("input_token_details") or {}).get("cache_creation") or 0
                    for m in messages if m.get("type") == "ai")
        submitted = read_json(path.parent / "artifacts/tmp/proposals.json") or []
        ran = trial.get("agent_execution") or {}
        timed = ran.get("started_at") and ran.get("finished_at")
        rows.append({
            "case": trial["task_name"].split("/")[-1], "trial": trial["trial_name"],
            "right": scores.get("reward") == 1.0, "action": scores.get("action") == 1.0,
            "amount": scores.get("amount") == 1.0, "sections": scores.get("sections") == 1.0,
            "proposal": submitted[-1] if submitted else None, "errored": bool(trial.get("exception_info")),
            "error": (trial.get("exception_info") or {}).get("exception_type", ""),
            "model_calls": sum(m.get("type") == "ai" for m in messages),
            "tool_calls": sum(m.get("type") == "tool" for m in messages),
            "agent_seconds": round((datetime.fromisoformat(ran["finished_at"])
                                    - datetime.fromisoformat(ran["started_at"])).total_seconds(), 1) if timed else "",
            "input_tokens": tokens_in, "cached_tokens": read, "cache_write_tokens": write, "output_tokens": out,
            "cost_usd": ((tokens_in - read - write) * price_in + read * price_read + write * price_write
                         + out * price_out) / 1e6,
        })
    return rows


def experiments(commit: str) -> list[dict]:
    """One row per agent graph, model and effort for this commit. Each row's "cases" holds its trials."""
    name = re.compile(rf"^(?P<graph>[a-z_]+)-(?P<model>.+)-(?P<effort>default|{'|'.join(EFFORTS)})-{re.escape(commit)}-\d{{4}}-\d{{6}}$")
    best: dict[tuple[str, str, str], dict] = {}
    for job in sorted(p for p in JOBS.iterdir() if p.is_dir()):
        named = name.match(job.name)
        if not named or named["model"] not in PRICES:
            continue
        graph, model, effort = named["graph"], named["model"], named["effort"]
        trials = trials_of(job, model)
        if not trials:
            continue

        def share(flags: list[bool]) -> float | None:
            return round(100 * sum(flags) / len(flags), 1) if flags else None

        # How often each case was right, over the times it was run.
        by_case: dict[str, list[bool]] = {}
        for t in trials:
            by_case.setdefault(t["case"], []).append(t["right"])
        times = {len(v) for v in by_case.values()}
        rates = [sum(v) / len(v) for v in by_case.values()]
        repeated = times != {1}
        settings = ((read_json(job / "config.json") or {}).get("agents") or [{}])[0].get("kwargs") or {}
        paced = bool((settings.get("configurable") or {}).get("calls_per_minute"))
        seconds = [t["agent_seconds"] for t in trials if t["agent_seconds"] != ""]
        row = {
            "graph": graph, "model": model, "effort": effort,
            "effective_effort": DEFAULT_EFFORT.get(model, "default") if effort == "default" else effort,
            "trials": len(trials), "repeats": min(times), "complete": len(by_case) == CASES and len(times) == 1,
            "reliable": model not in UNRELIABLE,
            "reward": share([t["right"] for t in trials]),
            # One run of the 80 cases would land this many points from the reward, give or take, by chance alone.
            "reward_sd": round(100 * sum(p * (1 - p) for p in rates) ** 0.5 / len(rates), 1) if repeated else "",
            "cases_always_right": sum(p == 1 for p in rates) if repeated else "",
            "cases_sometimes_right": sum(0 < p < 1 for p in rates) if repeated else "",
            "cases_never_right": sum(p == 0 for p in rates) if repeated else "",
            "reward_hand_labeled": share([t["right"] for t in trials if t["case"] in HAND_LABELED]),
            "reward_unlabeled": share([t["right"] for t in trials if t["case"] not in HAND_LABELED]),
            "action": share([t["action"] for t in trials]), "amount": share([t["amount"] for t in trials]),
            "sections": share([t["sections"] for t in trials]),
            "no_proposal": sum(t["proposal"] is None for t in trials), "errored": sum(t["errored"] for t in trials),
            "model_calls_per_case": round(sum(t["model_calls"] for t in trials) / len(trials), 1),
            "tool_calls_per_case": round(sum(t["tool_calls"] for t in trials) / len(trials), 1),
            "agent_seconds_per_case": round(median(seconds), 1) if seconds and not paced else "",
            "input_tokens": sum(t["input_tokens"] for t in trials), "cached_tokens": sum(t["cached_tokens"] for t in trials),
            "cache_write_tokens": sum(t["cache_write_tokens"] for t in trials),
            "output_tokens": sum(t["output_tokens"] for t in trials),
            "cost_per_case_usd": round(sum(t["cost_usd"] for t in trials) / len(trials), 6),
            "job": job.name, "cases": trials,
        }
        # A graph, model and effort run more than once (a restarted or test job) counts once: the job with the most trials.
        key = (graph, model, effort)
        if key not in best or row["trials"] > best[key]["trials"]:
            best[key] = row
    return list(best.values())


def frontier(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The points no other point beats on both cost and reward, from cheapest to dearest."""
    best, out = -1.0, []
    for cost, reward in sorted(points, key=lambda p: (p[0], -p[1])):
        if reward > best:
            out.append((cost, reward))
            best = reward
    return out


def dollars(x: float, _pos=None) -> str:
    return f"${x:.2f}" if x >= 0.1 else f"${x:.3f}".rstrip("0").rstrip(".") if x >= 0.001 else f"${x:.4f}"


def style(ax, title: str, subtitle: str) -> None:
    ax.set_title(title, loc="left", fontsize=14, fontweight="bold", pad=26)
    ax.text(0, 1.03, subtitle, transform=ax.transAxes, fontsize=9, color="#666666")
    ax.grid(True, color="#e6e6e6", linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#999999")


def name_models(ax, fig, anchors: dict[str, tuple[float, float]], points: list[tuple[float, float]], labels: list) -> None:
    """Write each model's name beside one of its points, at the first of a few positions that is clear of the
    points, the effort labels and the names already written."""
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    reach = 7 * fig.dpi / 72  # a marker's half-width in pixels, with a little room
    frame = ax.get_window_extent(renderer)
    placed = [label.get_window_extent(renderer) for label in labels]
    placed += [Bbox.from_extents(x - reach, y - reach, x + reach, y + reach) for x, y in ax.transData.transform(points)]
    spots = [((0, 13), "center", "bottom"), ((0, -13), "center", "top"), ((-12, 0), "right", "center"),
             ((0, 26), "center", "bottom"), ((0, -26), "center", "top"), ((-12, 14), "right", "bottom"),
             ((-12, -14), "right", "top"), ((0, 40), "center", "bottom"), ((0, -40), "center", "top")]
    for model, at in sorted(anchors.items(), key=lambda kv: -kv[1][1]):
        text = None
        for offset, ha, va in spots:
            if text is not None:
                text.remove()
            text = ax.annotate(NAMES[model], at, textcoords="offset points", xytext=offset, ha=ha, va=va, fontsize=8,
                               fontweight="bold", color=COLORS[model], zorder=6, path_effects=HALO)
            box = text.get_window_extent(renderer)
            if frame.contains(box.x0, box.y0) and frame.contains(box.x1, box.y1) and not any(box.overlaps(o) for o in placed):
                placed.append(box)
                break
        else:
            text.remove()  # no clear spot inside the plot: the legend names the model


def pareto_chart(rows: list[dict], path: Path, top: bool = False) -> None:
    """All experiments, or with top=True only the corner that matters for choosing: cheaper than the median
    experiment and at least 80% right."""
    rows = [r for r in rows if r["complete"] and r["cost_per_case_usd"] > 0]
    costs, rewards = [r["cost_per_case_usd"] for r in rows], [r["reward"] for r in rows]
    line = frontier([(r["cost_per_case_usd"], r["reward"]) for r in rows if r["reliable"]])
    left, right, floor = min(costs) * 0.7, max(costs) * 1.5, max(0, min(rewards) - 8)
    middle_cost, middle_reward = median(costs), median(rewards)
    if top:
        right, floor = middle_cost * 2.2, 78
    graphs = list(dict.fromkeys(r["graph"] for r in in_order(rows)))
    mark = dict(zip(graphs, "osD^v"))  # one marker shape per agent graph
    repeats = {r["repeats"] for r in rows}
    fig, ax = plt.subplots(figsize=(12, 6.8), dpi=160)
    style(ax, "Reward against cost per case" + (": the cheap, accurate corner" if top else ", by model and reasoning effort"),
          (f"Agent graph `{graphs[0]}`, " if len(graphs) == 1 else "") + f"{CASES} cases"
          + (f", each run {min(repeats)} times; bars show how far a single run would stray" if repeats != {1} else "")
          + ". Each point is one experiment; a line joins one model's reasoning efforts. Hollow point: no effort set.")
    ax.fill_between([left, middle_cost], middle_reward, 100, color="#dff5df", zorder=0,
                    label="Cheaper and better than the median experiment")
    anchors, points, labels = {}, [], []
    for model in PRICES:
        faded = 0.4 if model in UNRELIABLE else 1.0
        seen = []
        for graph in graphs:
            mine = [r for r in rows if r["model"] == model and r["graph"] == graph]
            # The line runs through the model's efforts in order. Where the provider says which effort is the
            # default, the run with no effort set stands in for that effort.
            swept = sorted((r for r in mine if r["effective_effort"] in EFFORTS),
                           key=lambda r: (EFFORTS.index(r["effective_effort"]), r["effort"] == "default"))
            swept = [r for i, r in enumerate(swept) if i == 0 or r["effective_effort"] != swept[i - 1]["effective_effort"]]
            ax.plot([r["cost_per_case_usd"] for r in swept], [r["reward"] for r in swept], "-", color=COLORS[model],
                    linewidth=1.6, zorder=3, alpha=faded)
            shown = [r for r in mine if left <= r["cost_per_case_usd"] <= right and r["reward"] >= floor]
            for r in shown:
                unset = r["effort"] == "default"
                if r["reward_sd"] != "":
                    ax.errorbar(r["cost_per_case_usd"], r["reward"], yerr=r["reward_sd"], color=COLORS[model], linewidth=1,
                                capsize=2.5, zorder=3, alpha=faded)
                ax.plot(r["cost_per_case_usd"], r["reward"], mark[graph], markersize=8 if unset else 5.5, color=COLORS[model],
                        markerfacecolor="white" if unset else COLORS[model], markeredgewidth=1.6, zorder=4, alpha=faded)
                points.append((r["cost_per_case_usd"], r["reward"]))
                labels.append(ax.annotate(
                    f"default ({r['effective_effort']})" if unset and r["effective_effort"] in EFFORTS else r["effort"],
                    (r["cost_per_case_usd"], r["reward"]), textcoords="offset points", xytext=(5, 5),
                    fontsize=7.5 if top else 6.5, color=COLORS[model], zorder=5, path_effects=HALO))
            seen += shown
        if seen:
            ax.plot([], [], "-", color=COLORS[model], linewidth=1.6, alpha=faded,
                    label=NAMES[model] + (" (provider unstable, understated)" if model in UNRELIABLE else ""))
            best = max(seen, key=lambda r: (r["reward"], -r["cost_per_case_usd"]))
            anchors[model] = (best["cost_per_case_usd"], best["reward"])
    if len(graphs) > 1:
        for graph in graphs:
            ax.plot([], [], mark[graph], color="#444444", markersize=5.5, linestyle="none", label=f"agent `{graph}`")
    ax.plot([c for c, _ in line], [r for _, r in line], ":", color="#111111", linewidth=1.4, label="Pareto line", zorder=2)
    ax.set_xscale("log")
    ax.xaxis.set_major_formatter(FuncFormatter(dollars))
    ax.xaxis.set_minor_formatter(FuncFormatter(lambda x, _pos: dollars(x) if f"{x:.0e}"[0] in ("1235" if top else "25") else ""))
    ax.tick_params(axis="x", which="minor", labelsize=7, colors="#777777")
    ax.set_xlim(left, right)
    ax.set_ylim(floor, 101.5 if top else 104)
    ax.set_xlabel("Cost per case (USD, log scale)")
    ax.set_ylabel("Cases with the right action and amount (%)")
    name_models(ax, fig, anchors, points, labels)
    fig.legend(*ax.get_legend_handles_labels(), loc="lower center", fontsize=8, frameon=False, ncol=5)
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    fig.savefig(path)
    plt.close(fig)


def ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def correlation(xs: list[float], ys: list[float]) -> float | None:
    """Spearman's rank correlation: how far the two orderings agree, from -1 to 1."""
    if len(xs) < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    top = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    bottom = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return round(top / bottom, 2) if bottom else None


def index_pairs(rows: list[dict]) -> list[dict]:
    """Our experiments beside the index score Artificial Analysis gives the same model and effort. The comparison
    is between models, so it takes one agent graph: the one with the most experiments."""
    graphs = [r["graph"] for r in rows]
    most = max(set(graphs), key=graphs.count)
    pairs = []
    for r in (r for r in rows if r["graph"] == most):
        if not r["complete"] or not r["reliable"]:
            continue
        index = AA_INDEX.get(r["model"], {}).get(r["effective_effort"])
        nearest = AA_NEAREST.get((r["model"], r["effort"]))
        if index is None and nearest is None:
            continue
        pairs.append({"model": r["model"], "effort": r["effective_effort"], "index": index or nearest,
                      "reward": r["reward"], "exact": index is not None,
                      "estimated": (r["model"], r["effective_effort"]) in AA_ESTIMATED})
    return pairs


def index_chart(rows: list[dict], path: Path) -> dict:
    """Draw the chart and return the rank correlations it reports."""
    pairs = index_pairs(rows)
    exact = [p for p in pairs if p["exact"]]
    found = {
        "pairs": len(pairs), "all": correlation([p["index"] for p in pairs], [p["reward"] for p in pairs]),
        "same_effort_pairs": len(exact),
        "same_effort": correlation([p["index"] for p in exact], [p["reward"] for p in exact]),
    }
    fig, ax = plt.subplots(figsize=(9.5, 6.2), dpi=160)
    style(ax, "Reward here against the Artificial Analysis Intelligence Index",
          f"{len(pairs)} experiments. Rank correlation {found['all']}; {found['same_effort']} over the {len(exact)} "
          "listed at the same effort. Hollow: estimated index. Square: nearest effort.")
    for p in pairs:
        ax.plot(p["index"], p["reward"], "o" if p["exact"] else "s", markersize=7, color=COLORS[p["model"]],
                markerfacecolor="white" if p["estimated"] else COLORS[p["model"]], markeredgewidth=1.5)
        ax.annotate(p["effort"], (p["index"], p["reward"]), textcoords="offset points", xytext=(5, 4), fontsize=7,
                    color=COLORS[p["model"]])
    for model in PRICES:
        if any(p["model"] == model for p in pairs):
            ax.plot([], [], "o", color=COLORS[model], label=NAMES[model])
    ax.set_xlabel("Artificial Analysis Intelligence Index (v4.3.2)")
    ax.set_ylabel("Cases with the right action and amount (%)")
    ax.legend(loc="lower right", fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return found


def in_order(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (r["graph"], list(PRICES).index(r["model"]), r["effort"] != "default",
                                       EFFORTS.index(r["effective_effort"]) if r["effective_effort"] in EFFORTS else 0))


def misses(rows: list[dict]) -> list[dict]:
    """One row per case and experiment where a trial was wrong, with the oracle's answer and what was given."""
    expected = {}
    for scenario in ALL:
        oracle = label(scenario.world, scenario.request)
        owed = oracle.proposed.amount_cents if oracle.proposed else oracle.amount_cents
        expected[scenario.id.lower()] = (ui_action(oracle.action.value), owed or 0)
    out = []
    for r in in_order(rows):
        by_case: dict[str, list[dict]] = {}
        for t in r["cases"]:
            by_case.setdefault(t["case"], []).append(t)
        for case, trials in sorted(by_case.items()):
            wrong = [t["proposal"] for t in trials if not t["right"]]
            if not wrong:
                continue
            given = Counter(f"{p['action']} {p['amount_cents'] or 0}" if p else "nothing" for p in wrong)
            out.append({"case": case, "graph": r["graph"], "model": r["model"], "effort": r["effort"],
                        "right": len(trials) - len(wrong), "trials": len(trials),
                        "expected_action": expected[case][0], "expected_amount_cents": expected[case][1],
                        "given": "; ".join(f"{answer} ({n})" for answer, n in given.most_common())})
    return sorted(out, key=lambda m: m["case"])


def write_csv(rows: list[dict], folder: Path) -> None:
    """results.csv, cases.csv and misses.csv. latency.csv, where evals/latency.py has written it, adds a column."""
    latency = {}
    if (folder / "latency.csv").exists():
        with (folder / "latency.csv").open() as f:
            latency = {r["job"]: r["model_seconds_per_case_estimated"] for r in csv.DictReader(f)}
    for r in rows:
        r["model_seconds_per_case_estimated"] = latency.get(r["job"], "")
    with (folder / "results.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[k for k in rows[0] if k != "cases"], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(in_order(rows))
    columns = ["graph", "model", "effort", "case", "trial", "right", "action", "amount", "sections", "proposed_action",
               "proposed_amount_cents", "error", "model_calls", "tool_calls", "agent_seconds", "input_tokens", "cached_tokens",
               "cache_write_tokens", "output_tokens", "cost_usd"]
    with (folder / "cases.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for r in in_order(rows):
            for t in sorted(r["cases"], key=lambda t: (t["case"], t["trial"])):
                proposed = t["proposal"] or {}
                writer.writerow(t | {"graph": r["graph"], "model": r["model"], "effort": r["effort"],
                                     "cost_usd": round(t["cost_usd"], 6),
                                     "proposed_action": proposed.get("action", ""),
                                     "proposed_amount_cents": proposed.get("amount_cents", "")})
    missed = misses(rows)
    with (folder / "misses.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["case", "graph", "model", "effort", "right", "trials", "expected_action",
                                               "expected_amount_cents", "given"])
        writer.writeheader()
        writer.writerows(missed)


if __name__ == "__main__":
    rows = experiments(sys.argv[1])
    out = OUT / sys.argv[1]
    out.mkdir(parents=True, exist_ok=True)
    write_csv(rows, out)
    found = "no experiment has every case yet, so no charts"
    if any(r["complete"] for r in rows):
        pareto_chart(rows, out / "pareto.png")
        pareto_chart(rows, out / "pareto-top.png", top=True)
        found = index_chart(rows, out / "index.png")
    for r in sorted(rows, key=lambda r: (-r["reward"], r["cost_per_case_usd"])):
        spread = "" if r["reward_sd"] == "" else f" (sd {r['reward_sd']})"
        print(f"{r['graph']:11} {NAMES[r['model']]:24} {r['effort']:8} reward {r['reward']:5.1f}%{spread}"
              f"  cost/case ${r['cost_per_case_usd']:.4f}  tool calls {r['tool_calls_per_case']:4}  seconds {r['agent_seconds_per_case']:>5}"
              f"  trials {r['trials']:3}{'' if r['complete'] else ' (not every case)'}"
              f"  no proposal {r['no_proposal']:2}  errored {r['errored']:2}")
    print(f"{len(rows)} experiments; rank correlation with the Artificial Analysis index: {found}")
