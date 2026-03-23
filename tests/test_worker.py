from __future__ import annotations

import importlib
import sys
import types


class DummyRedis:
    @classmethod
    def from_url(cls, url, decode_responses=False):
        return object()


class DummyQueue:
    def __init__(self, *args, **kwargs):
        pass


class DummyConnection:
    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class DummyWorker:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class DummySimpleWorker(DummyWorker):
    pass


def _load_worker_module(monkeypatch):
    redis_mod = types.ModuleType("redis")
    rq_mod = types.ModuleType("rq")

    redis_mod.Redis = DummyRedis
    rq_mod.Queue = DummyQueue
    rq_mod.Connection = DummyConnection
    rq_mod.Worker = DummyWorker
    rq_mod.SimpleWorker = DummySimpleWorker

    monkeypatch.setitem(sys.modules, "redis", redis_mod)
    monkeypatch.setitem(sys.modules, "rq", rq_mod)
    sys.modules.pop("app.services.queue", None)
    sys.modules.pop("app.worker", None)
    return importlib.import_module("app.worker")


def test_build_worker_name_includes_instance_host_and_pid(monkeypatch):
    monkeypatch.setenv("COMPARE_WORKER_NAME_PREFIX", "comp_docs_worker")
    monkeypatch.setenv("SERVICE_INSTANCE_NUMBER", "3")
    monkeypatch.delenv("COMPARE_WORKER_NAME", raising=False)
    worker_module = _load_worker_module(monkeypatch)
    monkeypatch.setattr(worker_module.socket, "gethostname", lambda: "srv prod")
    monkeypatch.setattr(worker_module.os, "getpid", lambda: 4242)

    name = worker_module.build_worker_name()

    assert name == "comp_docs_worker-3-srv-prod-4242"


def test_build_worker_name_honors_explicit_override(monkeypatch):
    monkeypatch.setenv("COMPARE_WORKER_NAME", "comp_docs_worker-prod-a")
    worker_module = _load_worker_module(monkeypatch)

    assert worker_module.build_worker_name() == "comp_docs_worker-prod-a"


def test_worker_class_uses_simple_worker_when_fork_is_unavailable(monkeypatch):
    monkeypatch.delenv("COMPARE_USE_SIMPLE_WORKER", raising=False)
    worker_module = _load_worker_module(monkeypatch)
    monkeypatch.delattr(worker_module.os, "fork", raising=False)

    assert worker_module.worker_class() is worker_module.SimpleWorker


def test_worker_class_respects_force_disable_simple_worker(monkeypatch):
    monkeypatch.setenv("COMPARE_USE_SIMPLE_WORKER", "false")
    worker_module = _load_worker_module(monkeypatch)
    monkeypatch.delattr(worker_module.os, "fork", raising=False)

    assert worker_module.worker_class() is worker_module.Worker
