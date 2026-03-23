from __future__ import annotations

import ast
import json
import logging
from collections.abc import Callable
import os
import time
import uuid
from json import JSONDecodeError
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
            LOGGER.info("Inicializando LLMClient con base_url=%s model=%s", self.base_url, self.model_name)
            self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    def shutdown(self) -> None:
        if self._client is not None:
            LOGGER.info("Cerrando LLMClient")
            self._client.close()
            self._client = None

    def __enter__(self) -> "LLMClient":
        self.startup()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()

    def _build_headers(self) -> dict[str, str] | None:
        if not self.api_key:
            return None
        auth_value = f"Bearer {self.api_key}"
        try:
            auth_value.encode("ascii")
        except UnicodeEncodeError:
            LOGGER.error(
                "LLAMA_SERVER_API_KEY contiene caracteres no ASCII; se omite Authorization para evitar errores de codificación."
            )
            return None
        return {"Authorization": auth_value}

    def _raise_if_aborted(self, *, stage: str) -> None:
        if callable(self.should_abort) and self.should_abort():
            LOGGER.info(
                "LLM compare request aborted before stage=%s model=%s base_url=%s",
                stage,
                self.model_name,
                self.base_url,
            )
            raise CompareInferenceAborted(f"compare_inference_aborted:{stage}")

    def _build_json_response_format(self, schema: dict[str, Any] | None) -> dict[str, Any]:
        if not schema:
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "document_compare_response",
                "strict": True,
                "schema": schema,
            },
        }

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
            "response_format": self._build_json_response_format(schema),
        }
        if schema:
            payload["extra_body"] = {
                "json_schema": schema,
                "guided_json": schema,
            }
        started = time.perf_counter()
        lease = self.inference_semaphore.acquire(
            timeout_seconds=float(os.getenv("COMPARE_INFERENCE_ACQUIRE_TIMEOUT_SECONDS", "600"))
        )
        if self.inference_semaphore.enabled() and lease is None:
            raise TimeoutError("compare_inference_slot_timeout")
        try:
            self._raise_if_aborted(stage="before_http_post")
            observe_inference_concurrency(self.inference_semaphore.active_count())
            response = self._client.post("chat/completions", json=payload, headers=self._build_headers())
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
        max_attempts = max(1, int(os.getenv("COMPARE_LLM_JSON_MAX_ATTEMPTS", "3")))
        prompt_messages = list(messages)
        last_exc: Exception | None = None
        try:
            for attempt in range(1, max_attempts + 1):
                try:
                    payload = self.chat_completion(
                        messages=prompt_messages,
                        schema=schema,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                    data = _extract_json_message(payload)
                    outcome = "ok"
                    return data
                except ValueError as exc:
                    last_exc = exc
                    if str(exc) != "llm_invalid_json" or attempt >= max_attempts:
                        outcome = "error"
                        raise
                    LOGGER.warning(
                        "LLM compare request returned invalid JSON on attempt=%s/%s model=%s; retrying with stricter reminder",
                        attempt,
                        max_attempts,
                        self.model_name,
                    )
                    prompt_messages = _with_json_retry_reminder(messages)
            outcome = "error"
            raise last_exc or ValueError("llm_invalid_json")
        except Exception:
            outcome = "error"
            raise
        finally:
            observe_llm_duration(time.perf_counter() - started, outcome=outcome)
            observe_inference_concurrency(self.inference_semaphore.active_count())

    def health_check(self) -> bool:
        if self._client is None:
            self.startup()
        try:
            response = self._client.get("health")
            response.raise_for_status()
            return True
        except Exception as exc:
            LOGGER.warning("Health check a llama.cpp falló: %s", exc)
            return False


def _with_json_retry_reminder(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reminder = {
        "role": "system",
        "content": (
            "IMPORTANTE: responde exclusivamente con un único objeto JSON válido. "
            "No uses markdown, fences, comentarios, explicaciones, prefijos ni sufijos. "
            "Si no estás seguro de un campo, devuélvelo vacío pero mantén JSON válido."
        ),
    }
    return [*messages, reminder]


def _extract_json_message(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("llm_invalid_payload")

    candidate_summaries: list[str] = []
    for candidate in _iter_json_candidates(payload):
        candidate_summaries.append(_summarize_candidate(candidate))
        parsed = _parse_json_candidate(candidate)
        if isinstance(parsed, dict):
            return parsed

    LOGGER.warning(
        "LLM compare response did not contain valid JSON. payload_keys=%s candidates=%s",
        sorted(payload.keys()),
        candidate_summaries[:8],
    )
    raise ValueError("llm_invalid_json")


def _iter_json_candidates(payload: dict[str, Any]):
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if isinstance(message, dict):
                refusal = message.get("refusal")
                if isinstance(refusal, str) and refusal.strip():
                    LOGGER.warning("LLM compare request refused structured output: %s", refusal.strip())
                yield from _iter_message_content_candidates(message)
            text = choice.get("text")
            if isinstance(text, (str, dict, list)):
                yield text

    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, (str, dict, list)):
                yield content

    for key in ("content", "text", "response", "generated_text"):
        value = payload.get(key)
        if isinstance(value, (str, dict, list)):
            yield value


def _iter_message_content_candidates(message: dict[str, Any]):
    content = message.get("content")
    if isinstance(content, dict):
        json_content = content.get("json")
        if isinstance(json_content, dict):
            yield json_content
        for key in ("content", "text"):
            nested = content.get(key)
            if isinstance(nested, (str, dict, list)):
                yield nested
    elif isinstance(content, (str, list)):
        yield content
        joined_content = _join_text_fragments(content)
        if joined_content:
            yield joined_content

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for tool in tool_calls:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function")
            args = fn.get("arguments") if isinstance(fn, dict) else None
            if isinstance(args, (str, dict, list)):
                yield args



def _parse_json_candidate(candidate: Any) -> dict[str, Any] | None:
    if isinstance(candidate, dict):
        direct_json = candidate.get("json")
        if isinstance(direct_json, dict):
            return direct_json
        for key in ("arguments", "content", "text", "output", "input"):
            nested = candidate.get(key)
            parsed = _parse_json_candidate(nested)
            if isinstance(parsed, dict):
                return parsed
        return candidate
    if isinstance(candidate, list):
        for item in candidate:
            if isinstance(item, dict):
                item_type = str(item.get("type") or "").strip().lower()
                if item_type in {"json", "output_json"} and isinstance(item.get("json"), dict):
                    return item["json"]
                for key in ("json", "text", "content", "arguments"):
                    nested = item.get(key)
                    parsed = _parse_json_candidate(nested)
                    if isinstance(parsed, dict):
                        return parsed
            elif isinstance(item, str):
                parsed = _loads_maybe_embedded_json(item)
                if isinstance(parsed, dict):
                    return parsed
        joined = _join_text_fragments(candidate)
        if joined:
            parsed = _loads_maybe_embedded_json(joined)
            if isinstance(parsed, dict):
                return parsed
        return None
    if isinstance(candidate, str):
        return _loads_maybe_embedded_json(candidate)
    return None



def _loads_maybe_embedded_json(raw: str) -> dict[str, Any] | None:
    text = _strip_reasoning_markup(raw or "").strip()
    if not text:
        return None

    direct = _try_json_loads(text)
    if isinstance(direct, dict):
        return direct

    fenced = text
    if fenced.startswith("```"):
        fenced = _strip_markdown_code_fence(fenced)
        direct = _try_json_loads(fenced)
        if isinstance(direct, dict):
            return direct

    embedded = _extract_first_json_object(text)
    if embedded:
        parsed = _try_json_loads(embedded)
        if isinstance(parsed, dict):
            return parsed
    trailing_embedded = _extract_last_json_object(text)
    if trailing_embedded and trailing_embedded != embedded:
        parsed = _try_json_loads(trailing_embedded)
        if isinstance(parsed, dict):
            return parsed

    literal = _try_python_dict_literal(text)
    if isinstance(literal, dict):
        return literal
    return None



def _try_json_loads(raw: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
    except JSONDecodeError:
        return None
    if isinstance(parsed, str):
        nested = parsed.strip()
        if nested and nested != raw:
            reparsed = _try_json_loads(nested)
            if isinstance(reparsed, dict):
                return reparsed
    return parsed if isinstance(parsed, dict) else None


def _try_python_dict_literal(raw: str) -> dict[str, Any] | None:
    try:
        parsed = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return None
    return parsed if isinstance(parsed, dict) else None



def _strip_markdown_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()



def _extract_first_json_object(text: str) -> str | None:
    start = text.find("{")
    while start >= 0:
        extracted = _extract_json_object_from_index(text, start)
        if extracted:
            return extracted
        start = text.find("{", start + 1)
    return None


def _extract_last_json_object(text: str) -> str | None:
    for start in range(len(text) - 1, -1, -1):
        if text[start] != "{":
            continue
        extracted = _extract_json_object_from_index(text, start)
        if extracted:
            return extracted
    return None


def _extract_json_object_from_index(text: str, start: int) -> str | None:
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _join_text_fragments(candidate: Any) -> str:
    fragments: list[str] = []
    _collect_text_fragments(candidate, fragments)
    return "".join(fragments).strip()


def _collect_text_fragments(candidate: Any, fragments: list[str]) -> None:
    if isinstance(candidate, str):
        fragments.append(candidate)
        return
    if isinstance(candidate, list):
        for item in candidate:
            _collect_text_fragments(item, fragments)
        return
    if not isinstance(candidate, dict):
        return
    for key in ("text", "content", "arguments", "output_text"):
        value = candidate.get(key)
        if isinstance(value, (str, list, dict)):
            _collect_text_fragments(value, fragments)


def _strip_reasoning_markup(text: str) -> str:
    stripped = text.strip()
    think_open = stripped.lower().find("<think>")
    think_close = stripped.lower().rfind("</think>")
    if think_open >= 0 and think_close > think_open:
        without_think = (stripped[:think_open] + stripped[think_close + len("</think>") :]).strip()
        if without_think:
            return without_think
    return stripped


def _summarize_candidate(candidate: Any) -> str:
    if isinstance(candidate, dict):
        return f"dict(keys={sorted(candidate.keys())[:6]})"
    if isinstance(candidate, list):
        joined = _join_text_fragments(candidate)
        return f"list(len={len(candidate)}, joined={joined[:120]!r})"
    if isinstance(candidate, str):
        compact = " ".join(candidate.split())
        return f"str({compact[:120]!r})"
    return type(candidate).__name__
