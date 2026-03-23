from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.extractors import extract_document_result
from app.llm_client import LLMClient
from app.schemas import ChangeRow, ComparisonResult, ExtractedDocument
from app.services.normalization import normalize_text
from app.services.postprocess import build_reconciliation_payload, merge_reconciled_rows
from app.services.segmenter import TextBlock, build_blocks
from app.settings import settings

logger = logging.getLogger(__name__)
TOKEN_RE = re.compile(r"\w+", re.UNICODE)
PAIRING_ALGORITHM = "sequence_alignment"
MATCH_REWARD_BASELINE = 0.45
GAP_PENALTY = 0.35


@dataclass(slots=True)
class ExtractionOptions:
    engine: str = "auto"
    soffice_path: str | None = None
    drop_headers: bool = True


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


def _relative_length_score(a: str, b: str) -> float:
    len_a = len(a.strip())
    len_b = len(b.strip())
    if not len_a or not len_b:
        return 0.0
    return min(len_a, len_b) / max(len_a, len_b)


def _relative_position_score(index_a: int, total_a: int, index_b: int, total_b: int) -> float:
    if total_a <= 1 and total_b <= 1:
        return 1.0
    pos_a = 0.0 if total_a <= 1 else index_a / (total_a - 1)
    pos_b = 0.0 if total_b <= 1 else index_b / (total_b - 1)
    return max(0.0, 1.0 - abs(pos_a - pos_b))


def _block_similarity(block_a: TextBlock, block_b: TextBlock, total_a: int, total_b: int) -> float:
    overlap = _token_overlap_score(block_a.text, block_b.text)
    length_score = _relative_length_score(block_a.text, block_b.text)
    position_score = _relative_position_score(block_a.index, total_a, block_b.index, total_b)
    return (overlap * 0.6) + (length_score * 0.25) + (position_score * 0.15)


def prepare_document(path: str | Path, *, extraction: ExtractionOptions | None = None) -> PreparedDocument:
    extraction = extraction or ExtractionOptions()
    extraction_result = extract_document_result(
        str(path),
        soffice_path=extraction.soffice_path,
        drop_headers=extraction.drop_headers,
        engine=extraction.engine,
    )
    raw_text = extraction_result.text
    clean_text = normalize_text(raw_text)
    blocks = build_blocks(clean_text, settings.block_target_chars, settings.block_overlap_chars)
    quality_payload = extraction_result.to_quality_dict()
    metadata = {
        **dict(extraction_result.metadata),
        "quality": quality_payload,
        "quality_signals": dict(extraction_result.quality_signals),
        "engine_requested": extraction.engine,
        "engine_used": extraction_result.metadata.get("engine_used", extraction_result.engine),
        "conversion": dict(extraction_result.metadata.get("conversion") or {"applied": False}),
        "source_format_real": extraction_result.metadata.get("source_format_real") or Path(path).suffix.lower().lstrip("."),
        "source_format": extraction_result.metadata.get("source_format") or Path(path).suffix.lower().lstrip("."),
        "drop_headers": extraction.drop_headers,
        "segment_count": len(blocks),
    }
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
    total_a = len(blocks_a)
    total_b = len(blocks_b)
    dp = [[0.0] * (total_b + 1) for _ in range(total_a + 1)]
    direction = [["start"] * (total_b + 1) for _ in range(total_a + 1)]

    for index_a in range(1, total_a + 1):
        dp[index_a][0] = dp[index_a - 1][0] - GAP_PENALTY
        direction[index_a][0] = "up"
    for index_b in range(1, total_b + 1):
        dp[0][index_b] = dp[0][index_b - 1] - GAP_PENALTY
        direction[0][index_b] = "left"

    for index_a in range(1, total_a + 1):
        for index_b in range(1, total_b + 1):
            block_a = blocks_a[index_a - 1]
            block_b = blocks_b[index_b - 1]
            similarity = _block_similarity(block_a, block_b, total_a, total_b)
            diagonal = dp[index_a - 1][index_b - 1] + ((similarity * 2) - 1 - MATCH_REWARD_BASELINE)
            up = dp[index_a - 1][index_b] - GAP_PENALTY
            left = dp[index_a][index_b - 1] - GAP_PENALTY
            best_score = max(diagonal, up, left)
            dp[index_a][index_b] = best_score
            if best_score == diagonal:
                direction[index_a][index_b] = "diag"
            elif best_score == up:
                direction[index_a][index_b] = "up"
            else:
                direction[index_a][index_b] = "left"

    raw_pairs: list[dict[str, Any]] = []
    index_a = total_a
    index_b = total_b
    while index_a > 0 or index_b > 0:
        move = direction[index_a][index_b]
        if move == "diag" and index_a > 0 and index_b > 0:
            block_a = blocks_a[index_a - 1]
            block_b = blocks_b[index_b - 1]
            raw_pairs.append(
                {
                    "a": block_a,
                    "b": block_b,
                    "alignment_score": _block_similarity(block_a, block_b, total_a, total_b),
                }
            )
            index_a -= 1
            index_b -= 1
            continue
        if (move == "up" and index_a > 0) or index_b == 0:
            raw_pairs.append({"a": blocks_a[index_a - 1], "b": None, "alignment_score": 0.0})
            index_a -= 1
            continue
        raw_pairs.append({"a": None, "b": blocks_b[index_b - 1], "alignment_score": 0.0})
        index_b -= 1

    pairs = list(reversed(raw_pairs))
    last_matched_a: int | None = None
    last_matched_b: int | None = None
    for pair in pairs:
        block_a = pair["a"]
        block_b = pair["b"]
        if block_a is None:
            pair["pair_type"] = "orphan_b"
            pair["reanchored"] = False
            continue
        if block_b is None:
            pair["pair_type"] = "orphan_a"
            pair["reanchored"] = False
            continue
        reanchored = False
        if last_matched_a is None or last_matched_b is None:
            reanchored = block_a.index != 0 or block_b.index != 0
        else:
            reanchored = (block_a.index != last_matched_a + 1) or (block_b.index != last_matched_b + 1)
        pair["pair_type"] = "reanchored" if reanchored else "matched"
        pair["reanchored"] = reanchored
        last_matched_a = block_a.index
        last_matched_b = block_b.index
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
    path_a: str | Path,
    path_b: str | Path,
    sid: str,
    llm_client: LLMClient | None = None,
    *,
    extraction: ExtractionOptions | None = None,
) -> ComparisonResult:
    client = llm_client or LLMClient()
    extraction = extraction or ExtractionOptions()
    prepared_a = prepare_document(path_a, extraction=extraction)
    prepared_b = prepare_document(path_b, extraction=extraction)
    pairs = _pair_blocks(prepared_a.segments, prepared_b.segments)
    pairing_counts = {
        "matched_pairs": sum(1 for pair in pairs if pair["pair_type"] == "matched"),
        "orphan_a": sum(1 for pair in pairs if pair["pair_type"] == "orphan_a"),
        "orphan_b": sum(1 for pair in pairs if pair["pair_type"] == "orphan_b"),
        "reanchored_pairs": sum(1 for pair in pairs if pair["pair_type"] == "reanchored"),
    }
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
                        "strategy": pair["pair_type"],
                        "pair_type": pair["pair_type"],
                        "algorithm": PAIRING_ALGORITHM,
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
            "extraction": asdict(extraction),
            "pairing": {
                "strategy": PAIRING_ALGORITHM,
                "pair_count": len(pairs),
                **pairing_counts,
            },
            "documents": {
                "a": prepared_a.document.model_dump(),
                "b": prepared_b.document.model_dump(),
            },
        },
    )
