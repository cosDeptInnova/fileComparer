from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class DummyRedis:
    @classmethod
    def from_url(cls, url, decode_responses=False):
        return object()


class DummyQueue:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class DummyWorker:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.work_calls = []

    def work(self, *args, **kwargs):
        self.work_calls.append({"args": args, "kwargs": kwargs})
        return True


class DummySimpleWorker(DummyWorker):
    pass


class DummySpawnWorker(DummyWorker):
    pass


def _clear_app_modules():
    sys.modules.pop("app.services.queue", None)
    sys.modules.pop("app.worker", None)


def _install_runtime_modules(monkeypatch, *, include_spawn=True):
    redis_mod = types.ModuleType("redis")
    rq_mod = types.ModuleType("rq")
    rq_worker_mod = types.ModuleType("rq.worker")

    redis_mod.Redis = DummyRedis
    rq_mod.Queue = DummyQueue
    rq_mod.Worker = DummyWorker
    rq_mod.SimpleWorker = DummySimpleWorker
    rq_worker_mod.Worker = DummyWorker
    if include_spawn:
        rq_worker_mod.SpawnWorker = DummySpawnWorker

    monkeypatch.setitem(sys.modules, "redis", redis_mod)
    monkeypatch.setitem(sys.modules, "rq", rq_mod)
    monkeypatch.setitem(sys.modules, "rq.worker", rq_worker_mod)


def _load_worker_module(monkeypatch, *, include_spawn=True):
    _install_runtime_modules(monkeypatch, include_spawn=include_spawn)
    _clear_app_modules()
    return importlib.import_module("app.worker")


def test_importing_worker_module_does_not_require_rq(monkeypatch):
    redis_mod = types.ModuleType("redis")
    redis_mod.Redis = DummyRedis
    monkeypatch.setitem(sys.modules, "redis", redis_mod)
    monkeypatch.delitem(sys.modules, "rq", raising=False)
    monkeypatch.delitem(sys.modules, "rq.worker", raising=False)
    _clear_app_modules()

    worker_module = importlib.import_module("app.worker")

    assert worker_module.requested_worker_class() == "auto"


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


def test_windows_selects_spawn_worker_instead_of_default_worker(monkeypatch):
    monkeypatch.delenv("COMPARE_WORKER_CLASS", raising=False)
    worker_module = _load_worker_module(monkeypatch, include_spawn=True)
    monkeypatch.setattr(worker_module.platform, "system", lambda: "Windows")

    assert worker_module.select_worker_class() is DummySpawnWorker
    assert worker_module.select_worker_class() is not DummyWorker


def test_non_windows_keeps_default_worker(monkeypatch):
    monkeypatch.delenv("COMPARE_WORKER_CLASS", raising=False)
    worker_module = _load_worker_module(monkeypatch, include_spawn=True)
    monkeypatch.setattr(worker_module.platform, "system", lambda: "Linux")

    assert worker_module.select_worker_class() is DummyWorker


def test_windows_forced_default_worker_aborts_with_explicit_error(monkeypatch):
    monkeypatch.setenv("COMPARE_WORKER_CLASS", "worker")
    worker_module = _load_worker_module(monkeypatch, include_spawn=True)
    monkeypatch.setattr(worker_module.platform, "system", lambda: "Windows")

    with pytest.raises(RuntimeError, match="rq worker"):
        worker_module.select_worker_class()


def test_windows_import_failure_returns_controlled_runtime_error(monkeypatch):
    worker_module = _load_worker_module(monkeypatch, include_spawn=True)
    monkeypatch.setattr(worker_module.platform, "system", lambda: "Windows")
    monkeypatch.delenv("COMPARE_WORKER_CLASS", raising=False)
    worker_module.reset_rq_runtime_cache()

    real_import_module = importlib.import_module

    def fake_import_module(name, package=None):
        if name == "rq":
            raise AttributeError("module 'os' has no attribute 'fork'")
        return real_import_module(name, package)

    monkeypatch.setattr(worker_module.importlib, "import_module", fake_import_module)

    with pytest.raises(RuntimeError) as excinfo:
        worker_module.select_worker_class()

    assert "No se pudo cargar RQ de forma segura en Windows" in str(excinfo.value)
    assert "fork()" in str(excinfo.value)


def test_windows_without_spawn_requires_rq_22_or_development_fallback(monkeypatch):
    monkeypatch.delenv("COMPARE_WORKER_CLASS", raising=False)
    monkeypatch.setenv("COMPARE_WINDOWS_WORKER_MODE", "production")
    worker_module = _load_worker_module(monkeypatch, include_spawn=False)
    monkeypatch.setattr(worker_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(worker_module, "rq_version", lambda: "1.16.2")

    with pytest.raises(RuntimeError, match="RQ >= 2.2"):
        worker_module.select_worker_class()


def test_windows_development_fallback_uses_simple_worker_only_when_spawn_missing(monkeypatch):
    monkeypatch.delenv("COMPARE_WORKER_CLASS", raising=False)
    monkeypatch.setenv("COMPARE_WINDOWS_WORKER_MODE", "development")
    worker_module = _load_worker_module(monkeypatch, include_spawn=False)
    monkeypatch.setattr(worker_module.platform, "system", lambda: "Windows")

    assert worker_module.select_worker_class() is DummySimpleWorker


def test_create_worker_builds_queues_and_name(monkeypatch):
    monkeypatch.delenv("COMPARE_WORKER_CLASS", raising=False)
    worker_module = _load_worker_module(monkeypatch, include_spawn=True)
    monkeypatch.setattr(worker_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(worker_module, "build_worker_name", lambda: "worker-name")

    worker = worker_module.create_worker(["compare", "priority"], connection=object())

    assert isinstance(worker, DummyWorker)
    assert worker.kwargs["name"] == "worker-name"
    assert len(worker.args[0]) == 2
