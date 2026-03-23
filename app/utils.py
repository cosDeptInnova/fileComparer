import asyncio
import json
import logging
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException, Request, status
from jose import JWTError, jwt

try:
    import redis.asyncio as aioredis
except Exception:  # pragma: no cover
    aioredis = None

_REDIS_CORE = None
_REDIS_CONV = None
_LOCAL_ENVIRONMENTS = {"", "dev", "development", "local", "test", "testing"}
_DEFAULT_SECRET_KEY = "secretkey123"

SECRET_KEY = os.getenv("COSMOS_SECRET_KEY", _DEFAULT_SECRET_KEY)
ALGORITHM = os.getenv("COSMOS_JWT_ALG", "HS256")
ACCESS_TOKEN_COOKIE_NAME = os.getenv("ACCESS_TOKEN_COOKIE_NAME", "access_token")


def ensure_auth_secret_is_safe() -> None:
    env_name = (os.getenv("ENV", os.getenv("ENVIRONMENT", "development")) or "development").strip().lower()
    if env_name in _LOCAL_ENVIRONMENTS:
        return
    if not SECRET_KEY or SECRET_KEY == _DEFAULT_SECRET_KEY:
        raise RuntimeError(
            "COSMOS_SECRET_KEY debe configurarse con un valor robusto y distinto del valor por defecto en producción."
        )


def init_redis_clients(core_client, conv_client=None) -> None:
    global _REDIS_CORE, _REDIS_CONV
    _REDIS_CORE = core_client
    _REDIS_CONV = conv_client or core_client


def get_redis_core_client():
    if _REDIS_CORE is None:
        raise RuntimeError("Redis principal no inicializado")
    return _REDIS_CORE


def get_redis_conversation_client():
    if _REDIS_CONV is None:
        raise RuntimeError("Redis de conversaciones no inicializado")
    return _REDIS_CONV


def build_redis_clients_from_env():
    if aioredis is None:
        raise RuntimeError("redis.asyncio no está disponible")
    host = os.getenv("REDIS_HOST", "localhost").strip() or "localhost"
    port = int(os.getenv("REDIS_PORT", "6379"))
    password = os.getenv("REDIS_PASSWORD") or None
    socket_timeout = float(os.getenv("REDIS_SOCKET_TIMEOUT_SECONDS", "5"))
    common_kwargs = {
        "host": host,
        "port": port,
        "password": password,
        "socket_connect_timeout": socket_timeout,
        "socket_timeout": socket_timeout,
        "health_check_interval": max(5, int(float(os.getenv("REDIS_HEALTH_CHECK_INTERVAL_SECONDS", "15")))),
        "retry_on_timeout": True,
        "decode_responses": True,
    }
    core = aioredis.Redis(db=int(os.getenv("REDIS_DB", "0")), **common_kwargs)
    conv = aioredis.Redis(db=int(os.getenv("REDIS_CONVERSATION_DB", "2")), **common_kwargs)
    return core, conv


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def validate_csrf_double_submit(
    request: Request,
    *,
    cookie_name: str,
    header_name: str,
    error_detail: str,
) -> None:
    cookie_token = request.cookies.get(cookie_name)
    header_token = request.headers.get(header_name)
    if not cookie_token or not header_token or not secrets.compare_digest(cookie_token, header_token):
        raise HTTPException(status_code=403, detail=error_detail)


async def add_ephemeral_file(
    *,
    user_id: int,
    filename: str,
    text: str,
    ttl: int = 3600,
    meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload = {
        "filename": filename,
        "text": text,
        "meta": meta or {},
        "created_at": int(time.time()),
    }
    client = get_redis_conversation_client()
    key = f"compare:ephemeral:{user_id}:{secrets.token_hex(8)}"
    await client.set(key, json.dumps(payload, ensure_ascii=False), ex=ttl)
    return payload


async def get_ephemeral_files(user_id: int) -> list[dict[str, Any]]:
    client = get_redis_conversation_client()
    pattern = f"compare:ephemeral:{user_id}:*"
    keys: list[str] = []
    async for key in client.scan_iter(match=pattern, count=100):
        keys.append(key)
    if not keys:
        return []
    values = await client.mget(keys)
    items: list[dict[str, Any]] = []
    for value in values:
        if not value:
            continue
        try:
            items.append(json.loads(value))
        except json.JSONDecodeError:
            logging.warning("No se pudo deserializar archivo efímero de comparación")
    return items


async def get_conversation_to_redis(user_id: int) -> str:
    client = get_redis_conversation_client()
    key = f"compare:conversation:{user_id}"
    await client.setnx(key, json.dumps({"user_id": int(user_id), "created_at": int(time.time())}))
    await client.expire(key, 86400)
    return key


async def reset_conversation_in_redis(user_id: int) -> None:
    client = get_redis_conversation_client()
    await client.delete(f"compare:conversation:{user_id}")


async def set_user_context(user_id: int, context: str, ttl: int = 3600) -> None:
    client = get_redis_conversation_client()
    await client.set(f"compare:user-context:{user_id}", context, ex=ttl)


def extract_raw_bearer_token(request: Request) -> Optional[str]:
    auth_header = request.headers.get("Authorization", "")
    cookie_token = request.cookies.get(ACCESS_TOKEN_COOKIE_NAME)

    if auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1].strip()

    if cookie_token and cookie_token.lower().startswith("bearer "):
        return cookie_token.split(" ", 1)[1].strip()

    return None


