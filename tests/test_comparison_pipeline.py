from pathlib import Path

from app.extractors import ExtractionResult
from app.llm_client import LLMResponseError
from app.schemas import LLMComparisonResponse
from app.services import comparison_pipeline
from app.services.segmenter import TextBlock


class PairingStubLLMClient:
    model_name = "stub-llm"

    def compare(self, messages):
        user_payload = messages[-1]["content"]
        if '"rows"' in user_payload:
            return LLMComparisonResponse.model_validate({"changes": []})
        if '"block_a": {"index": null' in user_payload:
            return LLMComparisonResponse.model_validate(
                {
                    "changes": [
                        {
                            "change_type": "añadido",
                            "source_a": "",
                            "source_b": "nuevo",
                            "summary": "bloque añadido",
                        }
                    ]
                }
            )
        if '"block_b": {"index": null' in user_payload:
            return LLMComparisonResponse.model_validate(
                {
                    "changes": [
                        {
                            "change_type": "eliminado",
                            "source_a": "faltante",
                            "source_b": "",
                            "summary": "bloque eliminado",
                        }
                    ]
                }
            )
        source = "alineado-y" if 'Y' in user_payload else "alineado-x"
        return LLMComparisonResponse.model_validate(
            {
                "changes": [
                    {
                        "change_type": "modificado",
                        "source_a": source,
                        "source_b": f"{source}-b",
                        "summary": "bloque alineado",
                    }
                ]
            }
        )


class FailingPairStubLLMClient(PairingStubLLMClient):
    def compare(self, messages):
        user_payload = messages[-1]["content"]
        if '"rows"' in user_payload:
            return super().compare(messages)
        if '"index": 1' in user_payload and "Y ajustado" in user_payload:
            raise RuntimeError("fallo controlado en pareja intermedia")
        return super().compare(messages)


class ReconciliationFailingStubLLMClient(PairingStubLLMClient):
    def compare(self, messages):
        user_payload = messages[-1]["content"]
        if '"rows"' in user_payload:
            raise RuntimeError("fallo controlado en reconciliación")
        return super().compare(messages)


class EmptyPayloadFallbackStubLLMClient(PairingStubLLMClient):
    def compare(self, messages):
        user_payload = messages[-1]["content"]
        if '"rows"' in user_payload:
            return LLMComparisonResponse.model_validate({"changes": []})
        raise LLMResponseError("Payload del LLM vacío.")


def make_block(index: int, text: str) -> TextBlock:
    start = index * 100
    return TextBlock(index=index, text=text, start_char=start, end_char=start + len(text))


def pair_signature(pairs: list[dict[str, object]]) -> list[tuple[str | None, str | None, str]]:
    signature = []
    for pair in pairs:
        block_a = pair["a"]
        block_b = pair["b"]
        signature.append(
            (
                None if block_a is None else block_a.text,
                None if block_b is None else block_b.text,
                pair["pair_type"],
            )
        )
    return signature


def test_pair_blocks_preserves_insertion_in_middle():
    blocks_a = [make_block(0, "X original"), make_block(1, "Y final")]
    blocks_b = [make_block(0, "X original"), make_block(1, "NUEVO intermedio"), make_block(2, "Y final")]

    pairs = comparison_pipeline._pair_blocks(blocks_a, blocks_b)

    assert pair_signature(pairs) == [
        ("X original", "X original", "matched"),
        (None, "NUEVO intermedio", "orphan_b"),
        ("Y final", "Y final", "reanchored"),
    ]


def test_pair_blocks_preserves_deletion_in_middle():
    blocks_a = [make_block(0, "X original"), make_block(1, "ELIMINADO intermedio"), make_block(2, "Y final")]
    blocks_b = [make_block(0, "X original"), make_block(1, "Y final")]

    pairs = comparison_pipeline._pair_blocks(blocks_a, blocks_b)

    assert pair_signature(pairs) == [
        ("X original", "X original", "matched"),
        ("ELIMINADO intermedio", None, "orphan_a"),
        ("Y final", "Y final", "reanchored"),
    ]


