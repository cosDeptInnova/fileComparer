from __future__ import annotations

import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compare_queue import RedisCompareQueue
from .compare_tasks import CompareTaskPayload
from .document_compare.pipeline import CompareDocumentsService
from .job_store import RedisJobStore
from .llm_client import CompareInferenceAborted, LLMClient
from .llm_runtime import DEFAULT_MODEL_NAME, resolve_llm_runtime_settings
from .metrics import record_job_event, set_compare_active_workers, set_compare_queue_depth
from .pair_cache import ComparePairCache
from .runtime_controls import RedisSlotSemaphore

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class WorkerSettings:
    concurrency: int
    max_inflight_jobs: int
    heartbeat_interval: float
    dequeue_timeout_seconds: int
    queue_prefetch: int
    local_model_name: str
    llm_base_url: str
    llm_api_key: str


def _load_settings() -> WorkerSettings:
    runtime = resolve_llm_runtime_settings()
    concurrency = max(1, int(os.getenv("COMPARE_WORKER_CONCURRENCY", "2")))
    return WorkerSettings(
        concurrency=concurrency,
        max_inflight_jobs=max(
            concurrency,
            int(os.getenv("COMPARE_MAX_INFLIGHT_JOBS", str(concurrency))),
        ),
        heartbeat_interval=max(1.0, float(os.getenv("COMPARE_WORKER_HEARTBEAT_SECONDS", "5"))),
        dequeue_timeout_seconds=max(1, int(os.getenv("COMPARE_WORKER_DEQUEUE_TIMEOUT_SECONDS", "2"))),
        queue_prefetch=max(1, int(os.getenv("COMPARE_QUEUE_PREFETCH", "1"))),
        local_model_name=str(runtime.get("model_name") or DEFAULT_MODEL_NAME),
        llm_base_url=str(runtime.get("base_url") or ""),
        llm_api_key=str(runtime.get("api_key") or ""),
    )


