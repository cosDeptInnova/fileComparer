from __future__ import annotations

from collections import OrderedDict
import re

from app.schemas import ChangeRow, LLMComparisonResponse


def _canonical_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _is_nested_duplicate(base: ChangeRow, candidate: ChangeRow) -> bool:
    if base.change_type != candidate.change_type:
        return False
    base_a = _canonical_text(base.display_text_a)
    base_b = _canonical_text(base.display_text_b)
    cand_a = _canonical_text(candidate.display_text_a)
    cand_b = _canonical_text(candidate.display_text_b)
    if not base_a and not base_b:
        return False
    return (cand_a in base_a and cand_b in base_b) or (base_a in cand_a and base_b in cand_b)


def deduplicate_rows(rows: list[ChangeRow]) -> list[ChangeRow]:
    unique: OrderedDict[tuple[str, str, str], ChangeRow] = OrderedDict()
    for row in rows:
        key = (row.change_type, _canonical_text(row.display_text_a), _canonical_text(row.display_text_b))
        if key not in unique:
            unique[key] = row
            continue
        existing = unique[key]
        if len(row.summary) > len(existing.summary):
            unique[key] = row
    deduped = list(unique.values())
    filtered: list[ChangeRow] = []
    for row in deduped:
        if any(_is_nested_duplicate(existing, row) for existing in filtered):
            continue
        filtered.append(row)
    return filtered


def build_reconciliation_payload(rows: list[ChangeRow]) -> list[dict[str, object]]:
    return [
        {
            "block_id": row.block_id,
            "change_type": row.change_type,
            "text_a": row.display_text_a,
            "text_b": row.display_text_b,
            "summary": row.summary,
        }
        for row in rows
    ]


def merge_reconciled_rows(
    original_rows: list[ChangeRow], reconciled: LLMComparisonResponse | None
) -> list[ChangeRow]:
    if reconciled is None or not reconciled.changes:
        return deduplicate_rows(original_rows)
    rows = deduplicate_rows(original_rows)
    next_id = max((row.block_id for row in rows), default=0) + 1
    for change in reconciled.changes:
        if any(
            row.change_type == change.change_type
            and row.display_text_a.strip() == change.source_a.strip()
            and row.display_text_b.strip() == change.source_b.strip()
            for row in rows
        ):
            continue
        rows.append(
            ChangeRow(
                block_id=next_id,
                pair_id=f"reconcile-{next_id}",
                text_a=change.source_a,
                text_b=change.source_b,
                display_text_a=change.source_a,
                display_text_b=change.source_b,
                change_type=change.change_type,
                confidence=change.confidence,
                severity=change.severity,
                summary=change.summary,
                llm_comment=change.evidence,
            )
        )
        next_id += 1
    return deduplicate_rows(rows)