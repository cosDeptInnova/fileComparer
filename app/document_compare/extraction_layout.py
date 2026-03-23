from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from statistics import mean
from typing import Any


_STRUCTURAL_BREAK_TYPES = {"page_break", "section_break"}
_TEXTUAL_TYPES = {"paragraph", "line", "list_item", "table_row", "header", "footer"}
_NOISE_TYPES = {"header", "footer"}
_PARAGRAPH_LIKE_TYPES = {"paragraph", "list_item", "table_row", "header", "footer"}


@dataclass(slots=True)
class ExtractionBlock:
    text: str
    page: int | None
    block_type: str
    source_engine: str
    bbox: tuple[float, float, float, float] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def normalized_text(self) -> str:
        return " ".join(str(self.text or "").split()).strip()

    def is_structural_break(self) -> bool:
        return self.block_type in _STRUCTURAL_BREAK_TYPES

    def should_drop_from_canonical(self) -> bool:
        return bool((self.metadata or {}).get("drop_in_canonical"))


@dataclass(slots=True)
class ExtractionLayout:
    blocks: list[ExtractionBlock]
    source_engine: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def canonical_blocks(self) -> list[ExtractionBlock]:
        preferred: list[ExtractionBlock] = []
        fallback: list[ExtractionBlock] = []
        for block in self.blocks:
            normalized = block.normalized_text()
            if normalized or block.is_structural_break():
                fallback.append(block)
            if block.should_drop_from_canonical():
                continue
            if normalized or block.is_structural_break():
                preferred.append(block)
        return preferred or fallback

    def canonical_text(self) -> str:
        segments: list[str] = []
        pending_break = False
        previous_block_type: str | None = None

        for block in self.canonical_blocks():
            if block.is_structural_break():
                pending_break = True
                continue

            normalized = block.normalized_text()
            if not normalized:
                pending_break = True
                continue

            if pending_break and segments:
                segments.append("")
            elif (
                segments
                and previous_block_type in _PARAGRAPH_LIKE_TYPES
                and block.block_type in _PARAGRAPH_LIKE_TYPES
            ):
                segments.append("")
            segments.append(normalized)
            pending_break = False
            previous_block_type = block.block_type

        return "\n".join(segments).strip()

    def quality_signals(self) -> dict[str, Any]:
        canonical_blocks = self.canonical_blocks()
        textual_blocks = [block for block in canonical_blocks if block.normalized_text()]
        all_textual_blocks = [block for block in self.blocks if block.normalized_text()]
        counts = Counter(block.block_type for block in all_textual_blocks)

        lengths = [len(block.normalized_text()) for block in textual_blocks]
        word_counts = [len(block.normalized_text().split()) for block in textual_blocks]
        duplicate_ratio = _duplicate_ratio(block.normalized_text() for block in textual_blocks)

        total_textual = max(1, len(textual_blocks))
        table_like_density = counts.get("table_row", 0) / max(1, len(all_textual_blocks))
        short_line_density = (
            sum(1 for size in lengths if size <= 24) / total_textual
            if lengths
            else 0.0
        )
        noise_density = (
            sum(1 for block in textual_blocks if block.block_type in _NOISE_TYPES) / total_textual
            if textual_blocks
            else 0.0
        )
        low_alpha_density = (
            sum(1 for block in textual_blocks if _alpha_ratio(block.normalized_text()) < 0.55) / total_textual
            if textual_blocks
            else 0.0
        )
        layout_noise_score = round(
            min(
                1.0,
                short_line_density * 0.35
                + noise_density * 0.25
                + table_like_density * 0.15
                + duplicate_ratio * 0.15
                + low_alpha_density * 0.10,
            ),
            4,
        )

        has_repeated_headers = any(
            (block.metadata or {}).get("is_repeated_header") or (block.metadata or {}).get("is_repeated_footer")
            for block in self.blocks
        )

        return {
            "source_engine": self.source_engine,
            "block_count": len(self.blocks),
            "canonical_block_count": len(textual_blocks),
            "avg_line_length": round(mean(lengths), 2) if lengths else 0.0,
            "avg_words_per_block": round(mean(word_counts), 2) if word_counts else 0.0,
            "table_like_density": round(table_like_density, 4),
            "list_item_density": round(counts.get("list_item", 0) / max(1, len(all_textual_blocks)), 4),
            "header_footer_density": round(
                (counts.get("header", 0) + counts.get("footer", 0)) / max(1, len(all_textual_blocks)),
                4,
            ),
            "duplicate_block_ratio": round(duplicate_ratio, 4),
            "layout_noise_score": layout_noise_score,
            "has_repeated_headers": has_repeated_headers,
            "block_type_counts": dict(counts),
        }


def _duplicate_ratio(texts: Any) -> float:
    normalized = [text for text in texts if text]
    if not normalized:
        return 0.0
    counts = Counter(normalized)
    duplicated = sum(count - 1 for count in counts.values() if count > 1)
    return duplicated / len(normalized)


def _alpha_ratio(text: str) -> float:
    if not text:
        return 0.0
    alpha = sum(1 for char in text if char.isalpha())
    return alpha / len(text)
