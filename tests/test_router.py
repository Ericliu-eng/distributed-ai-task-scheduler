from app.router import route_task

def test_short_standard_uses_small():
    decision = route_task("Explain an index", "standard", 0, 0)
    assert decision.tier == "small"
    assert "difficulty" in decision.reason

def test_complex_standard_uses_large():
    decision = route_task("x" * 900, "standard", 0, 0)
    assert decision.tier == "large"

def test_urgent_uses_shorter_queue():
    assert route_task("urgent", "urgent", 5, 1).tier == "large"
