from __future__ import annotations
import threading
import time
from app.store import SchedulerStore

def mock_inference(prompt: str, tier: str) -> str:
    lowered = prompt.lower()
    if "index" in lowered: return "Database indexes reduce lookup work through an ordered structure, trading storage and write cost for faster reads."
    if "summar" in lowered: return "Summary: the request was processed by the cost-aware scheduler and completed successfully."
    if tier == "large": return "Large-tier analysis complete: deeper reasoning was selected for this complex request."
    return "Small-tier response complete: fast, low-cost inference was sufficient for this request."

class WorkerPool:
    def __init__(self, store: SchedulerStore):
        self.store, self.stop_event, self.threads = store, threading.Event(), []
        self.states = {"worker-small-01": {"id": "worker-small-01", "tier": "small", "status": "idle", "task_id": None},
                       "worker-large-01": {"id": "worker-large-01", "tier": "large", "status": "idle", "task_id": None}}
        self.state_lock = threading.Lock()

    def start(self):
        if self.threads: return
        for worker in self.states.values():
            thread = threading.Thread(target=self._loop, args=(worker["id"], worker["tier"]), daemon=True)
            thread.start(); self.threads.append(thread)

    def stop(self):
        self.stop_event.set()
        for thread in self.threads: thread.join(timeout=2)

    def snapshot(self) -> list[dict]:
        with self.state_lock: return [dict(state) for state in self.states.values()]

    def _loop(self, worker_id: str, tier: str):
        while not self.stop_event.is_set():
            task = self.store.claim(worker_id, tier)
            if not task:
                self.stop_event.wait(0.25); continue
            with self.state_lock: self.states[worker_id].update(status="busy", task_id=task["id"])
            time.sleep(1.15 if tier == "large" else 0.75)
            if self.store.should_fail(task["id"], task["attempt"]):
                self.store.fail_or_retry(task["id"], worker_id, task["attempt"], "Simulated provider HTTP 503")
                time.sleep(0.55)
            else:
                self.store.finish(task["id"], worker_id, task["attempt"], mock_inference(task["prompt"], tier))
            with self.state_lock: self.states[worker_id].update(status="idle", task_id=None)
