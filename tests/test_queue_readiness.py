from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import app
from app.settings import settings


class DummyInspect:
    def __init__(self, payload):
        self.payload = payload

    def active_queues(self):
        return self.payload


class DummyControl:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def inspect(self, timeout=None):
        self.calls.append(timeout)
        return DummyInspect(self.payload)


class DummyCeleryApp:
    def __init__(self, payload):
        self.control = DummyControl(payload)


class DummyRedis:
    def ping(self):
        return True


def test_count_queue_workers_filters_by_queue(monkeypatch):
    from app.services import queue as queue_module

    monkeypatch.setattr(queue_module, "redis_connection", lambda: DummyRedis())
    monkeypatch.setattr(
        queue_module,
        "celery_app",
        DummyCeleryApp(
            {
                "worker-a": [{"name": "compare"}],
                "worker-b": [{"name": "other"}],
                "worker-c": [{"name": "compare"}, {"name": "priority"}],
            }
        ),
    )

    assert queue_module.count_queue_workers("compare") == 2
    assert queue_module.count_queue_workers("missing") == 0


def test_compare_endpoint_returns_503_when_workers_are_required(monkeypatch, tmp_path: Path):
    from app.routes import comparar as comparar_route

    monkeypatch.setattr(settings, "inline_jobs", False)
    monkeypatch.setattr(settings, "require_active_workers", True)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "compare-jobs")
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    state_store: dict[str, dict[str, object]] = {}

    def fake_update_job_state(job_id: str, **fields: object) -> None:
        state_store[job_id] = {**state_store.get(job_id, {}), "sid": job_id, **fields}

    monkeypatch.setattr(comparar_route, "update_job_state", fake_update_job_state)
    monkeypatch.setattr(
        comparar_route,
        "ensure_queue_backend_ready",
        lambda require_active_workers=True: (_ for _ in ()).throw(RuntimeError("No hay workers Celery activos")),
    )

    client = TestClient(app)
    csrf_token = client.get("/csrf-token").json()["csrf_token"]

    response = client.post(
        "/comparar",
        headers={"X-CSRFToken": csrf_token},
        cookies={settings.csrf_cookie_name: csrf_token},
        files={
            "file_a": ("a.txt", b"hola", "text/plain"),
            "file_b": ("b.txt", b"mundo", "text/plain"),
        },
        data={"engine": "auto"},
    )

    assert response.status_code == 503
    assert "workers Celery activos" in response.json()["detail"]
    assert any(state.get("status") == "error" for state in state_store.values())
