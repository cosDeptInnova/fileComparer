from __future__ import annotations

from app.schemas import ChangeRow, ComparisonResult
from app.services import jobs


def test_run_compare_job_persists_partial_result_and_warning_state(monkeypatch):
    stored_payloads: list[dict[str, object]] = []
    updated_states: list[dict[str, object]] = []

    def fake_compare_documents(*args, **kwargs):
        return ComparisonResult(
            sid="sid-job",
            status="done_with_warnings",
            progress={
                "percent": 100,
                "step": "completado",
                "detail": "Comparación finalizada con resultado parcial",
            },
            rows=[
                ChangeRow(
                    block_id=1,
                    pair_id="sid-job-1",
                    change_type="modificado",
                    display_text_a="A",
                    display_text_b="B",
                    summary="cambio",
                )
            ],
            meta={
                "partial_result": True,
                "diagnostics": {
                    "failed_blocks": 1,
                    "total_pairs": 3,
                    "partial_result": True,
                    "errors": [
                        {
                            "pair_id": "sid-job-2",
                            "stage": "compare_pair",
                            "error_type": "RuntimeError",
                            "message": "fallo parcial",
                        }
                    ],
                },
                "error_summary": {
                    "failed_blocks": 1,
                    "total_pairs": 3,
                    "failed_ratio": 1 / 3,
                },
            },
        )

    monkeypatch.setattr(jobs, "compare_documents", fake_compare_documents)
    monkeypatch.setattr(jobs, "persist_job_result", lambda sid, payload: stored_payloads.append(payload))
    monkeypatch.setattr(jobs, "update_job_state", lambda sid, **fields: updated_states.append({"sid": sid, **fields}))

    payload = jobs.run_compare_job("sid-job", "/tmp/a.txt", "/tmp/b.txt")

    assert payload["status"] == "done_with_warnings"
    assert stored_payloads and stored_payloads[0]["meta"]["partial_result"] is True
    assert updated_states[-1] == {
        "sid": "sid-job",
        "status": "done_with_warnings",
        "percent": 100,
        "step": "completado",
        "detail": "Comparación finalizada con resultado parcial",
        "partial_result": True,
        "summary": {
            "failed_blocks": 1,
            "total_pairs": 3,
            "failed_ratio": 1 / 3,
        },
        "diagnostics": {
            "failed_blocks": 1,
            "total_pairs": 3,
            "partial_result": True,
            "errors": [
                {
                    "pair_id": "sid-job-2",
                    "stage": "compare_pair",
                    "error_type": "RuntimeError",
                    "message": "fallo parcial",
                }
            ],
        },
        "failed_blocks": 1,
        "total_pairs": 3,
    }
