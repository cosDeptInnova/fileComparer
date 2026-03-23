import threading
from typing import Dict, Any
import time

class ProgressManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._store: Dict[str, Dict[str, Any]] = {}

    def init(self, sid: str):
        with self._lock:
            self._store[sid] = {
                "status": "running",
                "percent": 0,
                "step": "inicializando",
                "detail": "",
                "started_at": time.time(),
                "ended_at": None,
                "error": None,
            }

    def update(self, sid: str, percent: int, step: str, detail: str = ""):
        with self._lock:
            if sid in self._store:
                self._store[sid]["percent"] = int(max(0, min(100, percent)))
                self._store[sid]["step"] = step
                self._store[sid]["detail"] = detail

    def done(self, sid: str):
        with self._lock:
            if sid in self._store:
                self._store[sid]["status"] = "done"
                self._store[sid]["percent"] = 100
                self._store[sid]["ended_at"] = time.time()

    def fail(self, sid: str, error: str):
        with self._lock:
            if sid in self._store:
                self._store[sid]["status"] = "error"
                self._store[sid]["error"] = error
                self._store[sid]["ended_at"] = time.time()

    def get(self, sid: str):
        with self._lock:
            return self._store.get(sid, None)