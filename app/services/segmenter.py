from __future__ import annotations

import re
from dataclasses import dataclass

SENTENCE_RE = re.compile(r"(?<=[.!?;:])\s+(?=[A-ZÁÉÍÓÚÑ0-9])")


@dataclass(slots=True)
class TextBlock:
    index: int
    text: str
    start_char: int
    end_char: int


def sentence_segments(text: str) -> list[str]:
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    pieces = [piece.strip() for piece in SENTENCE_RE.split(cleaned) if piece.strip()]
    if len(pieces) == 1:
        return [segment.strip() for segment in re.split(r"\n+", cleaned) if segment.strip()]
    return pieces


def build_blocks(text: str, target_chars: int, overlap_chars: int) -> list[TextBlock]:
    sentences = sentence_segments(text)
    if not sentences:
        return []

    blocks: list[TextBlock] = []
    sentence_positions: list[tuple[str, int, int]] = []
    cursor = 0
    for sentence in sentences:
        start = text.find(sentence, cursor)
        if start < 0:
            start = cursor
        end = start + len(sentence)
        sentence_positions.append((sentence, start, end))
        cursor = end

    idx = 0
    pointer = 0
    while pointer < len(sentence_positions):
        start_char = sentence_positions[pointer][1]
        collected: list[str] = []
        end_char = start_char
        next_pointer = pointer
        while next_pointer < len(sentence_positions):
            sentence, _, sentence_end = sentence_positions[next_pointer]
            projected = " ".join(collected + [sentence]).strip()
            if collected and len(projected) > target_chars:
                break
            collected.append(sentence)
            end_char = sentence_end
            next_pointer += 1
        block_text = " ".join(collected).strip()
        blocks.append(
            TextBlock(index=idx, text=block_text, start_char=start_char, end_char=end_char)
        )
        idx += 1
        if next_pointer >= len(sentence_positions):
            break
        overlap_start = next_pointer
        while (
            overlap_start > pointer
            and sentence_positions[next_pointer - 1][2]
            - sentence_positions[overlap_start - 1][1]
            < overlap_chars
        ):
            overlap_start -= 1
        pointer = max(pointer + 1, overlap_start)
    return blocks