def test_pair_blocks_handles_repeated_blocks_without_losing_orphans():
    blocks_a = [make_block(0, "INTRO común"), make_block(1, "DETALLE repetido"), make_block(2, "CIERRE único")]
    blocks_b = [
        make_block(0, "INTRO común"),
        make_block(1, "DETALLE repetido"),
        make_block(2, "DETALLE repetido"),
        make_block(3, "CIERRE único"),
    ]

    pairs = comparison_pipeline._pair_blocks(blocks_a, blocks_b)

    assert pair_signature(pairs) == [
        ("INTRO común", "INTRO común", "matched"),
        ("DETALLE repetido", "DETALLE repetido", "matched"),
        (None, "DETALLE repetido", "orphan_b"),
        ("CIERRE único", "CIERRE único", "reanchored"),
    ]


def test_pair_blocks_marks_reordered_sections_as_reanchored():
    blocks_a = [make_block(0, "ALFA sección"), make_block(1, "BETA sección"), make_block(2, "GAMMA sección")]
    blocks_b = [make_block(0, "ALFA sección"), make_block(1, "GAMMA sección"), make_block(2, "BETA sección")]

    pairs = comparison_pipeline._pair_blocks(blocks_a, blocks_b)

    assert pair_signature(pairs) == [
        ("ALFA sección", "ALFA sección", "matched"),
        (None, "GAMMA sección", "orphan_b"),
        ("BETA sección", "BETA sección", "reanchored"),
        ("GAMMA sección", None, "orphan_a"),
    ]


def test_heuristic_compare_pair_does_not_short_circuit_high_similarity_texts():
    block_a = make_block(0, "El proveedor entregará informe mensual con anexos y métricas de calidad.")
    block_b = make_block(0, "El proveedor entregará informe mensual con anexos y métricas de calidad")

    result = comparison_pipeline._heuristic_compare_pair(block_a, block_b)

    assert result is None


def test_compare_documents_reports_pairing_counters_and_row_pair_types(monkeypatch, tmp_path: Path):
    file_a = tmp_path / "a.txt"
    file_b = tmp_path / "b.txt"
    file_a.write_text("X\nY", encoding="utf-8")
    file_b.write_text("X\nNUEVO\nY", encoding="utf-8")

    def fake_extract_document_result(path: str, *, soffice_path=None, drop_headers=True, engine="auto"):
        text = Path(path).read_text(encoding="utf-8")
        return ExtractionResult(
            text=text,
            engine="builtin",
            quality_score=0.95,
            metadata={
                "source_format": "txt",
                "source_format_real": "txt",
                "conversion": {"applied": False},
                "engine_used": "builtin",
            },
            blocks=[],
            quality_signals={"block_count": 0},
        )

    monkeypatch.setattr(comparison_pipeline, "extract_document_result", fake_extract_document_result)
    monkeypatch.setattr(comparison_pipeline, "normalize_text", lambda text: text)
    monkeypatch.setattr(comparison_pipeline, "build_blocks", lambda text, *_args: [make_block(i, part) for i, part in enumerate(text.splitlines()) if part])
    monkeypatch.setattr(comparison_pipeline, "_persist_runtime_snapshot", lambda **_kwargs: None)

    result = comparison_pipeline.compare_documents(file_a, file_b, sid="sid-test", llm_client=PairingStubLLMClient())

    assert result.meta["pairing"] == {
        "strategy": "sequence_alignment",
        "pair_count": 3,
        "matched_pairs": 1,
        "orphan_a": 0,
        "orphan_b": 1,
        "reanchored_pairs": 1,
    }
    assert [row.pairing["pair_type"] for row in result.rows] == ["orphan_b"]
    assert result.rows[0].change_type == "añadido"
    assert result.meta["diagnostics"]["fallback_blocks"] == 0


