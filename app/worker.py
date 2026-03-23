from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
from typing import Sequence

from app.celery_app import celery_app
from app.settings import settings

logger = logging.getLogger(__name__)


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


def default_worker_pool() -> str:
    configured = os.getenv("COMPARE_CELERY_POOL", "").strip().lower()
    if configured:
        return configured
    return "threads" if os.name == "nt" else "prefork"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Arranca el worker Celery del comparador documental."
    )
    parser.add_argument(
        "--queue",
        "--queues",
        dest="queues",
        action="append",
        help="Cola Celery a escuchar. Puede repetirse. Por defecto usa COMPARE_QUEUE_NAME.",
    )
    parser.add_argument("--burst", action="store_true", help="Compatibilidad heredada. Celery no soporta burst en este wrapper.")
    parser.add_argument(
        "--worker-name",
        dest="worker_name",
        default=os.getenv("COMPARE_WORKER_NAME", "").strip() or None,
        help="Nombre explícito del worker. Si se omite, se genera automáticamente.",
    )
    parser.add_argument(
        "--loglevel",
        default=os.getenv("COMPARE_WORKER_LOG_LEVEL", "INFO").upper(),
        help="Nivel de log para Celery.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=max(1, int(os.getenv("COMPARE_WORKER_CONCURRENCY", "1") or "1")),
        help="Concurrencia del worker Celery.",
    )
    parser.add_argument(
        "--pool",
        default=default_worker_pool(),
        help="Pool de Celery (por defecto threads en Windows, prefork fuera de Windows).",
    )
    return parser.parse_args(argv)


def queue_names_from_args(args: argparse.Namespace) -> list[str]:
    queues = [queue.strip() for queue in (args.queues or []) if queue and queue.strip()]
    return queues or [settings.compare_queue_name]


def build_worker_argv(args: argparse.Namespace) -> list[str]:
    if args.burst:
        raise RuntimeError("El flag --burst ya no está soportado con Celery en este proyecto.")

    queue_names = queue_names_from_args(args)
    worker_name = args.worker_name or build_worker_name()
    argv = [
        "worker",
        f"--loglevel={args.loglevel}",
        f"--hostname={worker_name}@%h",
        f"--concurrency={max(1, args.concurrency)}",
        f"--pool={args.pool}",
        f"--queues={','.join(queue_names)}",
    ]
    logger.info(
        "Inicializando worker Celery: queues=%s worker_name=%s pool=%s concurrency=%s broker=%s backend=%s",
        queue_names,
        worker_name,
        args.pool,
        max(1, args.concurrency),
        settings.celery_broker_url,
        settings.celery_result_backend,
    )
    return argv


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(
        level=os.getenv("COMPARE_WORKER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    args = parse_args(argv)
    celery_app.worker_main(build_worker_argv(args))


if __name__ == "__main__":
    main(sys.argv[1:])