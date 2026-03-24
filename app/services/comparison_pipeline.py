from __future__ import annotations

import json
import logging
import re
import unicodedata
from difflib import SequenceMatcher
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.extractors import extract_document_result
from app.llm_client import LLMClient, LLMResponseError
from app.schemas import ChangeRow, ComparisonResult, DiffSegment, ExtractedDocument, LLMComparisonResponse
from app.services.normalization import normalize_text
from app.services.postprocess import deduplicate_rows
from app.services.segmenter import TextBlock, build_blocks
from app.settings import settings

logger = logging.getLogger(__name__)
TOKEN_RE = re.compile(r"\w+", re.UNICODE)
PAIRING_ALGORITHM = "semantic_dp_pairing_v2"
MATCH_REWARD_BASELINE = 0.45
GAP_PENALTY = 0.35
MERGE_PENALTY = 0.18
ANCHOR_SIMILARITY_THRESHOLD = 0.72
SEMANTIC_EQUALITY_THRESHOLD = 0.93
NOISE_EQUIVALENCE_THRESHOLD = 0.84
MIN_MATCH_SIMILARITY = 0.58


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


def _text_similarity(a: str, b: str) -> float:
    left = (a or "").strip().lower()
    right = (b or "").strip().lower()
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return SequenceMatcher(a=left, b=right, autojunk=False).ratio()


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
    semantic = _text_similarity(block_a.text, block_b.text)
    length_score = _relative_length_score(block_a.text, block_b.text)
    position_score = _relative_position_score(block_a.index, total_a, block_b.index, total_b)
    return (overlap * 0.45) + (semantic * 0.35) + (length_score * 0.10) + (position_score * 0.10)


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


def _merge_blocks_text(blocks: list[TextBlock]) -> str:
    return " ".join(block.text.strip() for block in blocks if block.text.strip()).strip()


