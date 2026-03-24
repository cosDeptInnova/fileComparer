from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.extractors import get_pipeline_capabilities


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
    compare_queue_name: str = _env("COMPARE_QUEUE_NAME", default="compare")
    celery_broker_url: str = _env(
        "COMPARE_CELERY_BROKER_URL",
        "CELERY_BROKER_URL",
        "REDIS_URL",
        default="redis://127.0.0.1:6379/0",
    )
    celery_result_backend: str = _env(
        "COMPARE_CELERY_RESULT_BACKEND",
        "CELERY_RESULT_BACKEND",
        "REDIS_URL",
        default="redis://127.0.0.1:6379/0",
    )
    celery_inspect_timeout_seconds: float = _env_float(
        "COMPARE_CELERY_INSPECT_TIMEOUT_SECONDS",
        default=1.5,
    )
    inline_jobs: bool = _env_bool("COMPARE_INLINE_JOBS", default=False)
    require_active_workers: bool = _env_bool("COMPARE_REQUIRE_ACTIVE_WORKERS", default=False)
    queue_pop_timeout_seconds: int = _env_int("COMPARE_QUEUE_POP_TIMEOUT_SECONDS", default=5)
    worker_heartbeat_interval_seconds: float = _env_float("COMPARE_WORKER_HEARTBEAT_INTERVAL_SECONDS", default=10.0)
    worker_heartbeat_ttl_seconds: int = _env_int("COMPARE_WORKER_HEARTBEAT_TTL_SECONDS", default=45)
    worker_reclaim_interval_seconds: float = _env_float("COMPARE_WORKER_RECLAIM_INTERVAL_SECONDS", default=30.0)
    queue_max_job_attempts: int = _env_int("COMPARE_QUEUE_MAX_JOB_ATTEMPTS", default=3)
    max_file_mb: int = _env_int("COMPARE_MAX_FILE_MB", "TEXT_COMPARE_MAX_FILE_MB", default=40)
    block_target_chars: int = _env_int("COMPARE_BLOCK_TARGET_CHARS", default=550)
    block_overlap_chars: int = _env_int("COMPARE_BLOCK_OVERLAP_CHARS", default=220)
    compare_pair_chars: int = _env_int("COMPARE_PAIR_CHARS", default=250)
    context_window_tokens: int = _env_int("COMPARE_CONTEXT_WINDOW_TOKENS", "COMPARE_CONTEXT_WINDOW_CHARS", default=20000)
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
    compare_failed_blocks_error_ratio: float = _env_float(
        "COMPARE_FAILED_BLOCKS_ERROR_RATIO",
        default=0.5,
    )
    compare_reconcile_min_rows: int = _env_int(
        "COMPARE_RECONCILE_MIN_ROWS",
        default=2,
    )
    compare_reconcile_with_llm: bool = _env_bool(
        "COMPARE_RECONCILE_WITH_LLM",
        default=False,
    )
    compare_partial_persist_every_pairs: int = _env_int(
        "COMPARE_PARTIAL_PERSIST_EVERY_PAIRS",
        default=1,
    )
    llm_max_retries: int = _env_int("LLAMA_CPP_MAX_RETRIES", default=3)
    llm_temperature: float = _env_float("LLAMA_CPP_TEMPERATURE", default=0)
    llm_max_tokens: int = _env_int("LLAMA_CPP_MAX_TOKENS", default=1800)
    csrf_cookie_name: str = _env("COMPARE_CSRF_COOKIE", default="csrftoken_app")

    @property
    def rq_queue_name(self) -> str:
        return self.compare_queue_name

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def pipeline_capabilities(self) -> dict[str, object]:
        return get_pipeline_capabilities()

    @property
    def allowed_extensions(self) -> tuple[str, ...]:
        return tuple(self.pipeline_capabilities.get("allowed_extensions") or ())

    @property
    def accept(self) -> str:
        return ",".join(self.allowed_extensions)


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)