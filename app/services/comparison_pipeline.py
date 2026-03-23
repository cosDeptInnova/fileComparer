from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.settings import settings
from app.llm_client import LLMClient
from app.schemas import ChangeRow, ComparisonResult, ExtractedDocument
from app.services.extractors import extract_text_from_path
from app.services.normalization import normalize_text
from app.services.postprocess import build_reconciliation_payload, merge_reconciled_rows
from app.services.segmenter import TextBlock, build_blocks

logger = logging.getLogger(__name__)
TOKEN_RE = re.compile(r"\w+", re.UNICODE)


@dataclass(slots=True)
class PreparedDocument:
    document: ExtractedDocument
    segments: list[TextBlock]


def _token_overlap_score(a: str, b: str) -> float:
    set_a = set(TOKEN_RE.findall(a.lower()))
    set_b = set(TOKEN_RE.findall(b.lower()))
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def prepare_document(path: str | Path) -> PreparedDocument:
    raw_text, metadata = extract_text_from_path(path)
    clean_text = normalize_text(raw_text)
    blocks = build_blocks(clean_text, settings.block_target_chars, settings.block_overlap_chars)
    document = ExtractedDocument(
        filename=Path(path).name,
        extension=Path(path).suffix.lower(),
        raw_text=raw_text,
        clean_text=clean_text,
        blocks=[block.text for block in blocks],
        metadata=metadata,
    )
    return PreparedDocument(document=document, segments=blocks)


def _pair_blocks(blocks_a: list[TextBlock], blocks_b: list[TextBlock]) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    cursor_b = 0
    for block_a in blocks_a:
        candidates = []
        for candidate_index in range(cursor_b, min(len(blocks_b), cursor_b + 4)):
            block_b = blocks_b[candidate_index]
            score = _token_overlap_score(block_a.text, block_b.text)
            candidates.append((score, candidate_index, block_b))
        if candidates:
            best_score, best_index, best_block_b = max(candidates, key=lambda item: item[0])
            if best_score > 0.08:
                pairs.append(
                    {
                        "a": block_a,
                        "b": best_block_b,
                        "alignment_score": best_score,
                        "reanchored": best_index != cursor_b,
                    }
                )
                cursor_b = best_index + 1
                continue
        pairs.append({"a": block_a, "b": None, "alignment_score": 0.0, "reanchored": True})
    for orphan in blocks_b[cursor_b:]:
        pairs.append({"a": None, "b": orphan, "alignment_score": 0.0, "reanchored": True})
    return pairs


COMPARISON_SYSTEM_PROMPT = """Eres un comparador documental semántico.
Debes ignorar formato, maquetación, encabezados/pies repetidos, numeraciones y ruido OCR.
Solo clasifica hallazgos con: añadido, eliminado o modificado.
No inventes cambios ni expliques libremente.
Devuelve JSON estricto con esta forma: {\"changes\":[{\"change_type\":\"añadido|eliminado|modificado\",\"source_a\":\"texto en A o vacío\",\"source_b\":\"texto en B o vacío\",\"summary\":\"resumen breve\",\"confidence\":\"baja|media|alta\",\"severity\":\"baja|media|alta|critica\",\"evidence\":\"frase corta\",\"anchor_a\":0,\"anchor_b\":0}]}
Si no hay cambios, responde {\"changes\":[]}.
No dupliques hallazgos. Sé prudente cuando la evidencia sea débil."""

RECONCILE_SYSTEM_PROMPT = """Fusiona resultados parciales de comparación documental.
Corrige duplicados por solapamiento y conserva solo añadido, eliminado o modificado.
No añadas comentarios narrativos.
Devuelve JSON estricto con la misma forma {\"changes\":[...]}.
Si todos los hallazgos ya son consistentes, devuelve los mismos sin duplicados."""


