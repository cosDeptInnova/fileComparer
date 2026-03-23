from __future__ import annotations

import json
from pathlib import Path

from redis import Redis
from rq import Queue

from app.settings import settings


def redis_connection() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


def compare_queue() -> Queue:
    return Queue(
        settings.rq_queue_name,
        connection=redis_connection(),
        default_timeout=int(settings.llm_timeout_seconds * 4),
    )


def job_key(sid: str) -> str:
    return f"compare:job:{sid}"


def update_job_state(sid: str, **fields: object) -> None:
    payload = {
        key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        for key, value in fields.items()
    }
    connection = redis_connection()
    if payload:
        connection.hset(job_key(sid), mapping=payload)
    connection.expire(job_key(sid), 60 * 60 * 24)


def read_job_state(sid: str) -> dict[str, object]:
    raw = redis_connection().hgetall(job_key(sid))
    parsed: dict[str, object] = {}
    for key, value in raw.items():
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
