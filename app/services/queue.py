from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from typing import TYPE_CHECKING

try:
    from redis import Redis
except Exception as exc:  # pragma: no cover - allows import without redis in lightweight environments
    Redis = None  # type: ignore[assignment]
    _REDIS_IMPORT_ERROR = exc
else:
    _REDIS_IMPORT_ERROR = None

from app.services.rq_compat import load_rq_runtime
from app.settings import settings


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
    # RQ almacena payloads binarios (pickles/zlib) y necesita recibir bytes desde Redis.
    # Si decode_responses=True, redis-py convierte respuestas a str y RQ termina intentando
    # hacer .decode() sobre ellas o decodificando binario arbitrario como UTF-8.
    return Redis.from_url(settings.redis_url, decode_responses=False)



def compare_queue():
    queue_class = load_rq_runtime()["Queue"]
    return queue_class(
        settings.rq_queue_name,
        connection=redis_connection(),
        default_timeout=int(settings.llm_timeout_seconds * 4),
    )



def job_key(sid: str) -> str:
    return f"compare:job:{sid}"



def _ensure_hash_key(connection: Redis, key: str) -> None:
    raw_type = connection.type(key)
    key_type = _decode_if_bytes(raw_type).strip().lower() if raw_type is not None else "none"
    if key_type in {"none", "hash"}:
        return
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