def _comparison_messages(block_a: TextBlock | None, block_b: TextBlock | None) -> list[dict[str, str]]:
    payload = {
        "block_a": {
            "index": None if block_a is None else block_a.index,
            "text": "" if block_a is None else block_a.text,
        },
        "block_b": {
            "index": None if block_b is None else block_b.index,
            "text": "" if block_b is None else block_b.text,
        },
    }
    return [
        {"role": "system", "content": COMPARISON_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _reconcile_messages(rows: list[ChangeRow]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": RECONCILE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps({"rows": build_reconciliation_payload(rows)}, ensure_ascii=False),
        },
    ]


def compare_documents(
    path_a: str | Path, path_b: str | Path, sid: str, llm_client: LLMClient | None = None
) -> ComparisonResult:
    client = llm_client or LLMClient()
    prepared_a = prepare_document(path_a)
    prepared_b = prepare_document(path_b)
    pairs = _pair_blocks(prepared_a.segments, prepared_b.segments)
    rows: list[ChangeRow] = []
    for index, pair in enumerate(pairs, start=1):
        block_a = pair["a"]
        block_b = pair["b"]
        llm_response = client.compare(_comparison_messages(block_a, block_b))
        for change in llm_response.changes:
            rows.append(
                ChangeRow(
                    block_id=len(rows) + 1,
                    pair_id=f"{sid}-{index}",
                    text_a=change.source_a,
                    text_b=change.source_b,
                    display_text_a=change.source_a or ("" if block_a is None else block_a.text),
                    display_text_b=change.source_b or ("" if block_b is None else block_b.text),
                    change_type=change.change_type,
                    confidence=change.confidence,
                    severity=change.severity,
                    summary=change.summary,
                    llm_comment=change.evidence,
                    chunk_index_a=-1 if block_a is None else block_a.index,
                    chunk_index_b=-1 if block_b is None else block_b.index,
                    offset_start_a=0 if block_a is None else block_a.start_char,
                    offset_end_a=0 if block_a is None else block_a.end_char,
                    offset_start_b=0 if block_b is None else block_b.start_char,
                    offset_end_b=0 if block_b is None else block_b.end_char,
                    pairing={
                        "alignment_score": pair["alignment_score"],
                        "strategy": "correlative_window_overlap",
                        "reanchored": pair["reanchored"],
                    },
                    source_spans={
                        "a": None if block_a is None else {"start": block_a.start_char, "end": block_a.end_char},
                        "b": None if block_b is None else {"start": block_b.start_char, "end": block_b.end_char},
                    },
                )
            )
    reconciled = client.compare(_reconcile_messages(rows)) if rows else None
    final_rows = merge_reconciled_rows(rows, reconciled)
    return ComparisonResult(
        sid=sid,
        status="done",
        progress={"percent": 100, "step": "completado", "detail": "Comparación finalizada"},
        rows=final_rows,
        meta={
            "pagination": {
                "offset": 0,
                "limit": None,
                "returned": len(final_rows),
                "total": len(final_rows),
                "has_more": False,
                "next_offset": None,
                "truncated": False,
            },
            "audit": {
                "all_rows_count": len(final_rows),
                "filtered_rows_count": len(final_rows),
                "unchanged_rows_count": 0,
            },
            "cache": {
                "policy": "no-store",
                "resolved_from_cache": 0,
                "resolved_by_llm": len(final_rows),
                "failed_blocks": 0,
                "block_size_words": 0,
                "block_overlap_words": 0,
                "model_name": client.model_name,
                "comparison_mode": "llm_semantic_blocks",
            },
            "segmentation": {
                "block_target_chars": settings.block_target_chars,
                "block_overlap_chars": settings.block_overlap_chars,
                "doc_a_blocks": len(prepared_a.segments),
                "doc_b_blocks": len(prepared_b.segments),
            },
            "pairing": {"strategy": "correlative_window_overlap", "pair_count": len(pairs)},
            "documents": {
                "a": prepared_a.document.model_dump(),
                "b": prepared_b.document.model_dump(),
            },
        },
    )
