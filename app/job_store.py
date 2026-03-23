import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from typing import Any, Optional

_JOB_STORE = None
TERMINAL_JOB_STATUSES = frozenset({"done", "error", "cancelled", "canceled"})


def is_terminal_job_status(status: str | None) -> bool:
    return str(status or "").strip().lower() in TERMINAL_JOB_STATUSES


def init_job_store(store: "RedisJobStore") -> "RedisJobStore":
    global _JOB_STORE
    _JOB_STORE = store
    return store


def get_job_store() -> "RedisJobStore":
    if _JOB_STORE is None:
        raise RuntimeError("Job store no inicializado")
    return _JOB_STORE


class RedisJobStore:
    def __init__(self, async_client, sync_client):
        self.async_client = async_client
        self.sync_client = sync_client
        self.key_prefix = os.getenv("COMPARE_JOB_KEY_PREFIX", "compare:job:")
        self.progress_ttl = int(os.getenv("COMPARE_PROGRESS_TTL_SECONDS", str(60 * 60 * 6)))
        self.result_ttl = int(os.getenv("COMPARE_RESULT_TTL_SECONDS", str(60 * 60 * 24)))
        self.cleanup_interval = int(os.getenv("COMPARE_CLEANUP_INTERVAL_SECONDS", "60"))
        self.temp_root = os.path.join(tempfile.gettempdir(), "comp_docs_compare")
        self.signature_prefix = os.getenv("COMPARE_SIGNATURE_KEY_PREFIX", "compare:signature:")

    def _key(self, sid: str) -> str:
        return f"{self.key_prefix}{sid}"

    def _base_job(self, sid: str, owner_user_id: int, job_dir: str) -> dict[str, Any]:
        now = int(time.time())
        progress = {
            "status": "queued",
            "percent": 0,
            "step": "encolado",
            "detail": "Esperando worker dedicado",
            "started_at": now,
            "ended_at": None,
            "error": None,
        }
        return {
            "sid": sid,
            "owner_user_id": int(owner_user_id),
            "job_dir": job_dir,
            "created_at": now,
            "updated_at": now,
            "progress": progress,
            "progress_expires_at": now + self.progress_ttl,
            "result": None,
            "result_expires_at": 0,
        }

    async def create_job(self, *, sid: str, owner_user_id: int, job_dir: str) -> dict[str, Any]:
        job = self._base_job(sid, owner_user_id, job_dir)
        await self.async_client.set(self._key(sid), json.dumps(job, ensure_ascii=False), ex=self.result_ttl)
        return job

    def _signature_key(self, signature: str) -> str:
        return f"{self.signature_prefix}{signature}"

    async def find_reusable_job(self, signature: str) -> Optional[dict[str, Any]]:
        sid = await self.async_client.get(self._signature_key(signature))
        if isinstance(sid, bytes):
            sid = sid.decode("utf-8")
        if not sid:
            return None
        job = await self.get_job(str(sid))
        if not job:
            return None
        status = str((job.get("progress") or {}).get("status") or "")
        if status == "error":
            return None
        return job

    async def bind_signature(self, signature: str, sid: str) -> None:
        await self.async_client.set(self._signature_key(signature), sid, ex=self.result_ttl)


    async def get_job(self, sid: str) -> Optional[dict[str, Any]]:
        raw = await self.async_client.get(self._key(sid))
        return self._decode_job(raw)

    async def save_job(self, sid: str, job: dict[str, Any], *, ttl: Optional[int] = None) -> None:
        job["updated_at"] = int(time.time())
        await self.async_client.set(self._key(sid), json.dumps(job, ensure_ascii=False), ex=ttl or self.result_ttl)

    async def update_progress(self, sid: str, *, percent: int, step: str, detail: str = "", status: Optional[str] = None) -> dict[str, Any]:
        job = await self.get_job(sid)
        if not job:
            raise KeyError(sid)
        if self._is_terminal_job(job):
            logging.info("Comparador: ignorando actualización de progreso para job terminal sid=%s status=%s", sid, (job.get("progress") or {}).get("status"))
            return job
        self._apply_progress(job, percent=percent, step=step, detail=detail, status=status)
        await self.save_job(sid, job)
        return job

    async def mark_done(self, sid: str, result: dict[str, Any]) -> dict[str, Any]:
        job = await self.get_job(sid)
        if not job:
            raise KeyError(sid)
        if self._is_terminal_job(job):
            logging.info("Comparador: ignorando mark_done para job terminal sid=%s status=%s", sid, (job.get("progress") or {}).get("status"))
            return job
        self._apply_done(job, result)
        await self.save_job(sid, job)
        return job

    def mark_failed(self, sid: str, error: str) -> Optional[dict[str, Any]]:
        job = self.get_job_sync(sid)
        if not job:
            return None
        if self._is_terminal_job(job):
            logging.info("Comparador: ignorando mark_failed para job terminal sid=%s status=%s", sid, (job.get("progress") or {}).get("status"))
            return job
        now = int(time.time())
        progress = dict(job.get("progress") or {})
        progress.update({
            "status": "error",
            "step": "error",
            "detail": error,
            "error": error,
            "ended_at": now,
        })
        job["progress"] = progress
        job["progress_expires_at"] = now + self.progress_ttl
        self.save_job_sync(sid, job)
        return job

    def get_job_sync(self, sid: str) -> Optional[dict[str, Any]]:
        raw = self.sync_client.get(self._key(sid))
        return self._decode_job(raw)

    def save_job_sync(self, sid: str, job: dict[str, Any], *, ttl: Optional[int] = None) -> dict[str, Any]:
        job["updated_at"] = int(time.time())
        self.sync_client.set(self._key(sid), json.dumps(job, ensure_ascii=False), ex=ttl or self.result_ttl)
        return job

    def update_progress_sync(self, sid: str, *, percent: int, step: str, detail: str = "", status: Optional[str] = None) -> Optional[dict[str, Any]]:
        job = self.get_job_sync(sid)
        if not job:
            return None
        if self._is_terminal_job(job):
            logging.info("Comparador: ignorando update_progress_sync para job terminal sid=%s status=%s", sid, (job.get("progress") or {}).get("status"))
            return job
        self._apply_progress(job, percent=percent, step=step, detail=detail, status=status)
        return self.save_job_sync(sid, job)

    def persist_result_snapshot_sync(self, sid: str, result: dict[str, Any], *, ttl: Optional[int] = None) -> Optional[dict[str, Any]]:
        job = self.get_job_sync(sid)
        if not job:
            return None
        now = int(time.time())
        job["result"] = result
        job["result_expires_at"] = now + self.result_ttl
        job["progress_expires_at"] = now + self.progress_ttl
        return self.save_job_sync(sid, job, ttl=ttl)

    def mark_done_sync(self, sid: str, result: dict[str, Any]) -> Optional[dict[str, Any]]:
        job = self.get_job_sync(sid)
        if not job:
            return None
        if self._is_terminal_job(job):
            logging.info("Comparador: ignorando mark_done_sync para job terminal sid=%s status=%s", sid, (job.get("progress") or {}).get("status"))
            return job
        self._apply_done(job, result)
        return self.save_job_sync(sid, job)

    async def run_cleanup_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.cleanup_once()
            except Exception:
                logging.exception("Fallo durante la limpieza de jobs expirados")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.cleanup_interval)
            except asyncio.TimeoutError:
                continue

    async def cleanup_once(self) -> None:
        now = int(time.time())
        cursor = 0
        pattern = f"{self.key_prefix}*"
        while True:
            cursor, keys = await self.async_client.scan(cursor=cursor, match=pattern, count=200)
            for key in keys:
                raw = await self.async_client.get(key)
                job = self._decode_job(raw)
                if not job:
                    continue
                expires_at = int(job.get("result_expires_at") or 0)
                progress_expires_at = int(job.get("progress_expires_at") or 0)
                if expires_at and expires_at > now:
                    continue
                if not expires_at and progress_expires_at and progress_expires_at > now:
                    continue
                job_dir = job.get("job_dir")
                if job_dir and os.path.isdir(job_dir):
                    shutil.rmtree(job_dir, ignore_errors=True)
                await self.async_client.delete(key)
            if cursor == 0:
                break
        self._cleanup_temp_root(now)

    def _cleanup_temp_root(self, now: int) -> None:
        max_age_seconds = int(os.getenv("COMPARE_TEMP_FILE_TTL_SECONDS", str(self.result_ttl)))
        if not os.path.isdir(self.temp_root):
            return
        for entry in os.scandir(self.temp_root):
            try:
                age_seconds = now - int(entry.stat().st_mtime)
            except FileNotFoundError:
                continue
            if age_seconds < max_age_seconds:
                continue
            path = entry.path
            if entry.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    continue

    def get_job_status_sync(self, sid: str) -> str:
        job = self.get_job_sync(sid)
        if not isinstance(job, dict):
            return ""
        return str((job.get("progress") or {}).get("status") or "").strip().lower()

    def is_job_terminal_sync(self, sid: str) -> bool:
        return is_terminal_job_status(self.get_job_status_sync(sid))

    def _is_terminal_job(self, job: Optional[dict[str, Any]]) -> bool:
        if not isinstance(job, dict):
            return False
        return is_terminal_job_status((job.get("progress") or {}).get("status"))

    def _decode_job(self, raw: Any) -> Optional[dict[str, Any]]:
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    def _apply_progress(
        self,
        job: dict[str, Any],
        *,
        percent: int,
        step: str,
        detail: str = "",
        status: Optional[str] = None,
    ) -> None:
        progress = dict(job.get("progress") or {})
        next_status = status or progress.get("status") or "running"
        if next_status == "queued":
            next_status = status or "running"
        progress.update({
            "percent": max(0, min(100, int(percent))),
            "step": step,
            "detail": detail,
            "status": next_status,
            "error": None,
        })
        job["progress"] = progress
        job["progress_expires_at"] = int(time.time()) + self.progress_ttl

    def _apply_done(self, job: dict[str, Any], result: dict[str, Any]) -> None:
        now = int(time.time())
        progress = dict(job.get("progress") or {})
        progress.update({
            "status": "done",
            "percent": 100,
            "step": "completado",
            "detail": "Comparación finalizada",
            "ended_at": now,
            "error": None,
        })
        job["progress"] = progress
        job["progress_expires_at"] = now + self.progress_ttl
        job["result"] = result
        job["result_expires_at"] = now + self.result_ttl