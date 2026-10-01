from collections import Counter

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


def test_all_large_fixture_responses_pass_their_graders():
    cases = load_cases()
    assert all(grade(case, case.large_response) for case in cases)


def test_reference_threshold_has_reproducible_cost_quality_tradeoff():
    result = evaluate(load_cases(), [0.55])
    run = result["threshold_runs"][0]

    assert result["baseline"] == {
        "strategy": "all-large",
        "correct": 50,
        "accuracy_pct": 100.0,
        "estimated_cost": 0.9,
    }
    assert run["correct"] == 45
    assert run["quality_retention_pct"] == 90.0
    assert run["cost_saving_pct"] == 50.0
    assert run["routing"] == {"small": 30, "large": 20}
