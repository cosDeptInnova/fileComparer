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

    def _build_payload(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        prompt_chars = sum(len(message.get("content", "")) for message in messages)
        if prompt_chars > settings.context_window_chars:
            raise LLMResponseError(
                f"Prompt excede ventana máxima local de {settings.context_window_chars} caracteres."
            )
        return {
            "model": self.model_name,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }

    def compare(self, messages: list[dict[str, str]]) -> LLMComparisonResponse:
        payload = self._build_payload(messages)
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info("Llamando llama.cpp intento=%s model=%s", attempt, self.model_name)
                response = self._http_client().post("chat/completions", json=payload)
                response.raise_for_status()
                parsed = _extract_json_message(response.json())
                return LLMComparisonResponse.model_validate(parsed)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning(
                    "Fallo comparando con llama.cpp intento=%s error=%s", attempt, exc
                )
                time.sleep(min(0.7 * attempt, 2.0))
        raise LLMResponseError(
            f"No se pudo obtener una respuesta JSON válida del LLM: {last_error}"
        )


def _extract_json_message(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices") or []
    if not choices:
        raise LLMResponseError("Payload sin choices.")
    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if isinstance(content, list):
        content = "".join(
            str(item.get("text", "")) for item in content if isinstance(item, dict)
        )
    if isinstance(content, dict):
        return content
    text = str(content or "").strip()
    if not text:
        raise LLMResponseError("Payload del LLM vacío.")
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


def _json_candidates(text: str) -> list[str]:
    clean = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    candidates = [clean]
    candidates.extend(match.group(1) for match in JSON_BLOCK_RE.finditer(clean))
    candidates.extend(match.group(1) for match in JSON_INLINE_RE.finditer(clean))
    return candidates
