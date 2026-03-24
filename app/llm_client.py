from __future__ import annotations

import json
import logging
import re
import time
from ast import literal_eval
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
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
JSON_STRING_BLOCK_RE = re.compile(r'"(\{.*\})"', re.DOTALL)
EMPTY_PAYLOAD_ERROR = "Payload del LLM vacío."
STRICT_JSON_ERROR = "No se pudo extraer JSON estricto del mensaje del LLM."


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
        prompt_tokens = _estimate_messages_tokens(messages)
        requested_total = prompt_tokens + max(0, self.max_tokens)
        if requested_total > settings.context_window_tokens:
            raise LLMResponseError(
                "Prompt excede ventana máxima local de "
                f"{settings.context_window_tokens} tokens estimados "
                f"(prompt≈{prompt_tokens}, respuesta≈{self.max_tokens})."
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
        for mode_name, enforce_json_response in (
            ("json_object", True),
            ("prompt_only_json", False),
        ):
            transport_attempts = max(1, self.max_retries)
            for attempt in range(1, transport_attempts + 1):
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
                    parsed = _normalize_llm_payload(_extract_json_message(response.json()))
                    return LLMComparisonResponse.model_validate(parsed)
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    logger.warning(
                        "Fallo comparando con llama.cpp intento=%s mode=%s error=%s",
                        attempt,
                        mode_name,
                        exc,
                    )
                    if _is_non_retryable_response_error(exc):
                        break
                    if attempt < transport_attempts:
                        time.sleep(min(0.7 * attempt, 2.0))
            if not _should_try_next_mode(last_error, enforce_json_response=enforce_json_response):
                break
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
        content = "\n".join(text_parts)
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
        parsed = _parse_json_candidate(candidate)
        if parsed is not None:
            return parsed
    raise LLMResponseError(STRICT_JSON_ERROR)


def _normalize_llm_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"changes": []}
    rows = payload.get("changes")
    if rows is None:
        for alias in ("rows", "diffs", "results", "items"):
            maybe = payload.get(alias)
            if isinstance(maybe, list):
                rows = maybe
                break
    if not isinstance(rows, list):
        return {"changes": []}
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        change_type = _normalize_change_type(
            row.get("change_type")
            or row.get("type")
            or row.get("action")
            or row.get("kind")
            or row.get("change")
        )
        if change_type is None:
            continue
        normalized_rows.append(
            {
                "change_type": change_type,
                "source_a": str(row.get("source_a") or row.get("text_a") or row.get("a") or row.get("before") or ""),
                "source_b": str(row.get("source_b") or row.get("text_b") or row.get("b") or row.get("after") or ""),
                "summary": str(row.get("summary") or row.get("resumen") or row.get("description") or ""),
                "confidence": _normalize_confidence_level(row.get("confidence")),
                "severity": _normalize_severity_level(row.get("severity")),
                "evidence": str(row.get("evidence") or row.get("comment") or row.get("reason") or ""),
                "anchor_a": _normalize_optional_int(row.get("anchor_a") or row.get("index_a")),
                "anchor_b": _normalize_optional_int(row.get("anchor_b") or row.get("index_b")),
            }
        )
    return {"changes": normalized_rows}



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



def _should_try_next_mode(exc: Exception | None, *, enforce_json_response: bool) -> bool:
    if not enforce_json_response:
        return False
    return _should_retry_without_response_format(exc)



def _is_non_retryable_response_error(exc: Exception) -> bool:
    if not isinstance(exc, LLMResponseError):
        return False
    return True



def _should_retry_without_response_format(exc: Exception | None) -> bool:
    if not isinstance(exc, LLMResponseError):
        return False
    message = str(exc)
    return message in {
        EMPTY_PAYLOAD_ERROR,
        STRICT_JSON_ERROR,
    }



def _estimate_messages_tokens(messages: list[dict[str, str]]) -> int:
    total = 0
    for message in messages:
        content = str(message.get("content", "") or "")
        total += _estimate_text_tokens(content)
        total += 12
    return total



def _estimate_text_tokens(text: str) -> int:
    clean = (text or "").strip()
    if not clean:
        return 0
    word_count = len(re.findall(r"\S+", clean))
    char_based = max(1, round(len(clean) / 4))
    word_based = max(1, round(word_count * 1.35))
    return max(char_based, word_based)



def _json_candidates(text: str) -> list[str]:
    clean = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    clean = re.sub(r"^json\s*", "", clean, flags=re.IGNORECASE).strip()
    clean = clean.replace("\ufeff", "").strip()
    candidates: list[str] = []

    if clean:
        candidates.append(clean)

    for match in JSON_BLOCK_RE.finditer(clean):
        block = match.group(1).strip()
        if block:
            candidates.append(block)

    for match in JSON_STRING_BLOCK_RE.finditer(clean):
        block = match.group(1).strip()
        if block:
            candidates.append(block)

    balanced = _extract_balanced_json_objects(clean)
    candidates.extend(balanced)

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = candidate.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique



def _extract_balanced_json_objects(text: str) -> list[str]:
    candidates: list[str] = []
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            obj, end = decoder.raw_decode(text[index:])
        except Exception:
            continue
        if isinstance(obj, dict):
            candidates.append(text[index : index + end])
    return candidates


def _parse_json_candidate(candidate: str) -> dict[str, Any] | None:
    normalized = (candidate or "").strip()
    if not normalized:
        return None

    variants = [normalized, _repair_common_json_issues(normalized)]
    if normalized.startswith('"') and normalized.endswith('"'):
        variants.append(normalized[1:-1])
    variants.append(normalized.replace('\\"', '"'))

    for variant in variants:
        current = variant.strip()
        if not current:
            continue
        try:
            data = json.loads(current)
        except Exception:
            data = _parse_pythonic_dict(current)
            if data is None:
                continue
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                continue
        if isinstance(data, dict):
            return data
    return None


def _repair_common_json_issues(candidate: str) -> str:
    fixed = (candidate or "").strip()
    if not fixed:
        return fixed
    fixed = fixed.replace("“", '"').replace("”", '"').replace("’", "'")
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    return fixed


def _parse_pythonic_dict(candidate: str) -> dict[str, Any] | None:
    current = (candidate or "").strip()
    if not current:
        return None
    if not (current.startswith("{") and current.endswith("}")):
        return None
    try:
        data = literal_eval(current)
    except Exception:
        return None
    if isinstance(data, dict):
        return data
    return None


def _normalize_change_type(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    mapping = {
        "añadido": "añadido",
        "adicionado": "añadido",
        "agregado": "añadido",
        "insertado": "añadido",
        "added": "añadido",
        "addition": "añadido",
        "new": "añadido",
        "eliminado": "eliminado",
        "borrado": "eliminado",
        "removido": "eliminado",
        "deleted": "eliminado",
        "removed": "eliminado",
        "deletion": "eliminado",
        "modificado": "modificado",
        "cambiado": "modificado",
        "actualizado": "modificado",
        "updated": "modificado",
        "changed": "modificado",
        "modified": "modificado",
    }
    return mapping.get(normalized)


def _normalize_confidence_level(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    mapping = {
        "baja": "baja",
        "low": "baja",
        "media": "media",
        "medium": "media",
        "alta": "alta",
        "high": "alta",
    }
    return mapping.get(normalized, "media")


def _normalize_severity_level(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    mapping = {
        "baja": "baja",
        "low": "baja",
        "media": "media",
        "medium": "media",
        "alta": "alta",
        "high": "alta",
        "critica": "critica",
        "crítica": "critica",
        "critical": "critica",
    }
    return mapping.get(normalized, "media")


def _normalize_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except Exception:
        return None
