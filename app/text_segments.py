from typing import Any


def normalize_text_segments(segments: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        segment_type = str(segment.get("type") or "equal").strip().lower()
        if segment_type not in {"equal", "insert", "delete", "replace", "context"}:
            segment_type = "equal"
        text = str(segment.get("text") or "")
        if not text:
            continue
        if normalized and normalized[-1]["type"] == segment_type:
            normalized[-1]["text"] += text
            continue
        normalized.append({"type": segment_type, "text": text})
    return normalized
