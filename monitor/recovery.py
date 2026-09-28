from __future__ import annotations

import logging
import os
import signal
import threading

from app.store import SchedulerStore

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("recovery-monitor")


class RecoveryMonitor:
    def __init__(self, store: SchedulerStore, interval: float = 2.0):
        self.store = store
        self.interval = interval
        self.stop_event = threading.Event()

    def stop(self, *_):
        self.stop_event.set()

    def run_once(self) -> list[str]:
        recovered = self.store.requeue_stale_tasks()
        if recovered:
            log.warning("recovered %s expired task(s): %s", len(recovered), ", ".join(recovered))
        return recovered

    def run_forever(self):
        self.store.init()
        log.info("recovery monitor started interval=%ss", self.interval)
        try:
            while not self.stop_event.is_set():
                self.run_once()
                self.stop_event.wait(self.interval)
        finally:
            log.info("recovery monitor stopped")


def main():
    monitor = RecoveryMonitor(
        SchedulerStore(),
        interval=float(os.getenv("RECOVERY_INTERVAL", "2")),
    )
    signal.signal(signal.SIGINT, monitor.stop)
    signal.signal(signal.SIGTERM, monitor.stop)
    monitor.run_forever()


if __name__ == "__main__":
    main()
