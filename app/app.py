# app.py
import asyncio
import difflib
import hashlib
import json
import os, re, uuid, logging, shutil
import inspect
from typing import Any, Optional
from urllib.parse import urlparse

import redis
import redis.asyncio as aioredis
from fastapi import (
    FastAPI, UploadFile, File, Form, HTTPException, Request, Depends, Response, APIRouter
)
import time
from datetime import datetime, timezone
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.middleware.sessions import SessionMiddleware
from .entra_oidc import EntraOIDC
from .utils import (
    validate_csrf_double_submit,
    generate_csrf_token,
    init_redis_clients,
    add_ephemeral_file,
    get_conversation_to_redis,
    set_user_context,
    get_current_auth_compare,
    scan_with_clamav,
)
from .config.database import get_db
from .config.models import Conversation, AuditLog
from .job_store import RedisJobStore, get_job_store, init_job_store
from .compare_queue import RedisCompareQueue
from .compare_support import (
    build_compare_event,
    get_compare_trace_policy,
    persist_compare_event_async,
)
from .comparison_table import filter_comparison_rows, resolve_comparison_rows
from .compare_tasks import CompareTaskPayload
from .file_validation import validate_upload_payload
from .metrics import (
    metrics_snapshot,
    record_job_event,
    record_queue_event,
    setup_metrics,
    set_max_concurrent,
    set_compare_active_workers,
    set_compare_queue_depth,
)
from .startup_validation import (
    CompareRuntimeReadinessError,
    ensure_compare_runtime_ready,
    load_compare_runtime_settings,
)
from .text_segments import normalize_text_segments
from .capabilities import (
    build_capabilities_payload,
    ext_allowed,
    format_file_too_large_backend_message,
    format_unsupported_extension_message,
    get_text_compare_max_file_mb,
)

# ----------------- Paths / App -----------------
APP_DIR = os.path.dirname(__file__)
BASE_DIR = os.path.dirname(APP_DIR)
DATA_DIR = os.path.join(BASE_DIR, "data")

MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_JOBS", "3"))

os.makedirs(DATA_DIR, exist_ok=True)

_DEFAULT_SESSION_SECRET = "adñsdlÑSDÑñdÑldkmÑPLndbUEK847Yhs681E11d6w961bjk1m1bf9"
_LOCAL_ENVIRONMENTS = {"", "dev", "development", "local", "test", "testing"}
_VALID_SAMESITE_VALUES = {"lax", "strict", "none"}


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _configure_service_logging() -> None:
    level_name = (os.getenv("COMP_DOCS_LOG_LEVEL", os.getenv("LOG_LEVEL", "INFO")) or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root_logger = logging.getLogger()
    uvicorn_error_logger = logging.getLogger("uvicorn.error")

    if not root_logger.handlers and uvicorn_error_logger.handlers:
        root_logger.handlers = list(uvicorn_error_logger.handlers)

    if not root_logger.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    else:
        root_logger.setLevel(level)

    logging.getLogger("comp_docs").setLevel(level)
    logging.getLogger(__name__).info("Logging comp_docs configurado con nivel=%s", level_name)


def _parse_required_origin_list(raw_value: str) -> list[str]:
    origins = [origin.strip() for origin in raw_value.split(",") if origin.strip()]
    if not origins:
        raise RuntimeError(
            "ALLOWED_ORIGINS debe definir una lista explícita de orígenes separados por comas."
        )
    if any(origin == "*" for origin in origins):
        raise RuntimeError(
            "ALLOWED_ORIGINS no puede contener '*' cuando allow_credentials=True; configure orígenes explícitos."
        )
    return origins


def _validate_origin(origin: str, *, require_https: bool) -> str:
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"Origen inválido en ALLOWED_ORIGINS: {origin!r}.")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise RuntimeError(
            f"ALLOWED_ORIGINS solo admite origen puro (esquema + host + puerto), sin paths ni query: {origin!r}."
        )
    host = (parsed.hostname or "").lower()
    if require_https and parsed.scheme != "https":
        raise RuntimeError(
            f"En producción, todos los orígenes de ALLOWED_ORIGINS deben usar https: {origin!r}."
        )
    if require_https and host in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError(
            f"En producción, ALLOWED_ORIGINS no puede apuntar a hosts locales: {origin!r}."
        )
    return origin.rstrip("/")


def _validate_base_url(base_url: str, *, is_production: bool) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("BASE_URL debe ser una URL absoluta válida.")
    host = (parsed.hostname or "").lower()
    if is_production and parsed.scheme != "https":
        raise RuntimeError("En producción, BASE_URL debe usar https.")
    if is_production and host in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("En producción, BASE_URL no puede apuntar a localhost.")
    return base_url.rstrip("/")


def _redis_common_connection_kwargs(*, decode_responses: bool) -> dict[str, Any]:
    socket_timeout = float(os.getenv("REDIS_SOCKET_TIMEOUT_SECONDS", "5"))
    return {
        "host": os.getenv("REDIS_HOST", "localhost").strip() or "localhost",
        "port": int(os.getenv("REDIS_PORT", "6379")),
        "password": (os.getenv("REDIS_PASSWORD") or None),
        "socket_connect_timeout": socket_timeout,
        "socket_timeout": socket_timeout,
        "health_check_interval": max(5, int(float(os.getenv("REDIS_HEALTH_CHECK_INTERVAL_SECONDS", "15")))),
        "retry_on_timeout": True,
        "decode_responses": decode_responses,
    }


async def _close_redis_client(client: Any) -> None:
    if client is None:
        return
    close_result = None
    try:
        if hasattr(client, "aclose"):
            close_result = client.aclose()
        elif hasattr(client, "close"):
            close_result = client.close()
        if inspect.isawaitable(close_result):
            await close_result
    except Exception:
        logging.exception("No se pudo cerrar un cliente Redis del comparador")


