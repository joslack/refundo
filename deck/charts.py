"""Draw the deck's two charts from the evaluation's job folders.

    uv run --with matplotlib python deck/charts.py <folder that holds the job folders>

    images/model-sweep.png        the SQL agent on eight models at every reasoning effort (commit a3dd364)
    images/agent-comparison.png   the twelve experiments of the comparison (commit 6e71349) and the eight on Claude
                                  Haiku 5.5

evals/pareto.py draws one commit's experiments for the results folder. This uses its chart with three changes for a
slide: a wider figure, the slide's ground colour, and no title, because the slide has one. The Haiku runs were made at
three later commits, which changed only how an Anthropic model is called, so the tasks, the agents and the answer key
are the same and the twenty experiments belong on one chart. A job that was stopped is left out.
"""

import sys
from pathlib import Path

from matplotlib.patheffects import withStroke

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "evals"))
import pareto  # noqa: E402

GROUND = "#F7F6F2"  # the slides' background
SIZE = (14.5, 6.3)  # inches; a slide's chart is about 1,100 by 480 pixels

pareto.JOBS = Path(sys.argv[1])
pareto.HALO = [withStroke(linewidth=2.5, foreground=GROUND)]
pareto.plt.rcParams.update({"figure.facecolor": GROUND, "axes.facecolor": GROUND, "savefig.facecolor": GROUND})
subplots = pareto.plt.subplots
pareto.plt.subplots = lambda *args, **kwargs: subplots(*args, **(kwargs | {"figsize": SIZE}))
grid = pareto.style


def no_title(ax, title, subtitle):
    grid(ax, "", "")


pareto.style = no_title

sweep = pareto.experiments("a3dd364")
comparison = pareto.experiments("6e71349")
for commit in ("83b1efc", "4663e54", "2327600"):
    comparison += [r for r in pareto.experiments(commit) if "stopped" not in r["job"]]
for rows, name in ((sweep, "model-sweep"), (comparison, "agent-comparison")):
    pareto.pareto_chart(rows, HERE / f"images/{name}.png")
    print(f"{len(rows)} experiments drawn in images/{name}.png")
