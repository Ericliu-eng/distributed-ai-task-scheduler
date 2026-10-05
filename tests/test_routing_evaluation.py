from collections import Counter
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from bench.claude_adapter import MODELS, collect, load_recorded
from bench.evaluate import evaluate, grade, load_cases


def test_fixed_dataset_has_expected_shape():
    cases = load_cases()
    assert len(cases) == 50
    assert len({case.id for case in cases}) == 50
    assert Counter(case.category for case in cases) == {
        "arithmetic": 15,
        "structured_extraction": 15,
        "classification": 10,
        "code": 10,
    }
    assert Counter(case.difficulty for case in cases) == {"easy": 25, "hard": 25}


def test_prompts_are_self_contained_without_padding():
    for case in load_cases():
        assert "xxxx" not in case.prompt
        if case.difficulty == "easy":
            assert len(case.prompt) < 100


def test_all_large_fixture_responses_pass_their_graders():
    cases = load_cases()
    assert all(grade(case, case.large_response) for case in cases)


def test_reference_threshold_has_reproducible_cost_quality_tradeoff():
    result = evaluate(load_cases(), [0.25])
    run = result["threshold_runs"][0]

    assert result["baseline"] == {
        "strategy": "all-large",
        "correct": 50,
        "accuracy_pct": 100.0,
        "estimated_cost": 0.9,
    }
    assert run["correct"] == 48
    assert run["quality_retention_pct"] == 96.0
    assert run["cost_saving_pct"] == 45.0
    assert run["routing"] == {"small": 27, "large": 23}
    assert result["all_small"]["correct"] == 25


def test_graders_accept_fenced_model_output():
    cases = {case.id: case for case in load_cases()}
    assert grade(cases["json-001"], '```json\n{"name": "Ana", "city": "Reno"}\n```')
    assert grade(cases["code-002"], "```python\ndef solve(x):\n    return x * 2\n```")
    assert grade(cases["exact-006"], "**Database**")
    assert grade(cases["numeric-013"], "1,355")


def test_graders_extract_answers_without_loosening_correctness():
    cases = {case.id: case for case in load_cases()}
    assert grade(cases["exact-004"], "8 is divisible by 2, so the answer is:\n\neven")
    assert grade(cases["exact-009"], "eventual\n\nReasoning: W cannot read its own write.")
    assert not grade(cases["exact-009"], "It is not causal.\nThe model is strong.")
    assert grade(cases["json-006"], '{"priority": "High", "owner": "Lee"}')
    assert not grade(cases["json-006"], '{"priority": "low", "owner": "Lee"}')


def test_code_grader_supports_recursive_solutions():
    case = {case.id: case for case in load_cases()}["code-010"]
    assert grade(case, "def solve(n):\n    return n if n < 2 else solve(n - 1) + solve(n - 2)")


class FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="text", text="answer")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=100, output_tokens=10),
        )


def test_collect_records_each_call_once_and_resumes(tmp_path):
    cases = load_cases()[:2]
    path = tmp_path / "responses.jsonl"
    client = SimpleNamespace(messages=FakeMessages())

    assert collect(cases, path, client) == 4
    assert collect(cases, path, client) == 0
    assert len(client.messages.calls) == 4
    assert {call["model"] for call in client.messages.calls} == set(MODELS.values())

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    haiku = next(r for r in records if r["model"] == "claude-haiku-4-5")
    assert haiku["text"] == "answer"
    assert haiku["cost_usd"] == pytest.approx((100 * 1.0 + 10 * 5.0) / 1_000_000)


def test_recorded_responses_drive_evaluation_with_measured_costs(tmp_path):
    cases = load_cases()
    path = tmp_path / "responses.jsonl"
    rows = []
    for case in cases:
        for tier, cost in (("small", 0.001), ("large", 0.002)):
            text = case.large_response if tier == "large" or case.difficulty == "easy" else "wrong"
            rows.append({"case_id": case.id, "tier": tier, "model": MODELS[tier],
                         "text": text, "cost_usd": cost})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    result = evaluate(cases, [0.7], load_recorded(cases, path))

    assert result["baseline"]["estimated_cost"] == pytest.approx(0.1)
    assert result["threshold_runs"][0]["cost_saving_pct"] == 50.0
    assert result["threshold_runs"][0]["correct"] == 25
    assert result["models"] == MODELS


def test_load_recorded_rejects_missing_responses(tmp_path):
    path = tmp_path / "responses.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="no recorded small response"):
        load_recorded(load_cases()[:1], path)
