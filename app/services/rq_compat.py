from __future__ import annotations

import importlib
import logging
import multiprocessing
import os
import platform
import re
import sys
from importlib import metadata
from typing import Any

logger = logging.getLogger(__name__)

_RQ_RUNTIME_CACHE: dict[str, Any] | None = None
_RQ_RUNTIME_CACHE_SIGNATURE: tuple[int | None, int | None] | None = None
_PATCHED_WINDOWS_MP = False
_PATCHED_WINDOWS_SPAWN_WORKER = False
_WINDOWS_RQ_PATCH_THRESHOLD = (2, 3, 1)


def is_windows() -> bool:
    return platform.system().lower() == "windows"



def rq_version() -> str:
    try:
        return metadata.version("rq")
    except metadata.PackageNotFoundError:
        return "unknown"



def _parse_version(raw: str) -> tuple[int, ...]:
    numbers = [int(token) for token in re.findall(r"\d+", raw)]
    return tuple(numbers) if numbers else (0,)



def windows_needs_compat_patches() -> bool:
    if not is_windows():
        return False
    version = _parse_version(rq_version())
    if version == (0,):
        return True
    return version < _WINDOWS_RQ_PATCH_THRESHOLD



def _patch_windows_multiprocessing() -> None:
    global _PATCHED_WINDOWS_MP
    if _PATCHED_WINDOWS_MP or not windows_needs_compat_patches():
        return

    original_get_context = multiprocessing.get_context

    def _safe_get_context(method: str | None = None):
        if method == "fork":
            logger.debug(
                "RQ solicitó el contexto 'fork' en Windows; se redirige a 'spawn' como compatibilidad legacy."
            )
            method = "spawn"
        return original_get_context(method)

    multiprocessing.get_context = _safe_get_context  # type: ignore[assignment]
    try:
        import multiprocessing.context as mp_context

        mp_context.get_context = _safe_get_context  # type: ignore[assignment]
    except Exception:  # noqa: BLE001
        logger.debug("No se pudo parchear multiprocessing.context.get_context", exc_info=True)
    _PATCHED_WINDOWS_MP = True



def _patch_windows_spawn_worker(spawn_worker_cls: type[Any] | None) -> type[Any] | None:
    global _PATCHED_WINDOWS_SPAWN_WORKER
    if (
        _PATCHED_WINDOWS_SPAWN_WORKER
        or not windows_needs_compat_patches()
        or spawn_worker_cls is None
        or hasattr(os, "wait4")
    ):
        return spawn_worker_cls

    if not hasattr(os, "waitpid"):
        logger.warning(
            "Windows no expone os.wait4 ni os.waitpid; se mantiene SpawnWorker sin parche y podría fallar."
        )
        return spawn_worker_cls

    original_wait_for_horse = getattr(spawn_worker_cls, "wait_for_horse", None)
    if original_wait_for_horse is None:
        return spawn_worker_cls

    def _wait_for_horse(self):
        pid, status = os.waitpid(self.horse_pid, 0)
        return pid, status, None

    setattr(spawn_worker_cls, "wait_for_horse", _wait_for_horse)
    _PATCHED_WINDOWS_SPAWN_WORKER = True
    logger.debug(
        "RQ SpawnWorker usa os.wait4 en esta versión; se redirige a os.waitpid como compatibilidad legacy de Windows."
    )
    return spawn_worker_cls



def _missing_rq_error(exc: BaseException) -> RuntimeError:
    detail = f"{exc.__class__.__name__}: {exc}"
    if "has no attribute 'fork'" in str(exc) and "fork()" not in detail:
        detail = f"{detail} (se detectó una ruta que intenta usar fork())"
    return RuntimeError(
        "No se pudo cargar RQ de forma segura en Windows/Linux. Instala una versión compatible en el entorno 'cosmos' "
        "(recomendado: rq>=2.3.1, redis>=5,<7) y arranca el worker con `python -m app.worker`. "
        f"Error original: {detail}"
    )



def _runtime_signature() -> tuple[int | None, int | None]:
    rq_module = sys.modules.get("rq")
    rq_worker_module = sys.modules.get("rq.worker")
    return (
        id(rq_module) if rq_module is not None else None,
        id(rq_worker_module) if rq_worker_module is not None else None,
    )



def reset_rq_runtime_cache() -> None:
    global _RQ_RUNTIME_CACHE, _RQ_RUNTIME_CACHE_SIGNATURE
    _RQ_RUNTIME_CACHE = None
    _RQ_RUNTIME_CACHE_SIGNATURE = None



def load_rq_runtime() -> dict[str, Any]:
    global _RQ_RUNTIME_CACHE, _RQ_RUNTIME_CACHE_SIGNATURE
    signature = _runtime_signature()
    if _RQ_RUNTIME_CACHE is not None and _RQ_RUNTIME_CACHE_SIGNATURE == signature:
        return _RQ_RUNTIME_CACHE

    if windows_needs_compat_patches():
        _patch_windows_multiprocessing()

    try:
        rq_module = importlib.import_module("rq")
    except Exception as exc:  # noqa: BLE001
        raise _missing_rq_error(exc) from exc

    try:
        worker_module = importlib.import_module("rq.worker")
    except Exception:
        worker_module = None

    runtime = {
        "Queue": getattr(rq_module, "Queue", None),
        "SimpleWorker": getattr(rq_module, "SimpleWorker", None),
        "Worker": getattr(rq_module, "Worker", None),
        "SpawnWorker": _patch_windows_spawn_worker(
            getattr(worker_module, "SpawnWorker", None) if worker_module is not None else None
        ),
    }

    if runtime["Queue"] is None:
        raise RuntimeError(
            "La instalación de RQ es incompleta o incompatible. Se esperaba encontrar Queue. "
            f"Versión detectada: {rq_version()}."
        )

    _RQ_RUNTIME_CACHE = runtime
    _RQ_RUNTIME_CACHE_SIGNATURE = signature
    return runtime



def get_worker_classes() -> tuple[type[Any] | None, type[Any] | None, type[Any] | None]:
    runtime = load_rq_runtime()
    return runtime["Worker"], runtime["SimpleWorker"], runtime["SpawnWorker"]



def require_supported_windows_rq(*, spawn_worker_available: bool | None = None) -> None:
    if not is_windows():
        return
    version = _parse_version(rq_version())
    if spawn_worker_available is None:
        spawn_worker_available = load_rq_runtime()["SpawnWorker"] is not None
    if spawn_worker_available and version == (0,):
        logger.warning(
            "No se pudo determinar la versión de RQ, pero SpawnWorker está disponible; se continúa."
        )
        return
    if spawn_worker_available and version < (2, 2, 0):
        logger.warning(
            "La metadata reporta RQ %s pero SpawnWorker está disponible; se prioriza la feature detectada.",
            rq_version(),
        )
        return
    if version < (2, 2, 0):
        raise RuntimeError(
            "Este proyecto necesita RQ >= 2.2.0 en Windows para poder usar SpawnWorker. "
            f"Versión detectada: {rq_version()}."
        )
    if version < _WINDOWS_RQ_PATCH_THRESHOLD:
        logger.warning(
            "Se detectó RQ %s en Windows. Funciona con compatibilidad legacy, pero para un despliegue estable se recomienda al menos RQ 2.3.1.",
            rq_version(),
        )