def _pair_blocks(blocks_a: list[TextBlock], blocks_b: list[TextBlock]) -> list[dict[str, Any]]:
    total_a = len(blocks_a)
    total_b = len(blocks_b)
    dp = [[0.0] * (total_b + 1) for _ in range(total_a + 1)]
    direction = [[("start", 0)] * (total_b + 1) for _ in range(total_a + 1)]

    for index_a in range(1, total_a + 1):
        dp[index_a][0] = dp[index_a - 1][0] - GAP_PENALTY
        direction[index_a][0] = ("up", 1)
    for index_b in range(1, total_b + 1):
        dp[0][index_b] = dp[0][index_b - 1] - GAP_PENALTY
        direction[0][index_b] = ("left", 1)

    for index_a in range(1, total_a + 1):
        for index_b in range(1, total_b + 1):
            block_a = blocks_a[index_a - 1]
            block_b = blocks_b[index_b - 1]
            similarity = _block_similarity(block_a, block_b, total_a, total_b)
            diagonal_reward = ((similarity * 2) - 1 - MATCH_REWARD_BASELINE)
            if similarity < MIN_MATCH_SIMILARITY:
                diagonal_reward -= 0.45
            diagonal = dp[index_a - 1][index_b - 1] + diagonal_reward
            up = dp[index_a - 1][index_b] - GAP_PENALTY
            left = dp[index_a][index_b - 1] - GAP_PENALTY
            best_score = diagonal
            best_move = ("diag", 1)
            if up > best_score:
                best_score = up
                best_move = ("up", 1)
            if left > best_score:
                best_score = left
                best_move = ("left", 1)
            if index_a >= 2:
                single_prev_similarity = _block_similarity(blocks_a[index_a - 2], block_b, total_a, total_b)
                single_curr_similarity = _block_similarity(block_a, block_b, total_a, total_b)
                merged_a_similarity = _text_similarity(
                    f"{blocks_a[index_a - 2].text} {blocks_a[index_a - 1].text}",
                    block_b.text,
                )
                if merged_a_similarity >= max(single_prev_similarity, single_curr_similarity) + 0.08:
                    merge_up = dp[index_a - 2][index_b - 1] + ((merged_a_similarity * 2) - 1 - MERGE_PENALTY)
                    if merge_up > best_score:
                        best_score = merge_up
                        best_move = ("diag_up2", 2)
            if index_b >= 2:
                single_prev_similarity = _block_similarity(block_a, blocks_b[index_b - 2], total_a, total_b)
                single_curr_similarity = _block_similarity(block_a, block_b, total_a, total_b)
                merged_b_similarity = _text_similarity(
                    block_a.text,
                    f"{blocks_b[index_b - 2].text} {blocks_b[index_b - 1].text}",
                )
                if merged_b_similarity >= max(single_prev_similarity, single_curr_similarity) + 0.08:
                    merge_left = dp[index_a - 1][index_b - 2] + ((merged_b_similarity * 2) - 1 - MERGE_PENALTY)
                    if merge_left > best_score:
                        best_score = merge_left
                        best_move = ("diag_left2", 2)
            dp[index_a][index_b] = best_score
            direction[index_a][index_b] = best_move

    raw_pairs: list[dict[str, Any]] = []
    index_a = total_a
    index_b = total_b
    while index_a > 0 or index_b > 0:
        move, width = direction[index_a][index_b]
        if move == "diag" and index_a > 0 and index_b > 0:
            block_a = blocks_a[index_a - 1]
            block_b = blocks_b[index_b - 1]
            raw_pairs.append(
                {
                    "a": block_a,
                    "b": block_b,
                    "a_blocks": [block_a],
                    "b_blocks": [block_b],
                    "alignment_score": _block_similarity(block_a, block_b, total_a, total_b),
                    "match_reason": "1:1",
                }
            )
            index_a -= 1
            index_b -= 1
            continue
        if move == "diag_up2" and index_a >= 2 and index_b > 0:
            selected_a = [blocks_a[index_a - 2], blocks_a[index_a - 1]]
            block_b = blocks_b[index_b - 1]
            raw_pairs.append(
                {
                    "a": selected_a[-1],
                    "b": block_b,
                    "a_blocks": selected_a,
                    "b_blocks": [block_b],
                    "alignment_score": _text_similarity(_merge_blocks_text(selected_a), block_b.text),
                    "match_reason": "2:1_merge_a",
                }
            )
            index_a -= 2
            index_b -= 1
            continue
        if move == "diag_left2" and index_a > 0 and index_b >= 2:
            block_a = blocks_a[index_a - 1]
            selected_b = [blocks_b[index_b - 2], blocks_b[index_b - 1]]
            raw_pairs.append(
                {
                    "a": block_a,
                    "b": selected_b[-1],
                    "a_blocks": [block_a],
                    "b_blocks": selected_b,
                    "alignment_score": _text_similarity(block_a.text, _merge_blocks_text(selected_b)),
                    "match_reason": "1:2_merge_b",
                }
            )
            index_a -= 1
            index_b -= 2
            continue
        if (move == "up" and index_a > 0) or index_b == 0:
            raw_pairs.append(
                {
                    "a": blocks_a[index_a - 1],
                    "b": None,
                    "a_blocks": [blocks_a[index_a - 1]],
                    "b_blocks": [],
                    "alignment_score": 0.0,
                    "match_reason": "gap_b",
                }
            )
            index_a -= 1
            continue
        raw_pairs.append(
            {
                "a": None,
                "b": blocks_b[index_b - 1],
                "a_blocks": [],
                "b_blocks": [blocks_b[index_b - 1]],
                "alignment_score": 0.0,
                "match_reason": "gap_a",
            }
        )
        index_b -= 1

    pairs = list(reversed(raw_pairs))
    for index in range(len(pairs) - 1):
        current = pairs[index]
        nxt = pairs[index + 1]
        if (
            current["a"] is None
            and current["b"] is not None
            and nxt["a"] is not None
            and nxt["b"] is not None
            and normalize_text(current["b"].text) == normalize_text(nxt["b"].text)
        ):
            pairs[index], pairs[index + 1] = pairs[index + 1], pairs[index]
    last_matched_a: int | None = None
    last_matched_b: int | None = None
    for pair in pairs:
        block_a = pair["a"]
        block_b = pair["b"]
        if block_a is None:
            pair["pair_type"] = "orphan_b"
            pair["reanchored"] = False
            pair["match_confidence"] = "alta"
            continue
        if block_b is None:
            pair["pair_type"] = "orphan_a"
            pair["reanchored"] = False
            pair["match_confidence"] = "alta"
            continue
        reanchored = False
        if last_matched_a is None or last_matched_b is None:
            reanchored = block_a.index != 0 or block_b.index != 0
        else:
            reanchored = (block_a.index != last_matched_a + 1) or (block_b.index != last_matched_b + 1)
        pair["pair_type"] = "reanchored" if reanchored else "matched"
        pair["reanchored"] = reanchored
        pair["match_confidence"] = "alta" if pair["alignment_score"] >= ANCHOR_SIMILARITY_THRESHOLD else "media"
        last_matched_a = block_a.index
        last_matched_b = block_b.index
    return pairs