def load_security_settings() -> dict[str, Any]:
    env_name = (os.getenv("ENV", os.getenv("ENVIRONMENT", "development")) or "development").strip().lower()
    is_production = env_name not in _LOCAL_ENVIRONMENTS

    session_secret = os.getenv("SESSION_SECRET", "").strip()
    if not session_secret or session_secret == _DEFAULT_SESSION_SECRET:
        if is_production:
            raise RuntimeError(
                "SESSION_SECRET debe configurarse con un valor no vacío y distinto del valor por defecto."
            )
        session_secret = _DEFAULT_SESSION_SECRET

    base_url = _validate_base_url(
        os.getenv("BASE_URL", "http://localhost:8000").strip(),
        is_production=is_production,
    )

    raw_allowed_origins = os.getenv("ALLOWED_ORIGINS", "")
    if raw_allowed_origins.strip():
        allowed_origins = [
            _validate_origin(origin, require_https=is_production)
            for origin in _parse_required_origin_list(raw_allowed_origins)
        ]
    else:
        if is_production:
            raise RuntimeError(
                "ALLOWED_ORIGINS debe definir una lista explícita de orígenes separados por comas."
            )
        allowed_origins = [
            _validate_origin(base_url, require_https=False),
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]

    session_same_site = os.getenv(
        "SESSION_SAMESITE",
        "strict" if is_production else "lax",
    ).strip().lower()
    if session_same_site not in _VALID_SAMESITE_VALUES:
        raise RuntimeError("SESSION_SAMESITE debe ser lax, strict o none.")

    cookie_same_site = os.getenv(
        "COOKIE_SAMESITE",
        "strict" if is_production else "lax",
    ).strip().lower()
    if cookie_same_site not in _VALID_SAMESITE_VALUES:
        raise RuntimeError("COOKIE_SAMESITE debe ser lax, strict o none.")

    session_https_only = _is_truthy(
        os.getenv("SESSION_HTTPS_ONLY", "true" if is_production else "false")
    )
    cookie_secure = _is_truthy(
        os.getenv("COOKIE_SECURE", "true" if is_production else "false")
    )

    if is_production and not session_https_only:
        raise RuntimeError("En producción, SESSION_HTTPS_ONLY debe ser true.")
    if is_production and not cookie_secure:
        raise RuntimeError("En producción, COOKIE_SECURE debe ser true.")
    if session_same_site == "none" and not session_https_only:
        raise RuntimeError("SESSION_SAMESITE=none requiere SESSION_HTTPS_ONLY=true.")
    if cookie_same_site == "none" and not cookie_secure:
        raise RuntimeError("COOKIE_SAMESITE=none requiere COOKIE_SECURE=true.")

    return {
        "env_name": env_name,
        "is_production": is_production,
        "session_secret": session_secret,
        "session_same_site": session_same_site,
        "session_https_only": session_https_only,
        "session_max_age": int(os.getenv("SESSION_MAX_AGE", str(60 * 60 * 8))),
        "cookie_secure": cookie_secure,
        "cookie_same_site": cookie_same_site,
        "allowed_origins": allowed_origins,
        "base_url": base_url,
    }


SECURITY_SETTINGS = load_security_settings()
COMPARE_RUNTIME_SETTINGS = load_compare_runtime_settings(SECURITY_SETTINGS)
_configure_service_logging()


def _get_compare_runtime_settings() -> dict[str, Any]:
    current = getattr(app.state, "compare_runtime_settings", None)
    if isinstance(current, dict) and current:
        return current
    return dict(COMPARE_RUNTIME_SETTINGS)


