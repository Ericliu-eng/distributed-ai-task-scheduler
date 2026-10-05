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
IDLE_HEARTBEAT_SECONDS = 2.0
MAX_ERROR_BACKOFF_SECONDS = 10.0


class WorkerService:
    def __init__(self, store: SchedulerStore, worker_id: str, tier: str,
                 poll_interval: float = 0.25, lease_seconds: float = 15.0,
                 renew_interval: float = 3.0):
        if tier not in {"small", "large"}:
            raise ValueError("MODEL_TIER must be 'small' or 'large'")
        self.store = store
        self.worker_id = worker_id
        self.tier = tier
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self.renew_interval = renew_interval
        self.stop_event = threading.Event()
        self._last_idle_heartbeat: float | None = None

    def stop(self, *_):
        self.stop_event.set()

    def _idle_heartbeat(self, force: bool = False) -> None:
        # Polling every 250 ms should not mean writing the workers table every 250 ms.
        now = time.monotonic()
        if force or self._last_idle_heartbeat is None or now - self._last_idle_heartbeat >= IDLE_HEARTBEAT_SECONDS:
            self.store.heartbeat(self.worker_id, self.tier, "idle")
            self._last_idle_heartbeat = now

    def run_once(self, delay: bool = True) -> bool:
        self._idle_heartbeat()
        task = self.store.claim(self.worker_id, self.tier, self.lease_seconds)
        if task is None:
            return False
        self.store.heartbeat(self.worker_id, self.tier, "busy", task["id"])
        log.info("claimed task=%s attempt=%s tier=%s", task["id"], task["attempt"], self.tier)
        renewal_stop = threading.Event()
        renewal_thread = threading.Thread(
            target=self._renew_while_running,
            args=(task["id"], task["attempt"], renewal_stop),
            daemon=True,
        )
        renewal_thread.start()
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
            renewal_stop.set()
            renewal_thread.join(timeout=self.renew_interval + 1)
            self._idle_heartbeat(force=True)
        return True

    def _renew_while_running(self, task_id: str, attempt: int,
                             renewal_stop: threading.Event) -> None:
        while not renewal_stop.wait(self.renew_interval):
            renewed = self.store.renew_lease(
                task_id, self.worker_id, attempt, self.lease_seconds
            )
            if not renewed:
                log.warning("lease renewal rejected task=%s attempt=%s", task_id, attempt)
                return
            self.store.heartbeat(self.worker_id, self.tier, "busy", task_id)
            log.debug("renewed lease task=%s attempt=%s", task_id, attempt)

    def run_forever(self):
        self.store.init()
        self.store.heartbeat(self.worker_id, self.tier, "idle")
        log.info("worker started id=%s tier=%s", self.worker_id, self.tier)
        failures = 0
        try:
            while not self.stop_event.is_set():
                try:
                    worked = self.run_once()
                    failures = 0
                except Exception:
                    # A database outage must not kill the worker. Any claimed task keeps
                    # its lease and is recovered by the monitor if this attempt is lost.
                    failures += 1
                    backoff = min(MAX_ERROR_BACKOFF_SECONDS, self.poll_interval * 2 ** failures)
                    log.exception("worker loop error; retrying in %.2fs", backoff)
                    self.stop_event.wait(backoff)
                    continue
                if not worked:
                    self.stop_event.wait(self.poll_interval)
        finally:
            try:
                self.store.mark_worker_offline(self.worker_id)
            except Exception:
                log.exception("could not mark worker offline id=%s", self.worker_id)
            log.info("worker stopped id=%s", self.worker_id)


def main():
    tier = os.getenv("MODEL_TIER", "small").lower()
    worker_id = os.getenv("WORKER_ID", f"worker-{tier}-01")
    service = WorkerService(
        SchedulerStore(), worker_id, tier,
        lease_seconds=float(os.getenv("LEASE_SECONDS", "15")),
        renew_interval=float(os.getenv("LEASE_RENEW_INTERVAL", "3")),
    )
    signal.signal(signal.SIGINT, service.stop)
    signal.signal(signal.SIGTERM, service.stop)
    service.run_forever()


if __name__ == "__main__":
    main()
