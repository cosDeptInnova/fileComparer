from __future__ import annotations

import re
from dataclasses import dataclass

SENTENCE_RE = re.compile(r"(?<=[.!?;:])\s+")
NUMBERING_ONLY_SEGMENT_RE = re.compile(
    r"(?i)^(?:[ivxlcdm]+[\)\.]|\(?\d+(?:\.\d+)*[\)\.]|[a-z]\))$"
)
HEADING_RE = re.compile(r"^(?:\d+(?:\.\d+){0,3}\s+)?[A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ0-9 ,:;()/-]{4,}$")
SHORT_SEGMENT_JOIN_THRESHOLD = 10
WORD_RE = re.compile(r"\S+")
WORDS_PER_BLOCK_MIN = 250
WORDS_PER_BLOCK_MAX = 300
LONG_UNIT_WORD_LIMIT = 320


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

    paragraph_chunks = [chunk.strip() for chunk in re.split(r"\n{2,}", cleaned) if chunk.strip()]
    segments: list[str] = []
    source_segments = paragraph_chunks if paragraph_chunks else [cleaned]
    for source in source_segments:
        pieces = _paragraph_pieces(source)
        segments.extend(_merge_short_prefix_segments(pieces))
    return segments


def _paragraph_pieces(source: str) -> list[str]:
    lines = [line.strip() for line in re.split(r"\n+", source) if line.strip()]
    if not lines:
        return []
    pieces: list[str] = []
    for line in lines:
        if NUMBERING_ONLY_SEGMENT_RE.fullmatch(line):
            continue
        if HEADING_RE.match(line):
            pieces.append(line)
            continue
        sentence_like = [piece.strip() for piece in SENTENCE_RE.split(line) if piece.strip()]
        if sentence_like:
            pieces.extend(sentence_like)
    return pieces


def _merge_short_prefix_segments(segments: list[str]) -> list[str]:
    merged: list[str] = []
    pending_prefix = ""
    for segment in segments:
        current = segment.strip()
        if not current:
            continue
        if NUMBERING_ONLY_SEGMENT_RE.fullmatch(current):
            continue
        if pending_prefix:
            current = f"{pending_prefix} {current}".strip()
            pending_prefix = ""
        normalized = current.rstrip()
        if len(normalized) <= SHORT_SEGMENT_JOIN_THRESHOLD:
            pending_prefix = normalized
            continue
        merged.append(normalized)
    if pending_prefix:
        if merged:
            merged[-1] = f"{merged[-1]} {pending_prefix}".strip()
        else:
            merged.append(pending_prefix)
    return merged


def _word_count(text: str) -> int:
    return len(WORD_RE.findall(text or ""))


def _split_long_unit(unit: str, max_words: int) -> list[str]:
    words = WORD_RE.findall(unit or "")
    if len(words) <= max_words:
        return [unit.strip()] if unit.strip() else []
    return [" ".join(words[index : index + max_words]).strip() for index in range(0, len(words), max_words)]


def _target_word_budget(target_chars: int) -> tuple[int, int]:
    normalized_chars = max(1, target_chars)
    if normalized_chars >= 1200:
        derived_target = max(WORDS_PER_BLOCK_MIN, min(WORDS_PER_BLOCK_MAX, round(normalized_chars / 5)))
        return derived_target, max(derived_target, WORDS_PER_BLOCK_MAX)
    derived_target = max(24, round(normalized_chars / 5))
    return derived_target, max(derived_target + 12, round(normalized_chars / 4))


def build_blocks(text: str, target_chars: int, overlap_chars: int) -> list[TextBlock]:
    del overlap_chars
    units = sentence_segments(text)
    if not units:
        return []

    min_words, max_words = _target_word_budget(target_chars)
    normalized_units: list[tuple[str, int, int, int]] = []
    cursor = 0
    for unit in units:
        for piece in _split_long_unit(unit, LONG_UNIT_WORD_LIMIT):
            start = text.find(piece, cursor)
            if start < 0:
                start = cursor
            end = start + len(piece)
            normalized_units.append((piece, start, end, _word_count(piece)))
            cursor = end

    blocks: list[TextBlock] = []
    idx = 0
    pointer = 0
    while pointer < len(normalized_units):
        collected: list[str] = []
        start_char = normalized_units[pointer][1]
        end_char = normalized_units[pointer][2]
        word_total = 0
        next_pointer = pointer

        while next_pointer < len(normalized_units):
            piece, _, piece_end, piece_words = normalized_units[next_pointer]
            projected_words = word_total + piece_words
            if collected and projected_words > max_words and word_total >= min_words:
                break
            collected.append(piece)
            word_total = projected_words
            end_char = piece_end
            next_pointer += 1
            if word_total >= max_words:
                break

        block_text = " ".join(collected).strip()
        blocks.append(TextBlock(index=idx, text=block_text, start_char=start_char, end_char=end_char))
        idx += 1
        pointer = next_pointer

    return blocks
