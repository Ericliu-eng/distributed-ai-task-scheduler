from dataclasses import dataclass

@dataclass(frozen=True)
class RoutingDecision:
    tier: str
    reason: str
    difficulty: float
    estimated_cost: float

def route_task(prompt: str, sla: str, small_depth: int, large_depth: int) -> RoutingDecision:
    """Deterministic and explainable hackathon routing policy."""
    difficulty = min(len(prompt) / 1200, 1.0)
    if sla == "urgent":
        tier = "small" if small_depth <= large_depth else "large"
        reason = f"Urgent SLA → selected the shorter queue ({small_depth} small vs {large_depth} large)"
    elif difficulty >= 0.55:
        tier = "large"
        reason = f"Long/complex prompt → difficulty {difficulty:.2f} exceeds the 0.55 threshold"
    elif small_depth > 4:
        tier = "large"
        reason = f"Small tier is saturated ({small_depth} queued) → overflow to large tier"
    else:
        tier = "small"
        reason = f"Short prompt + standard SLA → difficulty {difficulty:.2f}, low-cost tier preferred"
    return RoutingDecision(tier, reason, difficulty, 0.018 if tier == "large" else 0.003)
