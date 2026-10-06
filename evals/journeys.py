"""A page for reading the agents' tool calls: the order each trial made them in, as a tree and as a heatmap.

    uv run --with matplotlib python evals/journeys.py <commit>
    uv run --with matplotlib python evals/journeys.py <commit> --jobs <dir> -o page.html --data journeys.json
    uv run --with matplotlib python evals/journeys.py <commit> --bare -o page.html
    uv run python evals/journeys.py <commit> --measure

Reads the same job folders as evals/pareto.py, from evals/jobs/ or from --jobs, and writes journeys-<commit>.html
beside them, one file with its data inside. The page has:

    Experiments      one row per experiment: steps and calls per trial, and the share of its trials that read
                     each table at least once
    Journey tree     for a chosen agent, model and effort, trials as paths through their steps, merged while
                     they share a prefix, with the number of trials on each branch and how many ended right
    Step heatmap     how many of those trials called each tool, and read each table, at each step
    Trials           the trials behind a chosen branch or cell, each with its calls in order and the SQL it ran

A step is one message of the model. The calls a model makes in one message run in parallel, so the tree reads a
step as the set of its calls: their order inside the message is dropped and a call repeated in it counts once.
A call the graph's code makes is a step of its own.

A `run_sql` call is named by what its query reads: the tables after FROM and JOIN, in subqueries and WITH
clauses too, as in `run_sql: invoices + invoice_lines`. A query of information_schema or pg_catalog is
`run_sql: schema`, one with no table `run_sql: no table`, text that does not begin as a statement
`run_sql: not a query`, and a name that is not a table is written with a question mark. A call whose result is
an error ends in `(failed)`. The other tools read fixed tables, taken from the queries in
evals/environment/mcp/server.py, so every agent's reads can be counted in the same terms.

The words on the page follow Simplified Technical English: short sentences in the active voice, and one term for
one thing (trial, case, experiment, agent, model, tool call, step, branch).

It also writes journeys-<commit>.json beside the page, or to --data, for another page to load:

    {"commit": "6e71349",
     "trials": {"be-16__AbC123x": {"case": "be-16", "graph": "sql", "model": "gpt-6-luna", "effort": "high", "right": true,
                                   "steps": [["get_request_context"], ["list_tables"],
                                             ["run_sql: invoices + invoice_lines", "run_sql: members"], ["submit_proposal"]]}}}

A trial is under its folder name, which a LangSmith run carries as `trial_name`. Each step lists the labels of
its calls as the page shows them, one per call, sorted.

The page reports what happened and draws no conclusion. --measure prints how many of the commit's queries the
parser could name, and needs neither matplotlib nor evals/pareto.py. --bare writes the page without the outer
document, the form a claude.ai artifact takes.
"""

import ast
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from world.schema import World  # noqa: E402
from world.sql import tables as record_tables  # noqa: E402

# Every table in a scenario's database, in the order the schema declares them. `proposals` is the one
# list_tables hides; a query can still name it.
TABLES = [*record_tables(World()), "request_context", "proposals"]
SERVER = HERE / "environment/mcp/server.py"
# What a fixed graph calls in code, in order. Its tool messages follow no request from the model.
CODE_CALLS = ["get_case_file", "submit_proposal"]
DECISION = "decision"  # the name agents/pipeline.py gives the model's answer; some providers carry it as a tool call
SIZE_LIMIT = 5_000_000  # bytes of page; past it the SQL text is cut shorter
SQL_CUTS = [None, 2000, 1000, 600, 400, 300, 200, 120]  # characters of each query to keep, tried in order

