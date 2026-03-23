from __future__ import annotations

import os

from app.settings import settings
from app.services.queue import redis_connection


def build_worker_name() -> str:
    prefix = os.getenv("COMPARE_WORKER_NAME_PREFIX", "comp_docs_worker").strip() or "comp_docs_worker"
    instance = os.getenv("SERVICE_INSTANCE_NUMBER", "0").strip() or "0"
    return f"{prefix}-{instance}"


def main() -> None:
    from rq import Connection, Worker

    with Connection(redis_connection()):
        worker = Worker([settings.rq_queue_name], name=build_worker_name())
        worker.work()


if __name__ == "__main__":
    main()
