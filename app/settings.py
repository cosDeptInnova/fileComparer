from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Settings:
    app_name: str = os.getenv("COMPARE_APP_NAME", "Comparador documental IA local")
    host: str = os.getenv("COMPARE_HOST", "0.0.0.0")
    port: int = int(os.getenv("COMPARE_PORT", "8007"))
    data_dir: Path = Path(
        os.getenv(
            "COMPARE_DATA_DIR",
            str(Path(__file__).resolve().parents[1] / "data" / "compare_jobs"),
        )
    )
    upload_dir_name: str = "inputs"
    result_file_name: str = "result.json"
    redis_url: str = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    rq_queue_name: str = os.getenv("COMPARE_QUEUE_NAME", "compare")
    inline_jobs: bool = os.getenv("COMPARE_INLINE_JOBS", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    max_file_mb: int = int(os.getenv("COMPARE_MAX_FILE_MB", "40"))
    block_target_chars: int = int(os.getenv("COMPARE_BLOCK_TARGET_CHARS", "1400"))
    block_overlap_chars: int = int(os.getenv("COMPARE_BLOCK_OVERLAP_CHARS", "220"))
    context_window_chars: int = int(os.getenv("COMPARE_CONTEXT_WINDOW_CHARS", "20000"))
    llm_base_url: str = os.getenv("LLAMA_CPP_BASE_URL", "http://127.0.0.1:8002/v1")
    llm_model: str = os.getenv("LLAMA_CPP_MODEL", "local-model")
    llm_timeout_seconds: float = float(os.getenv("LLAMA_CPP_TIMEOUT_SECONDS", "120"))
    llm_max_retries: int = int(os.getenv("LLAMA_CPP_MAX_RETRIES", "3"))
    llm_temperature: float = float(os.getenv("LLAMA_CPP_TEMPERATURE", "0"))
    llm_max_tokens: int = int(os.getenv("LLAMA_CPP_MAX_TOKENS", "1800"))
    csrf_cookie_name: str = os.getenv("COMPARE_CSRF_COOKIE", "csrftoken_app")
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
