from __future__ import annotations

import argparse
import builtins
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import string
from typing import Any

from app.router import route_task


DEFAULT_DATASET = Path(__file__).with_name("prompts.jsonl")
DEFAULT_RESPONSES = Path(__file__).with_name("results") / "claude-responses.jsonl"
DEFAULT_THRESHOLDS = [0.2, 0.25, 0.3, 0.4, 0.55, 0.7]
SMALL_COST = 0.003
LARGE_COST = 0.018
FENCED_BLOCK = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)
SAFE_BUILTINS = {
    name: getattr(builtins, name)
    for name in (
        "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "isinstance",
        "len", "list", "map", "max", "min", "range", "reversed", "set", "sorted", "str", "sum",
        "tuple", "zip",
    )
}


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


def _normalize_field(value: Any) -> Any:
    return value.strip().casefold() if isinstance(value, str) else value


def _strip_fences(value: str) -> str:
    match = FENCED_BLOCK.search(value)
    return (match.group(1) if match else value).strip()


def grade(case: EvaluationCase, response: str) -> bool:
    if case.grader in ("json_fields", "code_assert"):
        response = _strip_fences(response)
    if case.grader == "numeric":
        matches = re.findall(r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?", response)
        if not matches:
            return False
        actual = float(matches[-1].replace(",", ""))
        expected = float(case.answer)
        allowed = max(case.tolerance, abs(expected) * case.tolerance)
        return abs(actual - expected) <= allowed
    if case.grader == "exact":
        # Accept the label alone, or on its own first or last line after visible reasoning.
        lines = [line for line in response.strip().splitlines() if line.strip()]
        candidates = [response] + ([lines[0], lines[-1]] if lines else [])
        expected = _normalize_exact(str(case.answer))
        return any(_normalize_exact(candidate) == expected for candidate in candidates)
    if case.grader == "json_fields":
        try:
            actual = json.loads(response)
        except json.JSONDecodeError:
            return False
        return isinstance(actual, dict) and all(
            _normalize_field(actual.get(key)) == _normalize_field(value)
            for key, value in case.answer.items()
        )
    if case.grader == "code_assert":
        # One namespace for globals and locals so recursive helpers can see themselves.
        namespace: dict[str, Any] = {"__builtins__": dict(SAFE_BUILTINS)}
        try:
            exec(response, namespace)
            return all(bool(eval(assertion, namespace)) for assertion in case.assertions)
        except Exception:
            return False
    raise ValueError(f"unknown grader: {case.grader}")


@dataclass(frozen=True)
class ResponseSet:
    """Per-case small/large responses and the cost of producing each one."""

    provider: str
    text: dict[str, dict[str, str]]
    cost: dict[str, dict[str, float]]
    limitations: tuple[str, ...]
    models: dict[str, str] | None = None


def fixture_responses(cases: list[EvaluationCase]) -> ResponseSet:
    return ResponseSet(
        provider="deterministic fixture adapter",
        text={c.id: {"small": c.small_response, "large": c.large_response} for c in cases},
        cost={c.id: {"small": SMALL_COST, "large": LARGE_COST} for c in cases},
        limitations=(
            "Fixture responses validate routing, grading, and cost accounting, not real LLM quality.",
            "Run the same fixed dataset through provider adapters before using quality claims on a resume.",
        ),
    )


def evaluate(cases: list[EvaluationCase], thresholds: list[float],
             responses: ResponseSet | None = None) -> dict:
    responses = responses or fixture_responses(cases)
    baseline_correct = sum(grade(case, responses.text[case.id]["large"]) for case in cases)
    baseline_cost = sum(responses.cost[case.id]["large"] for case in cases)
    small_correct = sum(grade(case, responses.text[case.id]["small"]) for case in cases)
    runs = []
    for threshold in thresholds:
        correct = 0
        total_cost = 0.0
        distribution = {"small": 0, "large": 0}
        failures = []
        for case in cases:
            decision = route_task(case.prompt, "standard", 0, 0, threshold)
            distribution[decision.tier] += 1
            total_cost += responses.cost[case.id][decision.tier]
            passed = grade(case, responses.text[case.id][decision.tier])
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
            "estimated_cost": round(total_cost, 6),
            "cost_saving_pct": round((1 - total_cost / baseline_cost) * 100, 2),
            "routing": distribution,
            "failed_case_ids": failures,
        })
    report = {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "provider": responses.provider,
        "dataset_size": len(cases),
        "baseline": {
            "strategy": "all-large",
            "correct": baseline_correct,
            "accuracy_pct": round(baseline_correct / len(cases) * 100, 2),
            "estimated_cost": round(baseline_cost, 6),
        },
        "all_small": {
            "correct": small_correct,
            "accuracy_pct": round(small_correct / len(cases) * 100, 2),
            "estimated_cost": round(sum(responses.cost[c.id]["small"] for c in cases), 6),
        },
        "threshold_runs": runs,
        "limitations": list(responses.limitations),
    }
    if responses.models:
        report["models"] = responses.models
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate router cost/quality trade-offs")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--thresholds", type=float, nargs="+", default=DEFAULT_THRESHOLDS)
    parser.add_argument("--provider", choices=["fixture", "anthropic"], default="fixture",
                        help="anthropic calls the Claude API for any responses missing from --responses")
    parser.add_argument("--responses", type=Path, default=DEFAULT_RESPONSES,
                        help="recorded Claude responses; reused so reruns cost nothing")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cases = load_cases(args.dataset)
    responses = None
    if args.provider == "anthropic":
        from bench.claude_adapter import collect, load_recorded

        collect(cases, args.responses)
        responses = load_recorded(cases, args.responses)
    result = evaluate(cases, args.thresholds, responses)
    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
