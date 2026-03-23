from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

try:
    import httpx
except Exception:  # pragma: no cover - allows static tests before dependency install.
    class _FallbackHttpClient:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("httpx no está instalado; instala requirements.txt para usar llama.cpp")

    class httpx:  # type: ignore
        Client = _FallbackHttpClient

from app.settings import settings
from app.schemas import LLMComparisonResponse

logger = logging.getLogger(__name__)
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
JSON_INLINE_RE = re.compile(r"(\{.*\})", re.DOTALL)
EMPTY_PAYLOAD_ERROR = "Payload del LLM vacío."


class LLMResponseError(RuntimeError):
    pass


@dataclass(slots=True)
class LLMClient:
    base_url: str = settings.llm_base_url
    model_name: str = settings.llm_model
    timeout_seconds: float = settings.llm_timeout_seconds
    max_retries: int = settings.llm_max_retries
    temperature: float = settings.llm_temperature
    max_tokens: int = settings.llm_max_tokens
    client: httpx.Client | None = None

    def _http_client(self) -> httpx.Client:
        if self.client is None:
            self.client = httpx.Client(
                base_url=self.base_url.rstrip("/") + "/",
                timeout=self.timeout_seconds,
            )
        return self.client

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    def _build_payload(
        self,
        messages: list[dict[str, str]],
        *,
        enforce_json_response: bool = True,
    ) -> dict[str, Any]:
        prompt_chars = sum(len(message.get("content", "")) for message in messages)
        if prompt_chars > settings.context_window_chars:
            raise LLMResponseError(
                f"Prompt excede ventana máxima local de {settings.context_window_chars} caracteres."
            )
        payload = {
            "model": self.model_name,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if enforce_json_response:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def compare(self, messages: list[dict[str, str]]) -> LLMComparisonResponse:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            retry_without_format = False
            attempt_succeeded = False
            for mode_name, enforce_json_response in (
                ("json_object", True),
                ("prompt_only_json", False),
            ):
                if mode_name == "prompt_only_json" and not retry_without_format:
                    continue
                try:
                    payload = self._build_payload(
                        messages,
                        enforce_json_response=enforce_json_response,
                    )
                    logger.info(
                        "Llamando llama.cpp intento=%s model=%s mode=%s",
                        attempt,
                        self.model_name,
                        mode_name,
                    )
                    response = self._http_client().post("chat/completions", json=payload)
                    response.raise_for_status()
                    parsed = _extract_json_message(response.json())
                    attempt_succeeded = True
                    return LLMComparisonResponse.model_validate(parsed)
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    logger.warning(
                        "Fallo comparando con llama.cpp intento=%s mode=%s error=%s",
                        attempt,
                        mode_name,
                        exc,
                    )
                    if enforce_json_response and _should_retry_without_response_format(exc):
                        retry_without_format = True
                        continue
                    break
            if not attempt_succeeded and attempt < self.max_retries:
                time.sleep(min(0.7 * attempt, 2.0))
        raise LLMResponseError(
            f"No se pudo obtener una respuesta JSON válida del LLM: {last_error}"
        )


def _extract_json_message(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices") or []
    if not choices:
        raise LLMResponseError("Payload sin choices.")
    choice = choices[0]
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_text = item.get("text")
            if isinstance(item_text, str) and item_text.strip():
                text_parts.append(item_text)
                continue
            item_json = item.get("json")
            if isinstance(item_json, dict):
                return item_json
            if isinstance(item_json, str) and item_json.strip():
                text_parts.append(item_json)
        content = "".join(text_parts)
    if isinstance(content, dict):
        return content
    if content is None:
        content = choice.get("text")

    text_fragments = [str(content or "").strip()]
    reasoning_content = message.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content.strip():
        text_fragments.append(reasoning_content.strip())
    text_fragments.extend(_tool_call_candidates(message))
    text = "\n".join(fragment for fragment in text_fragments if fragment).strip()
    if not text:
        raise LLMResponseError(EMPTY_PAYLOAD_ERROR)
    for candidate in _json_candidates(text):
        try:
            data = json.loads(candidate)
            if isinstance(data, str):
                data = json.loads(data)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    raise LLMResponseError("No se pudo extraer JSON estricto del mensaje del LLM.")


def _tool_call_candidates(message: dict[str, Any]) -> list[str]:
    tool_calls = message.get("tool_calls") or []
    candidates: list[str] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        function_payload = tool_call.get("function") or {}
        arguments = function_payload.get("arguments")
        if isinstance(arguments, str) and arguments.strip():
            candidates.append(arguments.strip())
    return candidates


def _should_retry_without_response_format(exc: Exception) -> bool:
    if not isinstance(exc, LLMResponseError):
        return False
    message = str(exc)
    return message in {
        EMPTY_PAYLOAD_ERROR,
        "No se pudo extraer JSON estricto del mensaje del LLM.",
    }


def _json_candidates(text: str) -> list[str]:
    clean = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    candidates = [clean]
    candidates.extend(match.group(1) for match in JSON_BLOCK_RE.finditer(clean))
    candidates.extend(match.group(1) for match in JSON_INLINE_RE.finditer(clean))
    return candidates
