from __future__ import annotations

import importlib

import app.settings as settings_module
from app.services import queue as queue_module


class DummyRedis:
    def __init__(self, payload):
        self.payload = payload

    def hgetall(self, _key):
        return self.payload


class DummyRedisFactory:
    def __init__(self):
        self.calls = []

    def from_url(self, url, decode_responses=False):
        self.calls.append({"url": url, "decode_responses": decode_responses})
        return DummyRedis({})


def test_settings_accepts_legacy_env_names(monkeypatch):
    monkeypatch.setenv("LLAMA_SERVER_BASE_URL", "http://legacy-llm/v1")
    monkeypatch.setenv("LLM_MODEL", "legacy-model")
    monkeypatch.setenv("COMPARE_LLM_TIMEOUT_SECONDS", "33")
    monkeypatch.setenv("TEXT_COMPARE_MAX_FILE_MB", "55")
    monkeypatch.delenv("LLAMA_CPP_BASE_URL", raising=False)
    monkeypatch.delenv("LLAMA_CPP_MODEL", raising=False)
    monkeypatch.delenv("LLAMA_CPP_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("COMPARE_MAX_FILE_MB", raising=False)

    reloaded = importlib.reload(settings_module)
    try:
        settings = reloaded.Settings()
        assert settings.llm_base_url == "http://legacy-llm/v1"
        assert settings.llm_model == "legacy-model"
        assert settings.llm_timeout_seconds == 33.0
        assert settings.max_file_mb == 55
    finally:
        importlib.reload(settings_module)


def test_redis_connection_keeps_binary_responses(monkeypatch):
    factory = DummyRedisFactory()
    monkeypatch.setattr(queue_module, "Redis", factory)

    queue_module.redis_connection()

    assert factory.calls == [{"url": queue_module.settings.redis_url, "decode_responses": False}]


def test_read_job_state_decodes_bytes(monkeypatch):
    payload = {
        b"status": b"done",
        b"percent": b"100",
        b"progress": b'{"detail":"ok"}',
    }
    monkeypatch.setattr(queue_module, "redis_connection", lambda: DummyRedis(payload))

    state = queue_module.read_job_state("abc")

    assert state == {"status": "done", "percent": 100, "progress": {"detail": "ok"}}