DOCUMENT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{margin:0}}[hidden]{{display:none!important}}</style></head><body>
{page}
</body></html>
"""


# --- What a query reads ------------------------------------------------------

TOKEN = re.compile(r"""
      (?P<skip>   \s+ | --[^\n]* | /\*.*?(?:\*/|\Z) )
    | (?P<value>  [eE]'(?:[^'\\]|\\.|'')*(?:'|\Z) | '(?:[^']|'')*(?:'|\Z)
                | \$(?P<tag>(?:[A-Za-z_]\w*)?)\$.*?(?:\$(?P=tag)\$|\Z) | \d+(?:\.\d+)?(?:[eE][+-]?\d+)? )
    | (?P<name>   "(?:[^"]|"")*(?:"|\Z) )
    | (?P<word>   [A-Za-z_][\w$]* )
    | (?P<mark>   . )
""", re.S | re.X)
STARTS_QUERY = {("word", "select"), ("word", "with"), ("word", "values")}
ENDS_FROM = {"where", "group", "order", "having", "limit", "offset", "union", "intersect", "except", "window", "fetch",
             "returning", "select", "for"}
STATEMENTS = {"select", "with", "values", "table", "explain", "show"}


def tokens(query: str) -> list[tuple[str, str]]:
    """A query as (kind, text) pairs: words in lower case, quoted names, marks, and one empty `value` for each
    string or number. Comments are dropped, so nothing inside a string or a comment can read as a table."""
    out = []
    for found in TOKEN.finditer(query):
        kind = found.lastgroup
        if kind == "name":
            out.append(("name", found.group().strip('"').replace('""', '"').lower()))
        elif kind != "skip":
            out.append((kind, found.group().lower() if kind in ("word", "mark") else ""))
    return out


def relations(query: str) -> list[str]:
    """The names a query reads from: whatever follows FROM or JOIN, in subqueries and WITH bodies too, each once,
    in the order they first appear. A function in that place, such as jsonb_each(data), is not a name, and a name
    a WITH clause defines is left out where the query uses it."""
    toks = tokens(query)

    def at(i: int) -> tuple[str, str]:
        return toks[i] if 0 <= i < len(toks) else ("", "")

    closes, opened = {}, []
    for i, tok in enumerate(toks):
        if tok == ("mark", "("):
            opened.append(i)
        elif tok == ("mark", ")") and opened:
            closes[opened.pop()] = i
    opens = {close: start for start, close in closes.items()}

    # WITH names: `name AS (SELECT` or `name (columns) AS (SELECT`, each with the brackets that hold its body.
    defined = []
    for i, tok in enumerate(toks):
        if tok != ("word", "as"):
            continue
        body = i + 1
        while at(body) in {("word", "not"), ("word", "materialized")}:
            body += 1
        if at(body) != ("mark", "(") or at(body + 1) not in STARTS_QUERY:
            continue
        before = opens.get(i - 1, i) - 1 if at(i - 1) == ("mark", ")") else i - 1
        if at(before)[0] in ("word", "name"):
            defined.append((at(before)[1], body, closes.get(body, len(toks))))
    recursive = ("word", "recursive") in toks

    def from_with(name: str, i: int) -> bool:
        """Whether a name used at this token is one a WITH clause defined. Inside its own body a name is still
        the table it shadows, unless the clause is recursive."""
        return any(name == n and (i > close or (recursive and start < i < close)) for n, start, close in defined)

    found = []
    # One entry per open bracket, innermost last: [is it a query or a join, is a FROM clause open, is a name due]
    levels = [[True, False, False]]
    i = 0
    while i < len(toks):
        kind, value = toks[i]
        level = levels[-1]
        if (kind, value) == ("mark", "("):
            # A bracket where a name is due holds either a subquery or a join of names: FROM (a JOIN b ON ...).
            query_inside = at(i + 1) in STARTS_QUERY
            levels.append([True, True, True] if level[2] and not query_inside else [query_inside, False, False])
            level[2] = False
        elif (kind, value) == ("mark", ")"):
            if len(levels) > 1:
                levels.pop()
        elif (kind, value) == ("mark", ";"):
            level[1] = level[2] = False
        elif (kind, value) == ("mark", ","):
            level[2] = level[1]
        elif kind == "word" and value == "from":
            # EXTRACT(EPOCH FROM x) and SUBSTRING(x FROM 2) sit in a function's brackets, not at a query's level.
            if level[0] and at(i - 1) != ("word", "distinct"):
                level[1] = level[2] = True
        elif kind == "word" and value == "join":
            level[1] = level[2] = level[0]
        elif kind == "word" and value in ENDS_FROM:
            level[1] = level[2] = False
        elif level[2] and kind in ("word", "name") and (kind, value) not in {("word", "lateral"), ("word", "only")}:
            parts = [value]
            while at(i + 1) == ("mark", ".") and at(i + 2)[0] in ("word", "name"):
                parts.append(at(i + 2)[1])
                i += 2
            level[2] = False
            name = ".".join(parts)
            if at(i + 1) != ("mark", "(") and not from_with(name, i) and name not in found:
                found.append(name)
        i += 1
    return found


def reads(query: str | None) -> dict:
    """What a query reads: the tables of the database, whether it looks the schema up, and any name that is
    neither. `kind` is `tables`, `schema`, `other` (only names that are not tables), `none` (a statement that
    reads no table, such as SELECT 1) or `unparsed` (text that does not begin as a statement)."""
    names = relations(query or "")
    known, other, schema = [], [], False
    for name in names:
        *qualifier, last = name.split(".")
        if qualifier[-1:] in (["information_schema"], ["pg_catalog"]) or (not qualifier and last.startswith("pg_")):
            schema = True
        elif last in TABLES and qualifier in ([], ["public"]):
            known.append(last)
        else:
            other.append(name)
    first = next((value for kind, value in tokens(query or "") if (kind, value) != ("mark", "(")), "")
    kind = ("tables" if known else "schema" if schema else "other" if other
            else "none" if first in STATEMENTS else "unparsed")
    return {"kind": kind, "tables": sorted(set(known), key=TABLES.index), "schema": schema, "other": other}


# --- A trial's calls ----------------------------------------------------------

FAILED = re.compile(r"SQL error:|\d+ validation errors? for call\[|Tool call \w+ with id \S+ could not be executed"
                    r"|Error\b|since and until must be timestamps")
NOT_EXECUTED = re.compile(r"Tool call (\w+) with id \S+ could not be executed")


def text(content) -> str:
    """A message's content as text, whichever way the model's API or the MCP adapter shapes it."""
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content or ""


def tool_reads(source: str) -> dict[str, list[str]]:
    """The tables each tool of the MCP server reads, from the server's own code: the queries written in the
    tool's function and in the functions it calls. `schema` stands for information_schema."""
    functions = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}
    direct, calls = {}, {}
    for name, node in functions.items():
        strings = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        found = [reads(s) for s in strings if re.match(r"\s*(SELECT|WITH)\b", s)]
        direct[name] = {t for r in found for t in r["tables"]} | {"schema" for r in found if r["schema"]}
        calls[name] = {n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)} & set(functions)

    def all_of(name: str, seen: frozenset = frozenset()) -> set[str]:
        return direct[name].union(*(all_of(c, seen | {name}) for c in calls[name] - seen - {name}))

    tools = [name for name, node in functions.items()
             if any(isinstance(d, ast.Attribute) and d.attr == "tool" for d in node.decorator_list)]
    return {name: sorted(all_of(name), key=[*TABLES, "schema"].index) for name in tools}


