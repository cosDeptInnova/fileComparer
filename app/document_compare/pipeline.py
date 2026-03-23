from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..extractors import extract_document_result
from ..llm_client import CompareInferenceAborted, LLMClient
from ..metrics import observe_compare_phase_duration, record_cache_hit
from ..pair_cache import ComparePairCache
from ..text_segments import normalize_text_segments
from .alignment import AlignmentMatch, SemanticBlock, align_blocks, build_semantic_blocks, rescue_orphans_with_context
from .normalization import canonicalize_for_comparison, normalize_text, normalize_structural_markers, paragraph_blocks, strip_layout_metadata
from .structured_output import (
    COMPARE_JSON_SCHEMA,
    GLOBAL_REVIEW_JSON_SCHEMA,
    GLOBAL_REVIEW_PROMPT_VERSION,
    GLOBAL_TABLE_REVIEW_PROMPT_VERSION,
    LOCAL_COMPARE_PROMPT_VERSION,
    build_compare_messages,
    build_global_review_messages,
    build_global_table_review_messages,
)

PIPELINE_VERSION = "compare-pipeline-v6"
LOGGER = logging.getLogger(__name__)
AI_ONLY_COMPARE_ERROR = "compare_ai_only_requires_llm"


@dataclass(slots=True)
class CompareArtifacts:
    text_a: str
    text_b: str
    normalized_a: str
    normalized_b: str
    extraction_a: dict[str, Any]
    extraction_b: dict[str, Any]
    alignments: list[AlignmentMatch]


