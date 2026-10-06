"""The policy graph is whole: every rule stands for a section of the policy, can be reached, and is exercised.

The scenario catalog and the graph were written separately. The last test ties them together: an edge that no
scenario walks is a rule of the policy that no agent run tests.
"""

from langgraph.graph import END, START
from langsmith.run_helpers import tracing_context

from world.graph import EDGES, POLICY, authorize
from world.labeling import SECTIONS
from world.reading import annotated
from world.scenarios import ALL


def test_every_rule_stands_for_a_section_and_every_decision_rule_has_one():
    sections = {rule.section for rule in EDGES} - {None}
    assert sections <= set(SECTIONS)
    assert sections == {"3", "4.1", "4.2", "4.3", "4.4", "5", "6", "9", "11"}  # README: §2 and §10 are applied inside them


def test_every_rule_can_be_reached_and_every_edge_leads_to_a_rule_or_the_end():
    reached, todo = set(), [authorize]
    while todo:
        rule = todo.pop()
        if rule not in reached:
            reached.add(rule)
            todo += [target for target in EDGES[rule].values() if target is not END]
    assert reached == set(EDGES)


def test_the_langgraph_graph_is_the_table():
    drawn = POLICY.get_graph()
    assert set(drawn.nodes) == {START, END, "records"} | {rule.__name__ for rule in EDGES}
    from_the_table = {(rule.__name__, target if target is END else target.__name__)
                      for rule, edges in EDGES.items() for target in edges.values()}
    assert {(edge.source, edge.target) for edge in drawn.edges} == from_the_table | {(START, "records"), ("records", "authorize")}
    assert "authorize -. &nbsp;stop&nbsp; .-> __end__;" in drawn.draw_mermaid()


def test_every_edge_is_walked_by_a_scenario():
    walked = set()
    for s in ALL:
        reading = annotated(s.world, s.request)
        for charge in [reading.charge, *reading.other_charges]:
            case = {"world": s.world, "request": s.request, "reading": reading, "charge": charge}
            with tracing_context(enabled=False):
                for update in POLICY.stream(case, stream_mode="updates"):
                    walked |= {(rule, report["edge"]) for rule, report in update.items() if report and "edge" in report}
    assert walked == {(rule.__name__, edge) for rule, edges in EDGES.items() for edge in edges}
