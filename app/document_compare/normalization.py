from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

_SPACE_RX = re.compile(r"[ \t\f\v]+")
_LINE_SPACE_RX = re.compile(r" *\n *")
_MULTI_NL_RX = re.compile(r"\n{3,}")
_HYPHEN_BREAK_RX = re.compile(r"(?<=\w)[\-\u00ad]\n(?=\w)")
_BULLET_PREFIX_RX = re.compile(r"^(?:[\-–—•▪◦·●○■□]+|(?:\(?\d+[\)\.]|[a-z]\)|[ivxlcdm]+\.)\s+)", re.IGNORECASE)
_INLINE_LIST_BREAK_RX = re.compile(r"(?<=[\.\;\:\)])\s+(?=(?:[\-–—•▪◦·●○■□]+|(?:\(?\d+(?:\.\d+)*[\)\.]|[a-z]\)|[ivxlcdm]+[\)\.]))\s+)", re.IGNORECASE)
_STRUCTURAL_PREFIX_RX = re.compile(r"^(?:[\-–—•▪◦·●○■□]+|(?:\(?\d+(?:\.\d+)*[\)\.]|[a-z]\)|[ivxlcdm]+[\)\.]))\s+", re.IGNORECASE)
_PAGE_RX = re.compile(r"\f|\n\s*page\s+\d+(?:\s+of\s+\d+)?\s*\n", re.IGNORECASE)
_INLINE_LAYOUT_MARKER_RX = re.compile(
    r"(?:\[\s*(?:p[aá]gina|pagina|page)\s+\d+(?:\s+(?:de|of)\s+\d+)?\s*\]\s*)?"
    r"(?:p[aá]gina|pagina|page)\s+\d+(?:\s+(?:de|of)\s+\d+)?",
    re.IGNORECASE,
)
_BRACKET_LAYOUT_MARKER_RX = re.compile(
    r"\[\s*(?:p[aá]gina|pagina|page|slide|sheet)(?:[^\]\n]*)\]",
    re.IGNORECASE,
)
_LINE_LAYOUT_ONLY_RX = re.compile(
    r"^\s*(?:"
    r"(?:p[aá]gina|pagina|page)\s+\d+(?:\s+(?:de|of)\s+\d+)?"
    r"|slide\s+\d+"
    r"|sheet(?:\s*[:\-]\s*.*)?"
    r")\s*$",
    re.IGNORECASE,
)
_SENTENCE_BOUNDARY_RX = re.compile(r"(?<=[\.!?;:])\s+(?=(?:[A-ZÁÉÍÓÚÑ0-9]|[\(\[\-•▪◦·]))")
_CLAUSE_BOUNDARY_RX = re.compile(r"\s+(?=(?:\d+(?:\.\d+)*[\)\.]|[A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s]{4,}:)\s)")


@dataclass(slots=True)
class NormalizedText:
    original: str
    normalized: str
    canonical: str


def strip_layout_metadata(text: str) -> str:
    raw = unicodedata.normalize("NFKC", str(text or ""))
    raw = raw.replace("\u00a0", " ").replace("\u200b", "")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw = _PAGE_RX.sub("\n", raw)
    raw = _INLINE_LAYOUT_MARKER_RX.sub("\n", raw)
    raw = _BRACKET_LAYOUT_MARKER_RX.sub("\n", raw)

    cleaned_lines: list[str] = []
    for line in raw.split("\n"):
        stripped = line.strip()
        if stripped and _LINE_LAYOUT_ONLY_RX.fullmatch(stripped):
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def normalize_text(text: str) -> NormalizedText:
    raw = strip_layout_metadata(text)
    raw = _normalize_unicode_equivalents(raw)
    raw = _HYPHEN_BREAK_RX.sub("", raw)
    raw = _INLINE_LIST_BREAK_RX.sub("\n", raw)
    raw = _SPACE_RX.sub(" ", raw)
    raw = _LINE_SPACE_RX.sub("\n", raw)
    raw = _remove_repeated_layout_lines(raw)
    raw = _normalize_lines(raw)
    raw = _MULTI_NL_RX.sub("\n\n", raw)
    normalized = raw.strip()
    canonical = canonicalize_for_comparison(normalized)
    return NormalizedText(original=text or "", normalized=normalized, canonical=canonical)


def tokenize_words(text: str) -> list[str]:
    return re.findall(r"\w+|[^\w\s]", text or "", flags=re.UNICODE)


def canonicalize_for_comparison(text: str) -> str:
    normalized = normalize_structural_markers(text)
    return re.sub(r"\s+", " ", normalized.casefold()).strip()


