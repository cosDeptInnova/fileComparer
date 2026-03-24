from pathlib import Path

from app.extractors import ExtractionResult
from app.llm_client import LLMResponseError
from app.schemas import LLMComparisonResponse
from app.services import comparison_pipeline


class CountingStubLLMClient:
    model_name = "stub-llm"

    def __init__(self):
        self.calls = 0

    def compare(self, messages):
        self.calls += 1
        user_payload = messages[-1]["content"]
        if '"block_a": {"index": null' in user_payload:
            return LLMComparisonResponse.model_validate(
                {
                    "changes": [
                        {
                            "change_type": "añadido",
                            "source_a": "",
                            "source_b": "extra",
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
        return LLMComparisonResponse.model_validate(
            {
                "changes": [
                    {
                        "change_type": "modificado",
                        "source_a": "A",
                        "source_b": "B",
                        "summary": "bloque modificado",
                    }
                ]
            }
        )


class EmptyPayloadFallbackStubLLMClient(CountingStubLLMClient):
    def compare(self, messages):
        raise LLMResponseError("Payload del LLM vacío.")


def _fake_extract_document_result(path: str, *, soffice_path=None, drop_headers=True, engine="auto"):
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


def test_build_fixed_chunks_splits_in_250_char_windows():
    text = "a" * 520

    chunks = comparison_pipeline._build_fixed_chunks(text, 250)

    assert [len(chunk.text) for chunk in chunks] == [250, 250, 20]
    assert chunks[1].start_char == 250
    assert chunks[2].end_char == 520


def test_compare_documents_uses_fixed_pair_size_and_orphans(monkeypatch, tmp_path: Path):
    file_a = tmp_path / "a.txt"
    file_b = tmp_path / "b.txt"
    file_a.write_text("A" * 300, encoding="utf-8")
    file_b.write_text("A" * 560, encoding="utf-8")

    monkeypatch.setattr(comparison_pipeline, "extract_document_result", _fake_extract_document_result)
    monkeypatch.setattr(comparison_pipeline, "_persist_runtime_snapshot", lambda **_kwargs: None)
    monkeypatch.setattr(comparison_pipeline.settings, "compare_pair_chars", 250)

    result = comparison_pipeline.compare_documents(file_a, file_b, sid="sid-fixed", llm_client=CountingStubLLMClient())

    assert result.meta["pairing"]["strategy"] == "fixed_size_pairing"
    assert result.meta["pairing"]["pair_count"] == 3
    assert result.meta["pairing"]["orphan_b"] == 1
    assert result.meta["segmentation"]["pair_chars"] == 250


def test_compare_documents_caches_repeated_pairs(monkeypatch, tmp_path: Path):
    repeated = "M" * 250 + "N" * 250
    file_a = tmp_path / "a.txt"
    file_b = tmp_path / "b.txt"
    file_a.write_text(repeated + repeated, encoding="utf-8")
    file_b.write_text(("Z" * 250 + "Y" * 250) * 2, encoding="utf-8")

    client = CountingStubLLMClient()
    monkeypatch.setattr(comparison_pipeline, "extract_document_result", _fake_extract_document_result)
    monkeypatch.setattr(comparison_pipeline, "_persist_runtime_snapshot", lambda **_kwargs: None)
    monkeypatch.setattr(comparison_pipeline.settings, "compare_pair_chars", 250)

    result = comparison_pipeline.compare_documents(file_a, file_b, sid="sid-cache", llm_client=client)

    assert result.meta["cache"]["resolved_from_cache"] == 2
    assert client.calls == 2


def test_compare_documents_uses_local_fallback_when_llm_returns_empty_payload(monkeypatch, tmp_path: Path):
    file_a = tmp_path / "a.txt"
    file_b = tmp_path / "b.txt"
    file_a.write_text("Texto original", encoding="utf-8")
    file_b.write_text("Texto actualizado", encoding="utf-8")

    monkeypatch.setattr(comparison_pipeline, "extract_document_result", _fake_extract_document_result)
    monkeypatch.setattr(comparison_pipeline, "_persist_runtime_snapshot", lambda **_kwargs: None)

    result = comparison_pipeline.compare_documents(
        file_a,
        file_b,
        sid="sid-fallback",
        llm_client=EmptyPayloadFallbackStubLLMClient(),
    )

    assert result.status in {"done", "done_with_warnings"}
    if result.status == "done_with_warnings":
        assert result.meta["diagnostics"]["fallback_blocks"] == 1
        assert result.meta["diagnostics"]["errors"] == [
            {
                "pair_id": "sid-fallback-1",
                "stage": "compare_pair_fallback",
                "error_type": "LLMResponseError",
                "message": "Payload del LLM vacío.",
            }
        ]
