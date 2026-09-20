import time


def execute(prompt: str, tier: str, delay: bool = True) -> str:
    """Deterministic provider used by the MVP; replace with a model adapter later."""
    if delay:
        time.sleep(1.15 if tier == "large" else 0.75)
    lowered = prompt.lower()
    if "index" in lowered:
        return "Database indexes reduce lookup work through an ordered structure, trading storage and write cost for faster reads."
    if "summar" in lowered:
        return "Summary: the request was processed by the cost-aware scheduler and completed successfully."
    if tier == "large":
        return "Large-tier analysis complete: deeper reasoning was selected for this complex request."
    return "Small-tier response complete: fast, low-cost inference was sufficient for this request."