def _set_compare_runtime_settings(settings: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(settings or {})
    app.state.compare_runtime_settings = normalized
    return normalized


def _preflight_compare_runtime(*, startup_check: bool) -> dict[str, Any]:
    runtime_settings = ensure_compare_runtime_ready(
        startup_check=startup_check,
        security_settings=SECURITY_SETTINGS,
    )
    return _set_compare_runtime_settings(runtime_settings)

app = FastAPI(title="Comparador robusto DOC/DOCX/PDF/TXT (con SSO Entra)")

setup_metrics(app)
set_max_concurrent(MAX_CONCURRENT)
# ----------------- Seguridad de sesión / CORS -----------------
# SessionMiddleware se mantiene para el flujo OIDC interactivo del router de auth.
# Los endpoints API protegidos del comparador usan JWT + sesión en Redis mediante
# get_current_auth_compare, y no dependen de la cookie firmada local de Starlette.
app.add_middleware(
    SessionMiddleware,
    secret_key=SECURITY_SETTINGS["session_secret"],
    same_site=SECURITY_SETTINGS["session_same_site"],
    https_only=SECURITY_SETTINGS["session_https_only"],
    max_age=SECURITY_SETTINGS["session_max_age"],
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=SECURITY_SETTINGS["allowed_origins"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ----------------- Auth (EntraOIDC) -----------------
TENANT_ID     = os.getenv("TENANT_ID", "").strip()
CLIENT_ID     = os.getenv("CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "").strip()
BASE_URL      = SECURITY_SETTINGS["base_url"]
CSRF_COOKIE_NAME = "csrftoken_app"

if all([TENANT_ID, CLIENT_ID, CLIENT_SECRET, BASE_URL]):
    auth = EntraOIDC(
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        base_url=BASE_URL,
        redirect_path="/auth/callback",
        scopes=[],   # sin scopes de recurso: evita consentimientos
    )
    app.include_router(auth.router, tags=["auth"])
else:
    logging.warning("OIDC de Entra deshabilitado: faltan variables de configuración o dependencias opcionales.")
    auth = type("DisabledOIDC", (), {"router": APIRouter()})()

# ----------------- Startup: Redis / logging ---------------------------------
@app.on_event("startup")
async def initialize_redis():
    """
    Inicializa la conexión a Redis cuando la aplicación arranca.
    Usa las mismas variables de entorno que el resto de servicios.

    Registra los clientes en utils.init_redis_clients para que
    los helpers compartidos puedan usarlos.
    """
    from .metrics import set_redis_up, set_max_concurrent  # import local para evitar ciclos

    load_security_settings()
    _preflight_compare_runtime(startup_check=True)

    redis_db = int(os.getenv("REDIS_DB", "0"))
    redis_conversation_db = int(os.getenv("REDIS_CONVERSATION_DB", "2"))
    async_kwargs = _redis_common_connection_kwargs(decode_responses=True)
    sync_kwargs = _redis_common_connection_kwargs(decode_responses=True)

    try:
        core_client = aioredis.Redis(
            db=redis_db,
            **async_kwargs,
        )
        conv_client = aioredis.Redis(
            db=redis_conversation_db,
            **async_kwargs,
        )
        sync_core_client = redis.Redis(
            db=redis_db,
            **sync_kwargs,
        )
        await core_client.ping()
        await conv_client.ping()
        sync_core_client.ping()

        # Registramos los clientes en utils (única fuente de verdad)
        init_redis_clients(core_client, conv_client)
        init_job_store(RedisJobStore(core_client, sync_core_client))
        app.state.compare_queue = RedisCompareQueue(sync_core_client, get_job_store())
        app.state.compare_queue.ensure_group()
        set_compare_queue_depth(app.state.compare_queue.depth())
        active_workers = app.state.compare_queue.count_active_workers()
        set_compare_active_workers(active_workers)
        if active_workers <= 0:
            logging.warning("[startup] Comparador sin workers activos; /comparar devolverá 503 hasta que arranque uno.")
            app.state.compare_queue.log_worker_diagnostics(reason="startup_without_active_workers")

        app.state.compare_job_cleanup_stop = asyncio.Event()
        app.state.compare_job_cleanup_task = asyncio.create_task(
            get_job_store().run_cleanup_loop(app.state.compare_job_cleanup_stop)
        )
        app.state.compare_job_sync_redis = sync_core_client
        app.state.compare_core_redis = core_client
        app.state.compare_conv_redis = conv_client

        # Métricas: Redis arriba + límite de concurrencia configurado
        set_redis_up(True)
        try:
            set_max_concurrent(int(os.getenv("MAX_CONCURRENT_JOBS", "3")))
        except Exception:
            pass

        logging.info(
            "[startup] Redis conectado en %s:%s (db=%s y db=%s) y job store compartido inicializado.",
            async_kwargs["host"],
            async_kwargs["port"],
            redis_db,
            redis_conversation_db,
        )
    except Exception:
        set_redis_up(False)
        logging.exception("[startup] Fallo conectando Redis")
        raise


@app.on_event("shutdown")
async def shutdown_redis_resources():
    stop_event = getattr(app.state, "compare_job_cleanup_stop", None)
    cleanup_task = getattr(app.state, "compare_job_cleanup_task", None)
    sync_client = getattr(app.state, "compare_job_sync_redis", None)
    core_client = getattr(app.state, "compare_core_redis", None)
    conv_client = getattr(app.state, "compare_conv_redis", None)

    if stop_event is not None:
        stop_event.set()
    if cleanup_task is not None:
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
    await _close_redis_client(conv_client)
    await _close_redis_client(core_client)
    await _close_redis_client(sync_client)

# ----------------- Helpers CSRF / nombre seguro / validaciones ---------------
def validate_csrf(request: Request) -> None:
    """
    Double-submit cookie:
      - Cookie: csrftoken_app
      - Cabecera: X-CSRFToken
    """
    validate_csrf_double_submit(
        request,
        cookie_name=CSRF_COOKIE_NAME,
        header_name="X-CSRFToken",
        error_detail="CSRF token inválido o ausente (servicio comparador).",
    )

SAFE_FILENAME_RX = re.compile(r"[^A-Za-z0-9._-]+")

def safe_name(filename: str) -> str:
    base = os.path.basename(filename or "")
    base = base.strip() or "upload"
    return SAFE_FILENAME_RX.sub("_", base)[:180]


def sanitize_soffice_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip().strip('"').strip("'")
    return cleaned or None


def _write_audit_log(
    *,
    user_id: Optional[int],
    entity_name: str,
    entity_id: int,
    action: str,
    new_data: dict[str, Any],
    old_data: Optional[dict[str, Any]] = None,
) -> None:
    gen = get_db()
    db = next(gen)
    try:
        db.add(
            AuditLog(
                user_id=user_id,
                entity_name=entity_name,
                entity_id=entity_id,
                action=action,
                old_data=get_compare_trace_policy().sanitize_audit_payload(old_data) if old_data else None,
                new_data=get_compare_trace_policy().sanitize_audit_payload(new_data),
                timestamp=datetime.now(timezone.utc),
            )
        )
        db.commit()
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def _audit_compare_access(
    *,
    user_id: Optional[int],
    job: dict[str, Any],
    action: str,
    route: str,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    conv_row_id = int(job.get("conv_row_id") or 0)
    if conv_row_id <= 0:
        return
    try:
        _write_audit_log(
            user_id=user_id,
            entity_name="CompareJobAccess",
            entity_id=conv_row_id,
            action=action,
            new_data={
                "sid": job.get("sid"),
                "route": route,
                **(extra or {}),
            },
        )
    except Exception:
        logging.exception("Comparador: no se pudo auditar el acceso %s al job %s", route, job.get("sid"))

async def _get_owned_job_or_404(sid: str, auth_ctx: dict) -> dict:
    if not auth_ctx or not auth_ctx.get("session"):
        raise HTTPException(status_code=401, detail="No autenticado o sesión expirada.")

    user_id = (auth_ctx or {}).get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="No autenticado o sesión expirada.")

    job = await get_job_store().get_job(sid)
    if not job:
        raise HTTPException(status_code=404, detail="SID no encontrado o expirado")
    if job.get("owner_user_id") != int(user_id):
        raise HTTPException(status_code=403, detail="No autorizado para acceder a este job")
    return job


def _job_progress_view(job: dict) -> Optional[dict]:
    progress_expires_at = job.get("progress_expires_at") or 0
    if progress_expires_at <= int(time.time()):
        return None
    return job.get("progress")


def _job_result_view(job: dict) -> Optional[dict]:
    result_expires_at = job.get("result_expires_at") or 0
    if result_expires_at <= int(time.time()):
        return None
    result = job.get("result") or {}
    if not isinstance(result, dict):
        return None
    hydrated = dict(result)
    hydrated.setdefault("token_diffs", {})
    hydrated.setdefault("block_diffs", [])
    hydrated.setdefault("pair_records", hydrated.get("comparison_rows") or [])
    hydrated.setdefault("pair_records_schema", {})
    hydrated.setdefault("comparison_rows", hydrated.get("pair_records") or [])
    hydrated.setdefault("ai_comparison_rows", hydrated.get("pair_records") or [])
    hydrated.setdefault("literal_content_available", True)
    return hydrated

# ----------------- Estado/Jobs compartido -----------------

def _flatten_text(parts) -> str:
    if isinstance(parts, list):
        return "\n\n".join(str(p or "").strip() for p in parts if str(p or "").strip())
    return str(parts or "").strip()


def _parse_related_block_ids(value: Any, *, current_block_id: Optional[int] = None) -> list[int]:
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = [token.strip() for token in value.replace(";", ",").split(",")]
    else:
        items = []

    parsed: list[int] = []
    for item in items:
        if not str(item).isdigit():
            continue
        block_id = int(item)
        if block_id <= 0 or (current_block_id and block_id == current_block_id):
            continue
        parsed.append(block_id)
    return sorted(set(parsed))


def _relation_links_from_ids(block_ids: list[int]) -> list[dict[str, Any]]:
    return [
        {
            "block_id": block_id,
            "label": f"#{block_id}",
            "href": f"#block-{block_id}",
        }
        for block_id in block_ids
    ]


_ROW_SEGMENT_TOKEN_RX = re.compile(r"\S+|\s+")
_VISIBLE_ERROR_CODES = {
    "llm_invalid_json",
    "llm_empty_payload",
    "llm_payload_invalid",
    "llm_timeout",
    "llm_http_error",
    "llm_runtime_error",
    "compare_internal_error",
}


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "si"}


def _sanitize_visible_error_message(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    message = " ".join(str(value).split())
    if not message:
        return None
    lower_message = message.lower()
    if any(code in lower_message for code in _VISIBLE_ERROR_CODES):
        return message
    if (
        "/" in message
        or "\\" in message
        or "traceback" in lower_message
        or "http://" in lower_message
        or "https://" in lower_message
        or "line " in lower_message
    ):
        return "La comparación no pudo completarse. Inténtalo de nuevo más tarde."
    return message


def _sanitize_progress_state(progress_state: Optional[dict]) -> dict:
    sanitized = dict(progress_state or {})
    sanitized_error = _sanitize_visible_error_message(sanitized.get("error"))
    if sanitized_error is None:
        sanitized.pop("error", None)
    else:
        sanitized["error"] = sanitized_error
    return sanitized


def _diff_segments_from_texts(text_a: str, text_b: str) -> list[dict[str, str]]:
    if text_a == text_b:
        return normalize_text_segments([{"type": "equal", "text": text_a}]) if text_a else []

    tokens_a = _ROW_SEGMENT_TOKEN_RX.findall(text_a)
    tokens_b = _ROW_SEGMENT_TOKEN_RX.findall(text_b)
    matcher = difflib.SequenceMatcher(a=tokens_a, b=tokens_b, autojunk=False)
    raw_segments: list[dict[str, str]] = []

    for opcode, a_start, a_end, b_start, b_end in matcher.get_opcodes():
        if opcode == "equal":
            raw_segments.append({"type": "equal", "text": "".join(tokens_a[a_start:a_end])})
            continue
        if opcode in {"replace", "delete"}:
            raw_segments.append({"type": "delete", "text": "".join(tokens_a[a_start:a_end])})
        if opcode in {"replace", "insert"}:
            raw_segments.append({"type": "insert", "text": "".join(tokens_b[b_start:b_end])})

    return normalize_text_segments(raw_segments)


def _normalize_row_segments(row: dict[str, Any], *, side: str) -> list[dict[str, str]]:
    explicit_keys = [
        f"display_segments_{side}",
        f"text_{side}_segments",
    ]
    for key in explicit_keys:
        explicit_segments = row.get(key)
        if explicit_segments:
            return normalize_text_segments(explicit_segments)

    text_a = str(
        row.get("display_text_a")
        or row.get("text_a")
        or row.get("file_a_text")
        or row.get("literal_a")
        or ""
    )
    text_b = str(
        row.get("display_text_b")
        or row.get("text_b")
        or row.get("file_b_text")
        or row.get("literal_b")
        or ""
    )
    segments = _diff_segments_from_texts(text_a, text_b)
    if side == "a":
        return [segment for segment in segments if segment.get("type") != "insert"]
    return [segment for segment in segments if segment.get("type") != "delete"]


def _normalize_source_spans(row: dict[str, Any]) -> dict[str, Any]:
    source_spans = row.get("source_spans") if isinstance(row.get("source_spans"), dict) else {}
    file_a = source_spans.get("file_a") or source_spans.get("a")
    file_b = source_spans.get("file_b") or source_spans.get("b")
    return {
        "file_a": [int(file_a[0]), int(file_a[1])] if isinstance(file_a, (list, tuple)) and len(file_a) >= 2 else None,
        "file_b": [int(file_b[0]), int(file_b[1])] if isinstance(file_b, (list, tuple)) and len(file_b) >= 2 else None,
        "block_id": _safe_int(source_spans.get("block_id") or row.get("block_id") or row.get("row_id")) or None,
        "chunk_index_within_region": _safe_int(source_spans.get("chunk_index_within_region")) or None,
        "chunk_index_a": _safe_int(source_spans.get("chunk_index_a") or row.get("chunk_index_a")) or None,
        "chunk_index_b": _safe_int(source_spans.get("chunk_index_b") or row.get("chunk_index_b")) or None,
        "diff": source_spans.get("diff") if isinstance(source_spans.get("diff"), list) else [],
        "segments_a": source_spans.get("segments_a") if isinstance(source_spans.get("segments_a"), list) else [],
        "segments_b": source_spans.get("segments_b") if isinstance(source_spans.get("segments_b"), list) else [],
        "stable_region": source_spans.get("stable_region") if isinstance(source_spans.get("stable_region"), dict) else {},
    }


def _normalize_comparison_rows(rows: Optional[list]) -> list[dict]:
    normalized: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        block_id = _safe_int(row.get("block_id") or row.get("row_id"))
        if block_id <= 0:
            continue
        related_block_ids = row.get("related_block_ids") or []
        display_segments_a = _normalize_row_segments(row, side="a")
        display_segments_b = _normalize_row_segments(row, side="b")
        normalized.append(
            {
                "block_id": block_id,
                "pair_id": str(row.get("pair_id") or ""),
                "pair_hash": str(row.get("pair_hash") or row.get("cache_pair_hash") or ""),
                "text_a": str(row.get("text_a") or row.get("file_a_text") or row.get("literal_a") or ""),
                "text_b": str(row.get("text_b") or row.get("file_b_text") or row.get("literal_b") or ""),
                "display_text_a": str(row.get("display_text_a") or row.get("text_a") or row.get("file_a_text") or row.get("literal_a") or ""),
                "display_text_b": str(row.get("display_text_b") or row.get("text_b") or row.get("file_b_text") or row.get("literal_b") or ""),
                "display_segments_a": display_segments_a,
                "display_segments_b": display_segments_b,
                "text_a_segments": display_segments_a,
                "text_b_segments": display_segments_b,
                "context_before_a": row.get("context_before_a"),
                "context_after_a": row.get("context_after_a"),
                "context_before_b": row.get("context_before_b"),
                "context_after_b": row.get("context_after_b"),
                "change_type": str(row.get("change_type") or "pendiente_confirmacion"),
                "materiality": str(row.get("materiality") or "pendiente_confirmacion"),
                "confidence": str(row.get("confidence") or "baja"),
                "final_decision": str(row.get("final_decision") or "pendiente_confirmacion"),
                "severity": str(row.get("severity") or "baja"),
                "summary": str(row.get("summary") or ""),
                "impact": str(row.get("impact") or ""),
                "llm_comment": str(row.get("llm_comment") or row.get("summary") or ""),
                "justification": str(row.get("justification") or ""),
                "review_status": str(row.get("review_status") or ""),
                "decision_source": str(row.get("decision_source") or ""),
                "result_origin": str(row.get("result_origin") or ("cache" if _safe_bool(row.get("cache_hit")) else "llm")),
                "result_validation_status": str(row.get("result_validation_status") or "validated"),
                "fallback_applied": _safe_bool(row.get("fallback_applied") if row.get("fallback_applied") is not None else False),
                "started_at": str(row.get("started_at") or ""),
                "completed_at": str(row.get("completed_at") or ""),
                "cache_stored_at": _safe_int(row.get("cache_stored_at")) or None,
                "cache_hit": _safe_bool(row.get("cache_hit")),
                "cache_pair_hash": str(row.get("cache_pair_hash") or row.get("pair_hash") or ""),
                "llm_success": _safe_bool(row.get("llm_success")),
                "model_name": str(row.get("model_name") or ""),
                "prompt_version": str(row.get("prompt_version") or ""),
                "prompt_text_a_literal": str(row.get("prompt_text_a_literal") or row.get("text_a") or ""),
                "prompt_text_b_literal": str(row.get("prompt_text_b_literal") or row.get("text_b") or ""),
                "prompt_messages": row.get("prompt_messages") if isinstance(row.get("prompt_messages"), list) else [],
                "relation_type": str(row.get("relation_type") or ""),
                "relation_notes": str(row.get("relation_notes") or ""),
                "related_block_ids": [
                    int(item)
                    for item in related_block_ids
                    if str(item).isdigit() and int(item) > 0
                ] if isinstance(related_block_ids, list) else [],
                "related_blocks": _relation_links_from_ids([
                    int(item)
                    for item in related_block_ids
                    if str(item).isdigit() and int(item) > 0
                ]) if isinstance(related_block_ids, list) else [],
                "source_spans": _normalize_source_spans(row),
                "pairing": row.get("pairing") if isinstance(row.get("pairing"), dict) else {},
                "chunk_index_a": _safe_int(row.get("chunk_index_a")),
                "chunk_index_b": _safe_int(row.get("chunk_index_b")),
                "offset_start_a": _safe_int(row.get("offset_start_a")),
                "offset_end_a": _safe_int(row.get("offset_end_a")),
                "offset_start_b": _safe_int(row.get("offset_start_b")),
                "offset_end_b": _safe_int(row.get("offset_end_b")),
                "block_word_count_a": _safe_int(row.get("block_word_count_a")),
                "block_word_count_b": _safe_int(row.get("block_word_count_b")),
                "block_size_words": _safe_int(row.get("block_size_words")),
                "block_overlap_words": _safe_int(row.get("block_overlap_words")),
                "alignment_score": float(row.get("alignment_score") or 0.0),
                "alignment_strategy": str(row.get("alignment_strategy") or ""),
                "reanchored": _safe_bool(row.get("reanchored")),
            }
        )
    return normalized


def _resolve_all_comparison_rows(result: dict) -> list[dict]:
    block_diffs = result.get("block_diffs") or []
    preferred_rows = result.get("comparison_rows")
    if not isinstance(preferred_rows, list) or not preferred_rows:
        preferred_rows = result.get("pair_records")
    if not isinstance(preferred_rows, list) or not preferred_rows:
        preferred_rows = result.get("ai_comparison_rows")
    rebuilt_rows = resolve_comparison_rows(preferred_rows, block_diffs)
    return _normalize_comparison_rows(rebuilt_rows)


def _resolve_comparison_rows(result: dict) -> list[dict]:
    return filter_comparison_rows(_resolve_all_comparison_rows(result))




def _paginate_rows(
    *,
    filtered_rows: list[dict],
    all_rows_count: int,
    result: dict,
    offset: int = 0,
    limit: Optional[int] = None,
) -> dict:
    total_detected = len(filtered_rows)
    safe_offset = max(0, int(offset or 0))
    safe_limit = None if limit in (None, "", 0) else max(1, int(limit))
    page_end = safe_offset + safe_limit if safe_limit is not None else None
    page_rows = filtered_rows[safe_offset:page_end]
    returned = len(page_rows)
    next_offset = safe_offset + returned
    has_more = next_offset < total_detected
    return {
        "rows": page_rows,
        "meta": {
            "pagination": {
                "offset": safe_offset,
                "limit": safe_limit,
                "returned": returned,
                "total": total_detected,
                "has_more": has_more,
                "next_offset": next_offset if has_more else None,
                "truncated": bool(result.get("block_diffs_truncated")),
            },
            "audit": {
                "all_rows_count": int(all_rows_count),
                "filtered_rows_count": int(total_detected),
                "unchanged_rows_count": max(int(all_rows_count) - int(total_detected), 0),
            },
            "cache": {
                "policy": "no-store",
            },
        },
    }


def _build_result_view_model(
    sid: str,
    result: Optional[dict],
    progress_state: Optional[dict] = None,
    *,
    block_offset: int = 0,
    block_limit: Optional[int] = None,
) -> dict:
    result = result or {}
    all_rows = _resolve_all_comparison_rows(result)
    filtered_rows = filter_comparison_rows(all_rows)
    row_page = _paginate_rows(
        filtered_rows=filtered_rows,
        all_rows_count=len(all_rows),
        result=result,
        offset=block_offset,
        limit=block_limit,
    )
    rows = row_page["rows"]
    progress_state = _sanitize_progress_state(progress_state)
    status = progress_state.get("status") if progress_state else ("done" if result else "processing")
    ai_compliance = result.get("ai_compliance") or {}
    extraction = result.get("extraction") or {}
    block_size_words = int(
        (extraction.get("pairing") or {}).get("block_size_words")
        or ai_compliance.get("block_size_words")
        or 0
    )
    block_overlap_words = int(
        (extraction.get("pairing") or {}).get("block_overlap_words")
        or ai_compliance.get("block_overlap_words")
        or 0
    )
    row_page["meta"]["cache"] = {
        "policy": "no-store",
        "resolved_from_cache": int(ai_compliance.get("blocks_resolved_from_cache") or 0),
        "resolved_by_llm": int(ai_compliance.get("blocks_resolved_by_llm") or 0),
        "failed_blocks": int(ai_compliance.get("blocks_failed") or 0),
        "block_size_words": block_size_words,
        "block_overlap_words": block_overlap_words,
        "model_name": str(ai_compliance.get("model_name") or ""),
        "ai_only_enabled": bool(
            ai_compliance.get("ai_only_enabled")
            if ai_compliance.get("ai_only_enabled") is not None
            else _get_compare_runtime_settings()["ai_only_enabled"]
        ),
        "comparison_mode": str(
            ai_compliance.get("comparison_mode") or _get_compare_runtime_settings()["comparison_mode"]
        ),
    }
    extraction_pairing = extraction.get("pairing") if isinstance(extraction.get("pairing"), dict) else {}
    extraction_segmentation = extraction.get("segmentation") if isinstance(extraction.get("segmentation"), dict) else {}
    pair_records_schema = result.get("pair_records_schema") if isinstance(result.get("pair_records_schema"), dict) else {}
    row_contract_source = "comparison_rows" if isinstance(result.get("comparison_rows"), list) and result.get("comparison_rows") else "pair_records"
    row_page["meta"]["downloads"] = {
        "report_file": str(result.get("report_file") or ""),
        "report_download_url": str(result.get("report_download_url") or ""),
        "export_json_url": str(result.get("export_json_url") or f"/api/comparador/resultado/{sid}/export.json"),
    }
    row_page["meta"]["segmentation"] = extraction_segmentation
    row_page["meta"]["pairing"] = {
        **extraction_pairing,
        "row_contract_source": row_contract_source,
        "row_contract_version": str(pair_records_schema.get("version") or "pair-record-v1"),
        "row_contract_columns": pair_records_schema.get("columns") if isinstance(pair_records_schema.get("columns"), list) else [],
    }
    row_page["meta"]["row_formation"] = {
        "source": row_contract_source,
        "description": (
            "Cada fila visible proviene de ventanas de foco revisadas por LLM con contexto alrededor del cambio."
            if row_contract_source == "comparison_rows"
            else "Cada fila proviene de un registro canónico por pareja generado por el worker y no de reconstrucciones en cliente."
        ),
    }
    row_page["meta"]["diagnostics"] = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
    return {
        "sid": sid,
        "status": status,
        "progress": progress_state or {},
        "error": (progress_state or {}).get("error"),
        "ok": result.get("ok"),
        "reason": result.get("reason"),
        "rows": rows,
        "meta": row_page["meta"],
    }


def _set_no_store_headers(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


_START_TIME = time.monotonic()

@app.get("/health", tags=["health"])
async def health():
    uptime_seconds = int(time.monotonic() - _START_TIME)
    return {
        "status": "ok",
        "service": os.getenv("SERVICE_NAME", "unknown-service"),
        "uptime_seconds": uptime_seconds,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **{
            "ai_only_enabled": _get_compare_runtime_settings()["ai_only_enabled"],
            "comparison_mode": _get_compare_runtime_settings()["comparison_mode"],
        },
    }


@app.get("/metrics", response_model=dict)
async def metrics_endpoint(response: Response):
    _set_no_store_headers(response)
    return metrics_snapshot()

@app.get("/capabilities", response_model=dict)
async def get_capabilities_endpoint(response: Response):
    _set_no_store_headers(response)
    return build_capabilities_payload(_get_compare_runtime_settings())


@app.get("/workers/health", response_model=dict)
async def workers_health_endpoint(
    response: Response,
    auth_ctx: dict = Depends(get_current_auth_compare),
):
    _set_no_store_headers(response)
    queue = getattr(app.state, "compare_queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="La cola de workers no está disponible.")

    diagnostics = queue.worker_diagnostics()
    diagnostics["service"] = "comparador"
    diagnostics["required_active_workers"] = (
        os.getenv("COMPARE_REQUIRE_ACTIVE_WORKERS", "true").strip().lower()
        not in {"0", "false", "no", "off"}
    )
    diagnostics["redis"] = {
        "host": os.getenv("REDIS_HOST", "localhost"),
        "port": int(os.getenv("REDIS_PORT", "6379")),
        "db": int(os.getenv("REDIS_DB", "0")),
    }
    diagnostics["authenticated_user_id"] = (auth_ctx or {}).get("user_id")
    return diagnostics

@app.get("/csrf-token", response_model=dict)
async def get_csrf_token_endpoint(request: Request, response: Response):
    """
    Devuelve un token CSRF y lo deja también en una cookie (double-submit).
    Si ya existe, lo reutiliza (evita desincronizaciones con múltiples pestañas).
    """
    token = request.cookies.get(CSRF_COOKIE_NAME) or generate_csrf_token()
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        httponly=False,  # el front React puede leerlo si quiere
        secure=SECURITY_SETTINGS["cookie_secure"],
        samesite=SECURITY_SETTINGS["cookie_same_site"],
    )
    _set_no_store_headers(response)
    return {"csrf_token": token}

@app.post("/comparar", response_model=dict)
async def comparar(
    request: Request,
    response: Response,
    file_a: UploadFile = File(...),
    file_b: UploadFile = File(...),
    soffice: Optional[str] = Form(None),
    euro_mode: str = Form("strict"),
    min_euro: Optional[float] = Form(None),
    engine: str = Form("auto"),  # <-- motor configurable
    auth_ctx: dict = Depends(get_current_auth_compare),
):
    _set_no_store_headers(response)
    from .metrics import (
        inc_compare_request, inc_compare_error, observe_file_size, record_av_result,
    )

    start_t = time.time()

    # Normaliza valor de engine
    engine = (engine or "builtin").lower()
    if engine not in ("builtin", "docling", "auto"):
        engine = "builtin"
    soffice = sanitize_soffice_value(soffice)

    # Métricas: request /comparar
    inc_compare_request(engine)

    # CSRF double-submit (cookie+cabecera)
    try:
        validate_csrf(request)
    except HTTPException:
        inc_compare_error("csrf", engine)
        raise

    # Identidad y sesión
    user_id = (auth_ctx or {}).get("user_id")
    session_data = (auth_ctx or {}).get("session")
    user_role = (auth_ctx or {}).get("role")

    if not user_id or not session_data:
        inc_compare_error("unauthenticated", engine)
        raise HTTPException(status_code=401, detail="No autenticado o sesión expirada.")

    try:
        current_runtime_settings = _preflight_compare_runtime(startup_check=False)
    except CompareRuntimeReadinessError as exc:
        logging.warning("Comparador: preflight del runtime no disponible: %s", exc)
        raise HTTPException(status_code=503, detail=exc.public_detail) from exc

    # Tamaño máximo defensivo
    max_mb = get_text_compare_max_file_mb()
    max_bytes = max_mb * 1024 * 1024

    # Validaciones de nombres/extensiones
    name_a = safe_name(file_a.filename or "A")
    name_b = safe_name(file_b.filename or "B")
    logging.info(
        "Comparador: solicitud recibida user_id=%s role=%s files=%s,%s engine=%s runtime_provider=%s runtime_model=%s",
        user_id,
        user_role,
        name_a,
        name_b,
        engine,
        current_runtime_settings.get("llm_provider"),
        current_runtime_settings.get("llm_model_name"),
    )
    if not ext_allowed(name_a) or not ext_allowed(name_b):
        inc_compare_error("unsupported_ext", engine)
        bad_ext = os.path.splitext(name_a if not ext_allowed(name_a) else name_b)[1].lower()
        raise HTTPException(
            status_code=415,
            detail=format_unsupported_extension_message(bad_ext or "sin extensión"),
        )

    # Leer a memoria (para AV y size check)
    content_a = await file_a.read()
    content_b = await file_b.read()

    if len(content_a) == 0 or len(content_b) == 0:
        inc_compare_error("empty_file", engine)
        raise HTTPException(status_code=400, detail="Alguno de los archivos está vacío.")

    # Métricas: tamaños subidos
    observe_file_size("A", name_a, len(content_a))
    observe_file_size("B", name_b, len(content_b))

    if len(content_a) > max_bytes or len(content_b) > max_bytes:
        inc_compare_error("file_too_large", engine)
        raise HTTPException(
            status_code=413,
            detail=format_file_too_large_backend_message(),
        )

    try:
        validate_upload_payload(
            filename=name_a,
            content=content_a,
            declared_mime=file_a.content_type,
            max_bytes=max_bytes,
        )
        validate_upload_payload(
            filename=name_b,
            content=content_b,
            declared_mime=file_b.content_type,
            max_bytes=max_bytes,
        )
    except ValueError as exc:
        inc_compare_error("invalid_mime_or_signature", engine)
        raise HTTPException(status_code=415, detail=str(exc)) from exc

    # 1) Escaneo AV
    av_a = await scan_with_clamav(content_a, filename=name_a)
    av_b = await scan_with_clamav(content_b, filename=name_b)

    # Métricas: resultados AV
    record_av_result(av_a)
    record_av_result(av_b)

    def _av_guard(av_res, fname):
        status = (av_res or {}).get("status")
        if status == "INFECTED":
            inc_compare_error("av_infected", engine)
            virus = (av_res or {}).get("virus_name") or "malware"
            raise HTTPException(
                status_code=400,
                detail=f"{fname}: bloqueado por antivirus (detectado: {virus}).",
            )
        if status == "ERROR":
            inc_compare_error("av_error", engine)
            raise HTTPException(
                status_code=502,
                detail=f"{fname}: no se pudo analizar con antivirus; el archivo ha sido rechazado por seguridad.",
            )

    try:
        _av_guard(av_a, name_a)
        _av_guard(av_b, name_b)
    except HTTPException:
        # Auditoría de bloqueo AV (igual que en tu código original)
        try:
            from .config.models import AuditLog, Conversation
            
            gen = get_db()
            db2 = next(gen)
            try:
                conv = Conversation(
                    user_id=user_id,
                    conversation_text=f"Comparación AV bloqueada: {name_a} vs {name_b}",
                    created_at=datetime.now(timezone.utc),
                )
                db2.add(conv)
                db2.commit()
                db2.refresh(conv)
                conv_id = int(conv.id)

                def _audit(av_res, fname):
                    db2.add(
                        AuditLog(
                            user_id=user_id,
                            entity_name="CompareFileScan",
                            entity_id=conv_id,
                            action="CREATE",
                            old_data=None,
                            new_data={
                                "filename": fname,
                                "av_status": (av_res or {}).get("status"),
                                "av_virus": (av_res or {}).get("virus_name"),
                                "av_raw_result": (av_res or {}).get("raw_result"),
                                "av_error": (av_res or {}).get("error"),
                                "av_duration_s": (av_res or {}).get("duration_s"),
                                "size_bytes": (av_res or {}).get("size_bytes"),
                                "decision": "BLOCKED",
                            },
                            timestamp=datetime.now(timezone.utc),
                        )
                    )

                _audit(av_a, name_a)
                _audit(av_b, name_b)
                db2.commit()
            finally:
                try:
                    next(gen)
                except StopIteration:
                    pass
        except Exception:
            logging.exception(
                "Comparador: no se pudo persistir la auditoría de bloqueo AV para %s vs %s",
                name_a,
                name_b,
            )
        raise

    # 2) Persistencia efímera (Redis) + auditoría inicial BD
    conv_id_redis = None
    try:
        await get_conversation_to_redis(user_id=user_id)
        conv_id_redis = int(user_id)
    except Exception as e:
        logging.warning("No se pudo crear/recuperar conversación en Redis: %s", e)

    conv_row_id = None
    try:
        from .config.models import Conversation, AuditLog
        
        gen = get_db()
        db2 = next(gen)
        try:
            conv_row = Conversation(
                user_id=user_id,
                conversation_text=f"Comparación: {name_a} vs {name_b}",
                created_at=datetime.now(timezone.utc),
            )
            db2.add(conv_row)
            db2.commit()
            db2.refresh(conv_row)
            conv_row_id = int(conv_row.id)

            db2.add(
                AuditLog(
                    user_id=user_id,
                    entity_name="CompareJob",
                    entity_id=conv_row_id,
                    action="CREATE",
                    old_data=None,
                    new_data=get_compare_trace_policy().sanitize_audit_payload({
                        "role": user_role,
                        "files": [name_a, name_b],
                        "limits": {"max_mb": max_mb},
                        "opts": {
                            "euro_mode": euro_mode,
                            "min_euro": min_euro,
                            "soffice": soffice,
                            "engine": engine,
                        },
                    }),
                    timestamp=datetime.now(timezone.utc),
                )
            )
            for fname, av in ((name_a, av_a), (name_b, av_b)):
                db2.add(
                    AuditLog(
                        user_id=user_id,
                        entity_name="CompareFileScan",
                        entity_id=conv_row_id,
                        action="CREATE",
                        old_data=None,
                        new_data={
                            "filename": fname,
                            "av_status": (av or {}).get("status"),
                            "av_virus": (av or {}).get("virus_name"),
                            "av_raw_result": (av or {}).get("raw_result"),
                            "av_error": (av or {}).get("error"),
                            "av_duration_s": (av or {}).get("duration_s"),
                            "size_bytes": (av or {}).get("size_bytes"),
                            "decision": "ALLOWED",
                        },
                        timestamp=datetime.now(timezone.utc),
                    )
                )
            db2.commit()
        finally:
            try:
                next(gen)
            except StopIteration:
                pass
    except Exception:
        logging.exception(
            "Comparador: no se pudo crear la conversación/auditoría inicial en BD para job %s vs %s",
            name_a,
            name_b,
        )

    queue = getattr(app.state, "compare_queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="La cola de workers no está disponible.")

    job_store = get_job_store()
    job_signature = hashlib.sha256(
        json.dumps(
            {
                "user_id": int(user_id),
                "file_a_name": name_a,
                "file_b_name": name_b,
                "file_a_sha256": hashlib.sha256(content_a).hexdigest(),
                "file_b_sha256": hashlib.sha256(content_b).hexdigest(),
                "opts": {
                    "euro_mode": euro_mode,
                    "min_euro": min_euro,
                    "soffice": soffice,
                    "engine": engine,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    reuse_identical_jobs = (
        os.getenv("COMPARE_REUSE_IDENTICAL_JOBS", "false").strip().lower()
        in {"1", "true", "yes", "si", "on"}
    )
    if reuse_identical_jobs:
        reusable_job = await job_store.find_reusable_job(job_signature)
        if reusable_job is not None:
            record_queue_event("job_reused")
            logging.info(
                "Comparador: reutilizando job existente sid=%s signature=%s files=%s,%s",
                reusable_job.get("sid"),
                job_signature[:12],
                name_a,
                name_b,
            )
            return {
                "sid": reusable_job.get("sid"),
                "reused_existing_job": True,
                **{
                    "ai_only_enabled": _get_compare_runtime_settings()["ai_only_enabled"],
                    "comparison_mode": _get_compare_runtime_settings()["comparison_mode"],
                },
            }

    active_workers = queue.count_active_workers()
    require_active_workers = os.getenv("COMPARE_REQUIRE_ACTIVE_WORKERS", "true").strip().lower() not in {"0", "false", "no", "off"}
    if require_active_workers and active_workers <= 0:
        diagnostics = queue.log_worker_diagnostics(
            reason="compare_request_without_active_workers",
            level=logging.ERROR,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "No hay workers activos del comparador. Arranca al menos un worker dedicado "
                "(por ejemplo: `cd comp_docs && python -m app.compare_worker`) y reintenta. "
                f"Diagnóstico: workers_key={diagnostics.get('workers_key')} "
                f"ttl={diagnostics.get('worker_ttl_seconds')}s "
                f"active={diagnostics.get('active_worker_count')} "
                f"stale={diagnostics.get('stale_worker_count')}."
            ),
        )
    sid = uuid.uuid4().hex
    job_dir = os.path.join(DATA_DIR, sid)
    inputs_dir = os.path.join(job_dir, "inputs")
    runtime_dir = os.path.join(job_dir, "runtime")
    os.makedirs(inputs_dir, exist_ok=True)
    os.makedirs(runtime_dir, exist_ok=True)

    try:
        created_job = await job_store.create_job(sid=sid, owner_user_id=int(user_id), job_dir=job_dir)
        created_job["conv_row_id"] = conv_row_id
        created_job["requested_by_role"] = user_role
        created_job["input_files"] = [name_a, name_b]
        created_job["job_signature"] = job_signature
        await job_store.save_job(sid, created_job)
        if reuse_identical_jobs:
            await job_store.bind_signature(job_signature, sid)
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        logging.exception("No se pudo crear el job compartido %s", sid)
        raise HTTPException(status_code=503, detail="No se pudo inicializar el job compartido.") from e

    pa = os.path.join(inputs_dir, name_a)
    pb = os.path.join(inputs_dir, name_b)
    try:
        with open(pa, "wb") as fa:
            fa.write(content_a)
        with open(pb, "wb") as fb:
            fb.write(content_b)
    except Exception as e:
        logging.exception("No se pudieron persistir los artefactos del job %s", sid)
        job_store.mark_failed(
            sid,
            "No se pudieron persistir los artefactos del job.",
        )
        raise HTTPException(status_code=500, detail="No se pudieron persistir los artefactos del job.") from e

    meta_common = {
        "sid": sid,
        "job_dir": job_dir,
        "av": {name_a: av_a, name_b: av_b},
        "opts": {
            "euro_mode": euro_mode,
            "min_euro": min_euro,
            "soffice": soffice,
            "engine": engine,
        },
    }
    task_payload = CompareTaskPayload(
        sid=sid,
        job_dir=job_dir,
        file_a_path=pa,
        file_b_path=pb,
        file_a_name=name_a,
        file_b_name=name_b,
        user_id=int(user_id),
        conv_id_redis=conv_id_redis,
        conv_row_id=conv_row_id,
        av=meta_common["av"],
        opts=meta_common["opts"],
    )

    try:
        await add_ephemeral_file(
            user_id=user_id,
            filename=name_a,
            text="",
            ttl=3600,
            meta={**meta_common, "file_role": "A"},
        )
        await add_ephemeral_file(
            user_id=user_id,
            filename=name_b,
            text="",
            ttl=3600,
            meta={**meta_common, "file_role": "B"},
        )
        await set_user_context(user_id, f"compare_sid:{sid}", ttl=3600)
        await persist_compare_event_async(
            conv_id_redis,
            build_compare_event(
                "compare_start",
                sid,
                engine,
                [name_a, name_b],
                "queued",
                av=meta_common["av"],
                opts=meta_common["opts"],
            ),
            sid=sid,
            event_name="compare_start",
        )
    except Exception:
        logging.exception("Comparador: no se pudo registrar trazabilidad inicial en Redis para job %s", sid)

    try:
        if conv_row_id is not None:
            gen = get_db()
            db2 = next(gen)
            try:
                db2.add(
                    AuditLog(
                        user_id=user_id,
                        entity_name="CompareJob",
                        entity_id=conv_row_id,
                        action="UPDATE",
                        old_data=None,
                        new_data=get_compare_trace_policy().sanitize_audit_payload({"event": "queued", "sid": sid, "job_dir": job_dir}),
                        timestamp=datetime.now(timezone.utc),
                    )
                )
                db2.commit()
            finally:
                try:
                    next(gen)
                except StopIteration:
                    pass
    except Exception:
        logging.exception("Comparador: no se pudo registrar el estado queued para job %s", sid)

    try:
        queue.enqueue(task_payload)
        set_compare_queue_depth(queue.depth())
        record_queue_event("job_enqueued")
        logging.info(
            "Comparador: job %s encolado para %s vs %s (workers_activos=%s, conv_row_id=%s)",
            sid,
            name_a,
            name_b,
            active_workers,
            conv_row_id,
        )
    except Exception as e:
        logging.exception("No se pudo encolar el job compartido %s", sid)
        job_store.mark_failed(sid, "No se pudo encolar el job compartido.")
        raise HTTPException(status_code=503, detail="No se pudo encolar el job compartido.") from e

    return {
        "sid": sid,
        **{
            "ai_only_enabled": _get_compare_runtime_settings()["ai_only_enabled"],
            "comparison_mode": _get_compare_runtime_settings()["comparison_mode"],
        },
    }


# --- PROGRESS ---
@app.get("/progress/{sid}")
async def get_progress(sid: str, response: Response, auth_ctx: dict = Depends(get_current_auth_compare)):
    _set_no_store_headers(response)
    job = await _get_owned_job_or_404(sid, auth_ctx)
    _audit_compare_access(
        user_id=(auth_ctx or {}).get("user_id"),
        job=job,
        action="UPDATE",
        route="/progress/{sid}",
    )
    pr = _job_progress_view(job)
    if not pr:
        raise HTTPException(status_code=404, detail="Progreso no disponible o expirado")
    progress_payload = dict(pr)
    progress_payload["metrics"] = {
        "queue": metrics_snapshot().get("queue", {}),
    }
    return progress_payload

@app.get("/resultado/{sid}/json")
async def resultado_json(
    sid: str,
    response: Response,
    offset: int = 0,
    limit: Optional[int] = None,
    auth_ctx: dict = Depends(get_current_auth_compare),
):
    _set_no_store_headers(response)
    job = await _get_owned_job_or_404(sid, auth_ctx)
    _audit_compare_access(
        user_id=(auth_ctx or {}).get("user_id"),
        job=job,
        action="UPDATE",
        route="/resultado/{sid}/json",
        extra={"offset": offset, "limit": limit},
    )
    progress_state = _job_progress_view(job)
    result_payload = _job_result_view(job)
    return _build_result_view_model(
        sid=sid,
        result=result_payload,
        progress_state=progress_state,
        block_offset=offset,
        block_limit=limit,
    )


@app.get("/resultado/{sid}/block-diffs.json")
async def resultado_block_diffs_export(sid: str, auth_ctx: dict = Depends(get_current_auth_compare)):
    job = await _get_owned_job_or_404(sid, auth_ctx)
    _audit_compare_access(
        user_id=(auth_ctx or {}).get("user_id"),
        job=job,
        action="UPDATE",
        route="/resultado/{sid}/block-diffs.json",
    )
    progress_state = _job_progress_view(job)
    result_payload = _job_result_view(job)
    if not result_payload:
        raise HTTPException(status_code=404, detail="Resultado no disponible o expirado")

    all_comparison_rows = _resolve_all_comparison_rows(result_payload)
    filtered_comparison_rows = filter_comparison_rows(all_comparison_rows)

    export_payload = {
        "sid": sid,
        "status": (progress_state or {}).get("status") or ("done" if result_payload else "processing"),
        "comparison_rows": filtered_comparison_rows,
        "all_comparison_rows": all_comparison_rows,
        "block_diffs": result_payload.get("block_diffs") or [],
        "block_diffs_truncated": bool(result_payload.get("block_diffs_truncated")),
        "block_diffs_total_detected": int(result_payload.get("block_diffs_total_detected") or len(result_payload.get("block_diffs") or [])),
        "block_diffs_returned": int(result_payload.get("block_diffs_returned") or len(result_payload.get("block_diffs") or [])),
        "meta": {
            "result_kind": "block_diffs_export",
            "result_json_url": f"/api/comparador/resultado/{sid}/json",
            "audit": {
                "all_rows_count": len(all_comparison_rows),
                "filtered_rows_count": len(filtered_comparison_rows),
            },
        },
    }
    export_body = json.dumps(export_payload, ensure_ascii=False, indent=2).encode("utf-8")
    return _set_no_store_headers(Response(
        content=export_body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="block-diffs-{sid}.json"'},
    ))


@app.get("/resultado/{sid}/export.json")
async def resultado_export_json(sid: str, auth_ctx: dict = Depends(get_current_auth_compare)):
    job = await _get_owned_job_or_404(sid, auth_ctx)
    _audit_compare_access(
        user_id=(auth_ctx or {}).get("user_id"),
        job=job,
        action="UPDATE",
        route="/resultado/{sid}/export.json",
    )
    result_payload = _job_result_view(job)
    if not result_payload:
        raise HTTPException(status_code=404, detail="Resultado no disponible o expirado")
    export_body = json.dumps(result_payload, ensure_ascii=False, indent=2).encode("utf-8")
    return _set_no_store_headers(Response(
        content=export_body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="resultado-{sid}.json"'},
    ))

# --- DESCARGAR ---
@app.get("/descargar/{sid}/{fname}")
async def descargar(sid: str, fname: str, auth_ctx: dict = Depends(get_current_auth_compare)):
    job = await _get_owned_job_or_404(sid, auth_ctx)
    _audit_compare_access(
        user_id=(auth_ctx or {}).get("user_id"),
        job=job,
        action="UPDATE",
        route="/descargar/{sid}/{fname}",
        extra={"fname": fname},
    )
    result_payload = _job_result_view(job)
    if not result_payload:
        raise HTTPException(status_code=404, detail="Resultado no disponible o expirado")
    job_dir = job["job_dir"]
    safe_fname = safe_name(fname)
    report_path = str(result_payload.get("report_path") or "")
    if safe_fname == result_payload.get("report_file") and report_path and os.path.isfile(report_path):
        return FileResponse(report_path, filename=safe_fname)
    if safe_fname == "informe.pdf" and report_path and os.path.isfile(report_path):
        return FileResponse(report_path, filename=os.path.basename(report_path))
    path = os.path.join(job_dir, "inputs", safe_fname)
    if not os.path.isfile(path):
        path = os.path.join(job_dir, safe_fname)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return FileResponse(path, filename=safe_fname)