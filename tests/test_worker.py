from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _clear_app_modules():
    sys.modules.pop("app.worker", None)


def _load_worker_module():
    _clear_app_modules()
    return importlib.import_module("app.worker")


def test_build_worker_name_includes_instance_host_and_pid(monkeypatch):
    monkeypatch.setenv("COMPARE_WORKER_NAME_PREFIX", "comp_docs_worker")
    monkeypatch.setenv("SERVICE_INSTANCE_NUMBER", "3")
    monkeypatch.delenv("COMPARE_WORKER_NAME", raising=False)
    worker_module = _load_worker_module()
    monkeypatch.setattr(worker_module.socket, "gethostname", lambda: "srv prod")
    monkeypatch.setattr(worker_module.os, "getpid", lambda: 4242)

    name = worker_module.build_worker_name()

    assert name == "comp_docs_worker-3-srv-prod-4242"


def test_build_worker_name_honors_explicit_override(monkeypatch):
    monkeypatch.setenv("COMPARE_WORKER_NAME", "comp_docs_worker-prod-a")
    worker_module = _load_worker_module()

    assert worker_module.build_worker_name() == "comp_docs_worker-prod-a"


def test_default_worker_pool_uses_threads_on_windows(monkeypatch):
    worker_module = _load_worker_module()
    monkeypatch.setattr(worker_module.os, "name", "nt")
    monkeypatch.delenv("COMPARE_CELERY_POOL", raising=False)

    assert worker_module.default_worker_pool() == "threads"


def test_default_worker_pool_uses_prefork_outside_windows(monkeypatch):
    worker_module = _load_worker_module()
    monkeypatch.setattr(worker_module.os, "name", "posix")
    monkeypatch.delenv("COMPARE_CELERY_POOL", raising=False)

    assert worker_module.default_worker_pool() == "prefork"


def test_build_worker_argv_includes_queue_name(monkeypatch):
    worker_module = _load_worker_module()
    monkeypatch.setattr(worker_module, "build_worker_name", lambda: "worker-name")

    args = worker_module.parse_args(["--queue", "compare", "--queue", "priority", "--concurrency", "4", "--pool", "threads"])
    argv = worker_module.build_worker_argv(args)

    assert argv == [
        "worker",
        "--loglevel=INFO",
        "--hostname=worker-name@%h",
        "--concurrency=4",
        "--pool=threads",
        "--queues=compare,priority",
    ]


def test_burst_flag_is_rejected(monkeypatch):
    worker_module = _load_worker_module()
    args = worker_module.parse_args(["--burst"])

    with pytest.raises(RuntimeError, match="--burst"):
        worker_module.build_worker_argv(args)
