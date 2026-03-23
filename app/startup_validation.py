import os
from dataclasses import dataclass
from typing import Any

from .llm_runtime import resolve_llm_runtime_settings
from .utils import ensure_auth_secret_is_safe


@dataclass
class CompareRuntimeReadinessError(RuntimeError):
    public_detail: str

    def __str__(self) -> str:
        return self.public_detail


def load_compare_runtime_settings(security_settings: dict[str, Any] | None = None) -> dict[str, Any]:
    comparison_mode = os.getenv("COMPARE_MODE", "llm_strict_sequential").strip() or "llm_strict_sequential"
    ai_only = os.getenv("COMPARE_AI_ONLY", "true").strip().lower() in {"1", "true", "yes", "on"}
    llm_runtime = resolve_llm_runtime_settings()
    ocr_available = bool(os.getenv("TESSERACT_CMD") or os.getenv("OCR_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"})
    return {
        "comparison_mode": comparison_mode,
        "ai_only_enabled": ai_only,
        "llm_available": bool(llm_runtime["available"]),
        "llm_base_url": str(llm_runtime["base_url"] or ""),
        "llm_provider": str(llm_runtime["provider"] or ""),
        "llm_model_name": str(llm_runtime["model_name"] or ""),
        "llm_runtime_source": str(llm_runtime["source"] or ""),
        "ocr_available": ocr_available,
        "extract_timeout_seconds": float(os.getenv("COMPARE_EXTRACT_TIMEOUT_SECONDS", "45")),
        "llm_timeout_seconds": float(os.getenv("COMPARE_LLM_TIMEOUT_SECONDS", "120")),
        "security_base_url": (security_settings or {}).get("base_url"),
    }


def ensure_compare_runtime_ready(*, startup_check: bool, security_settings: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = load_compare_runtime_settings(security_settings)
    redis_host = os.getenv("REDIS_HOST", "localhost").strip()
    if not redis_host:
        raise CompareRuntimeReadinessError("La configuración de Redis del comparador no es válida.")
    if settings["ai_only_enabled"] and not settings["llm_available"]:
        raise CompareRuntimeReadinessError(
            "El modo AI-only requiere configurar el proveedor LLM "
            "(LLAMA_SERVER_BASE_URL/LLM_BASE_URL/OPENAI_BASE_URL, o habilitar COMPARE_ALLOW_DEFAULT_LOCAL_LLM)."
        )
    try:
        ensure_auth_secret_is_safe()
    except RuntimeError as exc:
        raise CompareRuntimeReadinessError(str(exc)) from exc
    return settings
