from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.schemas import ComparisonResult
from app.services.comparison_pipeline import ExtractionOptions, compare_documents
from app.services.queue import persist_job_result, update_job_state

logger = logging.getLogger(__name__)


def _has_useful_result_data(result: ComparisonResult) -> bool:
    return bool(result.rows) or bool(result.meta) or bool(result.error)


@celery_app.task(name="app.services.jobs.run_compare_job")
def run_compare_job(
    sid: str,
    path_a: str,
    path_b: str,
    *,
    soffice_path: str | None = None,
    engine: str = "auto",
    drop_headers: bool = True,
) -> dict[str, object]:
    update_job_state(
        sid,
        status="running",
        percent=20,
        step="extrayendo",
        detail="Extracción y normalización",
    )
    try:
        result: ComparisonResult = compare_documents(
            path_a,
            path_b,
            sid,
            extraction=ExtractionOptions(
                engine=engine,
                soffice_path=soffice_path,
                drop_headers=drop_headers,
            ),
        )
        payload = result.model_dump(mode="json")
        diagnostics = result.meta.get("diagnostics") if isinstance(result.meta, dict) else {}
        error_summary = result.meta.get("error_summary") if isinstance(result.meta, dict) else {}
        progress = result.progress if isinstance(result.progress, dict) else {}
        if _has_useful_result_data(result):
            persist_job_result(sid, payload)
        update_job_state(
            sid,
            status=result.status,
            percent=int(progress.get("percent", 100)),
            step=str(progress.get("step", "completado")),
            detail=str(progress.get("detail", "Comparación finalizada")),
            partial_result=bool(result.meta.get("partial_result")),
            summary=error_summary,
            diagnostics=diagnostics,
            failed_blocks=diagnostics.get("failed_blocks", 0) if isinstance(diagnostics, dict) else 0,
            total_pairs=diagnostics.get("total_pairs", 0) if isinstance(diagnostics, dict) else 0,
        )
        return payload
    except Exception as exc:  # noqa: BLE001
        logger.exception("Job %s falló", sid)
        update_job_state(
            sid,
            status="error",
            percent=100,
            step="error",
            detail=str(exc),
            error=str(exc),
        )
        raise
