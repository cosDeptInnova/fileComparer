from __future__ import annotations

import argparse
import logging
import os
import platform
import socket
import sys
from typing import Any, Sequence

from app.services.queue import redis_connection
from app.services.rq_compat import get_worker_classes, is_windows, load_rq_runtime, require_supported_windows_rq, rq_version
from app.settings import settings

logger = logging.getLogger(__name__)

WINDOWS_MODES = {"development", "production"}
WORKER_CLASS_ALIASES = {
    "auto": "auto",
    "spawn": "spawn",
    "spawnworker": "spawn",
    "simple": "simple",
    "simpleworker": "simple",
    "worker": "worker",
    "default": "worker",
    "classic": "worker",
}



def _clean_name_part(value: str) -> str:
    cleaned = "".join(
        ch if ch.isalnum() or ch in {"-", "_", "."} else "-"
        for ch in value.strip()
    )
    return cleaned.strip("-") or "unknown"



def build_worker_name() -> str:
    explicit_name = os.getenv("COMPARE_WORKER_NAME", "").strip()
    if explicit_name:
        return explicit_name

    prefix = os.getenv("COMPARE_WORKER_NAME_PREFIX", "comp_docs_worker").strip() or "comp_docs_worker"
    instance = os.getenv("SERVICE_INSTANCE_NUMBER", "0").strip() or "0"
    hostname = _clean_name_part(socket.gethostname())
    pid = os.getpid()
    return f"{prefix}-{instance}-{hostname}-{pid}"



def windows_worker_mode() -> str:
    mode = os.getenv("COMPARE_WINDOWS_WORKER_MODE", "production").strip().lower() or "production"
    if mode not in WINDOWS_MODES:
        raise RuntimeError(
            "COMPARE_WINDOWS_WORKER_MODE debe ser 'development' o 'production'. "
            f"Valor recibido: {mode!r}."
        )
    return mode



def requested_worker_class() -> str:
    raw_value = os.getenv("COMPARE_WORKER_CLASS", "auto").strip().lower() or "auto"
    normalized = WORKER_CLASS_ALIASES.get(raw_value)
    if normalized is None:
        allowed = ", ".join(sorted(WORKER_CLASS_ALIASES))
        raise RuntimeError(
            "COMPARE_WORKER_CLASS no es válido. "
            f"Usa uno de: {allowed}. Valor recibido: {raw_value!r}."
        )
    return normalized



def _legacy_worker_error() -> RuntimeError:
    return RuntimeError(
        "El worker por defecto de RQ (`Worker` / `rq worker`) no es válido en Windows para este proyecto "
        "porque intenta usar fork() en rutas no compatibles. Usa `python -m app.worker`, que selecciona "
        "`SpawnWorker` automáticamente cuando está disponible."
    )



def _missing_spawn_worker_error() -> RuntimeError:
    return RuntimeError(
        "No se encontró `rq.worker.SpawnWorker`. Este proyecto necesita RQ >= 2.2.0 en Windows y se recomienda "
        f"RQ >= 2.3.1 para producción. Versión detectada: {rq_version()}."
    )



def _queue_class() -> type[Any]:
    return load_rq_runtime()["Queue"]



def select_worker_class() -> type[Any]:
    requested = requested_worker_class()
    on_windows = is_windows()
    worker_cls, simple_worker_cls, spawn_worker_cls = get_worker_classes()

    if on_windows:
        require_supported_windows_rq()

    if on_windows and requested == "worker":
        raise _legacy_worker_error()

    if requested == "spawn":
        if spawn_worker_cls is None:
            raise _missing_spawn_worker_error()
        return spawn_worker_cls

    if requested == "simple":
        if simple_worker_cls is None:
            raise RuntimeError("La instalación de RQ no expone SimpleWorker.")
        if on_windows and windows_worker_mode() != "development":
            raise RuntimeError(
                "`SimpleWorker` solo puede usarse como fallback controlado de desarrollo en Windows. "
                "Configura `COMPARE_WINDOWS_WORKER_MODE=development` si necesitas ese modo temporal."
            )
        return simple_worker_cls

    if on_windows:
        if spawn_worker_cls is not None:
            return spawn_worker_cls
        if windows_worker_mode() == "development" and simple_worker_cls is not None:
            logger.warning(
                "SpawnWorker no está disponible en RQ %s; usando SimpleWorker solo como fallback de desarrollo en Windows.",
                rq_version(),
            )
            return simple_worker_cls
        raise _missing_spawn_worker_error()

    if worker_cls is None:
        raise RuntimeError("La instalación de RQ no expone Worker.")
    return worker_cls



def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Arranca el worker RQ del comparador con selección segura de clase por plataforma."
    )
    parser.add_argument(
        "--queue",
        "--queues",
        dest="queues",
        action="append",
        help="Cola RQ a escuchar. Puede repetirse. Por defecto usa COMPARE_QUEUE_NAME.",
    )
    parser.add_argument("--burst", action="store_true", help="Procesa la cola y sale cuando quede vacía.")
    parser.add_argument(
        "--worker-name",
        dest="worker_name",
        default=os.getenv("COMPARE_WORKER_NAME", "").strip() or None,
        help="Nombre explícito del worker. Si se omite, se genera automáticamente.",
    )
    return parser.parse_args(argv)



def queue_names_from_args(args: argparse.Namespace) -> list[str]:
    queues = [queue.strip() for queue in (args.queues or []) if queue and queue.strip()]
    return queues or [settings.rq_queue_name]



def create_worker(
    queue_names: Sequence[str],
    *,
    worker_name: str | None = None,
    connection=None,
):
    worker_cls = select_worker_class()
    queue_cls = _queue_class()
    resolved_name = worker_name or build_worker_name()
    conn = connection or redis_connection()
    queues = [queue_cls(name, connection=conn) for name in queue_names]
    logger.info(
        "Inicializando worker RQ: platform=%s rq_version=%s worker_class=%s queues=%s worker_name=%s windows_mode=%s",
        platform.system(),
        rq_version(),
        worker_cls.__name__,
        list(queue_names),
        resolved_name,
        windows_worker_mode() if is_windows() else "n/a",
    )
    return worker_cls(queues, connection=conn, name=resolved_name)



def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(
        level=os.getenv("COMPARE_WORKER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    args = parse_args(argv)
    queue_names = queue_names_from_args(args)
    worker = create_worker(queue_names, worker_name=args.worker_name)
    worker.work(burst=args.burst)



if __name__ == "__main__":
    main(sys.argv[1:])
