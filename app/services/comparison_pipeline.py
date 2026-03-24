from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.extractors import extract_document_result
from app.llm_client import LLMClient, LLMResponseError
from app.schemas import ChangeRow, ComparisonResult, ExtractedDocument, LLMComparisonResponse
from app.services.normalization import normalize_text
from app.services.postprocess import build_reconciliation_payload, deduplicate_rows, merge_reconciled_rows
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
Considera equivalentes las listas con viñetas y el mismo contenido escrito como frase corrida.
Solo clasifica hallazgos con: añadido, eliminado o modificado.
No inventes cambios ni expliques libremente.
Responde exclusivamente con un único objeto JSON válido, sin markdown ni texto adicional.
En source_a y source_b devuelve solo el fragmento mínimo necesario, no repitas bloques completos.
Devuelve JSON estricto con esta forma: {\"changes\":[{\"change_type\":\"añadido|eliminado|modificado\",\"source_a\":\"texto en A o vacío\",\"source_b\":\"texto en B o vacío\",\"summary\":\"resumen breve\",\"confidence\":\"baja|media|alta\",\"severity\":\"baja|media|alta|critica\",\"evidence\":\"frase corta\",\"anchor_a\":0,\"anchor_b\":0}]}
Si no hay cambios, responde {\"changes\":[]}.
No dupliques hallazgos. Sé prudente cuando la evidencia sea débil."""

RECONCILE_SYSTEM_PROMPT = """Fusiona resultados parciales de comparación documental.
Corrige duplicados por solapamiento y conserva solo añadido, eliminado o modificado.
No añadas comentarios narrativos.
Devuelve JSON estricto con la misma forma {\"changes\":[...]}.
Si todos los hallazgos ya son consistentes, devuelve los mismos sin duplicados."""

PAIR_PROMPT_CHAR_LIMIT = 2200


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


def _excerpt_for_prompt(text: str, *, limit: int = PAIR_PROMPT_CHAR_LIMIT) -> tuple[str, bool]:
    clean = (text or "").strip()
    if len(clean) <= limit:
        return clean, False
    head = max(200, limit // 2)
    tail = max(120, limit - head - 32)
    excerpt = f"{clean[:head].rstrip()}\n[…texto truncado…]\n{clean[-tail:].lstrip()}"
    return excerpt, True


def _heuristic_compare_pair(block_a: TextBlock | None, block_b: TextBlock | None) -> tuple[LLMComparisonResponse, str] | None:
    text_a = "" if block_a is None else block_a.text.strip()
    text_b = "" if block_b is None else block_b.text.strip()
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
    if normalize_text(text_a) == normalize_text(text_b):
        return LLMComparisonResponse.model_validate({"changes": []}), "normalized_equal"
    return None


def _llm_fallback_compare_pair(block_a: TextBlock | None, block_b: TextBlock | None) -> LLMComparisonResponse:
    text_a = "" if block_a is None else block_a.text.strip()
    text_b = "" if block_b is None else block_b.text.strip()
    if normalize_text(text_a) == normalize_text(text_b):
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
) -> list[dict[str, str]]:
    text_a, truncated_a = _excerpt_for_prompt("" if block_a is None else block_a.text)
    text_b, truncated_b = _excerpt_for_prompt("" if block_b is None else block_b.text)
    payload = {
        "comparison_rules": {
            "pair_type": pair_type,
            "alignment_score": round(alignment_score, 4),
            "ignore_formatting_noise": True,
            "report_only_real_semantic_changes": True,
        },
        "block_a": {
            "index": None if block_a is None else block_a.index,
            "text": text_a,
            "char_count": 0 if block_a is None else len(block_a.text),
            "truncated": truncated_a,
        },
        "block_b": {
            "index": None if block_b is None else block_b.index,
            "text": text_b,
            "char_count": 0 if block_b is None else len(block_b.text),
            "truncated": truncated_b,
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
        pairs = _pair_blocks(prepared_a.segments, prepared_b.segments)
        failure_threshold = max(0, int(len(pairs) * settings.compare_failed_blocks_error_ratio)) if pairs else 0
        pairing_counts = {
            "matched_pairs": sum(1 for pair in pairs if pair["pair_type"] == "matched"),
            "orphan_a": sum(1 for pair in pairs if pair["pair_type"] == "orphan_a"),
            "orphan_b": sum(1 for pair in pairs if pair["pair_type"] == "orphan_b"),
            "reanchored_pairs": sum(1 for pair in pairs if pair["pair_type"] == "reanchored"),
        }
        rows: list[ChangeRow] = []
        diagnostics_errors: list[dict[str, str]] = []
        failed_blocks = 0
        fallback_blocks = 0
        llm_generated_rows = 0
        fallback_generated_rows = 0
        heuristic_generated_rows = 0
        compared_pairs = 0
        threshold_reached = False
        for index, pair in enumerate(pairs, start=1):
            compared_pairs = index
            block_a = pair["a"]
            block_b = pair["b"]
            pair_id = f"{sid}-{index}"
            heuristic_result = _heuristic_compare_pair(block_a, block_b)
            if heuristic_result is not None:
                llm_response, heuristic_mode = heuristic_result
                row_source = "heuristic"
                if heuristic_mode != "normalized_equal":
                    logger.info("Pareja %s resuelta sin LLM usando heurística=%s", pair_id, heuristic_mode)
            else:
                try:
                    llm_response = client.compare(
                        _comparison_messages(
                            block_a,
                            block_b,
                            pair_type=pair["pair_type"],
                            alignment_score=pair["alignment_score"],
                        )
                    )
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
                        llm_response = _llm_fallback_compare_pair(block_a, block_b)
                        row_source = "fallback"
                    else:
                        failed_blocks += 1
                        diagnostics_errors.append(_build_error_summary(pair_id=pair_id, stage="compare_pair", exc=exc))
                        logger.exception("Error comparando pareja %s", pair_id)
                        if failed_blocks > failure_threshold:
                            threshold_reached = True
                            break
                        continue
            for change in llm_response.changes:
                rows.append(
                    ChangeRow(
                        block_id=len(rows) + 1,
                        pair_id=pair_id,
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
                if row_source == "llm":
                    llm_generated_rows += 1
                elif row_source == "fallback":
                    fallback_generated_rows += 1
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
        valid_rows_for_reconciliation = (
            settings.compare_reconcile_with_llm and len(rows) >= settings.compare_reconcile_min_rows
        )
        reconciled = None
        reconciliation_used = False
        reconciliation_failed = False
        if valid_rows_for_reconciliation:
            try:
                reconciled = client.compare(_reconcile_messages(rows))
                reconciliation_used = True
            except Exception as exc:  # noqa: BLE001
                reconciliation_failed = True
                diagnostics_errors.append(
                    _build_error_summary(pair_id=f"{sid}-reconcile", stage="reconcile", exc=exc)
                )
                logger.exception("Error reconciliando resultado %s", sid)
        final_rows = deduplicate_rows(rows) if not reconciliation_used else merge_reconciled_rows(rows, reconciled)
        failed_ratio = (failed_blocks / len(pairs)) if pairs else 0.0
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
                    "resolved_from_cache": 0,
                    "resolved_by_llm": llm_generated_rows,
                    "resolved_by_fallback": fallback_generated_rows,
                    "resolved_by_heuristic": heuristic_generated_rows,
                    "failed_blocks": failed_blocks,
                    "fallback_blocks": fallback_blocks,
                    "block_size_words": 0,
                    "block_overlap_words": 0,
                    "model_name": client.model_name,
                    "comparison_mode": "llm_semantic_blocks",
                    "reconciliation_mode": "llm" if settings.compare_reconcile_with_llm else "local_only",
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
