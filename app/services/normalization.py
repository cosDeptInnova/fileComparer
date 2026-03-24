from __future__ import annotations

import re
from collections import Counter

LINE_BREAK_HYPHEN_RE = re.compile(r"(\w)-\n(\w)")
WHITESPACE_RE = re.compile(r"[\t\r\f\v ]+")
MULTI_NEWLINE_RE = re.compile(r"\n{2,}")
BULLET_RE = re.compile(
    r"(?m)^\s*(?:[-–—•●◦▪■□]+|\(?\d+(?:\.\d+)*[\)\.]|[a-z]\)|[ivxlcdm]+[\)\.])\s+"
)
PAGE_LABEL_RE = re.compile(r"(?im)^\s*(?:page|página|pagina|hoja)\s+\d+\s*$")
OCR_NOISE_RE = re.compile(r"(?m)^[^\w\n]{3,}$")
SOFT_HYPHEN_RE = re.compile("\u00ad")
MULTI_PUNCT_RE = re.compile(r"([,.;:])\1+")
MULTI_DASH_RE = re.compile(r"[–—]{2,}")
SENTENCE_SPACE_RE = re.compile(r"\s+([,.;:])")
NUMBERING_ONLY_LINE_RE = re.compile(
    r"(?im)^\s*(?:[ivxlcdm]+[\)\.]|\(?\d+(?:\.\d+)*[\)\.]|[a-z]\))\s*$"
)
PARAGRAPH_BREAK_TOKEN = "\uE000PARA\uE000"


def _drop_repeated_edge_lines(lines: list[str]) -> list[str]:
    if len(lines) < 12:
        return lines
    head_counts = Counter(line for line in lines[: min(12, len(lines))] if len(line) > 3)
    tail_counts = Counter(line for line in lines[-min(12, len(lines)) :] if len(line) > 3)
    repeated = {line for line, count in (head_counts + tail_counts).items() if count > 1}
    return [line for line in lines if line not in repeated]


def normalize_text(raw_text: str) -> str:
    text = raw_text or ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = SOFT_HYPHEN_RE.sub("", text)
    text = LINE_BREAK_HYPHEN_RE.sub(r"\1\2", text)
    text = MULTI_NEWLINE_RE.sub(f"\n{PARAGRAPH_BREAK_TOKEN}\n", text)
    text = NUMBERING_ONLY_LINE_RE.sub("", text)
    text = BULLET_RE.sub("", text)
    text = PAGE_LABEL_RE.sub("", text)
    text = OCR_NOISE_RE.sub("", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    lines = [line for line in text.split("\n") if line.strip()]
    lines = _drop_repeated_edge_lines(lines)
    text = "\n".join(lines)
    text = WHITESPACE_RE.sub(" ", text)
    text = re.sub(r"(?<=\w)\n(?=\w)", " ", text)
    text = text.replace("\n", " ")
    text = text.replace(PARAGRAPH_BREAK_TOKEN, "\n\n")
    text = MULTI_DASH_RE.sub("-", text)
    text = MULTI_PUNCT_RE.sub(r"\1", text)
    text = SENTENCE_SPACE_RE.sub(r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]*\n\n[ \t]*", "\n\n", text)
    return text.strip()
