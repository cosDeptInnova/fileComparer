from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(*names: str, default: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value != "":
            return value
    return default


def _env_int(*names: str, default: int) -> int:
    return int(_env(*names, default=str(default)))


def _env_float(*names: str, default: float) -> float:
    return float(_env(*names, default=str(default)))


def _env_bool(*names: str, default: bool) -> bool:
    return _env(*names, default="1" if default else "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass(slots=True)
class Settings:
    app_name: str = _env("COMPARE_APP_NAME", default="Comparador documental IA local")
    host: str = _env("COMPARE_HOST", default="0.0.0.0")
    port: int = _env_int("COMPARE_PORT", default=8007)
    data_dir: Path = Path(
        _env(
            "COMPARE_DATA_DIR",
            default=str(Path(__file__).resolve().parents[1] / "data" / "compare_jobs"),
        )
    )
    upload_dir_name: str = "inputs"
    result_file_name: str = "result.json"
    redis_url: str = _env("REDIS_URL", default="redis://127.0.0.1:6379/0")
    rq_queue_name: str = _env("COMPARE_QUEUE_NAME", default="compare")
    inline_jobs: bool = _env_bool("COMPARE_INLINE_JOBS", default=False)
    max_file_mb: int = _env_int("COMPARE_MAX_FILE_MB", "TEXT_COMPARE_MAX_FILE_MB", default=40)
    block_target_chars: int = _env_int("COMPARE_BLOCK_TARGET_CHARS", default=1400)
    block_overlap_chars: int = _env_int("COMPARE_BLOCK_OVERLAP_CHARS", default=220)
    context_window_chars: int = _env_int("COMPARE_CONTEXT_WINDOW_CHARS", default=20000)
    llm_base_url: str = _env(
        "LLAMA_CPP_BASE_URL",
        "LLAMA_SERVER_BASE_URL",
        "LLM_BASE_URL",
        "OPENAI_BASE_URL",
        default="http://127.0.0.1:8002/v1",
    )
    llm_model: str = _env("LLAMA_CPP_MODEL", "LLM_MODEL", default="local-model")
    llm_timeout_seconds: float = _env_float(
        "LLAMA_CPP_TIMEOUT_SECONDS",
        "COMPARE_LLM_TIMEOUT_SECONDS",
        default=120,
    )
    llm_max_retries: int = _env_int("LLAMA_CPP_MAX_RETRIES", default=3)
    llm_temperature: float = _env_float("LLAMA_CPP_TEMPERATURE", default=0)
    llm_max_tokens: int = _env_int("LLAMA_CPP_MAX_TOKENS", default=1800)
    csrf_cookie_name: str = _env("COMPARE_CSRF_COOKIE", default="csrftoken_app")
    allowed_extensions: tuple[str, ...] = (
        ".pdf",
        ".doc",
        ".docx",
        ".txt",
        ".rtf",
        ".xls",
        ".xlsx",
        ".png",
        ".jpg",
        ".jpeg",
    )

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def accept(self) -> str:
        return ",".join(self.allowed_extensions)


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
