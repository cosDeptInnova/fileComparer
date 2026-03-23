from __future__ import annotations

import os
import socket

from rq import Connection, SimpleWorker, Worker

from app.settings import settings
from app.services.queue import redis_connection


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


def should_use_simple_worker() -> bool:
    forced = os.getenv("COMPARE_USE_SIMPLE_WORKER", "").strip().lower()
    if forced in {"1", "true", "yes", "on"}:
        return True
    if forced in {"0", "false", "no", "off"}:
        return False
    return not hasattr(os, "fork")


def worker_class() -> type[Worker]:
    return SimpleWorker if should_use_simple_worker() else Worker


def main() -> None:
    with Connection(redis_connection()):
        worker = worker_class()([settings.rq_queue_name], name=build_worker_name())
        worker.work()


if __name__ == "__main__":
    main()