async def get_session_from_redis(user_id: int) -> Optional[dict[str, Any]]:
    client = get_redis_core_client()
    session = await client.get(f"session:{int(user_id)}")
    if not session:
        return None
    try:
        return json.loads(session)
    except json.JSONDecodeError:
        logging.warning("Comparador: JSON corrupto para session:%s", user_id)
        return None


def verify_token(request: Request) -> dict[str, Any]:
    token_str = extract_raw_bearer_token(request)
    if not token_str:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado: Token no presente.",
        )

    try:
        payload = jwt.decode(token_str, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token no válido o expirado.",
        ) from exc

    email = payload.get("sub")
    user_id = payload.get("user_id")
    if not email or user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token no válido.",
        )
    return payload


async def get_current_auth_compare(request: Request) -> dict[str, Any]:
    """
    Dependencia de autenticación del comparador alineada con chat_document:
    valida JWT (cookie/header) y resuelve la sesión desde Redis, sin depender
    de la cookie de SessionMiddleware para los endpoints API protegidos.
    """
    token_payload = verify_token(request)
    user_id = token_payload.get("user_id")
    if user_id in (None, ""):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido (sin user_id).",
        )

    try:
        user_id = int(user_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido (user_id no numérico).",
        ) from exc

    try:
        session_data = await get_session_from_redis(user_id)
    except Exception:
        logging.exception("Comparador: error recuperando session:%s desde Redis", user_id)
        session_data = None

    if not isinstance(session_data, dict):
        session_data = {}

    return {
        "user_id": user_id,
        "role": token_payload.get("role"),
        "token_payload": token_payload,
        "session": session_data,
    }


async def scan_with_clamav(content: bytes, *, filename: str = "upload.bin") -> dict[str, Any]:
    start = time.perf_counter()
    mode = (os.getenv("COMPARE_AV_MODE", "stub").strip().lower() or "stub")
    if mode in {"off", "disabled", "stub", "allow"}:
        return {
            "status": "CLEAN",
            "virus_name": None,
            "raw_result": f"stub:{mode}",
            "error": None,
            "duration_s": round(time.perf_counter() - start, 6),
            "size_bytes": len(content),
            "filename": filename,
        }

    try:
        import pyclamd  # type: ignore

        host = os.getenv("CLAMD_HOST", "localhost")
        port = int(os.getenv("CLAMD_PORT", "3310"))
        cd = pyclamd.ClamdNetworkSocket(host=host, port=port)
        result = await asyncio.to_thread(cd.scan_stream, content)
        duration = round(time.perf_counter() - start, 6)
        if not result:
            return {
                "status": "CLEAN",
                "virus_name": None,
                "raw_result": "OK",
                "error": None,
                "duration_s": duration,
                "size_bytes": len(content),
                "filename": filename,
            }
        _, details = next(iter(result.items()))
        virus_name = details[1] if isinstance(details, tuple) and len(details) > 1 else "malware"
        return {
            "status": "INFECTED",
            "virus_name": virus_name,
            "raw_result": str(result),
            "error": None,
            "duration_s": duration,
            "size_bytes": len(content),
            "filename": filename,
        }
    except Exception as exc:
        logging.warning("Fallo antivirus ClamAV para %s: %s", filename, exc)
        return {
            "status": "ERROR",
            "virus_name": None,
            "raw_result": None,
            "error": str(exc),
            "duration_s": round(time.perf_counter() - start, 6),
            "size_bytes": len(content),
            "filename": filename,
        }


def write_temp_bytes(prefix: str, suffix: str, content: bytes) -> str:
    tmp_dir = Path(tempfile.gettempdir()) / "comp_docs_compare"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=tmp_dir)
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)
    return path
