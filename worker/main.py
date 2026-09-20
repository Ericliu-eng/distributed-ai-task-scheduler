from __future__ import annotations

import logging
import os
import signal
import threading
import time

from app.store import SchedulerStore
from worker.executor import execute

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scheduler-worker")


class WorkerService:
    def __init__(self, store: SchedulerStore, worker_id: str, tier: str,
                 poll_interval: float = 0.25):
        if tier not in {"small", "large"}:
            raise ValueError("MODEL_TIER must be 'small' or 'large'")
        self.store = store
        self.worker_id = worker_id
        self.tier = tier
        self.poll_interval = poll_interval
        self.stop_event = threading.Event()

    def stop(self, *_):
        self.stop_event.set()

    def run_once(self, delay: bool = True) -> bool:
        self.store.heartbeat(self.worker_id, self.tier, "idle")
        task = self.store.claim(self.worker_id, self.tier)
        if task is None:
            return False
        self.store.heartbeat(self.worker_id, self.tier, "busy", task["id"])
        log.info("claimed task=%s attempt=%s tier=%s", task["id"], task["attempt"], self.tier)
        try:
            if self.store.should_fail(task["id"], task["attempt"]):
                if delay:
                    time.sleep(0.4)
                raise RuntimeError("Simulated provider HTTP 503")
            result = execute(task["prompt"], self.tier, delay=delay)
            accepted = self.store.finish(task["id"], self.worker_id, task["attempt"], result)
            if accepted:
                log.info("completed task=%s", task["id"])
            else:
                log.warning("fencing rejected task=%s attempt=%s", task["id"], task["attempt"])
        except Exception as exc:
            outcome = self.store.fail_or_retry(task["id"], self.worker_id, task["attempt"], str(exc))
            log.warning("task=%s failed outcome=%s error=%s", task["id"], outcome, exc)
        finally:
            self.store.heartbeat(self.worker_id, self.tier, "idle")
        return True

    def run_forever(self):
        self.store.init()
        self.store.heartbeat(self.worker_id, self.tier, "idle")
        log.info("worker started id=%s tier=%s", self.worker_id, self.tier)
        try:
            while not self.stop_event.is_set():
                if not self.run_once():
                    self.stop_event.wait(self.poll_interval)
        finally:
            self.store.mark_worker_offline(self.worker_id)
            log.info("worker stopped id=%s", self.worker_id)


def main():
    tier = os.getenv("MODEL_TIER", "small").lower()
    worker_id = os.getenv("WORKER_ID", f"worker-{tier}-01")
    service = WorkerService(SchedulerStore(), worker_id, tier)
    signal.signal(signal.SIGINT, service.stop)
    signal.signal(signal.SIGTERM, service.stop)
    service.run_forever()


if __name__ == "__main__":
    main()
