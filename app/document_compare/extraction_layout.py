from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from statistics import mean
from typing import Any


BBox = tuple[float, float, float, float]


@dataclass(slots=True)
class ExtractionBlock:
    text: str
    page: int | None
    block_type: str
    source_engine: str
    bbox: BBox | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "page": self.page,
            "block_type": self.block_type,
            "bbox": list(self.bbox) if self.bbox else None,
            "source_engine": self.source_engine,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class ExtractionLayout:
    blocks: list[ExtractionBlock]
    source_engine: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def canonical_text(self) -> str:
        return canonical_text_from_blocks(self.blocks)

    def quality_signals(self) -> dict[str, Any]:
        return compute_quality_signals(self.blocks)


_BLOCK_SEPARATOR_MAP = {
    "paragraph": "\n\n",
    "list_item": "\n",
    "line": "\n",
    "header": "\n",
    "footer": "\n",
    "table_row": "\n",
    "page": "\n\n",
    "page_break": "\n\n",
    "section_break": "\n\n",
}


def canonical_text_from_blocks(blocks: list[ExtractionBlock]) -> str:
    parts: list[str] = []
    previous_type: str | None = None
    previous_page: int | None = None

    for block in blocks:
        text = _normalize_block_text(block.text)
        if not text:
            continue
        if block.metadata.get("drop_in_canonical"):
            continue
        separator = _separator_for_block(block_type=block.block_type, previous_type=previous_type, page=block.page, previous_page=previous_page)
        if separator and parts:
            parts.append(separator)
        if block.block_type in {"list_item", "table_row"}:
            prefix = "- " if block.block_type == "list_item" else "| "
            rendered = prefix + text
        elif block.block_type in {"page", "page_break"}:
            rendered = f"[PAGE {block.page}]" if block.page is not None else "[PAGE]"
        elif block.block_type == "section_break":
            rendered = "[SECTION BREAK]"
        else:
            rendered = text
        parts.append(rendered)
        previous_type = block.block_type
        previous_page = block.page

    return "".join(parts).strip()


def compute_quality_signals(blocks: list[ExtractionBlock]) -> dict[str, Any]:
    text_blocks = [block for block in blocks if block.text.strip() and block.block_type not in {"page", "page_break", "section_break"}]
    lines = [block for block in blocks if block.block_type == "line" and block.text.strip()]
    header_blocks = [block for block in blocks if block.block_type == "header" and block.text.strip()]
    footer_blocks = [block for block in blocks if block.block_type == "footer" and block.text.strip()]
    table_rows = [block for block in blocks if block.block_type == "table_row" and block.text.strip()]

    avg_line_length = round(mean(len(block.text.strip()) for block in lines), 2) if lines else 0.0
    avg_block_length = round(mean(len(block.text.strip()) for block in text_blocks), 2) if text_blocks else 0.0
    repeated_headers = _count_repeated_groups(header_blocks)
    repeated_footers = _count_repeated_groups(footer_blocks)
    table_like_density = round(len(table_rows) / max(1, len(text_blocks)), 4)
    short_line_ratio = round(sum(1 for block in lines if len(block.text.strip()) <= 40) / max(1, len(lines)), 4)
    line_block_ratio = round(len(lines) / max(1, len(text_blocks)), 4)
    layout_noise_score = round(min(1.0, (short_line_ratio * 0.45) + (table_like_density * 0.35) + (0.2 if repeated_headers or repeated_footers else 0.0) + (0.15 if line_block_ratio > 0.65 else 0.0)), 4)

    return {
        "has_repeated_headers": bool(repeated_headers),
        "has_repeated_footers": bool(repeated_footers),
        "table_like_density": table_like_density,
        "avg_line_length": avg_line_length,
        "avg_block_length": avg_block_length,
        "layout_noise_score": layout_noise_score,
        "line_block_ratio": line_block_ratio,
        "short_line_ratio": short_line_ratio,
        "repeated_header_groups": repeated_headers,
        "repeated_footer_groups": repeated_footers,
        "total_blocks": len(blocks),
        "text_block_count": len(text_blocks),
        "line_count": len(lines),
        "table_row_count": len(table_rows),
        "page_count": len({block.page for block in blocks if block.page is not None}),
    }


def _separator_for_block(*, block_type: str, previous_type: str | None, page: int | None, previous_page: int | None) -> str:
    if previous_page is not None and page is not None and page != previous_page:
        return "\n\n"
    if previous_type in {"page", "page_break", "section_break"}:
        return "\n\n"
    return _BLOCK_SEPARATOR_MAP.get(block_type, "\n")


def _normalize_block_text(text: str) -> str:
    cleaned = "\n".join(part.strip() for part in str(text or "").splitlines())
    return cleaned.strip()


def _count_repeated_groups(blocks: list[ExtractionBlock]) -> int:
    if not blocks:
        return 0
    counts: Counter[str] = Counter(_repetition_key(block.text) for block in blocks if _repetition_key(block.text))
    return sum(1 for count in counts.values() if count >= 2)


def _repetition_key(text: str) -> str:
    normalized = " ".join(str(text or "").casefold().split())
    normalized = "".join("#" if ch.isdigit() else ch for ch in normalized)
    return normalized if len(normalized) >= 3 else ""