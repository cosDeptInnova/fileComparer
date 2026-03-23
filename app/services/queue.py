from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.settings import settings

if TYPE_CHECKING:
    from redis import Redis
    from rq import Queue


def redis_connection() -> Redis:
    from redis import Redis

    return Redis.from_url(settings.redis_url, decode_responses=False)


def compare_queue() -> Queue:
    from rq import Queue

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
    for key, value in raw.items():
        normalized_key = _decode_redis_scalar(key)
        normalized_value = _decode_redis_scalar(value)
        try:
            parsed[normalized_key] = json.loads(normalized_value)
        except Exception:
            parsed[normalized_key] = normalized_value
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


def _decode_redis_scalar(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
