from __future__ import annotations

import os
from typing import Any

DEFAULT_LLAMA_BASE_URL = "http://127.0.0.1:8002/v1"
DEFAULT_MODEL_NAME = "local-compare-worker"
DEFAULT_MODEL_FALLBACKS = (
    "gpt-oss-20b",
    "gpt-oss:20b",
    "gpt-oss-20b-instruct",
    "local-compare-worker",
)
DEFAULT_PUBLIC_OPENAI_BASE_URL = "https://api.openai.com/v1"
_LOCAL_ENVIRONMENTS = {"", "dev", "development", "local", "test", "testing"}


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _first_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None:
            normalized = value.strip()
            if normalized:
                return normalized
    return ""


def resolve_llm_runtime_settings() -> dict[str, Any]:
    env_name = (os.getenv("ENV", os.getenv("ENVIRONMENT", "development")) or "development").strip().lower()
    is_local_environment = env_name in _LOCAL_ENVIRONMENTS

    base_url = _first_env("LLAMA_SERVER_BASE_URL", "LLM_BASE_URL", "OPENAI_BASE_URL")
    api_key = _first_env("LLAMA_SERVER_API_KEY", "OPENAI_API_KEY")
    model_name = _first_env("LLM_MODEL", "OPENAI_MODEL", "LLAMA_MODEL") or DEFAULT_MODEL_NAME
    raw_fallbacks = _first_env("COMPARE_LLM_MODEL_FALLBACKS")
    model_fallbacks = [
        candidate.strip()
        for candidate in raw_fallbacks.split(",")
        if candidate.strip()
    ] or list(DEFAULT_MODEL_FALLBACKS)

    allow_default_local = _is_truthy(
        os.getenv(
            "COMPARE_ALLOW_DEFAULT_LOCAL_LLM",
            "true" if is_local_environment else "false",
        )
    )

    source = "explicit"
    provider = "openai_compatible"

    if not base_url and api_key:
        base_url = DEFAULT_PUBLIC_OPENAI_BASE_URL
        provider = "openai_public_api"
        source = "api_key_default"
    elif not base_url and allow_default_local:
        base_url = DEFAULT_LLAMA_BASE_URL
        provider = "local_default"
        source = "default_local"

    return {
        "env_name": env_name,
        "is_local_environment": is_local_environment,
        "allow_default_local": allow_default_local,
        "provider": provider,
        "source": source,
        "base_url": base_url,
        "api_key": api_key,
        "model_name": model_name,
        "model_fallbacks": model_fallbacks,
        "available": bool(base_url),
    }
