from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class _MetricsState:
    redis_up: bool = False
    max_concurrent: int = 0
    compare_active_workers: int = 0
    compare_queue_depth: int = 0
    compare_requests: int = 0
    compare_errors: dict[str, int] = field(default_factory=dict)
    file_sizes: list[dict[str, Any]] = field(default_factory=list)
    av_results: list[dict[str, Any]] = field(default_factory=list)
    queue_events: dict[str, int] = field(default_factory=dict)
    job_events: dict[str, int] = field(default_factory=dict)
    extraction_durations: dict[str, list[float]] = field(default_factory=dict)
    compare_phase_durations: dict[str, list[float]] = field(default_factory=dict)
    llm_durations: list[float] = field(default_factory=list)
    llm_outcomes: dict[str, int] = field(default_factory=dict)
    cache_stats: dict[str, int] = field(default_factory=lambda: {"hits": 0, "misses": 0})
    inference_active_current: int = 0
    inference_active_max: int = 0
    inference_active_samples: list[int] = field(default_factory=list)


_STATE = _MetricsState()


def setup_metrics(app) -> None:
    app.state.metrics_enabled = True


def set_redis_up(value: bool) -> None:
    _STATE.redis_up = bool(value)


def set_max_concurrent(value: int) -> None:
    _STATE.max_concurrent = int(value)


def set_compare_active_workers(value: int) -> None:
    _STATE.compare_active_workers = int(value)


def set_compare_queue_depth(value: int) -> None:
    _STATE.compare_queue_depth = int(value)


def inc_compare_request(engine: str) -> None:
    _STATE.compare_requests += 1
    _STATE.queue_events[f"request:{engine or 'unknown'}"] = _STATE.queue_events.get(
        f"request:{engine or 'unknown'}", 0
    ) + 1


def inc_compare_error(reason: str, engine: str) -> None:
    _STATE.compare_errors[reason] = _STATE.compare_errors.get(reason, 0) + 1
    _STATE.job_events[f"error:{engine or 'unknown'}:{reason}"] = _STATE.job_events.get(
        f"error:{engine or 'unknown'}:{reason}", 0
    ) + 1


def observe_file_size(side: str, filename: str, size_bytes: int) -> None:
    _STATE.file_sizes.append({"side": side, "filename": filename, "size_bytes": int(size_bytes)})
    _STATE.file_sizes[:] = _STATE.file_sizes[-50:]


def record_av_result(result: dict[str, Any]) -> None:
    _STATE.av_results.append(dict(result or {}))
    _STATE.av_results[:] = _STATE.av_results[-50:]


def record_queue_event(name: str, *, count: int = 1) -> None:
    _STATE.queue_events[name] = _STATE.queue_events.get(name, 0) + int(count)


def record_job_event(name: str, *, count: int = 1) -> None:
    _STATE.job_events[name] = _STATE.job_events.get(name, 0) + int(count)


def observe_extraction_duration(engine: str, seconds: float) -> None:
    bucket = _STATE.extraction_durations.setdefault(engine or "unknown", [])
    bucket.append(float(seconds))
    bucket[:] = bucket[-200:]


def observe_compare_phase_duration(phase: str, seconds: float) -> None:
    bucket = _STATE.compare_phase_durations.setdefault(phase or "unknown", [])
    bucket.append(float(seconds))
    bucket[:] = bucket[-200:]


def observe_inference_concurrency(active: int) -> None:
    value = max(0, int(active))
    _STATE.inference_active_current = value
    _STATE.inference_active_max = max(_STATE.inference_active_max, value)
    _STATE.inference_active_samples.append(value)
    _STATE.inference_active_samples[:] = _STATE.inference_active_samples[-200:]


def observe_llm_duration(seconds: float, *, outcome: str) -> None:
    _STATE.llm_durations.append(float(seconds))
    _STATE.llm_durations[:] = _STATE.llm_durations[-200:]
    _STATE.llm_outcomes[outcome] = _STATE.llm_outcomes.get(outcome, 0) + 1


def record_cache_hit(hit: bool) -> None:
    key = "hits" if hit else "misses"
    _STATE.cache_stats[key] = _STATE.cache_stats.get(key, 0) + 1


def metrics_snapshot() -> dict[str, Any]:
    cache_hits = int(_STATE.cache_stats.get("hits", 0))
    cache_misses = int(_STATE.cache_stats.get("misses", 0))
    cache_total = cache_hits + cache_misses
    return {
        "generated_at_epoch": int(time.time()),
        "redis_up": _STATE.redis_up,
        "max_concurrent": _STATE.max_concurrent,
        "queue": {
            "depth": _STATE.compare_queue_depth,
            "active_workers": _STATE.compare_active_workers,
            "events": dict(_STATE.queue_events),
        },
        "jobs": {
            "requests_total": _STATE.compare_requests,
            "errors_total": sum(_STATE.compare_errors.values()),
            "errors_by_reason": dict(_STATE.compare_errors),
            "events": dict(_STATE.job_events),
        },
        "timings": {
            "extraction_seconds": {
                engine: _summarize(values) for engine, values in _STATE.extraction_durations.items()
            },
            "compare_phase_seconds": {
                phase: _summarize(values) for phase, values in _STATE.compare_phase_durations.items()
            },
            "llm_seconds": _summarize(_STATE.llm_durations),
            "llm_outcomes": dict(_STATE.llm_outcomes),
            "inference_concurrency": {
                "current": _STATE.inference_active_current,
                "max": _STATE.inference_active_max,
                "samples": _summarize([float(v) for v in _STATE.inference_active_samples]),
            },
        },
        "cache": {
            "hits": cache_hits,
            "misses": cache_misses,
            "hit_ratio": round(cache_hits / cache_total, 4) if cache_total else None,
        },
        "recent": {
            "file_sizes": list(_STATE.file_sizes[-10:]),
            "av_results": list(_STATE.av_results[-10:]),
        },
    }


def _summarize(values: list[float]) -> dict[str, Optional[float]]:
    if not values:
        return {"count": 0, "avg": None, "max": None}
    return {
        "count": len(values),
        "avg": round(sum(values) / len(values), 4),
        "max": round(max(values), 4),
    }
