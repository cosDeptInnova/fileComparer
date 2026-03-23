from __future__ import annotations

import importlib
import logging
import multiprocessing
import os
import platform
import re
from importlib import metadata
from typing import Any

logger = logging.getLogger(__name__)

_RQ_RUNTIME_CACHE: dict[str, Any] | None = None
_PATCHED_WINDOWS_MP = False
_PATCHED_WINDOWS_SPAWN_WORKER = False


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



def _patch_windows_multiprocessing() -> None:
    global _PATCHED_WINDOWS_MP
    if _PATCHED_WINDOWS_MP or not is_windows():
        return

    original_get_context = multiprocessing.get_context

    def _safe_get_context(method: str | None = None):
        if method == "fork":
            logger.warning(
                "RQ solicitó el contexto 'fork' en Windows; se redirige a 'spawn' para compatibilidad."
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
        or not is_windows()
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
    logger.warning(
        "RQ SpawnWorker usa os.wait4, que no existe en Windows; se redirige a os.waitpid para compatibilidad."
    )
    return spawn_worker_cls


def _missing_rq_error(exc: BaseException) -> RuntimeError:
    return RuntimeError(
        "No se pudo importar la dependencia 'rq'. Instala una versión compatible en el entorno 'cosmos' "
        "(recomendado: rq>=2.3.1, redis>=5,<7) y arranca el worker con `python -m app.worker`. "
        f"Error original: {exc.__class__.__name__}: {exc}"
    )



def load_rq_runtime() -> dict[str, Any]:
    global _RQ_RUNTIME_CACHE
    if _RQ_RUNTIME_CACHE is not None:
        return _RQ_RUNTIME_CACHE

    if is_windows():
        _patch_windows_multiprocessing()

    try:
        rq_module = importlib.import_module("rq")
        worker_module = importlib.import_module("rq.worker")
    except Exception as exc:  # noqa: BLE001
        raise _missing_rq_error(exc) from exc

    runtime = {
        "Queue": rq_module.Queue,
        "SimpleWorker": getattr(rq_module, "SimpleWorker", None),
        "Worker": getattr(rq_module, "Worker", None),
        "SpawnWorker": _patch_windows_spawn_worker(getattr(worker_module, "SpawnWorker", None)),
    }

    if runtime["Queue"] is None or runtime["Worker"] is None:
        raise RuntimeError(
            "La instalación de RQ es incompleta o incompatible. Se esperaba encontrar Queue y Worker. "
            f"Versión detectada: {rq_version()}."
        )

    _RQ_RUNTIME_CACHE = runtime
    return runtime



def get_worker_classes() -> tuple[type[Any] | None, type[Any] | None, type[Any] | None]:
    runtime = load_rq_runtime()
    return runtime["Worker"], runtime["SimpleWorker"], runtime["SpawnWorker"]



def require_supported_windows_rq() -> None:
    if not is_windows():
        return
    version = _parse_version(rq_version())
    if version < (2, 2, 0):
        raise RuntimeError(
            "Este proyecto necesita RQ >= 2.2.0 en Windows para poder usar SpawnWorker. "
            f"Versión detectada: {rq_version()}."
        )
    if version < (2, 3, 1):
        logger.warning(
            "Se detectó RQ %s en Windows. Para un despliegue estable se recomienda al menos RQ 2.3.1, "
            "que corrige fallos de compatibilidad en Windows.",
            rq_version(),
        )
