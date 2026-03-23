import json
import logging
import os
import socket
import time
from typing import Any, Optional

from .compare_tasks import CompareTaskPayload

LOGGER = logging.getLogger(__name__)


class RedisCompareQueue:
    def __init__(self, sync_client, job_store):
        self.sync_client = sync_client
        self.job_store = job_store
        self.queue_key = os.getenv("COMPARE_QUEUE_KEY", "compare:queue")
        self.processing_key = os.getenv("COMPARE_PROCESSING_QUEUE_KEY", "compare:processing")
        self.workers_key = os.getenv("COMPARE_WORKERS_KEY", "compare:workers")
        self.inflight_key = os.getenv("COMPARE_INFLIGHT_KEY", "compare:inflight")
        self.worker_ttl = int(os.getenv("COMPARE_WORKER_TTL_SECONDS", "30"))
        self.orphan_ttl = int(
            os.getenv(
                "COMPARE_ORPHAN_TTL_SECONDS",
                str(max(self.worker_ttl * 3, 90)),
            )
        )
        self.max_inflight_jobs = int(
            os.getenv(
                "COMPARE_MAX_INFLIGHT_JOBS",
                os.getenv("COMPARE_WORKER_CONCURRENCY", "1"),
            )
        )

    def ensure_group(self) -> None:
        self.sync_client.setnx(f"{self.queue_key}:meta", json.dumps({"type": "reliable-list-queue"}))

    def enqueue(self, payload: CompareTaskPayload) -> None:
        if self._is_terminal_job(payload.sid):
            LOGGER.info("Ignorando enqueue de job terminal sid=%s queue_key=%s", payload.sid, self.queue_key)
            return
        self._push_queue_entry(self._make_queue_entry(payload), front=False)

    def dequeue(self, *, timeout_seconds: int = 5) -> Optional[CompareTaskPayload]:
        payload, _ = self.dequeue_claim(worker_id="legacy-dequeue", timeout_seconds=timeout_seconds)
        return payload

    def dequeue_claim(
        self,
        *,
        worker_id: str,
        timeout_seconds: int = 5,
    ) -> tuple[Optional[CompareTaskPayload], Optional[dict[str, Any]]]:
        raw = self.sync_client.brpoplpush(self.queue_key, self.processing_key, timeout=timeout_seconds)
        if not raw:
            return None, None
        entry = self._decode_queue_entry(raw)
        task_payload = entry.get("task") or {}
        try:
            payload = CompareTaskPayload.from_dict(task_payload)
        except TypeError:
            self.sync_client.lrem(self.processing_key, 1, raw)
            raise
        if self._is_terminal_job(payload.sid):
            self.sync_client.lrem(self.processing_key, 1, raw)
            self.sync_client.hdel(self.inflight_key, payload.sid)
            LOGGER.info(
                "Descartando job ya finalizado antes de claim sid=%s queue_key=%s processing_key=%s",
                payload.sid,
                self.queue_key,
                self.processing_key,
            )
            return None, None
        self.claim_task(payload, worker_id=worker_id, queue_entry=entry, raw_queue_entry=raw)
        return payload, entry

    def requeue(self, payload: CompareTaskPayload, *, front: bool = False, attempt: int = 0) -> None:
        if self._is_terminal_job(payload.sid):
            LOGGER.info("Ignorando requeue de job terminal sid=%s queue_key=%s", payload.sid, self.queue_key)
            return
        self._push_queue_entry(self._make_queue_entry(payload, attempt=attempt), front=front)

    def depth(self) -> int:
        return int(self.sync_client.llen(self.queue_key) or 0)

    def register_worker(self, worker_id: Optional[str] = None, *, concurrency: Optional[int] = None) -> str:
        worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.heartbeat(worker_id, active_jobs=0, concurrency=concurrency)
        return worker_id

    def heartbeat(
        self,
        worker_id: str,
        *,
        active_jobs: Optional[int] = None,
        concurrency: Optional[int] = None,
    ) -> None:
        now = int(time.time())
        payload = {
            "worker_id": worker_id,
            "seen_at": now,
            "active_jobs": int(active_jobs or 0),
            "concurrency": int(concurrency or 0),
        }
        self.sync_client.hset(self.workers_key, worker_id, json.dumps(payload, ensure_ascii=False))
        self.sync_client.expire(self.workers_key, self.worker_ttl * 4)

    def unregister_worker(self, worker_id: str) -> None:
        self.sync_client.hdel(self.workers_key, worker_id)

    def claim_task(
        self,
        payload: CompareTaskPayload,
        *,
        worker_id: str,
        queue_entry: Optional[dict[str, Any]] = None,
        raw_queue_entry: Optional[str] = None,
    ) -> None:
        now = int(time.time())
        claim_payload = {
            "task": payload.to_dict(),
            "worker_id": worker_id,
            "claimed_at": now,
            "heartbeat_at": now,
            "queue_entry": queue_entry or self._make_queue_entry(payload),
            "raw_queue_entry": raw_queue_entry,
        }
        self.sync_client.hset(self.inflight_key, payload.sid, json.dumps(claim_payload, ensure_ascii=False))
        self.sync_client.expire(self.inflight_key, max(self.orphan_ttl * 4, 300))

    def touch_task(self, sid: str, *, worker_id: str) -> None:
        raw = self.sync_client.hget(self.inflight_key, sid)
        if not raw:
            return
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if payload.get("worker_id") != worker_id:
            return
        payload["heartbeat_at"] = int(time.time())
        self.sync_client.hset(self.inflight_key, sid, json.dumps(payload, ensure_ascii=False))
        self.sync_client.expire(self.inflight_key, max(self.orphan_ttl * 4, 300))

    def release_task(self, sid: str) -> None:
        raw = self.sync_client.hget(self.inflight_key, sid)
        if raw:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
            raw_queue_entry = payload.get("raw_queue_entry")
            if raw_queue_entry:
                self.sync_client.lrem(self.processing_key, 1, raw_queue_entry)
        self.sync_client.hdel(self.inflight_key, sid)

    def active_job_count(self) -> int:
        self._cleanup_stale_workers()
        self.recover_orphaned_tasks()
        values = self.sync_client.hgetall(self.inflight_key) or {}
        return len(values)

    def count_active_workers(self) -> int:
        active_workers = self._active_workers()
        return len(active_workers)

    def worker_diagnostics(self) -> dict[str, Any]:
        now = int(time.time())
        active_workers: dict[str, dict[str, Any]] = {}
        stale_workers: dict[str, dict[str, Any]] = {}
        values = self.sync_client.hgetall(self.workers_key) or {}
        for worker_id, raw in values.items():
            payload = self._decode_worker_payload(worker_id, raw)
            seen_at = int(payload.get("seen_at") or 0)
            age_seconds = max(0, now - seen_at) if seen_at else None
            payload["age_seconds"] = age_seconds
            if seen_at and now - seen_at <= self.worker_ttl:
                active_workers[worker_id] = payload
            else:
                stale_workers[worker_id] = payload

        inflight_values = self.sync_client.hgetall(self.inflight_key) or {}
        inflight_jobs: dict[str, dict[str, Any]] = {}
        for sid, raw in inflight_values.items():
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                inflight_payload = json.loads(raw)
            except json.JSONDecodeError:
                inflight_payload = {"decode_error": True}
            worker_id = str(inflight_payload.get("worker_id") or "")
            heartbeat_at = int(
                inflight_payload.get("heartbeat_at")
                or inflight_payload.get("claimed_at")
                or 0
            )
            inflight_jobs[str(sid)] = {
                "worker_id": worker_id,
                "heartbeat_at": heartbeat_at,
                "age_seconds": max(0, now - heartbeat_at) if heartbeat_at else None,
                "worker_known_active": worker_id in active_workers,
            }

        return {
            "queue_key": self.queue_key,
            "processing_key": self.processing_key,
            "workers_key": self.workers_key,
            "inflight_key": self.inflight_key,
            "worker_ttl_seconds": self.worker_ttl,
            "orphan_ttl_seconds": self.orphan_ttl,
            "active_worker_count": len(active_workers),
            "stale_worker_count": len(stale_workers),
            "queue_depth": self.depth(),
            "processing_depth": int(self.sync_client.llen(self.processing_key) or 0),
            "inflight_job_count": len(inflight_jobs),
            "active_workers": active_workers,
            "stale_workers": stale_workers,
            "inflight_jobs": inflight_jobs,
        }

    def log_worker_diagnostics(
        self,
        *,
        reason: str,
        level: int = logging.WARNING,
    ) -> dict[str, Any]:
        diagnostics = self.worker_diagnostics()
        LOGGER.log(
            level,
            "Compare worker diagnostics reason=%s active=%s stale=%s inflight=%s queue=%s workers_key=%s inflight_key=%s",
            reason,
            diagnostics["active_worker_count"],
            diagnostics["stale_worker_count"],
            diagnostics["inflight_job_count"],
            diagnostics["queue_depth"],
            diagnostics["workers_key"],
            diagnostics["inflight_key"],
        )
        if diagnostics["stale_worker_count"] > 0:
            LOGGER.log(
                level,
                "Compare worker diagnostics stale_workers=%s",
                json.dumps(diagnostics["stale_workers"], ensure_ascii=False),
            )
        return diagnostics

    def recover_orphaned_tasks(self) -> int:
        now = int(time.time())
        active_workers = self._active_workers()
        recovered = self._recover_untracked_processing_entries(now)
        values = self.sync_client.hgetall(self.inflight_key) or {}
        for sid, raw in values.items():
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                inflight = json.loads(raw)
            except json.JSONDecodeError:
                self.sync_client.hdel(self.inflight_key, sid)
                continue
            worker_id = str(inflight.get("worker_id") or "")
            heartbeat_at = int(
                inflight.get("heartbeat_at")
                or inflight.get("claimed_at")
                or 0
            )
            if worker_id in active_workers and now - heartbeat_at <= self.orphan_ttl:
                continue
            task_payload = inflight.get("task") or {}
            try:
                task = CompareTaskPayload.from_dict(task_payload)
            except TypeError:
                self.sync_client.hdel(self.inflight_key, sid)
                continue
            if self._is_terminal_job(task.sid):
                raw_queue_entry = inflight.get("raw_queue_entry")
                if raw_queue_entry:
                    self.sync_client.lrem(self.processing_key, 1, raw_queue_entry)
                self.sync_client.hdel(self.inflight_key, sid)
                LOGGER.info(
                    "Limpiando inflight huérfano de job finalizado sid=%s processing_key=%s inflight_key=%s",
                    task.sid,
                    self.processing_key,
                    self.inflight_key,
                )
                continue
            entry = inflight.get("queue_entry") if isinstance(inflight.get("queue_entry"), dict) else self._make_queue_entry(task)
            attempt = int(entry.get("attempt") or 0) + 1
            entry["attempt"] = attempt
            entry["enqueued_at"] = now
            raw_queue_entry = inflight.get("raw_queue_entry")
            if raw_queue_entry:
                self.sync_client.lrem(self.processing_key, 1, raw_queue_entry)
            self._push_queue_entry(entry, front=True)
            self.sync_client.hdel(self.inflight_key, sid)
            recovered += 1
        return recovered

    def worker_snapshot(self) -> dict[str, dict[str, Any]]:
        return self._active_workers()

    def _cleanup_stale_workers(self) -> None:
        self._active_workers()

    def _active_workers(self) -> dict[str, dict[str, Any]]:
        now = int(time.time())
        active: dict[str, dict[str, Any]] = {}
        values = self.sync_client.hgetall(self.workers_key) or {}
        for worker_id, raw in values.items():
            payload = self._decode_worker_payload(worker_id, raw)
            seen_at = int(payload.get("seen_at") or 0)
            if now - seen_at <= self.worker_ttl:
                active[worker_id] = payload
                continue
            LOGGER.warning(
                "Expirando heartbeat de worker comparador worker_id=%s age=%ss ttl=%ss",
                worker_id,
                max(0, now - seen_at),
                self.worker_ttl,
            )
            self.sync_client.hdel(self.workers_key, worker_id)
        return active

    def _decode_worker_payload(self, worker_id: str, raw: Any) -> dict[str, Any]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                payload.setdefault("worker_id", worker_id)
                payload.setdefault("seen_at", payload.get("ts") or 0)
                return payload
            try:
                return {"worker_id": worker_id, "seen_at": int(raw)}
            except (TypeError, ValueError):
                return {"worker_id": worker_id, "seen_at": 0}
        return {"worker_id": worker_id, "seen_at": 0}

    def _make_queue_entry(self, payload: CompareTaskPayload, *, attempt: int = 0) -> dict[str, Any]:
        return {
            "task": payload.to_dict(),
            "sid": payload.sid,
            "enqueued_at": int(time.time()),
            "attempt": max(0, int(attempt)),
        }

    def _push_queue_entry(self, entry: dict[str, Any], *, front: bool) -> None:
        serialized = json.dumps(entry, ensure_ascii=False)
        if front:
            self.sync_client.lpush(self.queue_key, serialized)
            return
        self.sync_client.rpush(self.queue_key, serialized)

    def _decode_queue_entry(self, raw: Any) -> dict[str, Any]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if isinstance(raw, str):
            payload = json.loads(raw)
        elif isinstance(raw, dict):
            payload = dict(raw)
        else:
            raise TypeError("Queue entry inválida")
        if isinstance(payload.get("task"), dict):
            task_payload = payload["task"]
        else:
            task_payload = payload
            payload = {
                "task": task_payload,
                "sid": task_payload.get("sid"),
                "enqueued_at": int(time.time()),
                "attempt": 0,
            }
        payload.setdefault("sid", task_payload.get("sid"))
        payload.setdefault("enqueued_at", int(time.time()))
        payload.setdefault("attempt", 0)
        return payload

    def _recover_untracked_processing_entries(self, now: int) -> int:
        try:
            processing_entries = self.sync_client.lrange(self.processing_key, 0, -1) or []
        except Exception:
            return 0
        if not processing_entries:
            return 0
        inflight_entries = self.sync_client.hgetall(self.inflight_key) or {}
        tracked_raw_entries: set[str] = set()
        tracked_sids: set[str] = set()
        for raw in inflight_entries.values():
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            raw_queue_entry = payload.get("raw_queue_entry")
            if isinstance(raw_queue_entry, str) and raw_queue_entry:
                tracked_raw_entries.add(raw_queue_entry)
            sid = str(payload.get("task", {}).get("sid") or payload.get("sid") or "")
            if sid:
                tracked_sids.add(sid)

        recovered = 0
        for raw_entry in processing_entries:
            raw_value = raw_entry.decode("utf-8") if isinstance(raw_entry, bytes) else raw_entry
            if raw_value in tracked_raw_entries:
                continue
            try:
                entry = self._decode_queue_entry(raw_value)
            except Exception:
                self.sync_client.lrem(self.processing_key, 1, raw_entry)
                recovered += 1
                continue
            sid = str(entry.get("sid") or entry.get("task", {}).get("sid") or "")
            enqueued_at = int(entry.get("enqueued_at") or 0)
            if sid and self._is_terminal_job(sid):
                self.sync_client.lrem(self.processing_key, 1, raw_entry)
                recovered += 1
                LOGGER.info(
                    "Eliminando processing entry residual de job finalizado sid=%s processing_key=%s",
                    sid,
                    self.processing_key,
                )
                continue
            if sid and sid in tracked_sids:
                continue
            if enqueued_at and now - enqueued_at < self.orphan_ttl:
                continue
            self.sync_client.lrem(self.processing_key, 1, raw_entry)
            entry["attempt"] = int(entry.get("attempt") or 0) + 1
            entry["enqueued_at"] = now
            self._push_queue_entry(entry, front=True)
            recovered += 1
        return recovered

    def _is_terminal_job(self, sid: str) -> bool:
        if not sid or self.job_store is None or not hasattr(self.job_store, "get_job_sync"):
            return False
        try:
            job = self.job_store.get_job_sync(sid)
        except Exception:
            LOGGER.exception("No se pudo consultar el estado del job sid=%s para evitar reprocesado", sid)
            return False
        if not isinstance(job, dict):
            return False
        status = str((job.get("progress") or {}).get("status") or "").strip().lower()
        return status in {"done", "error"}