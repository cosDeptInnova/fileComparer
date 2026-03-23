from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class DummySpawnWorker:
    def __init__(self):
        self.horse_pid = 77

    def wait_for_horse(self):
        raise AssertionError("Debe ser reemplazado por el parche de compatibilidad")


class DummyWorker:
    pass


class DummyQueue:
    pass


class DummySimpleWorker:
    pass


def _clear_modules():
    sys.modules.pop("app.services.rq_compat", None)
    sys.modules.pop("rq", None)
    sys.modules.pop("rq.worker", None)


def test_windows_spawn_worker_uses_waitpid_when_wait4_is_missing(monkeypatch):
    _clear_modules()

    rq_mod = types.ModuleType("rq")
    rq_worker_mod = types.ModuleType("rq.worker")
    rq_mod.Queue = DummyQueue
    rq_mod.Worker = DummyWorker
    rq_mod.SimpleWorker = DummySimpleWorker
    rq_worker_mod.SpawnWorker = DummySpawnWorker
    monkeypatch.setitem(sys.modules, "rq", rq_mod)
    monkeypatch.setitem(sys.modules, "rq.worker", rq_worker_mod)

    compat = importlib.import_module("app.services.rq_compat")
    monkeypatch.setattr(compat.platform, "system", lambda: "Windows")
    monkeypatch.setattr(compat.metadata, "version", lambda _: "2.3.0")
    monkeypatch.delattr(compat.os, "wait4", raising=False)
    monkeypatch.setattr(compat.os, "waitpid", lambda pid, options: (pid, 123))
    compat._RQ_RUNTIME_CACHE = None
    compat._PATCHED_WINDOWS_MP = False
    compat._PATCHED_WINDOWS_SPAWN_WORKER = False

    runtime = compat.load_rq_runtime()

    spawn_worker = runtime["SpawnWorker"]()
    assert spawn_worker.wait_for_horse() == (77, 123, None)
    assert compat._PATCHED_WINDOWS_SPAWN_WORKER is True


def test_windows_spawn_worker_skips_legacy_patch_on_supported_versions(monkeypatch):
    _clear_modules()

    rq_mod = types.ModuleType("rq")
    rq_worker_mod = types.ModuleType("rq.worker")
    rq_mod.Queue = DummyQueue
    rq_mod.Worker = DummyWorker
    rq_mod.SimpleWorker = DummySimpleWorker
    rq_worker_mod.SpawnWorker = DummySpawnWorker
    monkeypatch.setitem(sys.modules, "rq", rq_mod)
    monkeypatch.setitem(sys.modules, "rq.worker", rq_worker_mod)

    compat = importlib.import_module("app.services.rq_compat")
    monkeypatch.setattr(compat.platform, "system", lambda: "Windows")
    monkeypatch.setattr(compat.metadata, "version", lambda _: "2.7.0")
    monkeypatch.delattr(compat.os, "wait4", raising=False)
    compat._RQ_RUNTIME_CACHE = None
    compat._RQ_RUNTIME_CACHE_SIGNATURE = None
    compat._PATCHED_WINDOWS_MP = False
    compat._PATCHED_WINDOWS_SPAWN_WORKER = False

    runtime = compat.load_rq_runtime()

    assert runtime["SpawnWorker"] is DummySpawnWorker
    assert compat._PATCHED_WINDOWS_MP is False
    assert compat._PATCHED_WINDOWS_SPAWN_WORKER is False
