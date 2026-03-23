from __future__ import annotations

from rq import Connection, Worker

from app.settings import settings
from app.services.queue import redis_connection


if __name__ == "__main__":
    with Connection(redis_connection()):
        worker = Worker([settings.rq_queue_name])
        worker.work()
