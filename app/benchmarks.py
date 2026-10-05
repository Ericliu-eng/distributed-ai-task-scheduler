"""Read saved benchmark evidence without running work or touching the queue."""

import json
from pathlib import Path

from fastapi import HTTPException


RESULTS_DIR = Path(__file__).resolve().parents[1] / "bench" / "results"
RESULT_FILES = {
    "performance": "postgres-1000-tasks.json",
    "routing": "routing-evaluation-claude.json",
    "recovery": "postgres-recovery-20.json",
}


def load_result(kind: str) -> dict:
    # Only expose known reports, never accept a caller-supplied filesystem path.
    if kind not in RESULT_FILES:
        raise HTTPException(404, "Unknown benchmark report")
    try:
        report = json.loads((RESULTS_DIR / RESULT_FILES[kind]).read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("Expected a JSON object")
        return report
    except FileNotFoundError as exc:
        raise HTTPException(404, "Saved benchmark report is not available") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(503, "Saved benchmark report could not be read") from exc
