from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from contextlib import contextmanager
from typing import Iterator

LOGGER = logging.getLogger(__name__)


class RedisSlotSemaphore:
    """Small distributed semaphore backed by Redis SETNX per slot."""

    def __init__(
        self,
        sync_client=None,
        *,
        name: str,
        slots: int,
        ttl_seconds: int = 3600,
        wait_sleep_seconds: float = 0.05,
    ) -> None:
        self.sync_client = sync_client
        self.name = str(name)
        self.slots = max(1, int(slots))
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.wait_sleep_seconds = max(0.01, float(wait_sleep_seconds))
        self.owner_prefix = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    def enabled(self) -> bool:
        return self.sync_client is not None and self.slots > 0

    def _slot_key(self, index: int) -> str:
        return f"{self.name}:slot:{index}"

    def active_count(self) -> int:
        if not self.enabled():
            return 0
        active = 0
        for idx in range(self.slots):
            try:
                if self.sync_client.get(self._slot_key(idx)):
                    active += 1
            except Exception:
                LOGGER.exception("Failed to read semaphore slot name=%s idx=%s", self.name, idx)
                break
        return active

    def _read_owner(self, index: int) -> str:
        if not self.enabled():
            return ""
        try:
            owner = self.sync_client.get(self._slot_key(index))
        except Exception:
            LOGGER.exception("Failed to read semaphore slot owner name=%s idx=%s", self.name, index)
            return ""
        if isinstance(owner, bytes):
            owner = owner.decode("utf-8")
        return str(owner or "")

    @staticmethod
    def owner_identity(owner: str | None) -> str:
        parts = str(owner or "").split(":")
        if len(parts) >= 2 and parts[0] and parts[1]:
            return f"{parts[0]}:{parts[1]}"
        return str(owner or "")

    def acquire(self, *, blocking: bool = True, timeout_seconds: float | None = None) -> str | None:
        if not self.enabled():
            return None
        owner = f"{self.owner_prefix}:{uuid.uuid4().hex}"
        deadline = None if timeout_seconds is None else time.monotonic() + max(0.0, float(timeout_seconds))
        while True:
            for idx in range(self.slots):
                key = self._slot_key(idx)
                try:
                    acquired = bool(self.sync_client.setnx(key, owner))
                    if acquired:
                        self.sync_client.expire(key, self.ttl_seconds)
                        return f"{idx}:{owner}"
                except Exception:
                    LOGGER.exception("Failed to acquire semaphore name=%s idx=%s", self.name, idx)
                    return None
            if not blocking:
                return None
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(self.wait_sleep_seconds)

    def release(self, lease: str | None) -> None:
        if not self.enabled() or not lease:
            return
        idx_str, _, owner = str(lease).partition(":")
        try:
            idx = int(idx_str)
        except (TypeError, ValueError):
            return
        key = self._slot_key(idx)
        try:
            current = self.sync_client.get(key)
            if isinstance(current, bytes):
                current = current.decode("utf-8")
            if current == owner and hasattr(self.sync_client, "delete"):
                self.sync_client.delete(key)
        except Exception:
            LOGGER.exception("Failed to release semaphore name=%s idx=%s", self.name, idx)

    def renew(self, lease: str | None) -> bool:
        if not self.enabled() or not lease:
            return False
        idx_str, _, owner = str(lease).partition(":")
        try:
            idx = int(idx_str)
        except (TypeError, ValueError):
            return False
        key = self._slot_key(idx)
        try:
            current = self.sync_client.get(key)
            if isinstance(current, bytes):
                current = current.decode("utf-8")
            if current != owner:
                return False
            return bool(self.sync_client.expire(key, self.ttl_seconds))
        except Exception:
            LOGGER.exception("Failed to renew semaphore name=%s idx=%s", self.name, idx)
            return False

    def reap_orphaned_slots(self, *, active_owner_ids: set[str]) -> int:
        if not self.enabled():
            return 0
        recovered = 0
        for idx in range(self.slots):
            owner = self._read_owner(idx)
            if not owner:
                continue
            owner_id = self.owner_identity(owner)
            if not owner_id or owner_id in active_owner_ids:
                continue
            try:
                if hasattr(self.sync_client, "delete"):
                    recovered += int(self.sync_client.delete(self._slot_key(idx)) or 0)
            except Exception:
                LOGGER.exception("Failed to reap semaphore slot name=%s idx=%s owner_id=%s", self.name, idx, owner_id)
        return recovered

    @contextmanager
    def lease(self, *, timeout_seconds: float | None = None) -> Iterator[str | None]:
        token = self.acquire(timeout_seconds=timeout_seconds)
        try:
            yield token
        finally:
            self.release(token)