class CompareWorkerService:
    def __init__(self, *, queue: RedisCompareQueue | None = None, job_store: RedisJobStore | None = None) -> None:
        self.settings = _load_settings()
        self.job_store = job_store or _build_job_store()
        self.queue = queue or RedisCompareQueue(self.job_store.sync_client, self.job_store)
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self._stop = threading.Event()
        self.execution_semaphore = RedisSlotSemaphore(
            self.job_store.sync_client,
            name=os.getenv("COMPARE_EXECUTION_SEMAPHORE_KEY", "compare:execution"),
            slots=self.settings.max_inflight_jobs,
            ttl_seconds=int(os.getenv("COMPARE_EXECUTION_SEMAPHORE_TTL_SECONDS", "7200")),
            wait_sleep_seconds=float(os.getenv("COMPARE_EXECUTION_WAIT_SLEEP_SECONDS", "0.1")),
        )
        self._last_orphan_recovery_at = 0.0

    def run_forever(self) -> None:
        self.queue.ensure_group()
        self.queue.register_worker(self.worker_id, concurrency=self.settings.concurrency)
        LOGGER.info(
            "Compare worker started worker_id=%s concurrency=%s max_inflight_jobs=%s",
            self.worker_id,
            self.settings.concurrency,
            self.settings.max_inflight_jobs,
        )
        try:
            while not self._stop.is_set():
                self.queue.heartbeat(
                    self.worker_id,
                    active_jobs=0,
                    concurrency=self.settings.concurrency,
                )
                set_compare_active_workers(self.queue.count_active_workers())
                set_compare_queue_depth(self.queue.depth())
                self._maybe_recover_orphaned_tasks()

                execution_lease = self.execution_semaphore.acquire(blocking=False)
                if self.execution_semaphore.enabled() and execution_lease is None:
                    time.sleep(0.1)
                    continue
                try:
                    payload, _entry = self.queue.dequeue_claim(worker_id=self.worker_id, timeout_seconds=self.settings.dequeue_timeout_seconds)
                    if payload is None:
                        continue
                    self._process(payload)
                finally:
                    self.execution_semaphore.release(execution_lease)
        finally:
            self.queue.unregister_worker(self.worker_id)
            set_compare_active_workers(self.queue.count_active_workers())

    def _maybe_recover_orphaned_tasks(self) -> None:
        now = time.monotonic()
        min_interval = max(self.settings.heartbeat_interval, float(self.settings.dequeue_timeout_seconds), 1.0)
        if now - self._last_orphan_recovery_at < min_interval:
            return
        self._last_orphan_recovery_at = now
        try:
            recovered = self.queue.recover_orphaned_tasks()
        except Exception:
            LOGGER.exception("Compare worker orphan recovery failed worker_id=%s", self.worker_id)
            return
        if recovered > 0:
            LOGGER.warning(
                "Compare worker recovered orphaned tasks worker_id=%s recovered=%s",
                self.worker_id,
                recovered,
            )
            set_compare_queue_depth(self.queue.depth())

    def _job_is_terminal(self, sid: str) -> bool:
        try:
            return bool(getattr(self.job_store, "is_job_terminal_sync", lambda _sid: False)(sid))
        except Exception:
            LOGGER.exception("Compare worker failed checking terminal state sid=%s", sid)
            return False

    def _start_task_heartbeat(self, sid: str) -> tuple[threading.Event, threading.Thread]:
        stop_event = threading.Event()

        def _loop() -> None:
            while not stop_event.wait(self.settings.heartbeat_interval):
                try:
                    self.queue.heartbeat(
                        self.worker_id,
                        active_jobs=1,
                        concurrency=self.settings.concurrency,
                    )
                    self.queue.touch_task(sid, worker_id=self.worker_id)
                except Exception:
                    LOGGER.exception(
                        "Compare worker heartbeat loop failed worker_id=%s sid=%s",
                        self.worker_id,
                        sid,
                    )

        thread = threading.Thread(
            target=_loop,
            name=f"compare-worker-heartbeat-{self.worker_id}",
            daemon=True,
        )
        self.queue.heartbeat(
            self.worker_id,
            active_jobs=1,
            concurrency=self.settings.concurrency,
        )
        self.queue.touch_task(sid, worker_id=self.worker_id)
        thread.start()
        return stop_event, thread

    def _process(self, task: CompareTaskPayload) -> None:
        if self._job_is_terminal(task.sid):
            LOGGER.info("Compare worker skipping already-terminal job before processing sid=%s worker_id=%s", task.sid, self.worker_id)
            self.queue.release_task(task.sid)
            return
        record_job_event("worker:job_started")
        self.job_store.update_progress_sync(task.sid, percent=10, step="extraccion", detail="Procesando comparación")
        heartbeat_stop, heartbeat_thread = self._start_task_heartbeat(task.sid)
        llm_client = None
        if self.settings.llm_base_url:
            llm_client = LLMClient(
                base_url=self.settings.llm_base_url,
                api_key=self.settings.llm_api_key,
                model_name=self.settings.local_model_name,
                timeout=float(os.getenv("COMPARE_LLM_TIMEOUT_SECONDS", "120")),
                sync_client=self.job_store.sync_client,
                should_abort=lambda sid=task.sid: self._job_is_terminal(sid),
            )
        service = CompareDocumentsService(
            llm_client=llm_client,
            pair_cache=ComparePairCache(),
            should_abort=lambda sid=task.sid: self._job_is_terminal(sid),
        )
        try:
            started = time.perf_counter()
            result = service.compare_documents(
                file_a_path=task.file_a_path,
                file_b_path=task.file_b_path,
                file_a_name=task.file_a_name,
                file_b_name=task.file_b_name,
                opts=task.opts,
                progress_cb=lambda percent, step, detail: self.job_store.update_progress_sync(task.sid, percent=percent, step=step, detail=detail),
            )
            diagnostics = result.setdefault("diagnostics", {})
            diagnostics.setdefault("worker", {})
            diagnostics["worker"].update(
                {
                    "worker_id": self.worker_id,
                    "queue_prefetch": self.settings.queue_prefetch,
                    "worker_concurrency_limit": self.settings.concurrency,
                    "cluster_inflight_limit": self.settings.max_inflight_jobs,
                    "elapsed_seconds": round(time.perf_counter() - started, 4),
                }
            )
            report_path = Path(task.job_dir) / "informe.pdf"
            report_path.write_text(__import__("json").dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            result["report_path"] = str(report_path)
            result["report_file"] = "informe.pdf"
            result["report_download_url"] = f"/api/comparador/descargar/{task.sid}/informe.pdf"
            result["export_json_url"] = f"/api/comparador/resultado/{task.sid}/export.json"
            self.job_store.mark_done_sync(task.sid, result)
            record_job_event("worker:job_done")
        except CompareInferenceAborted as exc:
            if self._job_is_terminal(task.sid):
                LOGGER.info("Compare worker aborted post-terminal inference sid=%s worker_id=%s reason=%s", task.sid, self.worker_id, exc)
            else:
                LOGGER.warning("Compare worker aborted inference without terminal state sid=%s worker_id=%s reason=%s", task.sid, self.worker_id, exc)
                self.job_store.mark_failed(task.sid, f"compare_internal_error: {exc}")
                record_job_event("worker:job_failed")
        except Exception as exc:
            LOGGER.exception("Compare worker failed sid=%s", task.sid)
            self.job_store.mark_failed(task.sid, f"compare_internal_error: {exc}")
            record_job_event("worker:job_failed")
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=max(1.0, self.settings.heartbeat_interval))
            if llm_client is not None:
                llm_client.shutdown()
            self.queue.release_task(task.sid)
            self.queue.heartbeat(
                self.worker_id,
                active_jobs=0,
                concurrency=self.settings.concurrency,
            )
            set_compare_queue_depth(self.queue.depth())


def _build_job_store() -> RedisJobStore:
    import redis
    import redis.asyncio as aioredis

    socket_timeout = float(os.getenv("REDIS_SOCKET_TIMEOUT_SECONDS", "5"))
    kwargs: dict[str, Any] = {
        "host": os.getenv("REDIS_HOST", "localhost"),
        "port": int(os.getenv("REDIS_PORT", "6379")),
        "password": os.getenv("REDIS_PASSWORD") or None,
        "socket_connect_timeout": socket_timeout,
        "socket_timeout": socket_timeout,
        "decode_responses": True,
        "db": int(os.getenv("REDIS_DB", "0")),
    }
    async_client = aioredis.Redis(**kwargs)
    sync_client = redis.Redis(**kwargs)
    return RedisJobStore(async_client, sync_client)


def create_compare_worker_service() -> CompareWorkerService:
    return CompareWorkerService()


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("COMP_DOCS_LOG_LEVEL", "INFO"))
    create_compare_worker_service().run_forever()