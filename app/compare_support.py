import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from .utils import get_redis_conversation_client


@dataclass(frozen=True)
class CompareTracePolicy:
    max_text_length: int = 512
    sensitive_keys: tuple[str, ...] = (
        "content",
        "text",
        "text_a",
        "text_b",
        "display_text_a",
        "display_text_b",
        "raw_result",
        "authorization",
        "cookie",
        "csrf",
        "api_key",
        "client_secret",
        "path",
        "job_dir",
    )

    def sanitize_audit_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        def sanitize(value: Any, *, key: str = ""):
            if isinstance(value, dict):
                return {str(k): sanitize(v, key=str(k)) for k, v in value.items()}
            if isinstance(value, list):
                return [sanitize(item, key=key) for item in value]
            if isinstance(value, str):
                if key and any(token in key.lower() for token in self.sensitive_keys):
                    return self._redact_string(value)
                compact = " ".join(value.split())
                if len(compact) > self.max_text_length:
                    return compact[: self.max_text_length - 1] + "…"
                return compact
            return value

        return sanitize(payload)

    def _redact_string(self, value: str) -> str:
        compact = " ".join(str(value or "").split())
        if not compact:
            return ""
        return f"[REDACTED len={len(compact)}]"


_TRACE_POLICY = CompareTracePolicy()


def get_compare_trace_policy() -> CompareTracePolicy:
    return _TRACE_POLICY


def build_compare_event(
    event_name: str,
    sid: str,
    engine: str,
    files: list[str],
    status: str,
    **extra: Any,
) -> dict[str, Any]:
    event = {
        "event": event_name,
        "sid": sid,
        "engine": engine,
        "files": files,
        "status": status,
        "ts": int(time.time()),
    }
    event.update(extra)
    return _TRACE_POLICY.sanitize_audit_payload(event)


async def persist_compare_event_async(conv_id_redis: int | None, payload: dict[str, Any], *, sid: str, event_name: str) -> None:
    try:
        client = get_redis_conversation_client()
        key = f"compare:events:{conv_id_redis or 'anon'}:{sid}"
        await client.rpush(key, json.dumps(payload, ensure_ascii=False))
        await client.expire(key, 86400)
    except Exception:
        logging.exception("No se pudo persistir el evento %s del job %s", event_name, sid)