def call(tool: str, args: dict | None, result: str | None, covered: dict[str, list[str]], by_code: bool = False) -> dict:
    """One tool call as the page shows it: its label, the tables it read and whether it failed. `covered` is
    tool_reads() of the server. A failed call read nothing."""
    failed = bool(result is not None and FAILED.match(result))
    out = {"tool": tool, "label": tool, "kind": None, "tables": [t for t in covered.get(tool, []) if t != "schema"],
           "schema": "schema" in covered.get(tool, []), "failed": failed, "by_code": by_code, "sql": None}
    if tool == "run_sql":
        query = (args or {}).get("query")
        out["sql"] = query if isinstance(query, str) and query.strip() else None
        read = reads(out["sql"]) if out["sql"] else {"kind": "no query", "tables": [], "schema": False, "other": []}
        named = " + ".join(read["tables"] + [name + "?" for name in read["other"]])
        what = named or {"schema": "schema", "none": "no table", "unparsed": "not a query"}.get(read["kind"], read["kind"])
        out |= {"label": f"run_sql: {what}", "kind": read["kind"], "tables": read["tables"], "schema": read["schema"]}
    if failed:
        out |= {"label": out["label"] + " (failed)", "tables": [], "schema": False}
    return out


def journey(messages: list[dict], covered: dict[str, list[str]]) -> list[list[dict]]:
    """A trial's steps in order, each the calls made together. A tool message carries no tool name, so results
    are matched to the model's calls by position. A tool message no call asked for was made by the graph's
    code, one step each. A call the provider could not decode has a result and no request; it joins the step
    it came with."""
    steps, by_code, i = [], iter(CODE_CALLS), 0
    while i < len(messages):
        kind = messages[i].get("type")
        if kind not in ("ai", "tool"):
            i += 1
            continue
        asked = []
        if kind == "ai":
            asked = [c for c in messages[i].get("tool_calls") or [] if c.get("name") != DECISION]
            i += 1
        results = []
        while i < len(messages) and messages[i].get("type") == "tool":
            results.append(text(messages[i].get("content")))
            i += 1
        if not asked:
            steps += [[call(next(by_code, "unknown"), None, r, covered, by_code=True)] for r in results]
            continue
        undecoded = [r for r in results if NOT_EXECUTED.match(r)] if len(results) > len(asked) else []
        answers = [r for r in results if not NOT_EXECUTED.match(r)] if undecoded else results
        steps.append([call(NOT_EXECUTED.match(r)[1], None, r, covered) for r in undecoded]
                     + [call(c["name"], c.get("args"), answers[k] if k < len(answers) else None, covered)
                        for k, c in enumerate(asked)])
    return steps


