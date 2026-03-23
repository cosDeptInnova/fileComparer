from __future__ import annotations

import importlib
import sys
import types


def _load_queue_module():
    sys.modules.pop("app.services.queue", None)

    fake_redis = types.ModuleType("redis")
    fake_rq = types.ModuleType("rq")

    class FakeRedis:
        last_from_url: dict[str, object] | None = None

        @classmethod
        def from_url(cls, url, **kwargs):
            cls.last_from_url = {"url": url, **kwargs}
            return cls()

        def hgetall(self, _key):
            return {}

    class FakeQueue:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    fake_redis.Redis = FakeRedis
    fake_rq.Queue = FakeQueue

    sys.modules["redis"] = fake_redis
    sys.modules["rq"] = fake_rq
    module = importlib.import_module("app.services.queue")
    return module, FakeRedis


def test_redis_connection_uses_binary_safe_mode():
    module, fake_redis = _load_queue_module()

    module.redis_connection()

    assert fake_redis.last_from_url is not None
    assert fake_redis.last_from_url["decode_responses"] is False


def test_read_job_state_decodes_bytes_and_parses_json():
    module, _ = _load_queue_module()

    class FakeConnection:
        def hgetall(self, _key):
            return {
                b"status": b"done",
                b"percent": b"100",
                b"metrics": b'{"queue":"rq"}',
            }

    module.redis_connection = lambda: FakeConnection()

    payload = module.read_job_state("job-1")

    assert payload["status"] == "done"
    assert payload["percent"] == 100
    assert payload["metrics"] == {"queue": "rq"}
