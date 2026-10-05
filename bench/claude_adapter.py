"""Collect real small/large-tier responses from the Claude API for the routing evaluation.

Every response is appended to a JSONL file as soon as it arrives, so an interrupted run
resumes where it stopped and reruns of the evaluation never pay for the same call twice.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bench.evaluate import EvaluationCase, ResponseSet


MODELS = {"small": "claude-haiku-4-5", "large": "claude-sonnet-5-5"}
# USD per million input / output tokens (list prices, 2026-10). Thinking tokens bill as output.
PRICES_PER_MTOK = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00),
}
MAX_TOKENS = 16000
# The answer format lives in the system prompt so the routed prompt length stays the task itself.
SYSTEM_PROMPTS = {
    "numeric": "Answer with only the final number. No units, working, or explanation.",
    "exact": "Answer with only one label from the list given in the question, exactly as written.",
    "json_fields": "Answer with only a JSON object using exactly the keys requested. No markdown or prose.",
    "code_assert": (
        "Answer with only Python code that defines the requested function. Use no imports, "
        "no markdown fences, and no explanation."
    ),
}


def _cost(model: str, input_tokens: int, output_tokens: int) -> float:
    input_price, output_price = PRICES_PER_MTOK[model]
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def _read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _key(record: dict[str, Any]) -> tuple[str, str, str]:
    return record["case_id"], record["tier"], record["model"]


def collect(cases: list[EvaluationCase], path: Path, client: Any = None,
            models: dict[str, str] = MODELS) -> int:
    """Call Claude for every (case, tier) not already recorded; return the number of new calls."""
    done = {_key(record) for record in _read_records(path)}
    pending = [(case, tier) for case in cases for tier in ("small", "large")
               if (case.id, tier, models[tier]) not in done]
    if not pending:
        return 0
    if client is None:
        import anthropic

        client = anthropic.Anthropic(max_retries=5)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        for index, (case, tier) in enumerate(pending, 1):
            model = models[tier]
            response = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPTS[case.grader],
                messages=[{"role": "user", "content": case.prompt}],
            )
            text = "".join(block.text for block in response.content if block.type == "text")
            usage = response.usage
            record = {
                "case_id": case.id,
                "tier": tier,
                "model": model,
                "text": text,
                "stop_reason": response.stop_reason,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cost_usd": _cost(model, usage.input_tokens, usage.output_tokens),
            }
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
            output.flush()
            print(f"[{index}/{len(pending)}] {case.id} {tier} ({model}): {response.stop_reason}", flush=True)
    return len(pending)


def load_recorded(cases: list[EvaluationCase], path: Path,
                  models: dict[str, str] = MODELS) -> ResponseSet:
    records = {_key(record): record for record in _read_records(path)}
    text: dict[str, dict[str, str]] = {}
    cost: dict[str, dict[str, float]] = {}
    for case in cases:
        text[case.id], cost[case.id] = {}, {}
        for tier in ("small", "large"):
            record = records.get((case.id, tier, models[tier]))
            if record is None:
                raise ValueError(f"no recorded {tier} response for {case.id} from {models[tier]}")
            text[case.id][tier] = record["text"]
            cost[case.id][tier] = record["cost_usd"]
    return ResponseSet(
        provider=f"Claude API ({models['small']} small / {models['large']} large)",
        text=text,
        cost=cost,
        models=dict(models),
        limitations=(
            "One recorded sample per case and tier at default sampling settings; costs use measured "
            "token usage at list prices.",
            "50 hand-written cases are a smoke test of the routing trade-off, not a general quality benchmark.",
        ),
    )