def test_compare_documents_keeps_partial_result_when_pair_fails(monkeypatch, tmp_path: Path):
    file_a = tmp_path / "a.txt"
    file_b = tmp_path / "b.txt"
    file_a.write_text("X\nY", encoding="utf-8")
    file_b.write_text("X\nY ajustado", encoding="utf-8")

    def fake_extract_document_result(path: str, *, soffice_path=None, drop_headers=True, engine="auto"):
        text = Path(path).read_text(encoding="utf-8")
        return ExtractionResult(
            text=text,
            engine="builtin",
            quality_score=0.95,
            metadata={
                "source_format": "txt",
                "source_format_real": "txt",
                "conversion": {"applied": False},
                "engine_used": "builtin",
            },
            blocks=[],
            quality_signals={"block_count": 0},
        )

    monkeypatch.setattr(comparison_pipeline, "extract_document_result", fake_extract_document_result)
    monkeypatch.setattr(comparison_pipeline, "normalize_text", lambda text: text)
    monkeypatch.setattr(comparison_pipeline, "build_blocks", lambda text, *_args: [make_block(i, part) for i, part in enumerate(text.splitlines()) if part])
    monkeypatch.setattr(comparison_pipeline.settings, "compare_failed_blocks_error_ratio", 0.8)
    monkeypatch.setattr(comparison_pipeline, "_persist_runtime_snapshot", lambda **_kwargs: None)

    result = comparison_pipeline.compare_documents(file_a, file_b, sid="sid-partial", llm_client=FailingPairStubLLMClient())

    assert result.status == "done_with_warnings"
    assert result.meta["partial_result"] is True
    assert result.meta["cache"]["failed_blocks"] == 1
    assert result.meta["diagnostics"]["errors"] == [
        {
            "pair_id": "sid-partial-2",
            "stage": "compare_pair",
            "error_type": "RuntimeError",
            "message": "fallo controlado en pareja intermedia",
        }
    ]
    assert [row.pair_id for row in result.rows] == []


def test_compare_documents_keeps_original_rows_when_reconciliation_fails(monkeypatch, tmp_path: Path):
    file_a = tmp_path / "a.txt"
    file_b = tmp_path / "b.txt"
    file_a.write_text("X base\nY base", encoding="utf-8")
    file_b.write_text("X cambiado\nY cambiado", encoding="utf-8")

    def fake_extract_document_result(path: str, *, soffice_path=None, drop_headers=True, engine="auto"):
        text = Path(path).read_text(encoding="utf-8")
        return ExtractionResult(
            text=text,
            engine="builtin",
            quality_score=0.95,
            metadata={
                "source_format": "txt",
                "source_format_real": "txt",
                "conversion": {"applied": False},
                "engine_used": "builtin",
            },
            blocks=[],
            quality_signals={"block_count": 0},
        )

    monkeypatch.setattr(comparison_pipeline, "extract_document_result", fake_extract_document_result)
    monkeypatch.setattr(comparison_pipeline, "normalize_text", lambda text: text)
    monkeypatch.setattr(comparison_pipeline, "build_blocks", lambda text, *_args: [make_block(i, part) for i, part in enumerate(text.splitlines()) if part])
    monkeypatch.setattr(comparison_pipeline, "_persist_runtime_snapshot", lambda **_kwargs: None)
    monkeypatch.setattr(comparison_pipeline.settings, "compare_reconcile_with_llm", True)
    monkeypatch.setattr(comparison_pipeline.settings, "compare_reconcile_min_rows", 2)

    result = comparison_pipeline.compare_documents(
        file_a,
        file_b,
        sid="sid-reconcile",
        llm_client=ReconciliationFailingStubLLMClient(),
    )

    assert result.rows
    assert [row.pair_id for row in result.rows] == ["sid-reconcile-1", "sid-reconcile-2"]
    assert result.meta["diagnostics"]["reconciliation_failed"] is True
    assert result.meta["diagnostics"]["errors"] == [
        {
            "pair_id": "sid-reconcile-reconcile",
            "stage": "reconcile",
            "error_type": "RuntimeError",
            "message": "fallo controlado en reconciliación",
        }
    ]