# --- The tree -----------------------------------------------------------------


def step_key(labels: list) -> tuple:
    """A step as the tree compares it: the set of its calls, in a fixed order."""
    return tuple(sorted(set(labels)))


def tree(paths: list[list[tuple]], right: list[bool]) -> dict:
    """Trials merged while their steps agree. Each node holds the step that leads to it (`step`, None at the
    root), how many trials pass through it (`n`), how many of those ended right (`right`), the trials that made
    no further call (`ends`, as positions in `paths`), and the steps that followed (`next`, the most travelled
    first)."""

    def node(step) -> dict:
        return {"step": step, "n": 0, "right": 0, "ends": [], "next": {}}

    root = node(None)
    for index, (path, ok) in enumerate(zip(paths, right, strict=True)):
        passed = [root]
        for step in path:
            passed.append(passed[-1]["next"].setdefault(step, node(step)))
        for at in passed:
            at["n"] += 1
            at["right"] += bool(ok)
        passed[-1]["ends"].append(index)

    def ordered(at: dict) -> dict:
        return at | {"next": [ordered(n) for n in sorted(at["next"].values(), key=lambda n: (-n["n"], n["step"]))]}

    return ordered(root)


# --- The page -----------------------------------------------------------------


def read(commit: str, jobs: Path) -> dict:
    """The commit's experiments as evals/pareto.py picks them, in the page's order, each trial with its steps, and
    the names the page writes them under."""
    sys.path.insert(0, str(HERE))
    import pareto  # here and not at the top: it loads matplotlib, which the tests run without

    from world.scenarios import ALL

    pareto.JOBS = jobs
    rows = pareto.experiments(commit)
    listed = list(json.loads((ROOT / "agents/langgraph.json").read_text())["graphs"])
    graphs = [g for g in listed if any(r["graph"] == g for r in rows)] + sorted({r["graph"] for r in rows} - set(listed))
    models = [m for m in pareto.PRICES if any(r["model"] == m for r in rows)]
    efforts = ["default", *pareto.EFFORTS]
    rows.sort(key=lambda r: (graphs.index(r["graph"]), models.index(r["model"]), efforts.index(r["effort"])))
    covered = tool_reads(SERVER.read_text())

    def steps(job: str, trial: str) -> list[list[dict]]:
        return journey((pareto.read_json(jobs / job / trial / "agent/result.json") or {}).get("messages") or [], covered)

    return {
        "commit": commit, "generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "cases_count": len(ALL),
        "repeats": max((r["repeats"] for r in rows), default=1), "graphs": graphs, "absent": [g for g in listed if g not in graphs],
        "models": models, "model_names": {m: pareto.NAMES[m] for m in models}, "efforts": efforts, "covered": covered,
        "cases": {s.id.lower(): {"id": s.id, "area": s.archetype, "tier": s.difficulty, "intent": s.intent} for s in ALL},
        "experiments": [{"graph": r["graph"], "model": r["model"], "effort": r["effort"], "complete": r["complete"],
                         "trials": [{"case": t["case"], "trial": t["trial"], "right": t["right"], "error": t["error"],
                                     "steps": steps(r["job"], t["trial"])} for t in r["cases"]]} for r in rows],
    }


