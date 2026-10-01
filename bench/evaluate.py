from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import string
from typing import Any

from app.router import route_task


DEFAULT_DATASET = Path(__file__).with_name("prompts.jsonl")
SMALL_COST = 0.003
LARGE_COST = 0.018


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    category: str
    difficulty: str
    prompt: str
    grader: str
    answer: Any
    small_response: str
    large_response: str
    tolerance: float = 0.0
    assertions: tuple[str, ...] = ()


def load_cases(path: Path = DEFAULT_DATASET) -> list[EvaluationCase]:
    cases: list[EvaluationCase] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        raw = json.loads(line)
        target_length = int(raw.pop("prompt_length", len(raw["prompt"])))
        raw["prompt"] = raw["prompt"].ljust(target_length, "x")
        raw["assertions"] = tuple(raw.get("assertions", ()))
        try:
            cases.append(EvaluationCase(**raw))
        except TypeError as exc:
            raise ValueError(f"invalid dataset row {line_number}: {exc}") from exc
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("dataset case IDs must be unique")
    return cases


def _normalize_exact(value: str) -> str:
    return value.strip().lower().strip(string.whitespace + string.punctuation)


def grade(case: EvaluationCase, response: str) -> bool:
    if case.grader == "numeric":
        matches = re.findall(r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?", response)
        if not matches:
            return False
        actual = float(matches[-1].replace(",", ""))
        expected = float(case.answer)
        allowed = max(case.tolerance, abs(expected) * case.tolerance)
        return abs(actual - expected) <= allowed
    if case.grader == "exact":
        return _normalize_exact(response) == _normalize_exact(str(case.answer))
    if case.grader == "json_fields":
        try:
            actual = json.loads(response)
        except json.JSONDecodeError:
            return False
        return isinstance(actual, dict) and all(
            actual.get(key) == value for key, value in case.answer.items()
        )
    if case.grader == "code_assert":
        namespace: dict[str, Any] = {}
        safe_globals = {
            "__builtins__": {
                "len": len, "list": list, "max": max, "min": min,
                "range": range, "set": set, "sorted": sorted, "sum": sum,
            }
        }
        try:
            exec(response, safe_globals, namespace)
            return all(bool(eval(assertion, safe_globals, namespace))
                       for assertion in case.assertions)
        except Exception:
            return False
    raise ValueError(f"unknown grader: {case.grader}")


def evaluate(cases: list[EvaluationCase], thresholds: list[float]) -> dict:
    baseline_correct = sum(grade(case, case.large_response) for case in cases)
    baseline_cost = len(cases) * LARGE_COST
    runs = []
    for threshold in thresholds:
        correct = 0
        total_cost = 0.0
        distribution = {"small": 0, "large": 0}
        failures = []
        for case in cases:
            decision = route_task(case.prompt, "standard", 0, 0, threshold)
            distribution[decision.tier] += 1
            total_cost += SMALL_COST if decision.tier == "small" else LARGE_COST
            response = (case.small_response if decision.tier == "small"
                        else case.large_response)
            passed = grade(case, response)
            correct += int(passed)
            if not passed:
                failures.append(case.id)
        runs.append({
            "difficulty_threshold": threshold,
            "correct": correct,
            "accuracy_pct": round(correct / len(cases) * 100, 2),
            "quality_retention_pct": round(
                correct / baseline_correct * 100 if baseline_correct else 0, 2
            ),
            "estimated_cost": round(total_cost, 3),
            "cost_saving_pct": round((1 - total_cost / baseline_cost) * 100, 2),
            "routing": distribution,
            "failed_case_ids": failures,
        })
    return {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "provider": "deterministic fixture adapter",
        "dataset_size": len(cases),
        "baseline": {
            "strategy": "all-large",
            "correct": baseline_correct,
            "accuracy_pct": round(baseline_correct / len(cases) * 100, 2),
            "estimated_cost": round(baseline_cost, 3),
        },
        "threshold_runs": runs,
        "limitations": [
            "Fixture responses validate routing, grading, and cost accounting, not real LLM quality.",
            "Run the same fixed dataset through provider adapters before using quality claims on a resume.",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate router cost/quality trade-offs")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--thresholds", type=float, nargs="+", default=[0.3, 0.45, 0.55, 0.7])
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = evaluate(load_cases(args.dataset), args.thresholds)
    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
