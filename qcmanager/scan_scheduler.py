"""Periodic inventory discovery, independent of browser sessions."""
import threading
import time


class ScanScheduler:
    def __init__(self, manager, settings, interval):
        self.manager, self.settings, self.interval = manager, settings, interval
        self.last_purge = None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name='qc-scan-scheduler', daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join()

    def poll(self):
        # Share the API's settings lock: a project cannot be edited or removed
        # between checking its definition and reserving its scan work.
        if self.last_purge is None or time.monotonic() - self.last_purge >= 3600:
            self.manager.db.purge_history()
            self.last_purge = time.monotonic()
        with self.settings.lock:
            if self.settings.broken or self.stop_event.is_set():
                return
            for pid in list(self.manager.projects):
                if self.stop_event.is_set():
                    return
                with self.manager.guard:
                    finished = self.manager.scan_finished.get(pid)
                    pending = pid in self.manager.pending_scans
                if pending or (finished is not None and time.monotonic()-finished < self.interval):
                    continue
                try:
                    self.manager._queue_scan(pid)
                except Exception:
                    # Retry on the next interval, including queue saturation.
                    # Detailed scan failures are already logged by the worker.
                    self.manager.log.warning('automatic scan could not be queued project=%s', pid)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.poll()
            except Exception:
                self.manager.log.error('automatic scan scheduler failed; retrying')
            self.stop_event.wait(1)