class CompareDocumentsService:
    def __init__(
        self,
        *,
        llm_client: LLMClient | None = None,
        pair_cache: ComparePairCache | None = None,
        should_abort: Callable[[], bool] | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.pair_cache = pair_cache or ComparePairCache()
        self.should_abort = should_abort
        self.semantic_block_words = max(80, int(os.getenv("COMPARE_SEMANTIC_BLOCK_WORDS", "180")))
        self.max_prompt_chars_per_side = max(200, int(os.getenv("COMPARE_LLM_MAX_CHARS_PER_SIDE", "1800")))
        self.ambiguous_prompt_chars_per_side = max(
            self.max_prompt_chars_per_side,
            int(os.getenv("COMPARE_LLM_MAX_AMBIGUOUS_CHARS_PER_SIDE", "3200")),
        )
        self.global_review_window_rows = max(2, int(os.getenv("COMPARE_GLOBAL_REVIEW_WINDOW_ROWS", "6")))
        self.global_review_window_overlap = max(1, int(os.getenv("COMPARE_GLOBAL_REVIEW_WINDOW_OVERLAP", "2")))
        self.global_review_max_chars_per_side = max(
            200,
            int(os.getenv("COMPARE_GLOBAL_REVIEW_MAX_CHARS_PER_SIDE", "700")),
        )
        self.final_review_max_rows = max(2, int(os.getenv("COMPARE_FINAL_REVIEW_MAX_ROWS", "80")))
        self.final_review_max_chars_per_side = max(
            160,
            int(os.getenv("COMPARE_FINAL_REVIEW_MAX_CHARS_PER_SIDE", "420")),
        )
        self._current_blocks_a: list[SemanticBlock] = []
        self._current_blocks_b: list[SemanticBlock] = []

    def _raise_if_aborted(self, *, stage: str) -> None:
        if callable(self.should_abort) and self.should_abort():
            LOGGER.info("Comparador: abortando pipeline en stage=%s por job terminal", stage)
            raise CompareInferenceAborted(f"compare_pipeline_aborted:{stage}")

    def _require_llm_client(self) -> LLMClient:
        if self.llm_client is None:
            raise RuntimeError(AI_ONLY_COMPARE_ERROR)
        return self.llm_client

    def compare_documents(
        self,
        *,
        file_a_path: str,
        file_b_path: str,
        file_a_name: str | None = None,
        file_b_name: str | None = None,
        opts: dict[str, Any] | None = None,
        progress_cb: Callable[[int, str, str], None] | None = None,
    ) -> dict[str, Any]:
        opts = dict(opts or {})
        progress = progress_cb or (lambda percent, step, detail: None)
        self._raise_if_aborted(stage="before_extraction")
        timings: dict[str, float] = {}
        counts = {
            "pairs_total": 0,
            "pairs_equal": 0,
            "pairs_orphan": 0,
            "pairs_sent_to_llm": 0,
            "pairs_cache_hit": 0,
            "pairs_cache_miss": 0,
            "duplicate_pairs_skipped": 0,
            "pairs_failed": 0,
            "rows_compacted": 0,
            "reanchors_attempted": 0,
            "reanchors_successful": 0,
            "orphan_rows_prevented": 0,
            "global_review_windows": 0,
            "global_review_actions": 0,
            "global_review_rows_modified": 0,
            "global_review_failures": 0,
            "final_review_passes": 0,
            "final_review_actions": 0,
            "final_review_rows_modified": 0,
            "final_review_failures": 0,
        }
        progress(5, "extraccion", "Extrayendo texto de ambos documentos")
        t0 = time.perf_counter()
        extraction_a = extract_document_result(file_a_path, soffice_path=opts.get("soffice"), engine=opts.get("engine", "auto"))
        extraction_b = extract_document_result(file_b_path, soffice_path=opts.get("soffice"), engine=opts.get("engine", "auto"))
        opts["_qualityA"] = extraction_a.to_quality_dict()
        opts["_qualityB"] = extraction_b.to_quality_dict()
        timings["extraction_seconds"] = time.perf_counter() - t0
        observe_compare_phase_duration("extraction", timings["extraction_seconds"])

        self._raise_if_aborted(stage="after_extraction")
        progress(25, "normalizacion", "Normalizando contenido para ignorar maquetación")
        t0 = time.perf_counter()
        normalized_a = self._normalize_extraction_for_comparison(extraction_a=extraction_a, counterpart=extraction_b)
        normalized_b = self._normalize_extraction_for_comparison(extraction_a=extraction_b, counterpart=extraction_a)
        timings["normalization_seconds"] = time.perf_counter() - t0
        observe_compare_phase_duration("normalization", timings["normalization_seconds"])

        if normalized_a.canonical == normalized_b.canonical:
            progress(90, "salida", "Documentos equivalentes tras normalización")
            return self._build_result(
                rows=[],
                extraction_a=extraction_a,
                extraction_b=extraction_b,
                block_size=0,
                alignments=[],
                counts=counts,
                timings=timings,
                global_review_diagnostics={"window_review": [], "final_review": []},
            )

        progress(40, "segmentacion", "Dividiendo en bloques semánticos")
        t0 = time.perf_counter()
        block_size = max(80, int(opts.get("semantic_block_words") or self.semantic_block_words))
        blocks_a = build_semantic_blocks(normalized_a.normalized, paragraph_blocks(normalized_a.normalized, max_words=block_size))
        blocks_b = build_semantic_blocks(normalized_b.normalized, paragraph_blocks(normalized_b.normalized, max_words=block_size))
        self._current_blocks_a = blocks_a
        self._current_blocks_b = blocks_b
        timings["chunking_seconds"] = time.perf_counter() - t0
        observe_compare_phase_duration("chunking", timings["chunking_seconds"])

        progress(55, "alineacion", "Alineando bloques equivalentes")
        t0 = time.perf_counter()
        alignment_result = align_blocks(blocks_a, blocks_b)
        counts["reanchors_attempted"] += alignment_result.metrics.get("reanchors_attempted", 0)
        counts["reanchors_successful"] += alignment_result.metrics.get("reanchors_successful", 0)
        counts["orphan_rows_prevented"] += alignment_result.metrics.get("orphan_rows_prevented", 0)
        contextual_rescue = rescue_orphans_with_context(alignment_result.matches)
        counts["reanchors_attempted"] += contextual_rescue.metrics.get("reanchors_attempted", 0)
        counts["reanchors_successful"] += contextual_rescue.metrics.get("reanchors_successful", 0)
        counts["orphan_rows_prevented"] += contextual_rescue.metrics.get("orphan_rows_prevented", 0)
        alignments = contextual_rescue.matches
        timings["alignment_seconds"] = time.perf_counter() - t0
        observe_compare_phase_duration("alignment", timings["alignment_seconds"])

        self._raise_if_aborted(stage="before_diff")
        progress(70, "diff", "Analizando solo bloques candidatos a cambio")
        rows: list[dict[str, Any]] = []
        seen_pair_keys: set[str] = set()
        t0 = time.perf_counter()
        for row_index, alignment in enumerate(alignments, start=1):
            self._raise_if_aborted(stage=f"alignment_loop:{row_index}")
            counts["pairs_total"] += 1
            row = self._build_row(
                alignment=alignment,
                row_index=row_index,
                counts=counts,
                seen_pair_keys=seen_pair_keys,
            )
            if row is None or row["change_type"] == "sin_cambios":
                continue
            rows.append(row)
        rows = self._postprocess_rows(rows, counts=counts)
        rows, global_review_diagnostics = self._global_review_rows(rows, counts=counts)
        rows, final_review_diagnostics = self._final_review_rows(rows, counts=counts)
        timings["diff_seconds"] = time.perf_counter() - t0
        observe_compare_phase_duration("diff", timings["diff_seconds"])

        progress(90, "salida", "Construyendo payload compatible con frontend")
        result = self._build_result(
            rows=rows,
            extraction_a=extraction_a,
            extraction_b=extraction_b,
            block_size=block_size,
            alignments=alignments,
            counts=counts,
            timings=timings,
            global_review_diagnostics={
                "window_review": global_review_diagnostics,
                "final_review": final_review_diagnostics,
            },
        )
        progress(100, "completado", "Comparación finalizada")
        return result

    def _normalize_extraction_for_comparison(self, *, extraction_a, counterpart) -> Any:
        normalized = normalize_text(extraction_a.text)
        if not self._should_harden_layout_normalization(extraction_a=extraction_a, counterpart=counterpart):
            return normalized
        hardened = self._harden_layout_text(normalized.normalized, extraction=extraction_a)
        return normalize_text(hardened)

    def _should_harden_layout_normalization(self, *, extraction_a, counterpart) -> bool:
        engines = {str(extraction_a.engine or "").lower(), str(counterpart.engine or "").lower()}
        signals_a = extraction_a.quality_signals or {}
        signals_b = counterpart.quality_signals or {}
        mixed_docx_pdf = engines == {"docx", "pdf"}
        scan_like_pair = bool(extraction_a.metadata.get("scan_like")) or bool(counterpart.metadata.get("scan_like"))
        noisy_pair = max(
            float(signals_a.get("layout_noise_score") or 0.0),
            float(signals_b.get("layout_noise_score") or 0.0),
        ) >= 0.4
        repeated_headers = bool(signals_a.get("has_repeated_headers")) or bool(signals_b.get("has_repeated_headers"))
        dense_tables = max(
            float(signals_a.get("table_like_density") or 0.0),
            float(signals_b.get("table_like_density") or 0.0),
        ) >= 0.12
        return mixed_docx_pdf or scan_like_pair or noisy_pair or repeated_headers or dense_tables

    def _harden_layout_text(self, text: str, *, extraction) -> str:
        signals = extraction.quality_signals or {}
        hardened = text or ""
        dense_tables = float(signals.get("table_like_density") or 0.0) >= 0.12
        scan_like = bool(extraction.metadata.get("scan_like"))
        if dense_tables:
            hardened = re.sub(r"(?m)^\|\s*", "", hardened)
            hardened = hardened.replace(" | ", " ; ")
        if scan_like or float(signals.get("layout_noise_score") or 0.0) >= 0.4:
            hardened = normalize_structural_markers(hardened)
            hardened = re.sub(r"(?<!\n)\n(?!\n|- )", " ", hardened)
        if bool(signals.get("has_repeated_headers")) or bool(signals.get("has_repeated_footers")):
            hardened = strip_layout_metadata(hardened)
        if dense_tables or scan_like:
            hardened = re.sub(r"\n{2,}", "\n", hardened)
            hardened = re.sub(r"\s*\n\s*", " ; ", hardened)
            hardened = re.sub(r"(?:\s*;\s*){2,}", " ; ", hardened)
        hardened = re.sub(r"\n{3,}", "\n\n", hardened)
        return hardened.strip()

    def _build_row(
        self,
        *,
        alignment: AlignmentMatch,
        row_index: int,
        counts: dict[str, int],
        seen_pair_keys: set[str],
    ) -> dict[str, Any] | None:
        if alignment.block_a and alignment.block_b and alignment.block_a.fingerprint == alignment.block_b.fingerprint:
            counts["pairs_equal"] += 1
            return self._base_row(row_index=row_index, text_a=alignment.block_a.text, text_b=alignment.block_b.text, change_type="sin_cambios", alignment=alignment)
        if alignment.block_a is None:
            counts["pairs_orphan"] += 1
            return self._orphan_row(row_index=row_index, missing_side="a", text=alignment.block_b.text, alignment=alignment)
        if alignment.block_b is None:
            counts["pairs_orphan"] += 1
            return self._orphan_row(row_index=row_index, missing_side="b", text=alignment.block_a.text, alignment=alignment)


        self._raise_if_aborted(stage=f"before_pair:{row_index}")
        cache_key = self._pair_cache_key(alignment.block_a, alignment.block_b)
        if cache_key in seen_pair_keys:
            counts["duplicate_pairs_skipped"] += 1
            return None
        seen_pair_keys.add(cache_key)
        return self._diff_row(row_index=row_index, alignment=alignment, cache_key=cache_key, counts=counts)

    def _orphan_row(self, *, row_index: int, missing_side: str, text: str, alignment: AlignmentMatch) -> dict[str, Any]:
        if missing_side == "a":
            text_a = "[[BLOQUE AUSENTE EN A]]"
            text_b = text
        else:
            text_a = text
            text_b = "[[BLOQUE AUSENTE EN B]]"
        cache_key = self._orphan_cache_key(text_a=text_a, text_b=text_b)
        payload, llm_success, cache_hit, result_origin, prompt_inputs = self._resolve_payload_via_llm(
            row_index=row_index,
            text_a=text_a,
            text_b=text_b,
            alignment=alignment,
            cache_key=cache_key,
        )
        row = self._base_row(
            row_index=row_index,
            text_a=text_a,
            text_b=text_b,
            change_type=str(payload.get("change_type") or "modificado"),
            alignment=alignment,
            pair_hash=cache_key.split(":")[-1],
        )
        row.update({
            "display_text_a": str(payload.get("display_text_a") or text_a),
            "display_text_b": str(payload.get("display_text_b") or text_b),
            "display_segments_a": normalize_text_segments(payload.get("display_segments_a") or ([{"type": "replace", "text": text_a}] if text_a else [])),
            "display_segments_b": normalize_text_segments(payload.get("display_segments_b") or ([{"type": "replace", "text": text_b}] if text_b else [])),
            "review_label": str(payload.get("review_label") or "cambio_real"),
            "summary": str(payload.get("summary") or ""),
            "llm_comment": str(payload.get("summary") or ""),
            "justification": str(payload.get("justification") or ""),
            "result_origin": result_origin,
            "decision_source": "pair_cache" if cache_hit else "llm_orphan_resolution",
            "confidence": str(payload.get("confidence") or "media"),
            "severity": str(payload.get("severity") or "media"),
            "llm_success": llm_success,
            "fallback_applied": False,
            "result_validation_status": "validated",
            "cache_hit": cache_hit,
            "cache_pair_hash": cache_key.split(":")[-1],
            "prompt_messages": payload.get("prompt_messages") if isinstance(payload.get("prompt_messages"), list) else [],
            "context_before_a": prompt_inputs["previous_text_a"],
            "context_after_a": prompt_inputs["next_text_a"],
            "context_before_b": prompt_inputs["previous_text_b"],
            "context_after_b": prompt_inputs["next_text_b"],
        })
        return row

    def _diff_row(
        self,
        *,
        row_index: int,
        alignment: AlignmentMatch,
        cache_key: str,
        counts: dict[str, int],
    ) -> dict[str, Any]:
        text_a = alignment.block_a.text
        text_b = alignment.block_b.text
        cache_hash = cache_key.split(":")[-1]
        payload, llm_success, cache_hit, result_origin, prompt_inputs = self._resolve_payload_via_llm(
            row_index=row_index,
            text_a=text_a,
            text_b=text_b,
            alignment=alignment,
            cache_key=cache_key,
            counts=counts,
        )
        if self._is_payload_semantically_equal(payload):
            counts["pairs_equal"] += 1
            return self._base_row(row_index=row_index, text_a=text_a, text_b=text_b, change_type="sin_cambios", alignment=alignment, pair_hash=cache_hash)

        row = self._base_row(
            row_index=row_index,
            text_a=text_a,
            text_b=text_b,
            change_type=str(payload.get("change_type") or "modificado"),
            alignment=alignment,
            pair_hash=cache_hash,
        )
        row.update({
            "display_text_a": str(payload.get("display_text_a") or text_a),
            "display_text_b": str(payload.get("display_text_b") or text_b),
            "display_segments_a": normalize_text_segments(payload.get("display_segments_a") or ([{"type": "replace", "text": text_a}] if text_a else [])),
            "display_segments_b": normalize_text_segments(payload.get("display_segments_b") or ([{"type": "replace", "text": text_b}] if text_b else [])),
            "review_label": str(payload.get("review_label") or "cambio_real"),
            "summary": str(payload.get("summary") or ""),
            "impact": str(payload.get("impact") or ""),
            "llm_comment": str(payload.get("summary") or ""),
            "justification": str(payload.get("justification") or ""),
            "confidence": str(payload.get("confidence") or "media"),
            "severity": str(payload.get("severity") or "media"),
            "llm_success": llm_success,
            "fallback_applied": False,
            "result_validation_status": "validated",
            "result_origin": result_origin,
            "decision_source": "pair_cache" if cache_hit else "llm_candidate_blocks_only",
            "cache_hit": cache_hit,
            "cache_pair_hash": cache_hash,
            "prompt_messages": payload.get("prompt_messages") if isinstance(payload.get("prompt_messages"), list) else [],
            "context_before_a": prompt_inputs["previous_text_a"],
            "context_after_a": prompt_inputs["next_text_a"],
            "context_before_b": prompt_inputs["previous_text_b"],
            "context_after_b": prompt_inputs["next_text_b"],
        })
        return row

    def _source_payload(self, *, text_a: str, text_b: str) -> dict[str, Any]:
        return {
            "review_label": "cambio_real",
            "change_type": "modificado",
            "summary": "",
            "severity": "media",
            "confidence": "media",
            "text_a": text_a,
            "text_b": text_b,
            "display_text_a": text_a,
            "display_text_b": text_b,
            "display_segments_a": [{"type": "equal", "text": text_a}] if text_a else [],
            "display_segments_b": [{"type": "equal", "text": text_b}] if text_b else [],
            "justification": "",
            "impact": "",
        }

    def _resolve_payload_via_llm(
        self,
        *,
        row_index: int,
        text_a: str,
        text_b: str,
        alignment: AlignmentMatch,
        cache_key: str,
        counts: dict[str, int] | None = None,
    ) -> tuple[dict[str, Any], bool, bool, str, dict[str, Any]]:
        prompt_inputs = self._build_local_prompt_inputs(alignment=alignment, text_a=text_a, text_b=text_b)
        source_payload = self._source_payload(text_a=text_a, text_b=text_b)
        cached = self.pair_cache.get(cache_key)
        if isinstance(cached, dict):
            if counts is not None:
                counts["pairs_cache_hit"] += 1
            record_cache_hit(True)
            payload = self._sanitize_compare_payload(cached, source_payload=source_payload)
            return payload, bool(payload.get("llm_success", True)), True, "cache", prompt_inputs

        if counts is not None:
            counts["pairs_cache_miss"] += 1
        record_cache_hit(False)
        llm_client = self._require_llm_client()
        try:
            self._raise_if_aborted(stage=f"before_llm:{row_index}")
            prompt_messages = build_compare_messages(**prompt_inputs)
            payload = llm_client.chat_json(
                messages=prompt_messages,
                schema=COMPARE_JSON_SCHEMA,
                temperature=0.0,
                max_tokens=500,
            )
        except Exception:
            if counts is not None:
                counts["pairs_failed"] += 1
            raise

        sanitized = self._sanitize_compare_payload(payload, source_payload=source_payload)
        sanitized["llm_success"] = True
        sanitized["prompt_messages"] = prompt_messages
        self.pair_cache.set(cache_key, sanitized)
        if counts is not None:
            counts["pairs_sent_to_llm"] += 1
        return sanitized, True, False, "llm", prompt_inputs

    def _base_row(
        self,
        *,
        row_index: int,
        text_a: str,
        text_b: str,
        change_type: str,
        alignment: AlignmentMatch,
        pair_hash: str | None = None,
    ) -> dict[str, Any]:
        stable_hash = pair_hash or f"pair-{row_index}"
        return {
            "block_id": row_index,
            "pair_id": f"block-{row_index}",
            "pair_hash": stable_hash,
            "text_a": text_a,
            "text_b": text_b,
            "display_text_a": text_a,
            "display_text_b": text_b,
            "display_segments_a": [{"type": "equal", "text": text_a}] if text_a else [],
            "display_segments_b": [{"type": "equal", "text": text_b}] if text_b else [],
            "change_type": change_type,
            "materiality": "pendiente_confirmacion",
            "confidence": "media",
            "final_decision": "pendiente_confirmacion",
            "severity": "media",
            "summary": "",
            "impact": "",
            "llm_comment": "",
            "justification": "",
            "review_status": "pending",
            "decision_source": "llm_candidate_blocks_only",
            "result_origin": "literal",
            "result_validation_status": "validated",
            "fallback_applied": False,
            "cache_hit": False,
            "cache_pair_hash": stable_hash,
            "llm_success": False,
            "model_name": getattr(self.llm_client, "model_name", "local-compare-worker") if self.llm_client else "local-compare-worker",
            "prompt_version": LOCAL_COMPARE_PROMPT_VERSION,
            "global_review_prompt_version": GLOBAL_REVIEW_PROMPT_VERSION,
            "prompt_text_a_literal": text_a,
            "prompt_text_b_literal": text_b,
            "prompt_messages": [],
            "review_label": "cambio_real",
            "global_review_modified": False,
            "global_review_notes": [],
            "source_spans": {
                "block_id": row_index,
                "file_a": [0, len(text_a)],
                "file_b": [0, len(text_b)],
                "diff": [],
                "segments_a": [],
                "segments_b": [],
            },
            "pairing": {
                "alignment_score": alignment.score,
                "alignment_strategy": alignment.strategy,
                "reanchored": alignment.reanchored,
                "reanchor_strategy": alignment.reanchor_strategy,
            },
            "chunk_index_a": alignment.block_a.block_id if alignment.block_a else 0,
            "chunk_index_b": alignment.block_b.block_id if alignment.block_b else 0,
            "offset_start_a": 0,
            "offset_end_a": len(text_a),
            "offset_start_b": 0,
            "offset_end_b": len(text_b),
            "block_word_count_a": len(text_a.split()),
            "block_word_count_b": len(text_b.split()),
            "block_size_words": max(len(text_a.split()), len(text_b.split())),
            "block_overlap_words": 0,
            "alignment_score": alignment.score,
            "alignment_strategy": alignment.strategy,
            "reanchored": alignment.reanchored,
            "reanchor_strategy": alignment.reanchor_strategy,
        }

    def _sanitize_compare_payload(
        self,
        payload: dict[str, Any],
        *,
        source_payload: dict[str, Any],
    ) -> dict[str, Any]:
        sanitized = dict(payload or {})
        review_label = self._normalize_review_label(sanitized.get("review_label"))
        sanitized["review_label"] = review_label
        sanitized["change_type"] = self._map_public_change_type(
            review_label=review_label,
            requested_change_type=sanitized.get("change_type"),
            fallback_change_type=source_payload.get("change_type"),
        )

        for key in ("text_a", "text_b", "display_text_a", "display_text_b"):
            cleaned = strip_layout_metadata(str(sanitized.get(key) or "")).strip()
            if cleaned:
                sanitized[key] = cleaned
            elif source_payload.get(key):
                sanitized[key] = source_payload[key]

        for key, fallback_key in (("display_segments_a", "display_segments_a"), ("display_segments_b", "display_segments_b")):
            source_segments = sanitized.get(key)
            if not isinstance(source_segments, list):
                sanitized[key] = source_payload.get(fallback_key, [])
                continue

            cleaned_segments: list[dict[str, str]] = []
            for segment in source_segments:
                if not isinstance(segment, dict):
                    continue
                cleaned_text = strip_layout_metadata(str(segment.get("text") or "")).strip()
                if not cleaned_text:
                    continue
                cleaned_segments.append({"type": str(segment.get("type") or "equal"), "text": cleaned_text})
            sanitized[key] = cleaned_segments or source_payload.get(fallback_key, [])

        return sanitized

    def _pair_cache_key(self, block_a: SemanticBlock, block_b: SemanticBlock) -> str:
        normalized_a = canonicalize_for_comparison(strip_layout_metadata(block_a.text))
        normalized_b = canonicalize_for_comparison(strip_layout_metadata(block_b.text))
        return self.pair_cache.build_key(
            normalized_a=normalized_a,
            normalized_b=normalized_b,
            prompt_version=LOCAL_COMPARE_PROMPT_VERSION,
            pipeline_version=PIPELINE_VERSION,
            model_name=getattr(self.llm_client, "model_name", "local-compare-worker") if self.llm_client else "local-compare-worker",
            config={
                "max_prompt_chars_per_side": self.max_prompt_chars_per_side,
                "max_ambiguous_prompt_chars_per_side": self.ambiguous_prompt_chars_per_side,
            },
        )

    def _trim_for_prompt(self, text: str, *, budget: int) -> str:
        raw = strip_layout_metadata(text or "").strip()
        if len(raw) <= budget:
            return raw
        half = max(1, budget // 2)
        head = raw[:half].rstrip()
        tail = raw[-half:].lstrip()
        return f"{head}\n[…recortado…]\n{tail}"

    def _orphan_cache_key(self, *, text_a: str, text_b: str) -> str:
        return self.pair_cache.build_key(
            normalized_a=canonicalize_for_comparison(strip_layout_metadata(text_a)),
            normalized_b=canonicalize_for_comparison(strip_layout_metadata(text_b)),
            prompt_version=LOCAL_COMPARE_PROMPT_VERSION,
            pipeline_version=PIPELINE_VERSION,
            model_name=getattr(self.llm_client, "model_name", "local-compare-worker") if self.llm_client else "local-compare-worker",
            config={"orphan_resolution": True},
        )

    def _prompt_char_budget_for_pair(self, *, alignment: AlignmentMatch, text_a: str, text_b: str) -> int:
        ambiguous = alignment.score < 0.84 or max(len(text_a), len(text_b)) > self.max_prompt_chars_per_side
        return self.ambiguous_prompt_chars_per_side if ambiguous else self.max_prompt_chars_per_side

    def _neighbor_text(self, blocks: list[SemanticBlock], block: SemanticBlock | None, offset: int, budget: int) -> str:
        if block is None:
            return ""
        neighbor_index = block.block_id - 1 + offset
        if neighbor_index < 0 or neighbor_index >= len(blocks):
            return ""
        return self._trim_for_prompt(blocks[neighbor_index].text, budget=budget)

    def _build_local_prompt_inputs(self, *, alignment: AlignmentMatch, text_a: str, text_b: str) -> dict[str, Any]:
        budget = self._prompt_char_budget_for_pair(alignment=alignment, text_a=text_a, text_b=text_b)
        context_budget = max(120, budget // 4)
        return {
            "text_a": self._trim_for_prompt(text_a, budget=budget),
            "text_b": self._trim_for_prompt(text_b, budget=budget),
            "previous_text_a": self._neighbor_text(self._current_blocks_a, alignment.block_a, -1, context_budget),
            "next_text_a": self._neighbor_text(self._current_blocks_a, alignment.block_a, 1, context_budget),
            "previous_text_b": self._neighbor_text(self._current_blocks_b, alignment.block_b, -1, context_budget),
            "next_text_b": self._neighbor_text(self._current_blocks_b, alignment.block_b, 1, context_budget),
            "alignment_score": alignment.score,
            "alignment_strategy": alignment.strategy,
        }

    def _normalize_review_label(self, value: Any) -> str:
        label = str(value or "").strip().lower()
        if label in {"sin_cambios", "sin_cambios_por_reflujo", "reflujo"}:
            return "sin_cambios_por_reflujo"
        if label in {"posible_mal_emparejamiento", "mal_emparejamiento", "mismatch"}:
            return "posible_mal_emparejamiento"
        return "cambio_real"

    def _map_public_change_type(
        self,
        *,
        review_label: str,
        requested_change_type: Any,
        fallback_change_type: Any,
    ) -> str:
        normalized_requested = str(requested_change_type or "").strip().lower()
        normalized_fallback = str(fallback_change_type or "modificado").strip().lower()
        if review_label == "sin_cambios_por_reflujo":
            return "sin_cambios"
        if normalized_requested in {"sin_cambios", "modificado", "insertado", "eliminado"}:
            return normalized_requested
        if review_label == "posible_mal_emparejamiento":
            return normalized_fallback if normalized_fallback in {"modificado", "insertado", "eliminado"} else "modificado"
        return normalized_fallback if normalized_fallback in {"sin_cambios", "modificado", "insertado", "eliminado"} else "modificado"

    def _is_payload_semantically_equal(self, payload: dict[str, Any]) -> bool:
        change_type = str(payload.get("change_type") or "").strip().lower()
        return change_type == "sin_cambios"

    def _postprocess_rows(self, rows: list[dict[str, Any]], *, counts: dict[str, int]) -> list[dict[str, Any]]:
        compacted: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str, str]] = set()
        for row in rows:
            canonical_a = normalize_text(str(row.get("text_a") or "")).canonical
            canonical_b = normalize_text(str(row.get("text_b") or "")).canonical
            if canonical_a == canonical_b:
                counts["rows_compacted"] += 1
                continue
            dedupe_key = (str(row.get("change_type") or ""), canonical_a, canonical_b)
            if dedupe_key in seen_keys:
                counts["rows_compacted"] += 1
                continue
            seen_keys.add(dedupe_key)
            compacted.append(row)
        for index, row in enumerate(compacted, start=1):
            row["block_id"] = index
            row["pair_id"] = f"block-{index}"
        return compacted

    def _global_review_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        counts: dict[str, int],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if len(rows) < 2:
            return rows, []

        actions = self._collect_global_review_actions(rows, counts=counts)
        if not actions:
            return rows, []

        counts["global_review_actions"] += len(actions)
        modified_rows, diagnostics = self._apply_global_review_actions(rows, actions=actions, counts=counts)
        for index, row in enumerate(modified_rows, start=1):
            row["block_id"] = index
            row["pair_id"] = f"block-{index}"
        return modified_rows, diagnostics

    def _final_review_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        counts: dict[str, int],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not rows or not self._should_run_final_review(rows):
            return rows, []

        llm_client = self._require_llm_client()
        review_rows = [self._serialize_row_for_final_review(row) for row in rows[: self.final_review_max_rows]]
        if not review_rows:
            return rows, []

        counts["final_review_passes"] += 1
        try:
            self._raise_if_aborted(stage="final_review")
            prompt_messages = build_global_table_review_messages(
                rows=review_rows,
                total_rows=len(rows),
            )
            payload = llm_client.chat_json(
                messages=prompt_messages,
                schema=GLOBAL_REVIEW_JSON_SCHEMA,
                temperature=0.0,
                max_tokens=700,
            )
            actions = self._sanitize_global_review_actions(
                payload.get("actions"),
                valid_row_ids={int(row["block_id"]) for row in rows},
                source="llm_final_review",
            )
        except Exception:
            counts["final_review_failures"] += 1
            return rows, []

        if not actions:
            return rows, []

        counts["final_review_actions"] += len(actions)
        modified_rows, diagnostics = self._apply_global_review_actions(
            rows,
            actions=actions,
            counts=counts,
            modified_counter_key="final_review_rows_modified",
        )
        for index, row in enumerate(modified_rows, start=1):
            row["block_id"] = index
            row["pair_id"] = f"block-{index}"
        return modified_rows, diagnostics

    def _should_run_final_review(self, rows: list[dict[str, Any]]) -> bool:
        if len(rows) >= 2:
            return True
        row = rows[0]
        review_label = str(row.get("review_label") or "").strip().lower()
        alignment_strategy = str((row.get("pairing") or {}).get("alignment_strategy", row.get("alignment_strategy") or "")).strip().lower()
        return (
            bool(row.get("reanchored"))
            or review_label == "posible_mal_emparejamiento"
            or "reanchor" in alignment_strategy
        )

    def _collect_global_review_actions(
        self,
        rows: list[dict[str, Any]],
        *,
        counts: dict[str, int],
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        seen_signatures: set[tuple[str, tuple[int, ...]]] = set()
        llm_client = self._require_llm_client()
        for window_index, (start, end) in enumerate(self._iter_global_review_windows(len(rows)), start=1):
            window_rows = rows[start:end]
            counts["global_review_windows"] += 1
            actions: list[dict[str, Any]] = []
            try:
                self._raise_if_aborted(stage=f"global_review:{window_index}")
                review_rows = [self._serialize_row_for_global_review(row) for row in window_rows]
                prompt_messages = build_global_review_messages(
                    rows=review_rows,
                    window_start=start + 1,
                    window_end=end,
                    total_rows=len(rows),
                )
                payload = llm_client.chat_json(
                    messages=prompt_messages,
                    schema=GLOBAL_REVIEW_JSON_SCHEMA,
                    temperature=0.0,
                    max_tokens=600,
                )
                actions = self._sanitize_global_review_actions(
                    payload.get("actions"),
                    valid_row_ids={int(row["block_id"]) for row in window_rows},
                    source="llm_window_review",
                )
            except Exception:
                counts["global_review_failures"] += 1
            for action in actions:
                signature = (str(action.get("disposition") or ""), tuple(int(row_id) for row_id in action.get("row_ids") or []))
                if signature in seen_signatures:
                    continue
                seen_signatures.add(signature)
                collected.append(action)
        return collected

    def _iter_global_review_windows(self, total_rows: int) -> list[tuple[int, int]]:
        if total_rows <= 0:
            return []
        window_size = min(self.global_review_window_rows, total_rows)
        stride = max(1, window_size - min(self.global_review_window_overlap, window_size - 1))
        windows: list[tuple[int, int]] = []
        start = 0
        while start < total_rows:
            end = min(total_rows, start + window_size)
            windows.append((start, end))
            if end >= total_rows:
                break
            start += stride
        return windows

    def _serialize_row_for_global_review(self, row: dict[str, Any]) -> dict[str, Any]:
        side_budget = self.global_review_max_chars_per_side
        context_budget = max(100, side_budget // 3)
        pairing = row.get("pairing") if isinstance(row.get("pairing"), dict) else {}
        return {
            "row_id": int(row.get("block_id") or 0),
            "change_type": str(row.get("change_type") or ""),
            "review_label": str(row.get("review_label") or ""),
            "summary": str(row.get("summary") or ""),
            "alignment_score": pairing.get("alignment_score", row.get("alignment_score")),
            "alignment_strategy": pairing.get("alignment_strategy", row.get("alignment_strategy")),
            "text_a": self._trim_for_prompt(str(row.get("text_a") or ""), budget=side_budget),
            "text_b": self._trim_for_prompt(str(row.get("text_b") or ""), budget=side_budget),
            "context_before_a": self._trim_for_prompt(str(row.get("context_before_a") or ""), budget=context_budget),
            "context_after_a": self._trim_for_prompt(str(row.get("context_after_a") or ""), budget=context_budget),
            "context_before_b": self._trim_for_prompt(str(row.get("context_before_b") or ""), budget=context_budget),
            "context_after_b": self._trim_for_prompt(str(row.get("context_after_b") or ""), budget=context_budget),
        }

    def _serialize_row_for_final_review(self, row: dict[str, Any]) -> dict[str, Any]:
        side_budget = self.final_review_max_chars_per_side
        pairing = row.get("pairing") if isinstance(row.get("pairing"), dict) else {}
        return {
            "row_id": int(row.get("block_id") or 0),
            "change_type": str(row.get("change_type") or ""),
            "review_label": str(row.get("review_label") or ""),
            "summary": str(row.get("summary") or ""),
            "justification": self._trim_for_prompt(str(row.get("justification") or ""), budget=max(120, side_budget // 2)),
            "alignment_score": pairing.get("alignment_score", row.get("alignment_score")),
            "alignment_strategy": pairing.get("alignment_strategy", row.get("alignment_strategy")),
            "text_a": self._trim_for_prompt(str(row.get("text_a") or ""), budget=side_budget),
            "text_b": self._trim_for_prompt(str(row.get("text_b") or ""), budget=side_budget),
            "context_before_a": self._trim_for_prompt(str(row.get("context_before_a") or ""), budget=max(80, side_budget // 3)),
            "context_after_a": self._trim_for_prompt(str(row.get("context_after_a") or ""), budget=max(80, side_budget // 3)),
            "context_before_b": self._trim_for_prompt(str(row.get("context_before_b") or ""), budget=max(80, side_budget // 3)),
            "context_after_b": self._trim_for_prompt(str(row.get("context_after_b") or ""), budget=max(80, side_budget // 3)),
        }

    def _sanitize_global_review_actions(
        self,
        actions: Any,
        *,
        valid_row_ids: set[int],
        source: str = "llm",
    ) -> list[dict[str, Any]]:
        if not isinstance(actions, list):
            return []
        sanitized: list[dict[str, Any]] = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            disposition = str(action.get("disposition") or "").strip().lower()
            row_ids = sorted({int(row_id) for row_id in action.get("row_ids") or [] if isinstance(row_id, int) and row_id in valid_row_ids})
            if not row_ids or disposition not in {"keep", "merge", "drop", "sin_cambios"}:
                continue
            sanitized.append({
                "disposition": disposition,
                "row_ids": row_ids,
                "review_label": self._normalize_review_label(action.get("review_label")),
                "rationale": str(action.get("rationale") or "").strip(),
                "target_change_type": str(action.get("target_change_type") or "").strip().lower(),
                "source": source,
            })
        return sanitized

    def _apply_global_review_actions(
        self,
        rows: list[dict[str, Any]],
        *,
        actions: list[dict[str, Any]],
        counts: dict[str, int],
        modified_counter_key: str = "global_review_rows_modified",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        row_map = {int(row.get("block_id") or 0): row for row in rows}
        drop_ids: set[int] = set()
        replacements: dict[int, dict[str, Any]] = {}
        diagnostics: list[dict[str, Any]] = []
        consumed_ids: set[int] = set()

        def mark_row_note(target_row: dict[str, Any], note: dict[str, Any]) -> None:
            notes = target_row.get("global_review_notes")
            if not isinstance(notes, list):
                notes = []
            notes.append(note)
            target_row["global_review_notes"] = notes
            target_row["global_review_modified"] = True
            target_row["global_review_prompt_version"] = GLOBAL_REVIEW_PROMPT_VERSION

        for action in actions:
            disposition = str(action.get("disposition") or "")
            row_ids = [row_id for row_id in action.get("row_ids") or [] if row_id in row_map]
            if not row_ids or any(row_id in consumed_ids for row_id in row_ids):
                continue
            rationale = str(action.get("rationale") or "").strip()
            review_label = self._normalize_review_label(action.get("review_label"))
            target_change_type = self._map_public_change_type(
                review_label=review_label,
                requested_change_type=action.get("target_change_type"),
                fallback_change_type="modificado",
            )
            if disposition in {"drop", "sin_cambios"}:
                drop_ids.update(row_ids)
                consumed_ids.update(row_ids)
                counts[modified_counter_key] += len(row_ids)
                diagnostics.append({
                    "action": disposition,
                    "row_ids": row_ids,
                    "review_label": review_label,
                    "rationale": rationale,
                    "source": str(action.get("source") or "llm"),
                })
                continue
            if disposition == "merge" and len(row_ids) >= 2:
                merged_row = self._merge_rows_for_global_review(
                    [row_map[row_id] for row_id in row_ids],
                    review_label=review_label,
                    rationale=rationale,
                    target_change_type=target_change_type,
                )
                keep_id = row_ids[0]
                replacements[keep_id] = merged_row
                drop_ids.update(row_ids[1:])
                consumed_ids.update(row_ids)
                counts[modified_counter_key] += len(row_ids)
                diagnostics.append({
                    "action": "merge",
                    "row_ids": row_ids,
                    "replacement_row_id": keep_id,
                    "review_label": review_label,
                    "rationale": rationale,
                    "source": str(action.get("source") or "llm"),
                })
                continue
            if disposition == "keep" and len(row_ids) == 1 and review_label == "sin_cambios_por_reflujo":
                drop_ids.update(row_ids)
                consumed_ids.update(row_ids)
                counts[modified_counter_key] += 1
                diagnostics.append({
                    "action": "reclassify_as_sin_cambios",
                    "row_ids": row_ids,
                    "review_label": review_label,
                    "rationale": rationale,
                    "source": str(action.get("source") or "llm"),
                })

        final_rows: list[dict[str, Any]] = []
        for row in rows:
            row_id = int(row.get("block_id") or 0)
            if row_id in drop_ids:
                continue
            updated = dict(replacements.get(row_id) or row)
            if row_id in replacements:
                mark_row_note(updated, {"action": "merge", "rationale": replacements[row_id].get("justification", "")})
            if str(updated.get("change_type") or "").strip().lower() == "sin_cambios":
                continue
            final_rows.append(updated)
        return final_rows, diagnostics

    def _merge_rows_for_global_review(
        self,
        rows: list[dict[str, Any]],
        *,
        review_label: str,
        rationale: str,
        target_change_type: str,
    ) -> dict[str, Any]:
        ordered_rows = sorted(rows, key=lambda row: int(row.get("block_id") or 0))
        text_a = "\n\n".join(str(row.get("text_a") or "") for row in ordered_rows).strip()
        text_b = "\n\n".join(str(row.get("text_b") or "") for row in ordered_rows).strip()
        merged = dict(ordered_rows[0])
        synthetic_alignment = AlignmentMatch(
            block_a=None,
            block_b=None,
            score=round(sum(float((row.get("pairing") or {}).get("alignment_score", row.get("alignment_score") or 0.0)) for row in ordered_rows) / max(1, len(ordered_rows)), 4),
            strategy="global_review_merge",
            reanchored=False,
            reanchor_strategy="",
        )
        merged_payload, _llm_success, _cache_hit, _result_origin, _prompt_inputs = self._resolve_payload_via_llm(
            row_index=int(ordered_rows[0].get("block_id") or 0),
            text_a=text_a,
            text_b=text_b,
            alignment=synthetic_alignment,
            cache_key=self._orphan_cache_key(text_a=text_a, text_b=text_b),
        )
        if target_change_type in {"modificado", "insertado", "eliminado"}:
            merged_payload["change_type"] = target_change_type
        merged_payload["review_label"] = review_label
        merged.update({
            "text_a": text_a,
            "text_b": text_b,
            "display_text_a": merged_payload["display_text_a"],
            "display_text_b": merged_payload["display_text_b"],
            "display_segments_a": merged_payload["display_segments_a"],
            "display_segments_b": merged_payload["display_segments_b"],
            "change_type": merged_payload["change_type"],
            "review_label": merged_payload.get("review_label", review_label),
            "summary": str(merged_payload.get("summary") or merged.get("summary") or "Filas fusionadas en revisión global."),
            "llm_comment": str(merged_payload.get("summary") or merged.get("summary") or "Filas fusionadas en revisión global."),
            "justification": f"Revisión global: {rationale}".strip(),
            "result_origin": "global_review_merge",
            "decision_source": "global_review_window",
            "prompt_version": LOCAL_COMPARE_PROMPT_VERSION,
            "global_review_modified": True,
            "global_review_notes": [{
                "action": "merge",
                "merged_row_ids": [int(row.get("block_id") or 0) for row in ordered_rows],
                "review_label": review_label,
                "rationale": rationale,
            }],
            "pairing": {
                **(merged.get("pairing") or {}),
                "alignment_score": round(sum(float((row.get("pairing") or {}).get("alignment_score", row.get("alignment_score") or 0.0)) for row in ordered_rows) / max(1, len(ordered_rows)), 4),
                "alignment_strategy": "global_review_merge",
            },
            "context_before_a": str(ordered_rows[0].get("context_before_a") or ""),
            "context_after_a": str(ordered_rows[-1].get("context_after_a") or ""),
            "context_before_b": str(ordered_rows[0].get("context_before_b") or ""),
            "context_after_b": str(ordered_rows[-1].get("context_after_b") or ""),
        })
        return merged

    def _build_result(
        self,
        *,
        rows: list[dict[str, Any]],
        extraction_a,
        extraction_b,
        block_size: int,
        alignments: list[AlignmentMatch],
        counts: dict[str, int],
        timings: dict[str, float],
        global_review_diagnostics: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        alignment_avg = round(sum(match.score for match in alignments) / max(1, len(alignments)), 4) if alignments else 1.0
        model_name = getattr(self.llm_client, "model_name", "local-compare-worker") if self.llm_client else "local-compare-worker"
        return {
            "ok": True,
            "reason": "completed",
            "comparison_rows": rows,
            "pair_records": rows,
            "block_diffs": rows,
            "block_diffs_total_detected": len(rows),
            "block_diffs_returned": len(rows),
            "block_diffs_truncated": False,
            "pair_records_schema": {
                "version": "pair-record-v2",
                "columns": [{"name": key, "type": "mixed"} for key in sorted({k for row in rows for k in row.keys()})],
            },
            "report_file": "resultado.json",
            "report_download_url": "",
            "export_json_url": "",
            "extraction": {
                "file_a": extraction_a.to_quality_dict(),
                "file_b": extraction_b.to_quality_dict(),
                "segmentation": {
                    "mode": "semantic_paragraph_blocks",
                    "blocks_a": len(paragraph_blocks(normalize_text(extraction_a.text).normalized, max_words=max(80, block_size or self.semantic_block_words))) if block_size else 0,
                    "blocks_b": len(paragraph_blocks(normalize_text(extraction_b.text).normalized, max_words=max(80, block_size or self.semantic_block_words))) if block_size else 0,
                    "block_size_words": block_size,
                },
                "pairing": {
                    "strategy": "anchor_hash+hybrid_similarity+dynamic_programming",
                    "block_size_words": block_size,
                    "block_overlap_words": 0,
                    "alignments_total": len(alignments),
                    "alignment_score_avg": alignment_avg,
                },
            },
            "ai_compliance": {
                "model_name": model_name,
                "comparison_mode": "llm_candidate_blocks_only",
                "ai_only_enabled": True,
                "blocks_resolved_from_cache": counts["pairs_cache_hit"],
                "blocks_resolved_by_llm": counts["pairs_sent_to_llm"],
                "blocks_failed": counts["pairs_failed"],
                "block_size_words": block_size,
                "block_overlap_words": 0,
            },
            "diagnostics": {
                "pipeline_version": PIPELINE_VERSION,
                "prompt_versions": {
                    "local_compare": LOCAL_COMPARE_PROMPT_VERSION,
                    "global_review": GLOBAL_REVIEW_PROMPT_VERSION,
                    "global_table_review": GLOBAL_TABLE_REVIEW_PROMPT_VERSION,
                },
                "timings": {key: round(value, 4) for key, value in timings.items()},
                "counts": counts,
                "global_review": {
                    "window_rows": self.global_review_window_rows,
                    "window_overlap": self.global_review_window_overlap,
                    "max_chars_per_side": self.global_review_max_chars_per_side,
                    "modified_rows": global_review_diagnostics.get("window_review", []),
                },
                "final_review": {
                    "max_rows": self.final_review_max_rows,
                    "max_chars_per_side": self.final_review_max_chars_per_side,
                    "modified_rows": global_review_diagnostics.get("final_review", []),
                },
            },
        }


def compare_documents(**kwargs: Any) -> dict[str, Any]:
    service = CompareDocumentsService(llm_client=kwargs.pop("llm_client", None))
    return service.compare_documents(**kwargs)