def plain(run: dict) -> dict:
    """The journeys as a data file another page can load: each trial under its folder name, which is the
    trial_name a LangSmith run carries, with the labels of its steps in order. The labels are the ones this page
    shows. A step lists one label per call, sorted, so a label repeats where a message made two such calls; the
    tree on the page reads the same step as a set."""
    return {"commit": run["commit"], "trials": {
        t["trial"]: {"case": t["case"], "graph": e["graph"], "model": e["model"], "effort": e["effort"], "right": bool(t["right"]),
                     "steps": [sorted(c["label"] for c in step) for step in t["steps"]]}
        for e in run["experiments"] for t in e["trials"]}}


def packed(run: dict) -> tuple[dict, list[str]]:
    """Everything the page shows but the SQL text, and the SQL text. To keep the page small, each label and each
    query is listed once and referred to by position, a call is a label or [label, query], a trial is
    [case, right, error, steps, folder name], and a tree node is [step, trials, right, trials that end here, next]."""
    tools = [*(t for t in run["covered"] if t != "submit_proposal"), "submit_proposal"]

    # A label is what a call is named in full. In the tree that shows run_sql as one tool, it is the tool's name.
    labels = {}
    for c in (c for e in run["experiments"] for t in e["trials"] for step in t["steps"] for c in step):
        labels.setdefault((c["tool"], c["by_code"]), {"t": c["tool"], "tool": c["tool"], "reads": [], "schema": False,
                                                      "failed": False, "code": c["by_code"]})
        labels[c["label"], c["by_code"]] = {"t": c["label"], "tool": c["tool"], "reads": [TABLES.index(t) for t in c["tables"]],
                                            "schema": c["schema"], "failed": c["failed"], "code": c["by_code"]}
    order = sorted(labels, key=lambda k: (tools.index(labels[k]["tool"]) if labels[k]["tool"] in tools else len(tools),
                                          labels[k]["t"] != labels[k]["tool"], labels[k]["failed"], labels[k]["reads"], *k))
    position = {key: i for i, key in enumerate(order)}
    queries: dict[str, int] = {}

    def short(c: dict) -> int | list[int]:
        label = position[c["label"], c["by_code"]]
        if c["tool"] != "run_sql":
            return label
        return [label, queries.setdefault(" ".join(c["sql"].split()), len(queries)) if c["sql"] else -1]

    def flat(node: dict) -> list:
        return [list(node["step"] or ()), node["n"], node["right"], node["ends"], [flat(n) for n in node["next"]]]

    def grown(trials: list[dict], name: str) -> list:
        return flat(tree([[step_key([position[c[name], c["by_code"]] for c in step]) for step in t["steps"]] for t in trials],
                         [t["right"] for t in trials]))

    experiments = [{
        "graph": e["graph"], "model": e["model"], "effort": e["effort"], "complete": e["complete"],
        "trials": [[t["case"], int(t["right"]), t["error"], [[short(c) for c in step] for step in t["steps"]], t["trial"]]
                   for t in e["trials"]],
        "trees": {"tool": grown(e["trials"], "tool"), "query": grown(e["trials"], "label")},
    } for e in run["experiments"]]
    found = run | {"tables": TABLES, "tools": tools, "labels": [labels[key] for key in order], "experiments": experiments}
    return found, list(queries)


