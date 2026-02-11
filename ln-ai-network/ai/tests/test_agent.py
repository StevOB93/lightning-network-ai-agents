from ai.agent import run_agent

def test_no_route_proposes_open_channel():
    out = run_agent("ai/mocks/fixtures/no_route.json", node_count=3)
    assert out["intent"] in ("open_channel", "noop")
    if out["intent"] == "open_channel":
        assert "from_node" in out and "to_node" in out and "amount_sat" in out

def test_tool_failure_results_in_noop():
    out = run_agent("ai/mocks/fixtures/tool_failure.json", node_count=3)
    assert out["intent"] == "noop"
