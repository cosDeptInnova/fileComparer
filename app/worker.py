from __future__ import annotations

import os
import socket

from rq import Connection, Worker

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


def main() -> None:
    with Connection(redis_connection()):
        worker = Worker([settings.rq_queue_name], name=build_worker_name())
        worker.work()


if __name__ == "__main__":
    main()