def test_compare_documents_uses_local_fallback_when_llm_returns_empty_payload(monkeypatch, tmp_path: Path):
    file_a = tmp_path / "a.txt"
    file_b = tmp_path / "b.txt"
    file_a.write_text("Texto original", encoding="utf-8")
    file_b.write_text("Texto actualizado", encoding="utf-8")

    def fake_extract_document_result(path: str, *, soffice_path=None, drop_headers=True, engine="auto"):
        text = Path(path).read_text(encoding="utf-8")
        return ExtractionResult(
            text=text,
            engine="builtin",
            quality_score=0.95,
            metadata={
                "source_format": "txt",
                "source_format_real": "txt",
                "conversion": {"applied": False},
                "engine_used": "builtin",
            },
            blocks=[],
            quality_signals={"block_count": 0},
        )

    monkeypatch.setattr(comparison_pipeline, "extract_document_result", fake_extract_document_result)
    monkeypatch.setattr(comparison_pipeline, "normalize_text", lambda text: text)
    monkeypatch.setattr(comparison_pipeline, "build_blocks", lambda text, *_args: [make_block(i, part) for i, part in enumerate(text.splitlines()) if part])
    monkeypatch.setattr(comparison_pipeline, "_persist_runtime_snapshot", lambda **_kwargs: None)

    result = comparison_pipeline.compare_documents(
        file_a,
        file_b,
        sid="sid-fallback",
        llm_client=EmptyPayloadFallbackStubLLMClient(),
    )

    assert result.status == "done_with_warnings"
    assert len(result.rows) == 1
    assert result.rows[0].change_type == "modificado"
    assert result.meta["diagnostics"]["fallback_blocks"] == 1
    assert result.meta["diagnostics"]["errors"] == [
        {
            "pair_id": "sid-fallback-1",
            "stage": "compare_pair_fallback",
            "error_type": "LLMResponseError",
            "message": "Payload del LLM vacío.",
        }
    ]


def test_compare_documents_avoids_false_differences_for_equivalent_docx_and_pdf_text(monkeypatch, tmp_path: Path):
    file_a = tmp_path / "contrato.docx"
    file_b = tmp_path / "contrato.pdf"
    file_a.write_text("placeholder", encoding="utf-8")
    file_b.write_text("placeholder", encoding="utf-8")

    texts = {
        str(file_a): (
            "PRIMERA.- OBJETO DEL CONTRATO\n"
            "El PROVEEDOR diseñará y desarrollará un sitio web corporativo.\n\n"
            "SEGUNDA.- PROYECTO\n"
            "Auditoría SEO\n\n"
            "Investigación del mercado en entornos digitales\n\n"
            "Diseño y desarrollo web"
        ),
        str(file_b): (
            "I.\n"
            "PRIMERA.- OBJETO DEL CONTRATO\n"
            "El PROVEEDOR diseñará y desarrollará un sitio web corporativo.\n\n"
            "II.\n"
            "SEGUNDA.- PROYECTO\n"
            "1.\n"
            "Auditoría SEO\n\n"
            "2.\n"
            "Investigación del mercado en entornos digitales\n\n"
            "3.\n"
            "Diseño y desarrollo web"
        ),
    }

    def fake_extract_document_result(path: str, *, soffice_path=None, drop_headers=True, engine="auto"):
        return ExtractionResult(
            text=texts[str(path)],
            engine="builtin",
            quality_score=0.95,
            metadata={
                "source_format": Path(path).suffix.lstrip("."),
                "source_format_real": Path(path).suffix.lstrip("."),
                "conversion": {"applied": False},
                "engine_used": "builtin",
            },
            blocks=[],
            quality_signals={"block_count": 0},
        )

    monkeypatch.setattr(comparison_pipeline, "extract_document_result", fake_extract_document_result)
    monkeypatch.setattr(comparison_pipeline, "_persist_runtime_snapshot", lambda **_kwargs: None)

    result = comparison_pipeline.compare_documents(
        file_a,
        file_b,
        sid="sid-equivalente",
        llm_client=PairingStubLLMClient(),
    )

    assert result.status == "done"
    assert result.rows == []
    assert result.meta["pairing"]["pair_count"] == 1
