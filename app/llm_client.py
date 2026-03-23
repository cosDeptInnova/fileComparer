from __future__ import annotations

import json
import logging
from collections.abc import Callable
import os
import time
import uuid
from typing import Any

import httpx

from .llm_runtime import DEFAULT_LLAMA_BASE_URL, DEFAULT_MODEL_NAME, resolve_llm_runtime_settings
from .metrics import observe_inference_concurrency, observe_llm_duration
from .runtime_controls import RedisSlotSemaphore

LOGGER = logging.getLogger(__name__)
DEFAULT_TIMEOUT_SECONDS = 120.0


class CompareInferenceAborted(RuntimeError):
    """Raised when a compare job reached a terminal state before another inference call."""


class LLMClient:
    """Cliente reutilizable para llama.cpp / endpoints OpenAI-compatible."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        model_name: str | None = None,
        sync_client=None,
        inference_semaphore: RedisSlotSemaphore | None = None,
        should_abort: Callable[[], bool] | None = None,
    ) -> None:
        runtime = resolve_llm_runtime_settings()
        self.base_url = str(base_url or runtime.get("base_url") or DEFAULT_LLAMA_BASE_URL).rstrip("/")
        self.api_key = str(api_key or runtime.get("api_key") or "").strip()
        self.timeout = float(timeout or DEFAULT_TIMEOUT_SECONDS)
        self.model_name = str(model_name or runtime.get("model_name") or DEFAULT_MODEL_NAME).strip() or DEFAULT_MODEL_NAME
        self._client: httpx.Client | None = None
        max_inference = max(1, int(os.getenv("COMPARE_MAX_INFERENCE_CONCURRENCY", "1")))
        self.should_abort = should_abort
        self.inference_semaphore = inference_semaphore or RedisSlotSemaphore(
            sync_client,
            name=os.getenv("COMPARE_INFERENCE_SEMAPHORE_KEY", "compare:inference"),
            slots=max_inference,
            ttl_seconds=int(os.getenv("COMPARE_INFERENCE_SEMAPHORE_TTL_SECONDS", "900")),
            wait_sleep_seconds=float(os.getenv("COMPARE_INFERENCE_WAIT_SLEEP_SECONDS", "0.05")),
        )

    def startup(self) -> None:
        if self._client is None:
            self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    def shutdown(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "LLMClient":
        self.startup()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()

    def _headers(self) -> dict[str, str] | None:
        if not self.api_key:
            return None
        return {"Authorization": f"Bearer {self.api_key}"}

    def _raise_if_aborted(self, *, stage: str) -> None:
        if callable(self.should_abort) and self.should_abort():
            LOGGER.info(
                "LLM compare request aborted before stage=%s model=%s base_url=%s",
                stage,
                self.model_name,
                self.base_url,
            )
            raise CompareInferenceAborted(f"compare_inference_aborted:{stage}")

    def chat_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
        max_tokens: int = 700,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._client is None:
            self.startup()
        self._raise_if_aborted(stage="before_request_build")
        request_id = uuid.uuid4().hex[:8]
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        if schema:
            payload["extra_body"] = {"json_schema": schema}
        started = time.perf_counter()
        lease = self.inference_semaphore.acquire(
            timeout_seconds=float(os.getenv("COMPARE_INFERENCE_ACQUIRE_TIMEOUT_SECONDS", "600"))
        )
        if self.inference_semaphore.enabled() and lease is None:
            raise TimeoutError("compare_inference_slot_timeout")
        try:
            self._raise_if_aborted(stage="before_http_post")
            observe_inference_concurrency(self.inference_semaphore.active_count())
            response = self._client.post("/chat/completions", json=payload, headers=self._headers())
            LOGGER.info(
                "LLM compare request %s finished in %.3fs status=%s active_inference=%s lease=%s",
                request_id,
                time.perf_counter() - started,
                response.status_code,
                self.inference_semaphore.active_count(),
                bool(lease),
            )
            response.raise_for_status()
            return response.json()
        finally:
            self.inference_semaphore.release(lease)

    def chat_json(
        self,
        *,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 700,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        outcome = "invalid_json"
        try:
            payload = self.chat_completion(messages=messages, schema=schema, temperature=temperature, max_tokens=max_tokens)
            data = _extract_json_message(payload)
            outcome = "ok"
            return data
        except Exception:
            outcome = "error"
            raise
        finally:
            observe_llm_duration(time.perf_counter() - started, outcome=outcome)
            observe_inference_concurrency(self.inference_semaphore.active_count())


def _extract_json_message(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("llm_invalid_payload")
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] or {}
        message = first.get("message") if isinstance(first, dict) else None
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, dict):
                if isinstance(content.get("json"), dict):
                    return content["json"]
                content = content.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and isinstance(item.get("text"), str) and item.get("text").strip():
                        return json.loads(item["text"])
            if isinstance(content, str) and content.strip():
                return json.loads(content)
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for tool in tool_calls:
                    fn = tool.get("function") if isinstance(tool, dict) else None
                    args = fn.get("arguments") if isinstance(fn, dict) else None
                    if isinstance(args, str) and args.strip():
                        return json.loads(args)
        text = first.get("text") if isinstance(first, dict) else None
        if isinstance(text, str) and text.strip():
            return json.loads(text)
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            content = item.get("content") if isinstance(item, dict) else None
            if isinstance(content, list):
                for subitem in content:
                    if isinstance(subitem, dict) and isinstance(subitem.get("text"), str) and subitem.get("text").strip():
                        return json.loads(subitem["text"])
    raise ValueError("llm_invalid_json")