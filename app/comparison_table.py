from __future__ import annotations

from typing import Any

from .text_segments import normalize_text_segments


def build_comparison_rows(block_diffs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_row(row, index) for index, row in enumerate(block_diffs or [], start=1)]


def resolve_comparison_rows(preferred_rows: list[dict[str, Any]] | None, block_diffs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    rows = preferred_rows if preferred_rows else block_diffs
    return [normalize_row(row, index) for index, row in enumerate(rows or [], start=1) if isinstance(row, dict)]


def filter_comparison_rows(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [normalize_row(row, index) for index, row in enumerate(rows or [], start=1) if str((row or {}).get("change_type") or "").lower() != "sin_cambios"]


def build_focus_comparison_rows(*, text_a: str, text_b: str, block_diffs: list[dict[str, Any]] | None, **_: Any) -> list[dict[str, Any]]:
    if block_diffs:
        return build_comparison_rows(block_diffs)
    if (text_a or "").strip() == (text_b or "").strip():
        return []
    return [normalize_row({"block_id": 1, "text_a": text_a, "text_b": text_b, "change_type": "modificado"}, 1)]


def normalize_row(row: dict[str, Any], index: int) -> dict[str, Any]:
    text_a = str(row.get("text_a") or row.get("display_text_a") or "")
    text_b = str(row.get("text_b") or row.get("display_text_b") or "")
    return {
        **row,
        "block_id": int(row.get("block_id") or index),
        "pair_id": str(row.get("pair_id") or f"block-{index}"),
        "pair_hash": str(row.get("pair_hash") or row.get("cache_pair_hash") or f"pair-{index}"),
        "text_a": text_a,
        "text_b": text_b,
        "display_text_a": str(row.get("display_text_a") or text_a),
        "display_text_b": str(row.get("display_text_b") or text_b),
        "display_segments_a": normalize_text_segments(row.get("display_segments_a") or ([{"type": "equal", "text": text_a}] if text_a else [])),
        "display_segments_b": normalize_text_segments(row.get("display_segments_b") or ([{"type": "equal", "text": text_b}] if text_b else [])),
        "change_type": str(row.get("change_type") or "modificado"),
        "summary": str(row.get("summary") or ""),
        "impact": str(row.get("impact") or ""),
        "llm_comment": str(row.get("llm_comment") or row.get("summary") or ""),
        "justification": str(row.get("justification") or ""),
        "confidence": str(row.get("confidence") or "media"),
        "severity": str(row.get("severity") or "media"),
        "final_decision": str(row.get("final_decision") or "pendiente_confirmacion"),
        "materiality": str(row.get("materiality") or "pendiente_confirmacion"),
        "review_status": str(row.get("review_status") or "pending"),
        "decision_source": str(row.get("decision_source") or "llm_candidate_blocks_only"),
        "result_origin": str(row.get("result_origin") or "llm"),
        "result_validation_status": str(row.get("result_validation_status") or "validated"),
        "fallback_applied": bool(row.get("fallback_applied", False)),
        "cache_hit": bool(row.get("cache_hit", False)),
        "cache_pair_hash": str(row.get("cache_pair_hash") or row.get("pair_hash") or f"pair-{index}"),
        "llm_success": bool(row.get("llm_success", False)),
        "model_name": str(row.get("model_name") or "local-compare-worker"),
        "prompt_version": str(row.get("prompt_version") or "compare-block-v3"),
        "prompt_text_a_literal": str(row.get("prompt_text_a_literal") or text_a),
        "prompt_text_b_literal": str(row.get("prompt_text_b_literal") or text_b),
        "prompt_messages": row.get("prompt_messages") if isinstance(row.get("prompt_messages"), list) else [],
        "relation_type": str(row.get("relation_type") or ""),
        "relation_notes": str(row.get("relation_notes") or ""),
        "related_block_ids": row.get("related_block_ids") if isinstance(row.get("related_block_ids"), list) else [],
        "source_spans": row.get("source_spans") if isinstance(row.get("source_spans"), dict) else {},
        "pairing": row.get("pairing") if isinstance(row.get("pairing"), dict) else {},
        "chunk_index_a": int(row.get("chunk_index_a") or 0),
        "chunk_index_b": int(row.get("chunk_index_b") or 0),
        "offset_start_a": int(row.get("offset_start_a") or 0),
        "offset_end_a": int(row.get("offset_end_a") or 0),
        "offset_start_b": int(row.get("offset_start_b") or 0),
        "offset_end_b": int(row.get("offset_end_b") or 0),
        "block_word_count_a": int(row.get("block_word_count_a") or len(text_a.split())),
        "block_word_count_b": int(row.get("block_word_count_b") or len(text_b.split())),
        "block_size_words": int(row.get("block_size_words") or max(len(text_a.split()), len(text_b.split()))),
        "block_overlap_words": int(row.get("block_overlap_words") or 0),
        "alignment_score": float(row.get("alignment_score") or 0.0),
        "alignment_strategy": str(row.get("alignment_strategy") or ""),
        "reanchored": bool(row.get("reanchored", False)),
    }