def page(found: dict, queries: list[str]) -> str:
    """The page with its data inside. Each query is kept whole if the page then fits in SIZE_LIMIT, and
    otherwise cut to the longest of SQL_CUTS that makes it fit."""
    template = (HERE / "journeys_page.html").read_text()
    for cut in SQL_CUTS:
        inside = found | {"sql_cut": cut, "queries": [q if not cut or len(q) <= cut else q[:cut] + "…" for q in queries]}
        # `<` is written as an escape so that no query can close the script element the data sits in.
        html = template.replace("__DATA__", json.dumps(inside, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c"))
        if len(html.encode()) <= SIZE_LIMIT:
            break
    return html


def measure(commit: str, jobs: Path) -> str:
    """How the parser named every run_sql call in the commit's job folders, with the text it could not name. It
    reads every job folder named for the commit, where the page reads one per experiment."""
    covered = tool_reads(SERVER.read_text())
    kinds, failed, names, unnamed, folders, trials = Counter(), Counter(), Counter(), Counter(), set(), 0
    for path in sorted(jobs.glob(f"[!_]*-{commit}-*/*__*/agent/result.json")):
        try:
            messages = json.loads(path.read_text()).get("messages") or []
        except (OSError, ValueError):
            continue
        folders.add(path.parents[2].name)
        trials += 1
        for c in (c for step in journey(messages, covered) for c in step if c["tool"] == "run_sql"):
            kinds[c["kind"]] += 1
            failed[c["kind"]] += c["failed"]
            names.update(reads(c["sql"])["other"] if c["sql"] else [])
            if c["kind"] in ("unparsed", "none") and not re.match(r"\s*select\s+1\b", c["sql"], re.I):
                unnamed[c["kind"], " ".join(c["sql"].split())[:90]] += 1
    total = sum(kinds.values())
    what = {"tables": "at least one table of the database", "schema": "information_schema or pg_catalog and no table",
            "other": "only names that are not tables", "none": "a statement that reads no table, such as SELECT 1",
            "unparsed": "text that does not begin as a statement", "no query": "a call with no query in it"}
    lines = [f"{commit}: {len(folders)} job folders, {trials:,} trials, {total:,} run_sql calls"]
    lines += [f"  {kind:9} {kinds[kind]:7,} {100 * kinds[kind] / max(total, 1):5.1f}%  {failed[kind]:6,} failed  {about}"
              for kind, about in what.items()]
    lines.append("names that are not tables: " + ", ".join(f"{name} {n}" for name, n in names.most_common(12)))
    lines += [f"  {kind:9} {n:3}  {query}" for (kind, query), n in unnamed.most_common(24)]
    return "\n".join(lines)


if __name__ == "__main__":
    args = sys.argv[1:]
    commit = args[0]
    jobs = Path(args[args.index("--jobs") + 1]).resolve() if "--jobs" in args else HERE / "jobs"
    if "--measure" in args:
        print(measure(commit, jobs))
        sys.exit()
    out = Path(args[args.index("-o") + 1]) if "-o" in args else jobs / f"journeys-{commit}.html"
    data = Path(args[args.index("--data") + 1]) if "--data" in args else out.parent / f"journeys-{commit}.json"
    run = read(commit, jobs)
    found, queries = packed(run)
    html = page(found, queries)
    out.write_text(html if "--bare" in args else DOCUMENT.format(page=html))
    data.write_text(json.dumps(plain(run), ensure_ascii=False, separators=(",", ":")))
    kept = "whole" if '"sql_cut":null' in html else "cut to fit"
    print(f"{out}: {len(found['experiments'])} experiments, {sum(len(e['trials']) for e in found['experiments'])} trials, "
          f"{len(found['labels'])} labels, {len(queries)} distinct queries ({kept}), {out.stat().st_size / 1e6:.1f} MB")
    print(f"{data}: the same trials' steps as data, {data.stat().st_size / 1e6:.1f} MB")
