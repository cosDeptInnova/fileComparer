from __future__ import annotations

import logging

from app.schemas import ComparisonResult
from app.services.comparison_pipeline import compare_documents
from app.services.queue import persist_job_result, update_job_state

logger = logging.getLogger(__name__)


def run_compare_job(sid: str, path_a: str, path_b: str) -> dict[str, object]:
    update_job_state(
        sid,
        sid=sid,
        status="running",
        percent=20,
        step="extrayendo",
        detail="Extracción y normalización",
    )
    try:
        result: ComparisonResult = compare_documents(path_a, path_b, sid)
        payload = result.model_dump(mode="json")
        persist_job_result(sid, payload)
        update_job_state(
            sid,
            sid=sid,
            status="done",
            percent=100,
            step="completado",
            detail="Comparación finalizada",
        )
        return payload
    except Exception as exc:  # noqa: BLE001
        logger.exception("Job %s falló", sid)
        update_job_state(
            sid,
            sid=sid,
            status="error",
            percent=100,
            step="error",
            detail=str(exc),
            error=str(exc),
        )
        raise
