from __future__ import annotations

from typing import Any

from .document_compare.pipeline import CompareDocumentsService


def compare_texts(A: str, B: str, opts: dict, progress_cb=lambda p, s, d: None):
    service = CompareDocumentsService(llm_client=None)
    return service._diff_row(  # type: ignore[attr-defined]
        row_index=1,
        text_a=A,
        text_b=B,
        alignment=type("Alignment", (), {"score": 1.0, "strategy": "direct", "block_a": type("B", (), {"block_id": 1})(), "block_b": type("B", (), {"block_id": 1})()})(),
    )


def compare_files(path_a: str, path_b: str, opts: dict[str, Any], progress_cb=lambda p, s, d: None):
    service = CompareDocumentsService(llm_client=None)
    return service.compare_documents(
        file_a_path=path_a,
        file_b_path=path_b,
        file_a_name=path_a,
        file_b_name=path_b,
        opts=opts,
        progress_cb=progress_cb,
    )
