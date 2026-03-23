from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

try:
    from redis import Redis
except Exception as exc:  # pragma: no cover - allows import without redis in lightweight environments
    Redis = None  # type: ignore[assignment]
    _REDIS_IMPORT_ERROR = exc
else:
    _REDIS_IMPORT_ERROR = None

from app.celery_app import celery_app
from app.settings import settings


class CeleryQueue:
    def __init__(self, name: str):
        self.name = name

    def enqueue(self, func: Any, *args: object, job_id: str | None = None, **kwargs: object):
        if not hasattr(func, "apply_async"):
            raise RuntimeError(
                f"La tarea {getattr(func, '__name__', func)!r} no está registrada como task de Celery."
            )
        return func.apply_async(args=args, kwargs=kwargs, task_id=job_id, queue=self.name)


def _decode_if_bytes(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def redis_connection() -> Redis:
    if Redis is None:
        raise RuntimeError(
            "La dependencia 'redis' no está instalada. Instala redis-py en el entorno antes de arrancar comp_docs. "
            f"Error original: {_REDIS_IMPORT_ERROR}"
        )
    return Redis.from_url(settings.redis_url, decode_responses=False)


def _require_queue_connection(connection: Redis | None = None) -> Redis:
    conn = connection or redis_connection()
    if hasattr(conn, "ping"):
        try:
            conn.ping()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "No se pudo conectar a Redis para la cola de comparación. "
                f"Verifica REDIS_URL={settings.redis_url!r}. Error: {exc}"
            ) from exc
    return conn


def _active_queue_names(record: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(record, list):
        for queue_data in record:
            if isinstance(queue_data, dict) and queue_data.get("name"):
                names.add(str(queue_data["name"]))
    return names


def count_queue_workers(queue_name: str, *, connection: Redis | None = None) -> int:
    _require_queue_connection(connection)
    try:
        inspector = celery_app.control.inspect(timeout=settings.celery_inspect_timeout_seconds)
        active_queues = inspector.active_queues() or {}
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"No se pudo consultar los workers Celery activos: {exc}") from exc

    active = 0
    for worker_data in active_queues.values():
        if queue_name in _active_queue_names(worker_data):
            active += 1
    return active


def ensure_queue_backend_ready(*, require_active_workers: bool | None = None, connection: Redis | None = None) -> Redis:
    conn = _require_queue_connection(connection)
    must_have_workers = settings.require_active_workers if require_active_workers is None else require_active_workers
    if must_have_workers:
        worker_count = count_queue_workers(settings.compare_queue_name, connection=conn)
        if worker_count < 1:
            raise RuntimeError(
                "No hay workers Celery activos escuchando la cola de comparación. "
                "Arranca comp_docs_worker antes de invocar /comparar. "
                f"Queue={settings.compare_queue_name!r} Redis={settings.redis_url!r}."
            )
    return conn


def compare_queue() -> CeleryQueue:
    ensure_queue_backend_ready(require_active_workers=False)
    return CeleryQueue(settings.compare_queue_name)


def job_key(sid: str) -> str:
    return f"compare:job:{sid}"


def _ensure_hash_key(connection: Redis, key: str) -> None:
    if not hasattr(connection, "type"):
        return
    raw_type = connection.type(key)
    key_type = _decode_if_bytes(raw_type).strip().lower() if raw_type is not None else "none"
    if key_type in {"none", "hash"}:
        return
    if hasattr(connection, "delete"):
        connection.delete(key)


def update_job_state(job_id: str, **fields: object) -> None:
    normalized_fields = {"sid": job_id, **fields}
    payload = {
        key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        for key, value in normalized_fields.items()
    }
    connection = redis_connection()
    key = job_key(job_id)
    _ensure_hash_key(connection, key)
    if payload:
        connection.hset(key, mapping=payload)
    connection.expire(key, 60 * 60 * 24)


def read_job_state(sid: str) -> dict[str, object]:
    connection = redis_connection()
    key = job_key(sid)
    _ensure_hash_key(connection, key)
    raw = connection.hgetall(key)
    parsed: dict[str, object] = {}
    for raw_key, raw_value in raw.items():
        key_name = _decode_if_bytes(raw_key)
        value = _decode_if_bytes(raw_value)
        try:
            parsed[key_name] = json.loads(value)
        except Exception:
            parsed[key_name] = value
    return parsed


def persist_job_result(sid: str, payload: dict[str, object]) -> Path:
    target_dir = settings.data_dir / sid
    target_dir.mkdir(parents=True, exist_ok=True)
    output_path = target_dir / settings.result_file_name
    temp_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, output_path)
    return output_path


def load_job_result(sid: str) -> dict[str, object] | None:
    output_path = settings.data_dir / sid / settings.result_file_name
    if not output_path.exists():
        return None
    return json.loads(output_path.read_text(encoding="utf-8"))
