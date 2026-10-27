from __future__ import annotations

import logging
import os
import signal
import threading
import time

from app.store import SchedulerStore

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("recovery-monitor")


class RecoveryMonitor:
    def __init__(self, store: SchedulerStore, interval: float = 2.0,
                 retention_minutes: float | None = None, purge_every: float = 60.0):
        self.store = store
        self.interval = interval
        # A public demo deletes finished work after this long (None keeps everything).
        self.retention_minutes = retention_minutes
        self.purge_every = purge_every
        self._last_purge: float | None = None
        self.stop_event = threading.Event()

    def stop(self, *_):
        self.stop_event.set()

    def run_once(self) -> list[str]:
        recovered = self.store.requeue_stale_tasks()
        if recovered:
            log.warning("recovered %s expired task(s): %s", len(recovered), ", ".join(recovered))
        self._purge_if_due()
        return recovered

    def _purge_if_due(self) -> None:
        if not self.retention_minutes:
            return
        now = time.monotonic()
        if self._last_purge is not None and now - self._last_purge < self.purge_every:
            return
        self._last_purge = now
        removed = self.store.purge_finished(self.retention_minutes)
        if any(removed.values()):
            log.info("purged finished work older than %s min: %s", self.retention_minutes, removed)

    def run_forever(self):
        self.store.init()
        log.info("recovery monitor started interval=%ss", self.interval)
        try:
            while not self.stop_event.is_set():
                try:
                    self.run_once()
                except Exception:
                    # Keep scanning after transient database errors; expired leases
                    # stay expired, so the next successful pass recovers them.
                    log.exception("recovery pass failed; retrying in %ss", self.interval)
                self.stop_event.wait(self.interval)
        finally:
            log.info("recovery monitor stopped")


def main():
    retention = float(os.getenv("RETENTION_MINUTES", "0"))
    monitor = RecoveryMonitor(
        SchedulerStore(),
        interval=float(os.getenv("RECOVERY_INTERVAL", "2")),
        retention_minutes=retention or None,
    )
    signal.signal(signal.SIGINT, monitor.stop)
    signal.signal(signal.SIGTERM, monitor.stop)
    monitor.run_forever()


if __name__ == "__main__":
    main()
