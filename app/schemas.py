from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


ChangeType = Literal["añadido", "eliminado", "modificado"]


class DiffSegment(BaseModel):
    type: Literal["equal", "insert", "delete", "replace"] = "equal"
    text: str = ""


class ChangeRow(BaseModel):
    block_id: int
    pair_id: str
    text_a: str = ""
    text_b: str = ""
    display_text_a: str = ""
    display_text_b: str = ""
    display_segments_a: list[DiffSegment] = Field(default_factory=list)
    display_segments_b: list[DiffSegment] = Field(default_factory=list)
    change_type: ChangeType
    confidence: str = "media"
    severity: str = "media"
    summary: str = ""
    llm_comment: str = ""
    chunk_index_a: int = 0
    chunk_index_b: int = 0
    offset_start_a: int = 0
    offset_end_a: int = 0
    offset_start_b: int = 0
    offset_end_b: int = 0
    result_origin: str = ""
    llm_success: bool = False
    cache_hit: bool = False
    fallback_applied: bool = False
    decision_source: str = ""
    model_name: str = ""
    related_block_ids: list[int] = Field(default_factory=list)
    pairing: dict[str, Any] = Field(default_factory=dict)
    source_spans: dict[str, Any] = Field(default_factory=dict)


class ComparisonResult(BaseModel):
    sid: str
    status: str
    progress: dict[str, Any]
    rows: list[ChangeRow]
    ok: bool = True
    error: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class LLMChange(BaseModel):
    change_type: ChangeType
    source_a: str = ""
    source_b: str = ""
    summary: str = ""
    confidence: Literal["baja", "media", "alta"] = "media"
    severity: Literal["baja", "media", "alta", "critica"] = "media"
    evidence: str = ""
    anchor_a: int | None = None
    anchor_b: int | None = None


class LLMComparisonResponse(BaseModel):
    changes: list[LLMChange] = Field(default_factory=list)


class ExtractedDocument(BaseModel):
    filename: str
    extension: str
    raw_text: str
    clean_text: str
    blocks: list[str]
    metadata: dict[str, Any] = Field(default_factory=dict)
