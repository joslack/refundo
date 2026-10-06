"""The journeys page names each SQL query by the tables it reads and merges trials into a tree by their steps.

A query named for the wrong tables, or a result matched to the wrong call, would put trials on branches they never
took, and the page would still look plausible. So the naming is checked on queries copied from real trials, and
the steps and the tree on small conversations in the shape the job folders hold.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest

from world.scenarios import ALL
from world.sql import seed_sql

# Loaded by path, as tests/test_tools.py loads the server: `evals` is a folder of scripts, not a package.
_spec = importlib.util.spec_from_file_location("journeys", Path(__file__).parents[1] / "evals/journeys.py")
journeys = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(journeys)

COVERED = journeys.tool_reads(journeys.SERVER.read_text())

# Queries the agents wrote, and the tables each reads, in the schema's order.
REAL_QUERIES = [
    ("SELECT * FROM invoices WHERE customer=(SELECT stripe_customer_id FROM workspaces WHERE id='ws_001') ORDER BY created",
     ["workspaces", "invoices"]),
    ("SELECT t.id, t.subject, t.opened_at, tm.at, tm.author_type, tm.body FROM tickets t LEFT JOIN ticket_messages tm "
     "ON tm.ticket_id=t.id WHERE t.workspace_id='ws_001' ORDER BY t.opened_at, tm.at", ["tickets", "ticket_messages"]),
    ("SELECT d.* FROM disputes d JOIN invoices i ON d.charge_id=i.charge_id JOIN workspaces w ON i.customer=w.stripe_customer_id "
     "WHERE w.id='ws_001' ORDER BY d.created", ["workspaces", "invoices", "disputes"]),
    ("select count(*)::int as usage_actions from session_events where workspace_id='ws_001' and at >= '2026-09-08 14:00:00+00:00' "
     "and action in ('create','edit','export');", ["session_events"]),
    ("SELECT at, type, actor_user_id, data\nFROM app_events\nWHERE workspace_id = 'ws_001' AND at >= TIMESTAMPTZ '2026-08-01 00:00:00+00'\n"
     "ORDER BY at", ["app_events"]),
    ("SELECT i.id, il.tier FROM invoices i LEFT JOIN invoice_lines il ON il.invoice_id=i.id WHERE i.customer IN "
     "(SELECT stripe_customer_id FROM workspaces WHERE id='ws_001') ORDER BY i.created DESC LIMIT 30",
     ["workspaces", "invoices", "invoice_lines"]),
    # A WITH clause: its names are not tables, and one of them has the name of the table it reads.
    ("WITH target AS (SELECT 'ws_001'::text AS workspace_id), inv AS (SELECT i.* FROM invoices i JOIN target t ON "
     "i.customer=t.workspace_id), disputes AS (SELECT d.* FROM disputes d JOIN inv i ON d.charge_id=i.charge_id) "
     "SELECT 'invoice', to_jsonb(inv) FROM inv UNION ALL SELECT 'dispute', to_jsonb(disputes) FROM disputes", ["invoices", "disputes"]),
    ("WITH ctx AS (SELECT 'ws_001'::text AS workspace_id), ws AS (SELECT w.* FROM workspaces w, ctx WHERE w.id=ctx.workspace_id) "
     "SELECT jsonb_build_object('workspace', (SELECT to_jsonb(ws) FROM ws), 'plans', (SELECT jsonb_agg(ph) FROM plan_history ph, ctx "
     "WHERE ph.workspace_id=ctx.workspace_id))", ["workspaces", "plan_history"]),
    # FROM inside a function is not a table.
    ("SELECT COUNT(DISTINCT (at AT TIME ZONE 'UTC')::date) FILTER (WHERE action IN ('create','edit','export')) AS usage_days, "
     "EXTRACT(EPOCH FROM (max(at) - min(at)))/86400 AS span FROM session_events WHERE workspace_id='ws_001'", ["session_events"]),
]


@pytest.mark.parametrize("query, tables", REAL_QUERIES, ids=lambda v: v[:40] if isinstance(v, str) else None)
def test_a_real_query_is_named_by_its_tables(query, tables):
    read = journeys.reads(query)
    assert (read["kind"], read["tables"], read["other"], read["schema"]) == ("tables", tables, [], False)


@pytest.mark.parametrize("query, tables", [
    ("SELECT * FROM members WHERE name = 'moved from invoices, refunds' -- from credits", ["members"]),
    ("SELECT * FROM members /* JOIN disputes */ WHERE role IS DISTINCT FROM 'owner'", ["members"]),
    ("SELECT $$ it's from invoices $$ AS note, $tag$ join refunds $tag$ FROM credits WHERE description = E'from\\' tickets'", ["credits"]),
    ("SELECT e.id, kv.key FROM app_events e, LATERAL jsonb_each_text(e.data) AS kv, tickets t", ["app_events", "tickets"]),
    ("SELECT * FROM (SELECT * FROM invoices WHERE status = 'paid') i JOIN refunds r ON r.charge_id = i.charge_id", ["invoices", "refunds"]),
    ("SELECT * FROM (invoices i JOIN invoice_lines l ON l.invoice_id = i.id) WHERE i.status = 'paid'", ["invoices", "invoice_lines"]),
    ("SELECT id FROM refunds UNION ALL (SELECT id FROM credits) ORDER BY id, 1", ["refunds", "credits"]),
    ('SELECT * FROM public.invoices; SELECT * FROM "disputes"', ["invoices", "disputes"]),
    ("SELECT * FROM proposals", ["proposals"]),
    ("SELECT a.id FROM tickets a WHERE EXISTS (SELECT 1 FROM ticket_messages m WHERE m.ticket_id = a.id AND substring(m.body FROM 1 FOR 6) = 'cancel')",
     ["tickets", "ticket_messages"]),
])
def test_only_names_after_from_and_join_count(query, tables):
    assert journeys.reads(query)["tables"] == tables


@pytest.mark.parametrize("query, kind, other", [
    ("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'invoices'", "schema", []),
    ("SELECT tablename FROM pg_tables WHERE schemaname = 'public'", "schema", []),
    ("SELECT c.relname FROM pg_catalog.pg_class c", "schema", []),
    ("SELECT * FROM users WHERE id = 'usr_dana'", "other", ["users"]),
    ("SELECT * FROM workspace_members wm JOIN support_tickets st ON true", "other", ["workspace_members", "support_tickets"]),
    ("SELECT 1", "none", []),
    ("SELECT EXTRACT(EPOCH FROM (TIMESTAMPTZ '2026-09-07 14:03:00+00' - TIMESTAMPTZ '2026-06-08 14:00:00+00'))/86400.0 AS days", "none", []),
    ("with params as (select '2026-09-08 14:00:00+00:00'::timestamptz as t0) select t0 + interval '30 days' from params", "none", []),
    ("SELECT * FROM generate_series(1, 3) AS n", "none", []),
    ("...refunds...", "unparsed", []),
    (">> first 200 invoices related to ws_001 where status is charged twice", "unparsed", []),
    ("", "unparsed", []),
])
def test_a_query_with_no_table_says_why(query, kind, other):
    read = journeys.reads(query)
    assert (read["kind"], read["tables"], read["other"]) == (kind, [], other)


def test_a_recursive_with_name_is_not_a_table():
    query = "WITH RECURSIVE days(d) AS (SELECT 1 UNION ALL SELECT d + 1 FROM days WHERE d < 3) SELECT d, i.id FROM days, invoices i"
    assert journeys.relations(query) == ["invoices"]


def test_the_tables_are_the_ones_a_scenario_is_seeded_with():
    sql = seed_sql(ALL[0].world, ALL[0].request)
    assert [t for t in journeys.TABLES if f"CREATE TABLE {t} (" not in sql] == []
    assert sql.count("CREATE TABLE") == len(journeys.TABLES)


def test_each_tool_reads_the_tables_its_queries_name():
    assert COVERED["list_charges"] == ["invoices", "invoice_lines", "refunds", "disputes"]
    assert COVERED["list_account_credits"] == ["credits"]
    assert COVERED["list_tables"] == ["schema"]
    assert COVERED["run_sql"] == COVERED["submit_proposal"] == []
    # get_usage reads the request's time through another function, and the case file is every other read at once.
    assert COVERED["get_usage"] == ["session_events", "request_context"]
    assert COVERED["get_case_file"] == [t for t in journeys.TABLES if t != "proposals"]


def test_tool_reads_follows_calls_and_skips_what_is_not_a_tool():
    source = '''
async def helper():
    """Read from the platform, not from the message."""
    return await rows("SELECT received_at FROM request_context")

@mcp.tool
async def one():
    return await rows("SELECT * FROM credits ORDER BY created"), await helper()

@mcp.tool
async def write(action: str):
    await rows("INSERT INTO proposals (action) VALUES ($1)", action)
'''
    assert journeys.tool_reads(source) == {"one": ["credits", "request_context"], "write": []}


def labels(steps: list[list[dict]]) -> list[list[str]]:
    return [[c["label"] for c in step] for step in steps]


def ai(*calls: tuple[str, dict]) -> dict:
    return {"type": "ai", "content": "", "tool_calls": [{"name": name, "args": args, "id": f"call_{i}"} for i, (name, args) in enumerate(calls)]}


def tool(content) -> dict:
    return {"type": "tool", "content": content}


SQL_TRIAL = [
    {"type": "human", "content": "Can I get a refund?"},
    ai(("get_request_context", {})),
    tool('{"workspace_id":"ws_001"}'),
    ai(("list_tables", {}), ("run_sql", {"query": "SELECT 1"})),
    tool('{"invoices":["id text"]}'), tool('[{"?column?":1}]'),
    ai(("run_sql", {"query": "SELECT * FROM invoices i JOIN invoice_lines l ON l.invoice_id = i.id"}),
       ("run_sql", {"query": "SELECT * FROM users"}),
       ("run_sql", {"query": "SELECT * FROM members WHERE workspace_id = 'ws_001'"}),
       ("run_sql", {})),
    # Results come back as text, or as blocks of text, and an empty list of rows as nothing at all.
    tool([{"type": "text", "text": '[{"id":"in_001"}]'}]), tool('SQL error: relation "users" does not exist'), tool(""),
    tool("1 validation error for call[run_sql]\nquery\n  Missing required argument"),
    ai(("submit_proposal", {"action": "deny", "amount_cents": 0, "sections": ["5"], "rationale": "..."})),
    tool("Recorded."),
    {"type": "ai", "content": "We cannot refund this charge."},
]


def test_a_step_is_one_message_and_its_results_are_matched_in_order():
    steps = journeys.journey(SQL_TRIAL, COVERED)
    assert labels(steps) == [
        ["get_request_context"],
        ["list_tables", "run_sql: no table"],
        ["run_sql: invoices + invoice_lines", "run_sql: users? (failed)", "run_sql: members", "run_sql: no query (failed)"],
        ["submit_proposal"],
    ]
    first, unknown, empty, missing = steps[2]
    assert (first["tables"], first["failed"]) == (["invoices", "invoice_lines"], False)
    assert (unknown["tables"], unknown["failed"], unknown["kind"]) == ([], True, "other")
    assert (empty["tables"], empty["failed"]) == (["members"], False)
    assert (missing["sql"], missing["kind"]) == (None, "no query")
    assert steps[0][0]["tables"] == ["members", "request_context"] and steps[1][0]["schema"]


def test_a_failed_query_keeps_its_name_and_reads_nothing():
    failed = journeys.call("run_sql", {"query": "SELECT workspace_id FROM invoices"}, 'SQL error: column "workspace_id" does not exist', COVERED)
    assert (failed["label"], failed["tables"], failed["failed"]) == ("run_sql: invoices (failed)", [], True)


@pytest.mark.parametrize("query, result, label", [
    ("SELECT column_name FROM information_schema.columns WHERE table_name = 'invoices'", "[]", "run_sql: schema"),
    ("SELECT now()", '[{"now":"2026-09-20"}]', "run_sql: no table"),
    ("...refunds...", 'SQL error: syntax error at or near ".."', "run_sql: not a query (failed)"),
    ("SELECT * FROM invoices i JOIN charges c ON c.id = i.charge_id", 'SQL error: relation "charges" does not exist', "run_sql: invoices + charges? (failed)"),
    ("   ", "", "run_sql: no query"),
])
def test_a_query_with_no_table_has_a_name_in_plain_words(query, result, label):
    assert journeys.call("run_sql", {"query": query}, result, COVERED)["label"] == label


def test_calls_made_by_a_fixed_graph_are_steps_of_their_own():
    said = {"type": "ai", "content": '{"action":"deny"}'}
    as_call = ai(("decision", {"action": "deny", "amount_cents": 0}))  # how some providers carry the model's answer
    for decided in (said, as_call):
        steps = journeys.journey([{"type": "human", "content": "Refund?"}, tool('{"request":{}}'), decided, tool("Recorded."),
                                  {"type": "ai", "content": "No."}], COVERED)
        assert labels(steps) == [["get_case_file"], ["submit_proposal"]]
        assert all(c["by_code"] for step in steps for c in step)
        assert steps[0][0]["tables"] == COVERED["get_case_file"]


def test_a_call_the_provider_could_not_decode_joins_its_step():
    steps = journeys.journey([
        ai(("run_sql", {"query": "SELECT * FROM refunds"}), ("run_sql", {"query": "SELECT * FROM credits"})),
        tool("Tool call run_sql with id chatcmpl-tool-b4dc9b7702fa0b34 could not be executed - arguments were malformed or truncated."),
        tool('[{"id":"re_001"}]'), tool(""),
    ], COVERED)
    assert labels(steps) == [["run_sql: no query (failed)", "run_sql: refunds", "run_sql: credits"]]


def test_a_trial_cut_off_before_its_results_still_has_its_calls():
    steps = journeys.journey([ai(("get_request_context", {})), tool("{}"), ai(("run_sql", {"query": "SELECT * FROM tickets"}))], COVERED)
    assert labels(steps) == [["get_request_context"], ["run_sql: tickets"]]
    assert journeys.journey([], COVERED) == []


def test_a_step_is_compared_as_a_set():
    assert journeys.step_key(["b", "a", "b"]) == journeys.step_key(["a", "b"]) == ("a", "b")


def test_the_tree_merges_trials_while_their_steps_agree():
    a, b, c, end = ("context",), ("invoices", "members"), ("refunds",), ("submit",)
    paths = [[a, b, end], [a, b, end], [a, b, c, end], [a, c], [a, b], []]
    root = journeys.tree(paths, [True, False, True, False, True, True])
    assert (root["step"], root["n"], root["right"], root["ends"]) == (None, 6, 4, [5])
    (first,) = root["next"]
    assert (first["step"], first["n"], first["right"], first["ends"]) == (a, 5, 3, [])
    wide, narrow = first["next"]  # the most travelled branch first
    assert (wide["step"], wide["n"], wide["right"], wide["ends"]) == (b, 4, 3, [4])
    assert (narrow["step"], narrow["n"], narrow["right"], narrow["ends"]) == (c, 1, 0, [3])
    done, more = wide["next"]
    assert (done["step"], done["n"], done["right"], done["ends"], done["next"]) == (end, 2, 1, [0, 1], [])
    assert (more["step"], more["n"], more["next"][0]["ends"]) == (c, 1, [2])


def test_every_trial_ends_at_one_node_of_the_tree():
    paths = [[("a",)] * depth + [("b", str(i % 3))] for i, depth in enumerate([0, 1, 1, 2, 3, 3, 3])]
    root = journeys.tree(paths, [i % 2 == 0 for i in range(len(paths))])

    def ends(node: dict) -> list[int]:
        assert node["n"] == len(node["ends"]) + sum(n["n"] for n in node["next"])
        return node["ends"] + [i for n in node["next"] for i in ends(n)]

    assert sorted(ends(root)) == list(range(len(paths)))


def run_of(trials: list[tuple[str, str, bool, list[dict]]]) -> dict:
    """One experiment in the form read() returns, from (case, folder name, right, messages)."""
    return {"commit": "abc1234", "covered": COVERED, "experiments": [{
        "graph": "sql", "model": "gpt-6-luna", "effort": "high", "complete": False,
        "trials": [{"case": case, "trial": name, "right": right, "error": "", "steps": journeys.journey(messages, COVERED)}
                   for case, name, right, messages in trials]}]}


def test_the_data_file_lists_each_trial_under_its_folder_name():
    assert journeys.plain(run_of([("be-16", "be-16__AbC123x", True, SQL_TRIAL), ("an-01", "an-01__Zz9", False, [])])) == {
        "commit": "abc1234",
        "trials": {
            "be-16__AbC123x": {"case": "be-16", "graph": "sql", "model": "gpt-6-luna", "effort": "high", "right": True, "steps": [
                ["get_request_context"],
                ["list_tables", "run_sql: no table"],
                ["run_sql: invoices + invoice_lines", "run_sql: members", "run_sql: no query (failed)", "run_sql: users? (failed)"],
                ["submit_proposal"]]},
            "an-01__Zz9": {"case": "an-01", "graph": "sql", "model": "gpt-6-luna", "effort": "high", "right": False, "steps": []},
        },
    }


def test_the_data_file_keeps_one_label_for_each_call():
    twice = [ai(("run_sql", {"query": "SELECT * FROM refunds"}), ("run_sql", {"query": "SELECT id FROM refunds"})), tool(""), tool("")]
    assert journeys.plain(run_of([("be-01", "be-01__a", True, twice)]))["trials"]["be-01__a"]["steps"] == [["run_sql: refunds", "run_sql: refunds"]]


def test_the_page_and_the_data_file_name_calls_alike():
    run = run_of([("be-16", "be-16__AbC123x", True, SQL_TRIAL), ("be-16", "be-16__d", False, SQL_TRIAL[:6])])
    found, queries = journeys.packed(run)
    names = [label["t"] for label in found["labels"]]
    case, right, error, steps, folder = found["experiments"][0]["trials"][0]
    assert (case, right, error, folder) == ("be-16", 1, "", "be-16__AbC123x")
    shown = [sorted(names[c[0] if isinstance(c, list) else c] for c in step) for step in steps]
    assert shown == journeys.plain(run)["trials"]["be-16__AbC123x"]["steps"]
    # A query is listed once and a run_sql call points at it; a call with no query points at none.
    assert [queries[c[1]] if c[1] >= 0 else None for c in steps[2]] == [
        "SELECT * FROM invoices i JOIN invoice_lines l ON l.invoice_id = i.id", "SELECT * FROM users",
        "SELECT * FROM members WHERE workspace_id = 'ws_001'", None]
    # Both trees hold both trials. Named by table they part at the third step; named by tool alone they do too,
    # because only one trial has a third step.
    for name in ("tool", "query"):
        step, n, right, ends, after = found["experiments"][0]["trees"][name]
        assert (step, n, right, ends) == ([], 2, 1, [])
        assert [names[i] for i in after[0][0]] == ["get_request_context"]
        second = after[0][4][0]
        assert (second[1], second[3]) == (2, [1])
    assert [names[i] for i in found["experiments"][0]["trees"]["tool"][4][0][4][0][0]] == ["list_tables", "run_sql"]
    assert [names[i] for i in found["experiments"][0]["trees"]["query"][4][0][4][0][0]] == ["list_tables", "run_sql: no table"]


def embedded(html: str) -> dict:
    start = html.index('<script id="data" type="application/json">') + len('<script id="data" type="application/json">')
    return json.loads(html[start:html.index("</script>", start)])


def test_no_query_can_close_the_script_the_data_sits_in():
    hostile = "SELECT '</script><!-- <script>' FROM members"
    found, queries = journeys.packed(run_of([("be-01", "be-01__a", True, [ai(("run_sql", {"query": hostile})), tool("")])]))
    html = journeys.page(found, queries)
    assert html.count("</script>") == 2 and "<!--" not in html.split('type="application/json">')[1].split("</script>")[0]
    assert embedded(html)["queries"] == [hostile] and embedded(html)["sql_cut"] is None


def test_long_queries_are_cut_when_the_page_would_be_too_large(monkeypatch):
    long = "SELECT id FROM members WHERE id IN (" + ", ".join(f"'usr_{i}'" for i in range(4000)) + ")"
    found, queries = journeys.packed(run_of([("be-01", "be-01__a", True, [ai(("run_sql", {"query": long})), tool("")])]))
    monkeypatch.setattr(journeys, "SIZE_LIMIT", len(journeys.page(found, queries).encode()) - 1000)
    inside = embedded(journeys.page(found, queries))
    assert inside["sql_cut"] == 2000 and inside["queries"] == [long[:2000] + "…"]
    assert inside["labels"][inside["experiments"][0]["trials"][0][3][0][0][0]]["t"] == "run_sql: members"


def test_measure_counts_the_queries_of_every_job_folder_of_the_commit(tmp_path):
    for job, name in [("sql-gpt-6-luna-high-abc1234-1005-172003", "be-16__a"), ("sql-glm-5p3-flash-high-abc1234-1005-172003", "be-16__b"),
                      ("sql-gpt-6-luna-high-fff0000-1005-172003", "be-16__c"), ("_invalid-sql-abc1234-x", "be-16__d")]:
        (tmp_path / job / name / "agent").mkdir(parents=True)
        (tmp_path / job / name / "agent/result.json").write_text(json.dumps({"messages": SQL_TRIAL}))
    lines = journeys.measure("abc1234", tmp_path).splitlines()
    assert lines[0] == "abc1234: 2 job folders, 2 trials, 10 run_sql calls"
    kinds = ["tables", "schema", "other", "none", "unparsed", "no query"]
    assert [int(re.match(rf"  {kind} +(\d+) ", line)[1]) for kind, line in zip(kinds, lines[1:7])] == [4, 0, 2, 2, 0, 2]
    assert "users 2" in lines[7]
