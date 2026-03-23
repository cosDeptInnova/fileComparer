from __future__ import annotations

import argparse
import logging
import os
import platform
import socket
import sys
from importlib import metadata
from typing import Sequence

from rq import Queue, SimpleWorker, Worker

from app.settings import settings
from app.services.queue import redis_connection

try:
    from rq.worker import SpawnWorker
except ImportError:  # pragma: no cover - exercised via tests with monkeypatched rq modules
    SpawnWorker = None  # type: ignore[assignment]

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


def rq_version() -> str:
    try:
        return metadata.version("rq")
    except metadata.PackageNotFoundError:
        return "unknown"


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


def is_windows() -> bool:
    return platform.system().lower() == "windows"


def _spawn_worker_class() -> type[Worker] | None:
    return SpawnWorker


def _legacy_worker_error() -> RuntimeError:
    return RuntimeError(
        "El worker por defecto de RQ (`Worker` / `rq worker`) no es válido en Windows para este proyecto "
        "porque depende de fork() y acaba rompiendo con `AttributeError: module 'os' has no attribute 'fork'`. "
        "Usa `python -m app.worker`, que selecciona `SpawnWorker` automáticamente cuando RQ >= 2.2 está disponible. "
        "No vuelvas a arrancar `rq worker` a mano en Windows."
    )


def _missing_spawn_worker_error() -> RuntimeError:
    return RuntimeError(
        "Este proyecto necesita RQ >= 2.2 en Windows para poder usar `rq.worker.SpawnWorker`. "
        f"Versión detectada de RQ: {rq_version()}. "
        "Actualiza requirements/entorno y vuelve a instalar dependencias. "
        "Si necesitas un escape temporal de desarrollo en Windows, arranca con "
        "`COMPARE_WINDOWS_WORKER_MODE=development` para usar `SimpleWorker`, sabiendo que no es una ruta "
        "recomendada para producción. Para producción estable, ejecuta Redis + workers en Linux/WSL/Docker."
    )


def select_worker_class() -> type[Worker]:
    requested = requested_worker_class()
    on_windows = is_windows()

    if on_windows and requested == "worker":
        raise _legacy_worker_error()

    if requested == "spawn":
        spawn_worker = _spawn_worker_class()
        if spawn_worker is None:
            raise _missing_spawn_worker_error()
        return spawn_worker

    if requested == "simple":
        if on_windows and windows_worker_mode() != "development":
            raise RuntimeError(
                "`SimpleWorker` solo puede usarse como fallback controlado de desarrollo en Windows. "
                "Configura `COMPARE_WINDOWS_WORKER_MODE=development` si de verdad necesitas ese modo temporal. "
                "No es una configuración de producción."
            )
        return SimpleWorker

    if on_windows:
        spawn_worker = _spawn_worker_class()
        if spawn_worker is not None:
            return spawn_worker
        if windows_worker_mode() == "development":
            logger.warning(
                "SpawnWorker no está disponible en RQ %s; usando SimpleWorker solo como fallback de desarrollo en Windows.",
                rq_version(),
            )
            return SimpleWorker
        raise _missing_spawn_worker_error()

    return Worker


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
) -> Worker:
    worker_cls = select_worker_class()
    resolved_name = worker_name or build_worker_name()
    conn = connection or redis_connection()
    queues = [Queue(name, connection=conn) for name in queue_names]
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
    try:
        worker.work(burst=args.burst)
    except RuntimeError:
        raise
    except AttributeError as exc:
        if is_windows() and "fork" in str(exc).lower():
            raise _legacy_worker_error() from exc
        raise


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