def normalize_structural_markers(text: str) -> str:
    cleaned_lines: list[str] = []
    for line in str(text or "").split("\n"):
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append("")
            continue
        while True:
            normalized = _STRUCTURAL_PREFIX_RX.sub("", stripped, count=1).strip()
            if normalized == stripped:
                break
            stripped = normalized
        cleaned_lines.append(stripped)
    normalized = "\n".join(cleaned_lines)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def paragraph_blocks(text: str, *, max_words: int = 180, min_words: int = 24) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", text or "") if part.strip()]
    if not paragraphs:
        return []

    blocks: list[str] = []
    fragment_buffer: list[str] = []
    fragment_words = 0

    for paragraph in paragraphs:
        paragraph_words = len(tokenize_words(paragraph))
        units = [paragraph] if paragraph_words <= max_words else _semantic_units_from_paragraph(paragraph, max_words=max_words)
        if len(units) == 1 and paragraph_words <= max_words:
            if fragment_buffer:
                blocks.append("\n\n".join(fragment_buffer).strip())
                fragment_buffer = []
                fragment_words = 0
            blocks.append(paragraph)
            continue

        for unit in units:
            word_count = len(tokenize_words(unit))
            if not word_count:
                continue
            if fragment_buffer and fragment_words + word_count > max_words:
                blocks.append("\n\n".join(fragment_buffer).strip())
                fragment_buffer = []
                fragment_words = 0
            fragment_buffer.append(unit)
            fragment_words += word_count
            if fragment_words >= min_words:
                blocks.append("\n\n".join(fragment_buffer).strip())
                fragment_buffer = []
                fragment_words = 0

    if fragment_buffer:
        blocks.append("\n\n".join(fragment_buffer).strip())
    return [block for block in blocks if block]


def _normalize_unicode_equivalents(text: str) -> str:
    replacements = {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\u2022": "•",
        "\u25aa": "•",
        "\u25e6": "•",
        "\u00b7": "•",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\ufb01": "fi",
        "\ufb02": "fl",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text


def _remove_repeated_layout_lines(text: str) -> str:
    lines = [line.rstrip() for line in text.split("\n")]
    normalized_counts: Counter[str] = Counter()
    for line in lines:
        key = _repetition_key(line)
        if key:
            normalized_counts[key] += 1

    repeated = {
        key
        for key, count in normalized_counts.items()
        if count >= 2 and len(key.split()) <= 12 and not _looks_like_contentful_sentence(key)
    }
    if not repeated:
        return text

    kept: list[str] = []
    for line in lines:
        key = _repetition_key(line)
        if key and key in repeated:
            continue
        kept.append(line)
    return "\n".join(kept)


def _repetition_key(line: str) -> str:
    stripped = re.sub(r"\d+", "#", (line or "").strip().casefold())
    stripped = re.sub(r"\s+", " ", stripped)
    return stripped if len(stripped) >= 3 else ""


def _looks_like_contentful_sentence(line: str) -> bool:
    words = re.findall(r"\w+", line, flags=re.UNICODE)
    return len(words) >= 8 and any(ch in line for ch in ".;:")


def _normalize_lines(text: str) -> str:
    result: list[str] = []
    lines = [line.strip() for line in text.split("\n")]
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line:
            if result and result[-1] != "":
                result.append("")
            i += 1
            continue

        line = _normalize_bullet_prefix(line)
        combined = line
        while i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if not nxt:
                break
            nxt = _normalize_bullet_prefix(nxt)
            if _should_join_lines(combined, nxt):
                combined = _join_lines(combined, nxt)
                i += 1
            else:
                break
        result.append(combined)
        i += 1

    normalized = "\n".join(result)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized


def _normalize_bullet_prefix(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return ""
    if _BULLET_PREFIX_RX.match(stripped):
        body = _BULLET_PREFIX_RX.sub("", stripped, count=1).strip()
        return f"- {body}" if body else ""
    return stripped


def _should_join_lines(current: str, nxt: str) -> bool:
    if not current or not nxt:
        return False
    if current.endswith((".", ":", ";", "?", "!")):
        return False
    if current.endswith("-"):
        return True
    if current.startswith("-") or nxt.startswith("-"):
        return False
    if re.search(r"\b(?:art\.|cl[aá]usula|anexo|apartado|section|chapter)\s+$", current.casefold()):
        return True
    if nxt[:1].islower() or nxt[:1].isdigit() or nxt[:1] in ")],;:":
        return True
    if len(tokenize_words(current)) <= 6:
        return True
    return False


def _join_lines(current: str, nxt: str) -> str:
    if current.endswith("-"):
        return current[:-1].rstrip() + nxt.lstrip()
    if current.endswith("/"):
        return current + nxt.lstrip()
    return f"{current.rstrip()} {nxt.lstrip()}".strip()


def _semantic_units_from_paragraph(paragraph: str, *, max_words: int) -> list[str]:
    cleaned = paragraph.strip()
    if not cleaned:
        return []
    if len(tokenize_words(cleaned)) <= max_words:
        return [cleaned]

    pieces: list[str] = []
    for clause in _CLAUSE_BOUNDARY_RX.split(cleaned):
        clause = clause.strip()
        if not clause:
            continue
        sentence_parts = [part.strip() for part in _SENTENCE_BOUNDARY_RX.split(clause) if part.strip()]
        if not sentence_parts:
            continue
        accumulator: list[str] = []
        acc_words = 0
        for sentence in sentence_parts:
            word_count = len(tokenize_words(sentence))
            if accumulator and acc_words + word_count > max_words:
                pieces.append(" ".join(accumulator).strip())
                accumulator = []
                acc_words = 0
            accumulator.append(sentence)
            acc_words += word_count
        if accumulator:
            pieces.append(" ".join(accumulator).strip())
    return pieces or [cleaned]