COMPARISON_SYSTEM_PROMPT = """Eres un comparador documental semántico.
Debes ignorar formato, maquetación, encabezados/pies repetidos, numeraciones y ruido OCR.
Considera equivalentes las listas con viñetas y el mismo contenido escrito como frase corrida.
Solo clasifica hallazgos con: añadido, eliminado o modificado.
No inventes cambios ni expliques libremente.
Responde exclusivamente con un único objeto JSON válido, sin markdown ni texto adicional.
En source_a y source_b devuelve solo el fragmento mínimo necesario, no repitas bloques completos.
Devuelve JSON estricto con esta forma: {\"changes\":[{\"change_type\":\"añadido|eliminado|modificado\",\"source_a\":\"texto en A o vacío\",\"source_b\":\"texto en B o vacío\",\"summary\":\"resumen breve\",\"confidence\":\"baja|media|alta\",\"severity\":\"baja|media|alta|critica\",\"evidence\":\"frase corta\",\"anchor_a\":0,\"anchor_b\":0}]}
Si no hay cambios, responde {\"changes\":[]}.
No dupliques hallazgos. Sé prudente cuando la evidencia sea débil."""

def _max_pair_chars() -> int:
    available_tokens = settings.context_window_tokens - settings.llm_max_tokens - 600
    return max(500, int(available_tokens * 4) // 2)


def _build_fixed_chunks(text: str, chunk_chars: int) -> list[TextBlock]:
    clean = (text or "").strip()
    if not clean:
        return []
    step = max(1, int(chunk_chars))
    chunks: list[TextBlock] = []
    for idx, start in enumerate(range(0, len(clean), step)):
        piece = clean[start : start + step]
        chunks.append(
            TextBlock(
                index=idx,
                text=piece,
                start_char=start,
                end_char=start + len(piece),
            )
        )
    return chunks


def _pair_fixed_chunks(chunks_a: list[TextBlock], chunks_b: list[TextBlock]) -> list[dict[str, Any]]:
    pair_count = max(len(chunks_a), len(chunks_b))
    pairs: list[dict[str, Any]] = []
    for idx in range(pair_count):
        block_a = chunks_a[idx] if idx < len(chunks_a) else None
        block_b = chunks_b[idx] if idx < len(chunks_b) else None
        if block_a is None:
            pair_type = "orphan_b"
            match_reason = "index_gap_a"
            alignment_score = 0.0
        elif block_b is None:
            pair_type = "orphan_a"
            match_reason = "index_gap_b"
            alignment_score = 0.0
        else:
            pair_type = "matched"
            match_reason = "index_1:1"
            alignment_score = _text_similarity(block_a.text, block_b.text)
        pairs.append(
            {
                "a": block_a,
                "b": block_b,
                "a_blocks": [] if block_a is None else [block_a],
                "b_blocks": [] if block_b is None else [block_b],
                "alignment_score": alignment_score,
                "match_reason": match_reason,
                "pair_type": pair_type,
                "reanchored": False,
                "match_confidence": "alta" if alignment_score >= ANCHOR_SIMILARITY_THRESHOLD else "media",
            }
        )
    return pairs


def _safe_error_message(exc: Exception) -> str:
    message = str(exc).strip()
    return message or exc.__class__.__name__


def _build_error_summary(*, pair_id: str, stage: str, exc: Exception) -> dict[str, str]:
    return {
        "pair_id": pair_id,
        "stage": stage,
        "error_type": exc.__class__.__name__,
        "message": _safe_error_message(exc),
    }


def _resolve_result_status(*, failed_blocks: int, total_pairs: int, fallback_blocks: int = 0) -> str:
    if total_pairs <= 0:
        return "done"
    failed_ratio = failed_blocks / total_pairs
    if failed_blocks <= 0 and fallback_blocks <= 0:
        return "done"
    if failed_ratio >= settings.compare_failed_blocks_error_ratio:
        return "error"
    return "done_with_warnings"


def _excerpt_for_prompt(text: str, *, limit: int | None = None) -> tuple[str, bool]:
    effective_limit = limit if limit is not None else _max_pair_chars()
    clean = (text or "").strip()
    if len(clean) <= effective_limit:
        return clean, False
    half = effective_limit // 2
    head = max(1, half - 16)
    tail = max(1, effective_limit - head - 32)
    excerpt = f"{clean[:head].rstrip()}\n[...texto truncado...]\n{clean[-tail:].lstrip()}"
    return excerpt, True


def _build_display_segments(text_a: str, text_b: str) -> tuple[list[DiffSegment], list[DiffSegment]]:
    a = (text_a or "").strip()
    b = (text_b or "").strip()
    if not a and not b:
        return [], []
    if not a:
        return [], [DiffSegment(type="insert", text=b)]
    if not b:
        return [DiffSegment(type="delete", text=a)], []
    words_a = re.split(r"(\s+)", a)
    words_b = re.split(r"(\s+)", b)
    sm = SequenceMatcher(a=words_a, b=words_b, autojunk=False)
    segs_a: list[DiffSegment] = []
    segs_b: list[DiffSegment] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        chunk_a = "".join(words_a[i1:i2])
        chunk_b = "".join(words_b[j1:j2])
        if tag == "equal":
            if chunk_a:
                segs_a.append(DiffSegment(type="equal", text=chunk_a))
            if chunk_b:
                segs_b.append(DiffSegment(type="equal", text=chunk_b))
        elif tag == "replace":
            if chunk_a:
                segs_a.append(DiffSegment(type="delete", text=chunk_a))
            if chunk_b:
                segs_b.append(DiffSegment(type="insert", text=chunk_b))
        elif tag == "delete":
            if chunk_a:
                segs_a.append(DiffSegment(type="delete", text=chunk_a))
        elif tag == "insert":
            if chunk_b:
                segs_b.append(DiffSegment(type="insert", text=chunk_b))
    return segs_a, segs_b


def _pair_text(blocks: list[TextBlock]) -> str:
    return " ".join(block.text.strip() for block in blocks if block.text.strip()).strip()


def _aggressive_canonical_text(text: str) -> str:
    raw = unicodedata.normalize("NFKD", (text or "").lower())
    no_diacritics = "".join(char for char in raw if not unicodedata.combining(char))
    letters_and_numbers = re.sub(r"[^\w\s]", " ", no_diacritics)
    return re.sub(r"\s+", " ", letters_and_numbers).strip()


def _is_noise_equivalent(text_a: str, text_b: str) -> bool:
    normalized_a = normalize_text(text_a)
    normalized_b = normalize_text(text_b)
    if normalized_a == normalized_b:
        return True
    canonical_a = _aggressive_canonical_text(normalized_a)
    canonical_b = _aggressive_canonical_text(normalized_b)
    if not canonical_a or not canonical_b:
        return not canonical_a and not canonical_b
    if canonical_a == canonical_b:
        return True
    lexical_overlap = _token_overlap_score(canonical_a, canonical_b)
    semantic_similarity = _text_similarity(canonical_a, canonical_b)
    length_score = _relative_length_score(canonical_a, canonical_b)
    return (
        lexical_overlap >= NOISE_EQUIVALENCE_THRESHOLD
        and semantic_similarity >= SEMANTIC_EQUALITY_THRESHOLD
        and length_score >= 0.96
    )


def _heuristic_compare_pair(
    block_a: TextBlock | None,
    block_b: TextBlock | None,
    *,
    text_a: str | None = None,
    text_b: str | None = None,
) -> tuple[LLMComparisonResponse, str] | None:
    text_a = (text_a if text_a is not None else ("" if block_a is None else block_a.text)).strip()
    text_b = (text_b if text_b is not None else ("" if block_b is None else block_b.text)).strip()
    if not text_a and not text_b:
        return LLMComparisonResponse.model_validate({"changes": []}), "empty_pair"
    if block_a is None and text_b:
        return (
            LLMComparisonResponse.model_validate(
                {
                    "changes": [
                        {
                            "change_type": "añadido",
                            "source_a": "",
                            "source_b": text_b,
                            "summary": "Bloque presente solo en el documento B.",
                            "confidence": "alta",
                            "severity": "media",
                            "evidence": "Fallback heurístico: bloque huérfano en B.",
                            "anchor_a": None,
                            "anchor_b": block_b.index if block_b is not None else None,
                        }
                    ]
                }
            ),
            "orphan_b",
        )
    if block_b is None and text_a:
        return (
            LLMComparisonResponse.model_validate(
                {
                    "changes": [
                        {
                            "change_type": "eliminado",
                            "source_a": text_a,
                            "source_b": "",
                            "summary": "Bloque presente solo en el documento A.",
                            "confidence": "alta",
                            "severity": "media",
                            "evidence": "Fallback heurístico: bloque huérfano en A.",
                            "anchor_a": block_a.index if block_a is not None else None,
                            "anchor_b": None,
                        }
                    ]
                }
            ),
            "orphan_a",
        )
    if _is_noise_equivalent(text_a, text_b):
        return LLMComparisonResponse.model_validate({"changes": []}), "normalized_equal"
    return None


def _llm_fallback_compare_pair(
    block_a: TextBlock | None,
    block_b: TextBlock | None,
    *,
    text_a: str | None = None,
    text_b: str | None = None,
) -> LLMComparisonResponse:
    text_a = (text_a if text_a is not None else ("" if block_a is None else block_a.text)).strip()
    text_b = (text_b if text_b is not None else ("" if block_b is None else block_b.text)).strip()
    if _is_noise_equivalent(text_a, text_b):
        return LLMComparisonResponse.model_validate({"changes": []})
    change_type = "modificado"
    if not text_a and text_b:
        change_type = "añadido"
    elif text_a and not text_b:
        change_type = "eliminado"
    return LLMComparisonResponse.model_validate(
        {
            "changes": [
                {
                    "change_type": change_type,
                    "source_a": text_a,
                    "source_b": text_b,
                    "summary": "Cambio detectado mediante fallback local al no obtener JSON válido del LLM.",
                    "confidence": "baja",
                    "severity": "media",
                    "evidence": "Fallback local por respuesta vacía o no estructurada del modelo.",
                    "anchor_a": None if block_a is None else block_a.index,
                    "anchor_b": None if block_b is None else block_b.index,
                }
            ]
        }
    )


def _comparison_messages(
    block_a: TextBlock | None,
    block_b: TextBlock | None,
    *,
    pair_type: str,
    alignment_score: float,
    text_a: str | None = None,
    text_b: str | None = None,
    indices_a: list[int] | None = None,
    indices_b: list[int] | None = None,
) -> list[dict[str, str]]:
    effective_text_a = text_a if text_a is not None else ("" if block_a is None else block_a.text)
    effective_text_b = text_b if text_b is not None else ("" if block_b is None else block_b.text)
    text_a, truncated_a = _excerpt_for_prompt(effective_text_a)
    text_b, truncated_b = _excerpt_for_prompt(effective_text_b)
    payload = {
        "comparison_rules": {
            "pair_type": pair_type,
            "alignment_score": round(alignment_score, 4),
            "ignore_formatting_noise": True,
            "report_only_real_semantic_changes": True,
        },
        "block_a": {
            "index": None if block_a is None else block_a.index,
            "indices": indices_a or ([] if block_a is None else [block_a.index]),
            "text": text_a,
            "char_count": len(effective_text_a),
            "truncated": truncated_a,
        },
        "block_b": {
            "index": None if block_b is None else block_b.index,
            "indices": indices_b or ([] if block_b is None else [block_b.index]),
            "text": text_b,
            "char_count": len(effective_text_b),
            "truncated": truncated_b,
        },
    }
    return [
        {"role": "system", "content": COMPARISON_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _rows_to_payload(rows: list[ChangeRow]) -> list[dict[str, Any]]:
    return [row.model_dump(mode="json") for row in rows]


def _persist_runtime_snapshot(
    *,
    sid: str,
    rows: list[ChangeRow],
    compared_pairs: int,
    total_pairs: int,
    fallback_blocks: int,
    failed_blocks: int,
    partial_result: bool = False,
    status: str = "running",
    step: str = "comparando",
    detail: str = "Comparando bloques",
) -> None:
    try:
        from app.services.queue import persist_job_result, update_job_state
        visible_rows = deduplicate_rows(rows)
        percent = 20 if total_pairs <= 0 else min(99, 20 + int((compared_pairs / total_pairs) * 75))
        payload = {
            "sid": sid,
            "status": status,
            "ok": status != "error",
            "error": None,
            "rows": _rows_to_payload(visible_rows),
            "progress": {
                "percent": percent if status == "running" else 100,
                "step": step,
                "detail": detail,
                "completed_pairs": compared_pairs,
                "total_pairs": total_pairs,
                "failed_blocks": failed_blocks,
                "fallback_blocks": fallback_blocks,
            },
            "meta": {
                "partial_result": partial_result,
                "pagination": {
                    "offset": 0,
                    "limit": None,
                    "returned": len(visible_rows),
                    "total": len(visible_rows),
                    "has_more": False,
                    "next_offset": None,
                    "truncated": False,
                },
                "cache": {
                    "policy": "incremental_result_file",
                    "resolved_from_cache": 0,
                    "failed_blocks": failed_blocks,
                    "fallback_blocks": fallback_blocks,
                },
            },
        }
        persist_job_result(sid, payload)
        update_job_state(
            sid,
            status=status,
            percent=payload["progress"]["percent"],
            step=step,
            detail=detail,
            partial_result=partial_result,
            failed_blocks=failed_blocks,
            total_pairs=total_pairs,
            completed_pairs=compared_pairs,
            fallback_blocks=fallback_blocks,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo persistir snapshot intermedio %s: %s", sid, exc)


def compare_documents(
    path_a: str | Path,
    path_b: str | Path,
    sid: str,
    llm_client: LLMClient | None = None,
    *,
    extraction: ExtractionOptions | None = None,
) -> ComparisonResult:
    owns_client = llm_client is None
    client = llm_client or LLMClient()
    extraction = extraction or ExtractionOptions()
    try:
        prepared_a = prepare_document(path_a, extraction=extraction)
        prepared_b = prepare_document(path_b, extraction=extraction)
        blocks_a = prepared_a.segments or _build_fixed_chunks(prepared_a.document.clean_text, settings.compare_pair_chars)
        blocks_b = prepared_b.segments or _build_fixed_chunks(prepared_b.document.clean_text, settings.compare_pair_chars)
        pairs = _pair_blocks(blocks_a, blocks_b)
        failure_threshold = max(0, int(len(pairs) * settings.compare_failed_blocks_error_ratio)) if pairs else 0
        pairing_counts = {
            "matched_pairs": sum(1 for pair in pairs if pair["pair_type"] == "matched"),
            "orphan_a": sum(1 for pair in pairs if pair["pair_type"] == "orphan_a"),
            "orphan_b": sum(1 for pair in pairs if pair["pair_type"] == "orphan_b"),
            "reanchored_pairs": sum(1 for pair in pairs if pair["pair_type"] == "reanchored"),
        }
        rows: list[ChangeRow] = []
        diagnostics_errors: list[dict[str, str]] = []
        pairing_debug: list[dict[str, Any]] = []
        failed_blocks = 0
        fallback_blocks = 0
        cache_hits = 0
        llm_pair_cache: dict[str, LLMComparisonResponse] = {}
        llm_generated_rows = 0
        fallback_generated_rows = 0
        heuristic_generated_rows = 0
        compared_pairs = 0
        threshold_reached = False
        aligned_chars_a = 0
        aligned_chars_b = 0
        for index, pair in enumerate(pairs, start=1):
            compared_pairs = index
            block_a = pair["a"]
            block_b = pair["b"]
            pair_blocks_a = pair.get("a_blocks") or ([] if block_a is None else [block_a])
            pair_blocks_b = pair.get("b_blocks") or ([] if block_b is None else [block_b])
            pair_text_a = _pair_text(pair_blocks_a)
            pair_text_b = _pair_text(pair_blocks_b)
            indices_a = [block.index for block in pair_blocks_a]
            indices_b = [block.index for block in pair_blocks_b]
            pair_id = f"{sid}-{index}"
            heuristic_result = _heuristic_compare_pair(block_a, block_b, text_a=pair_text_a, text_b=pair_text_b)
            if heuristic_result is not None:
                llm_response, heuristic_mode = heuristic_result
                row_source = "heuristic"
                if heuristic_mode != "normalized_equal":
                    logger.info("Pareja %s resuelta sin LLM usando heurística=%s", pair_id, heuristic_mode)
            else:
                cache_key = f"{normalize_text(pair_text_a)}||{normalize_text(pair_text_b)}"
                cached = llm_pair_cache.get(cache_key)
                if cached is not None:
                    llm_response = cached
                    row_source = "cache"
                    cache_hits += 1
                else:
                    try:
                        llm_response = client.compare(
                            _comparison_messages(
                                block_a,
                                block_b,
                                pair_type=pair["pair_type"],
                                alignment_score=pair["alignment_score"],
                                text_a=pair_text_a,
                                text_b=pair_text_b,
                                indices_a=indices_a,
                                indices_b=indices_b,
                            )
                        )
                        llm_pair_cache[cache_key] = llm_response
                        row_source = "llm"
                    except Exception as exc:  # noqa: BLE001
                        if isinstance(exc, LLMResponseError):
                            fallback_blocks += 1
                            diagnostics_errors.append(
                                _build_error_summary(pair_id=pair_id, stage="compare_pair_fallback", exc=exc)
                            )
                            logger.warning(
                                "Usando fallback local para pareja %s tras fallo del LLM: %s",
                                pair_id,
                                exc,
                            )
                            llm_response = _llm_fallback_compare_pair(
                                block_a,
                                block_b,
                                text_a=pair_text_a,
                                text_b=pair_text_b,
                            )
                            llm_pair_cache[cache_key] = llm_response
                            row_source = "fallback"
                        else:
                            failed_blocks += 1
                            diagnostics_errors.append(_build_error_summary(pair_id=pair_id, stage="compare_pair", exc=exc))
                            logger.exception("Error comparando pareja %s", pair_id)
                            if failed_blocks > failure_threshold:
                                threshold_reached = True
                                break
                            continue
            if block_a is not None and block_b is not None:
                aligned_chars_a += sum(len(block.text) for block in pair_blocks_a)
                aligned_chars_b += sum(len(block.text) for block in pair_blocks_b)
            pairing_debug.append(
                {
                    "pair_id": pair_id,
                    "pair_type": pair["pair_type"],
                    "match_reason": pair.get("match_reason", "1:1"),
                    "alignment_score": round(pair["alignment_score"], 4),
                    "indices_a": indices_a,
                    "indices_b": indices_b,
                    "row_source": row_source,
                }
            )
            for change in llm_response.changes:
                segs_a, segs_b = _build_display_segments(pair_text_a, pair_text_b)
                _is_llm = row_source == "llm"
                _is_cache = row_source == "cache"
                _is_fallback = row_source == "fallback"
                rows.append(
                    ChangeRow(
                        block_id=len(rows) + 1,
                        pair_id=pair_id,
                        text_a=change.source_a,
                        text_b=change.source_b,
                        display_text_a=pair_text_a,
                        display_text_b=pair_text_b,
                        display_segments_a=segs_a,
                        display_segments_b=segs_b,
                        change_type=change.change_type,
                        confidence=change.confidence,
                        severity=change.severity,
                        summary=change.summary,
                        llm_comment=change.evidence,
                        result_origin=row_source,
                        llm_success=_is_llm or _is_cache,
                        cache_hit=_is_cache,
                        fallback_applied=_is_fallback,
                        decision_source=row_source,
                        model_name=settings.llm_model if (_is_llm or _is_cache) else "local",
                        chunk_index_a=-1 if block_a is None else min(indices_a),
                        chunk_index_b=-1 if block_b is None else min(indices_b),
                        offset_start_a=0 if not pair_blocks_a else min(block.start_char for block in pair_blocks_a),
                        offset_end_a=0 if not pair_blocks_a else max(block.end_char for block in pair_blocks_a),
                        offset_start_b=0 if not pair_blocks_b else min(block.start_char for block in pair_blocks_b),
                        offset_end_b=0 if not pair_blocks_b else max(block.end_char for block in pair_blocks_b),
                        pairing={
                            "alignment_score": pair["alignment_score"],
                            "strategy": pair["pair_type"],
                            "pair_type": pair["pair_type"],
                            "algorithm": PAIRING_ALGORITHM,
                            "reanchored": pair["reanchored"],
                            "match_reason": pair.get("match_reason", "1:1"),
                            "matched_indices_a": indices_a,
                            "matched_indices_b": indices_b,
                        },
                        source_spans={
                            "a": (
                                None
                                if not pair_blocks_a
                                else {
                                    "start": min(block.start_char for block in pair_blocks_a),
                                    "end": max(block.end_char for block in pair_blocks_a),
                                }
                            ),
                            "b": (
                                None
                                if not pair_blocks_b
                                else {
                                    "start": min(block.start_char for block in pair_blocks_b),
                                    "end": max(block.end_char for block in pair_blocks_b),
                                }
                            ),
                        },
                    )
                )
                if row_source == "llm":
                    llm_generated_rows += 1
                elif row_source == "fallback":
                    fallback_generated_rows += 1
                elif row_source == "cache":
                    llm_generated_rows += 1
                else:
                    heuristic_generated_rows += 1
            if (
                settings.compare_partial_persist_every_pairs > 0
                and (index % max(1, settings.compare_partial_persist_every_pairs) == 0 or index == len(pairs))
            ):
                _persist_runtime_snapshot(
                    sid=sid,
                    rows=rows,
                    compared_pairs=index,
                    total_pairs=len(pairs),
                    fallback_blocks=fallback_blocks,
                    failed_blocks=failed_blocks,
                    partial_result=fallback_blocks > 0 or failed_blocks > 0,
                    status="running",
                    step="comparando",
                    detail="Comparando bloques y guardando resultados parciales",
                )
        valid_rows_for_reconciliation = False
        reconciliation_used = False
        reconciliation_failed = False
        final_rows = deduplicate_rows(rows)
        deduplicated_count = max(0, len(rows) - len(final_rows))
        failed_ratio = (failed_blocks / len(pairs)) if pairs else 0.0
        coverage_a = 0.0 if not prepared_a.document.clean_text else min(1.0, aligned_chars_a / len(prepared_a.document.clean_text))
        coverage_b = 0.0 if not prepared_b.document.clean_text else min(1.0, aligned_chars_b / len(prepared_b.document.clean_text))
        partial_result = failed_blocks > 0 or fallback_blocks > 0 or threshold_reached or reconciliation_failed
        status = _resolve_result_status(
            failed_blocks=failed_blocks,
            total_pairs=len(pairs),
            fallback_blocks=fallback_blocks,
        )
        error_summary = {
            "failed_blocks": failed_blocks,
            "fallback_blocks": fallback_blocks,
            "total_pairs": len(pairs),
            "failed_ratio": failed_ratio,
            "threshold": failure_threshold,
            "threshold_reached": threshold_reached,
            "reconciliation_failed": reconciliation_failed,
            "reconciliation_used": reconciliation_used,
            "reconciliation_skipped": not valid_rows_for_reconciliation,
            "reconciliation_disabled": not settings.compare_reconcile_with_llm,
            "valid_rows_before_reconciliation": len(rows),
            "errors": diagnostics_errors,
        }
        warning_details: list[str] = []
        if failed_blocks > 0:
            warning_details.append(f"{failed_blocks} de {len(pairs)} bloques fallidos")
        if fallback_blocks > 0:
            warning_details.append(f"{fallback_blocks} bloques resueltos con fallback local")
        if reconciliation_failed:
            warning_details.append("reconciliación final no disponible")
        return ComparisonResult(
            sid=sid,
            status=status,
            progress={
                "percent": 100,
                "step": "completado" if status != "error" else "completado_con_errores",
                "detail": (
                    "Comparación finalizada"
                    if not partial_result
                    else "Comparación finalizada con resultado parcial"
                ),
                "completed_pairs": compared_pairs,
                "total_pairs": len(pairs),
                "failed_blocks": failed_blocks,
                "fallback_blocks": fallback_blocks,
            },
            rows=final_rows,
            ok=status != "error",
            error=(
                None
                if status == "done"
                else f"Resultado parcial: {'; '.join(warning_details) if warning_details else 'se detectaron advertencias'}"
            ),
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
                    "policy": "incremental_result_file",
                    "resolved_from_cache": cache_hits,
                    "resolved_by_llm": llm_generated_rows,
                    "resolved_by_fallback": fallback_generated_rows,
                    "resolved_by_heuristic": heuristic_generated_rows,
                    "failed_blocks": failed_blocks,
                    "fallback_blocks": fallback_blocks,
                    "block_size_words": 0,
                    "block_overlap_words": 0,
                    "model_name": client.model_name,
                    "comparison_mode": "llm_semantic_block_pairs",
                    "reconciliation_mode": "disabled",
                },
                "segmentation": {
                    "target_chars": settings.block_target_chars,
                    "overlap_chars": settings.block_overlap_chars,
                    "pair_chars_fallback": settings.compare_pair_chars,
                    "doc_a_blocks": len(blocks_a),
                    "doc_b_blocks": len(blocks_b),
                },
                "extraction": asdict(extraction),
                "pairing": {
                    "strategy": PAIRING_ALGORITHM,
                    "pair_count": len(pairs),
                    **pairing_counts,
                },
                "diagnostics": {
                    "failed_blocks": failed_blocks,
                    "fallback_blocks": fallback_blocks,
                    "compared_pairs": compared_pairs,
                    "total_pairs": len(pairs),
                    "failed_ratio": failed_ratio,
                    "partial_result": partial_result,
                    "threshold": failure_threshold,
                    "threshold_reached": threshold_reached,
                    "reconciliation_attempted": valid_rows_for_reconciliation,
                    "reconciliation_disabled": not settings.compare_reconcile_with_llm,
                    "reconciliation_failed": reconciliation_failed,
                    "reconciliation_used": reconciliation_used,
                    "alignment_coverage_ratio_a": round(coverage_a, 4),
                    "alignment_coverage_ratio_b": round(coverage_b, 4),
                    "orphan_pairs": pairing_counts["orphan_a"] + pairing_counts["orphan_b"],
                    "deduplicated_rows": deduplicated_count,
                    "pairing_debug": pairing_debug,
                    "errors": diagnostics_errors,
                },
                "partial_result": partial_result,
                "error_summary": error_summary,
                "documents": {
                    "a": prepared_a.document.model_dump(),
                    "b": prepared_b.document.model_dump(),
                },
            },
        )
    finally:
        if owns_client and hasattr(client, "close"):
            client.close()
