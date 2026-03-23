from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(slots=True)
class CircuitBreakerState:
    failures: int = 0
    opened_until: float = 0.0
    last_error: str = ""


class SimpleCircuitBreaker:
    def __init__(self, *, failure_threshold: int = 3, recovery_seconds: float = 120.0) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_seconds = max(1.0, float(recovery_seconds))
        self._state = CircuitBreakerState()
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        with self._lock:
            if self._state.opened_until <= 0:
                return False
            if time.monotonic() >= self._state.opened_until:
                self._state.opened_until = 0.0
                self._state.failures = 0
                self._state.last_error = ""
                return False
            return True

    def allow_request(self) -> bool:
        return not self.is_open()

    def record_success(self) -> None:
        with self._lock:
            self._state.failures = 0
            self._state.opened_until = 0.0
            self._state.last_error = ""

    def record_failure(self, error: Exception | str) -> None:
        with self._lock:
            self._state.failures += 1
            self._state.last_error = str(error)
            if self._state.failures >= self.failure_threshold:
                self._state.opened_until = time.monotonic() + self.recovery_seconds

    def snapshot(self) -> dict[str, float | int | str | bool]:
        with self._lock:
            now = time.monotonic()
            open_now = self._state.opened_until > now
            return {
                "open": open_now,
                "failures": self._state.failures,
                "opened_until_monotonic": self._state.opened_until,
                "retry_in_seconds": max(0.0, self._state.opened_until - now) if open_now else 0.0,
                "last_error": self._state.last_error,
            }
