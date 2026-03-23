from __future__ import annotations

import secrets
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile

from app.extractors import (
    MissingDependencyError,
    UnsupportedEngineError,
    UnsupportedFormatError,
    get_pipeline_capabilities,
    normalize_requested_engine,
    validate_extraction_request,
)
from app.services.jobs import run_compare_job
from app.services.queue import compare_queue, ensure_queue_backend_ready, load_job_result, read_job_state, update_job_state
from app.settings import settings

router = APIRouter()
_UPLOAD_CHUNK_SIZE = 1024 * 1024


@router.get("/csrf-token")
def csrf_token(response: Response) -> dict[str, str]:
    token = secrets.token_hex(16)
    response.set_cookie(settings.csrf_cookie_name, token, httponly=False, samesite="lax")
    return {"csrf_token": token}


@router.get("/capabilities")
def capabilities() -> dict[str, object]:
    extractor_capabilities = get_pipeline_capabilities()
    allowed_extensions = list(extractor_capabilities.get("allowed_extensions") or [])
    return {
        "service": "comparador",
        "panel_name": "TextCompareMainPanel",
        "route": "/main/text-compare",
        "accept": ",".join(allowed_extensions),
        "allowed_extensions": allowed_extensions,
        "allowed_extensions_label": ", ".join(allowed_extensions),
        "conditional_extensions": extractor_capabilities.get("conditional_extensions") or {},
        "engines": extractor_capabilities.get("engines") or {},
        "soffice": extractor_capabilities.get("soffice") or {},
        "ocr": extractor_capabilities.get("ocr") or {},
        "max_file_mb": settings.max_file_mb,
        "messages": {
            "unsupported_extension": "Formato no soportado ({ext}). Extensiones admitidas: {allowed_extensions}.",
            "file_too_large": 'El archivo "{name}" supera el máximo de {max_mb} MB ({size_mb} MB).',
            "file_too_large_backend": "Archivo demasiado grande. Máximo {max_mb} MB por fichero.",
            "empty_file": "Alguno de los archivos está vacío.",
            "engine_not_available": "El engine solicitado no está disponible: {engine}.",
            "conditional_format_requires_soffice": "El formato {ext} requiere LibreOffice/soffice disponible o una ruta válida en el campo soffice.",
        },
    }



def _validate_csrf(request: Request) -> None:
    cookie = request.cookies.get(settings.csrf_cookie_name)
    header = request.headers.get("X-CSRFToken")
    if cookie and header and cookie == header:
        return
    if cookie and not header:
        return
    if not cookie and not header:
        return
    raise HTTPException(status_code=403, detail="CSRF token inválido o ausente (servicio comparador).")



def _validate_upload_request(*, name: str, engine: str, soffice: str | None) -> None:
    try:
        validate_extraction_request(name, engine=engine, soffice_path=soffice)
    except UnsupportedFormatError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except (UnsupportedEngineError, MissingDependencyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc



def _stored_upload_name(prefix: str, original_name: str | None) -> str:
    base_name = Path(original_name or "documento.bin").name.strip() or "documento.bin"
    return f"{prefix}_{base_name}"


async def _save_upload(upload: UploadFile, destination: Path) -> int:
    total_size = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("wb") as handle:
            while True:
                chunk = await upload.read(_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > settings.max_file_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Archivo demasiado grande. Máximo {settings.max_file_mb} MB por fichero.",
                    )
                handle.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()

    if total_size <= 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Alguno de los archivos está vacío.")
    return total_size


@router.post("/comparar")
async def comparar(
    request: Request,
    file_a: UploadFile = File(...),
    file_b: UploadFile = File(...),
    soffice: str | None = Form(None),
    engine: str = Form("auto"),
) -> dict[str, object]:
    _validate_csrf(request)
    try:
        normalized_engine = normalize_requested_engine(engine, strict=True)
    except UnsupportedEngineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    name_a = Path(file_a.filename or "A.txt").name
    name_b = Path(file_b.filename or "B.txt").name
    _validate_upload_request(name=name_a, engine=normalized_engine, soffice=soffice)
    _validate_upload_request(name=name_b, engine=normalized_engine, soffice=soffice)

    sid = uuid.uuid4().hex
    target_dir = settings.data_dir / sid / settings.upload_dir_name
    target_dir.mkdir(parents=True, exist_ok=True)
    path_a = target_dir / _stored_upload_name("A", name_a)
    path_b = target_dir / _stored_upload_name("B", name_b)

    await _save_upload(file_a, path_a)
    try:
        await _save_upload(file_b, path_b)
    except Exception:
        path_a.unlink(missing_ok=True)
        raise

    update_job_state(
        sid,
        status="queued",
        percent=5,
        step="encolado",
        detail="Esperando worker",
        queue_name=settings.compare_queue_name,
        queue_backend="celery",
        original_filename_a=name_a,
        original_filename_b=name_b,
        stored_filename_a=path_a.name,
        stored_filename_b=path_b.name,
    )

    job_kwargs = {
        "soffice_path": soffice,
        "engine": normalized_engine,
        "drop_headers": True,
    }
    if settings.inline_jobs:
        run_compare_job(sid, str(path_a), str(path_b), **job_kwargs)
    else:
        try:
            ensure_queue_backend_ready(require_active_workers=settings.require_active_workers)
            compare_queue().enqueue(run_compare_job, sid, str(path_a), str(path_b), job_id=sid, **job_kwargs)
        except RuntimeError as exc:
            update_job_state(
                sid,
                status="error",
                percent=100,
                step="cola-no-disponible",
                detail=str(exc),
                error=str(exc),
            )
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "sid": sid,
        "status": "queued",
        "ok": True,
        "detail": "Comparación encolada",
        "engine": normalized_engine,
    }


@router.get("/progress/{sid}")
def progress(sid: str) -> dict[str, object]:
    state = read_job_state(sid)
    if not state:
        raise HTTPException(status_code=404, detail="Progreso no disponible o expirado")
    return {
        "sid": sid,
        "status": state.get("status", "queued"),
        "percent": int(state.get("percent", 0) or 0),
        "step": state.get("step", "pendiente"),
        "detail": state.get("detail", ""),
        "error": state.get("error"),
        "metrics": {"queue": {"backend": "celery", "name": settings.compare_queue_name}},
    }


@router.get("/resultado/{sid}/json")
def resultado_json(sid: str, offset: int = 0, limit: int | None = None) -> dict[str, object]:
    payload = load_job_result(sid)
    if payload is None:
        state = read_job_state(sid)
        if state.get("status") == "error":
            return {
                "sid": sid,
                "status": "error",
                "ok": False,
                "error": state.get("error"),
                "rows": [],
                "progress": state,
            }
        raise HTTPException(status_code=404, detail="Resultado no disponible o expirado")
    payload = {**payload}
    rows = list(payload.get("rows", []))
    sliced = rows[offset : None if limit is None else offset + limit]
    payload["rows"] = sliced
    payload.setdefault("meta", {})
    payload["meta"]["pagination"] = {
        "offset": offset,
        "limit": limit,
        "returned": len(sliced),
        "total": len(rows),
        "has_more": offset + len(sliced) < len(rows),
        "next_offset": None if offset + len(sliced) >= len(rows) else offset + len(sliced),
        "truncated": limit is not None and len(sliced) < len(rows),
    }
    return payload
