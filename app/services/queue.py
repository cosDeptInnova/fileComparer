from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from redis import Redis
from rq import Queue

from app.settings import settings


def _decode_if_bytes(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def redis_connection() -> Redis:
    # RQ almacena payloads binarios (pickles/zlib) y necesita recibir bytes desde Redis.
    # Si decode_responses=True, redis-py convierte respuestas a str y RQ termina intentando
    # hacer .decode() sobre ellas o decodificando binario arbitrario como UTF-8.
    return Redis.from_url(settings.redis_url, decode_responses=False)


def compare_queue() -> Queue:
    return Queue(
        settings.rq_queue_name,
        connection=redis_connection(),
        default_timeout=int(settings.llm_timeout_seconds * 4),
    )


def job_key(sid: str) -> str:
    return f"compare:job:{sid}"


def update_job_state(job_id: str, **fields: object) -> None:
    normalized_fields = {"sid": job_id, **fields}
    payload = {
        key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        for key, value in normalized_fields.items()
    }
    connection = redis_connection()
    if payload:
        connection.hset(job_key(job_id), mapping=payload)
    connection.expire(job_key(job_id), 60 * 60 * 24)


def read_job_state(sid: str) -> dict[str, object]:
    raw = redis_connection().hgetall(job_key(sid))
    parsed: dict[str, object] = {}
    for raw_key, raw_value in raw.items():
        key = _decode_if_bytes(raw_key)
        value = _decode_if_bytes(raw_value)
        try:
            parsed[key] = json.loads(value)
        except Exception:
            parsed[key] = value
    return parsed


def persist_job_result(sid: str, payload: dict[str, object]) -> Path:
    target_dir = settings.data_dir / sid
    target_dir.mkdir(parents=True, exist_ok=True)
    output_path = target_dir / settings.result_file_name
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def load_job_result(sid: str) -> dict[str, object] | None:
    output_path = settings.data_dir / sid / settings.result_file_name
    if not output_path.exists():
        return None
    return json.loads(output_path.read_text(encoding="utf-8"))
