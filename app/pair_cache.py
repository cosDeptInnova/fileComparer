from __future__ import annotations

import hashlib
import json
import os
from typing import Any


class ComparePairCache:
    def __init__(self, sync_client=None) -> None:
        self.sync_client = sync_client
        self.key_prefix = os.getenv("COMPARE_PAIR_CACHE_PREFIX", "compare:pair-cache:")
        self.ttl_seconds = int(os.getenv("COMPARE_PAIR_CACHE_TTL_SECONDS", str(60 * 60 * 24 * 7)))
        self._memory: dict[str, dict[str, Any]] = {}

    def build_key(
        self,
        *,
        normalized_a: str,
        normalized_b: str,
        prompt_version: str,
        pipeline_version: str,
        model_name: str,
        config: dict[str, Any] | None = None,
    ) -> str:
        payload = {
            "a": normalized_a,
            "b": normalized_b,
            "prompt_version": prompt_version,
            "pipeline_version": pipeline_version,
            "model_name": model_name,
            "config": config or {},
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"{self.key_prefix}{digest}"

    def get(self, key: str) -> dict[str, Any] | None:
        if self.sync_client is None:
            value = self._memory.get(key)
            return dict(value) if isinstance(value, dict) else None
        raw = self.sync_client.get(key)
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def set(self, key: str, value: dict[str, Any]) -> None:
        payload = dict(value or {})
        if self.sync_client is None:
            self._memory[key] = payload
            return
        self.sync_client.set(key, json.dumps(payload, ensure_ascii=False), ex=self.ttl_seconds)
