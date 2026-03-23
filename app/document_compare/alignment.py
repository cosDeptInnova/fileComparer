from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .normalization import canonicalize_for_comparison, tokenize_words


@dataclass(slots=True)
class SemanticBlock:
    block_id: int
    text: str
    canonical_text: str
    words: list[str]
    fingerprint: str
    embedding: list[float]
    token_count: int = 0
    source_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class AlignmentMatch:
    block_a: SemanticBlock | None
    block_b: SemanticBlock | None
    score: float
    strategy: str
    reanchored: bool = False
    reanchor_strategy: str = ""


@dataclass(slots=True)
class AlignmentResult:
    matches: list[AlignmentMatch]
    metrics: dict[str, int]


def build_semantic_blocks(text: str, canonical_blocks: list[str]) -> list[SemanticBlock]:
    blocks: list[SemanticBlock] = []
    for index, block in enumerate(canonical_blocks, start=1):
        comparison_text = canonicalize_for_comparison(block)
        words = tokenize_words(comparison_text)
        fingerprint = hashlib.sha1(" ".join(words).encode("utf-8")).hexdigest()
        blocks.append(
            SemanticBlock(
                block_id=index,
                text=block,
                canonical_text=comparison_text,
                words=words,
                fingerprint=fingerprint,
                embedding=_hashed_embedding(words),
                token_count=len(words),
                source_ids=[index],
            )
        )
    return _merge_split_candidates(blocks)


def _merge_split_candidates(blocks: list[SemanticBlock], *, min_tokens: int = 12) -> list[SemanticBlock]:
    if not blocks:
        return []
    merged: list[SemanticBlock] = []
    pending: SemanticBlock | None = None
    for block in blocks:
        if pending is None:
            pending = block
            continue
        if pending.token_count < min_tokens and block.token_count < min_tokens and (not _looks_complete_sentence(pending.text) or not _looks_complete_sentence(block.text)):
            pending = _merge_blocks(pending, block)
            continue
        merged.append(pending)
        pending = block
    if pending is not None:
        if merged and pending.token_count < max(6, min_tokens // 2) and not _looks_complete_sentence(pending.text):
            merged[-1] = _merge_blocks(merged[-1], pending)
        else:
            merged.append(pending)

    for index, block in enumerate(merged, start=1):
        block.block_id = index
    return merged




def _looks_complete_sentence(text: str) -> bool:
    stripped = (text or '').strip()
    return stripped.endswith(('.', ';', ':', '?', '!')) or stripped.startswith('- ')

def _merge_blocks(left: SemanticBlock, right: SemanticBlock) -> SemanticBlock:
    text = f"{left.text}\n\n{right.text}".strip()
    canonical = f"{left.canonical_text} {right.canonical_text}".strip()
    words = left.words + right.words
    return SemanticBlock(
        block_id=left.block_id,
        text=text,
        canonical_text=canonical,
        words=words,
        fingerprint=hashlib.sha1(" ".join(words).encode("utf-8")).hexdigest(),
        embedding=_hashed_embedding(words),
        token_count=len(words),
        source_ids=[*left.source_ids, *right.source_ids],
    )


def _hashed_embedding(words: list[str], *, dims: int = 64) -> list[float]:
    vector = [0.0] * dims
    if not words:
        return vector
    for token in words:
        digest = hashlib.md5(token.encode("utf-8")).digest()
        idx = digest[0] % dims
        sign = 1.0 if digest[1] % 2 == 0 else -1.0
        vector[idx] += sign
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def _jaccard(words_a: list[str], words_b: list[str]) -> float:
    if not words_a and not words_b:
        return 1.0
    set_a = set(words_a)
    set_b = set(words_b)
    if not set_a and not set_b:
        return 1.0
    return len(set_a & set_b) / max(1, len(set_a | set_b))


def _containment(words_a: list[str], words_b: list[str]) -> float:
    set_a = set(words_a)
    set_b = set(words_b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / max(1, min(len(set_a), len(set_b)))


def _similarity(block_a: SemanticBlock, block_b: SemanticBlock) -> tuple[float, str]:
    if block_a.fingerprint == block_b.fingerprint:
        return 1.0, "hash"
    lexical = SequenceMatcher(None, block_a.canonical_text, block_b.canonical_text, autojunk=False).ratio()
    jaccard = _jaccard(block_a.words, block_b.words)
    embedding = _cosine(block_a.embedding, block_b.embedding)
    containment = _containment(block_a.words, block_b.words)
    length_ratio = min(block_a.token_count, block_b.token_count) / max(1, max(block_a.token_count, block_b.token_count))
    score = 0.35 * lexical + 0.25 * jaccard + 0.2 * embedding + 0.15 * containment + 0.05 * length_ratio
    return score, "hybrid"


def align_blocks(blocks_a: list[SemanticBlock], blocks_b: list[SemanticBlock], *, gap_penalty: float = -0.34) -> AlignmentResult:
    if not blocks_a and not blocks_b:
        return AlignmentResult(matches=[], metrics=_new_alignment_metrics())
    anchors = _find_anchor_pairs(blocks_a, blocks_b)
    matches: list[AlignmentMatch] = []
    metrics = _new_alignment_metrics()
    cursor_a = 0
    cursor_b = 0
    for anchor_a, anchor_b in anchors:
        if anchor_a > cursor_a or anchor_b > cursor_b:
            range_result = _align_range(blocks_a[cursor_a:anchor_a], blocks_b[cursor_b:anchor_b], gap_penalty=gap_penalty)
            matches.extend(range_result.matches)
            _merge_alignment_metrics(metrics, range_result.metrics)
        score, strategy = _similarity(blocks_a[anchor_a], blocks_b[anchor_b])
        matches.append(AlignmentMatch(block_a=blocks_a[anchor_a], block_b=blocks_b[anchor_b], score=score, strategy=f"anchor_{strategy}"))
        cursor_a = anchor_a + 1
        cursor_b = anchor_b + 1

    tail_result = _align_range(blocks_a[cursor_a:], blocks_b[cursor_b:], gap_penalty=gap_penalty)
    matches.extend(tail_result.matches)
    _merge_alignment_metrics(metrics, tail_result.metrics)
    compacted = _compact_duplicate_orphans(matches)
    metrics["orphan_rows_prevented"] += max(0, len(matches) - len(compacted))
    return AlignmentResult(matches=compacted, metrics=metrics)


def _find_anchor_pairs(blocks_a: list[SemanticBlock], blocks_b: list[SemanticBlock]) -> list[tuple[int, int]]:
    counts_a = Counter(block.fingerprint for block in blocks_a)
    counts_b = Counter(block.fingerprint for block in blocks_b)
    positions_b: defaultdict[str, list[int]] = defaultdict(list)
    for idx, block in enumerate(blocks_b):
        positions_b[block.fingerprint].append(idx)

    candidates: list[tuple[int, int]] = []
    for idx_a, block in enumerate(blocks_a):
        if counts_a[block.fingerprint] == 1 and counts_b[block.fingerprint] == 1:
            candidates.append((idx_a, positions_b[block.fingerprint][0]))

    # Longest increasing subsequence on B to preserve global ordering.
    if not candidates:
        return []
    candidates.sort()
    size = len(candidates)
    dp = [1] * size
    parent = [-1] * size
    best = 0
    for i in range(size):
        for j in range(i):
            if candidates[j][1] < candidates[i][1] and dp[j] + 1 > dp[i]:
                dp[i] = dp[j] + 1
                parent[i] = j
        if dp[i] > dp[best]:
            best = i
    seq: list[tuple[int, int]] = []
    cursor = best
    while cursor != -1:
        seq.append(candidates[cursor])
        cursor = parent[cursor]
    seq.reverse()
    return seq


def _align_range(blocks_a: list[SemanticBlock], blocks_b: list[SemanticBlock], *, gap_penalty: float) -> AlignmentResult:
    rows = len(blocks_a)
    cols = len(blocks_b)
    metrics = _new_alignment_metrics()
    if rows == 0:
        return AlignmentResult(matches=[AlignmentMatch(block_a=None, block_b=block, score=0.0, strategy="gap_b") for block in blocks_b], metrics=metrics)
    if cols == 0:
        return AlignmentResult(matches=[AlignmentMatch(block_a=block, block_b=None, score=0.0, strategy="gap_a") for block in blocks_a], metrics=metrics)

    dp = [[0.0] * (cols + 1) for _ in range(rows + 1)]
    back: list[list[tuple[str, float] | None]] = [[None] * (cols + 1) for _ in range(rows + 1)]

    for i in range(1, rows + 1):
        dp[i][0] = dp[i - 1][0] + gap_penalty
        back[i][0] = ("up", gap_penalty)
    for j in range(1, cols + 1):
        dp[0][j] = dp[0][j - 1] + gap_penalty
        back[0][j] = ("left", gap_penalty)

    sim_cache: dict[tuple[int, int], tuple[float, str]] = {}
    for i in range(1, rows + 1):
        for j in range(1, cols + 1):
            block_a = blocks_a[i - 1]
            block_b = blocks_b[j - 1]
            score, strategy = _similarity(block_a, block_b)
            sim_cache[(i, j)] = (score, strategy)
            diag_bonus = _diag_bonus(score, block_a, block_b)
            diag = dp[i - 1][j - 1] + diag_bonus
            up = dp[i - 1][j] + gap_penalty
            left = dp[i][j - 1] + gap_penalty
            best = max((diag, "diag"), (up, "up"), (left, "left"), key=lambda item: item[0])
            dp[i][j] = best[0]
            back[i][j] = best

    matches: list[AlignmentMatch] = []
    i, j = rows, cols
    while i > 0 or j > 0:
        move = back[i][j][1] if back[i][j] else None
        if i > 0 and j > 0 and move == "diag":
            score, strategy = sim_cache[(i, j)]
            if _prefer_gap(score, blocks_a[i - 1], blocks_b[j - 1], gap_penalty):
                if dp[i - 1][j] >= dp[i][j - 1]:
                    matches.append(AlignmentMatch(block_a=blocks_a[i - 1], block_b=None, score=0.0, strategy="gap_a"))
                    i -= 1
                else:
                    matches.append(AlignmentMatch(block_a=None, block_b=blocks_b[j - 1], score=0.0, strategy="gap_b"))
                    j -= 1
                continue
            matches.append(AlignmentMatch(block_a=blocks_a[i - 1], block_b=blocks_b[j - 1], score=score, strategy=strategy))
            i -= 1
            j -= 1
        elif i > 0 and (j == 0 or move == "up"):
            matches.append(AlignmentMatch(block_a=blocks_a[i - 1], block_b=None, score=0.0, strategy="gap_a"))
            i -= 1
        else:
            matches.append(AlignmentMatch(block_a=None, block_b=blocks_b[j - 1], score=0.0, strategy="gap_b"))
            j -= 1

    matches.reverse()
    matches, local_metrics = _reanchor_local_windows(matches)
    _merge_alignment_metrics(metrics, local_metrics)
    return AlignmentResult(matches=matches, metrics=metrics)


def _diag_bonus(score: float, block_a: SemanticBlock, block_b: SemanticBlock) -> float:
    if score >= 0.985:
        return score + 0.4
    if score >= 0.9:
        return score + 0.2
    if score >= 0.74:
        return score
    if score >= 0.55:
        return score - 0.15
    if score >= 0.4 and _containment(block_a.words, block_b.words) >= 0.75:
        return score - 0.18
    return -0.55


def _prefer_gap(score: float, block_a: SemanticBlock, block_b: SemanticBlock, gap_penalty: float) -> bool:
    if score >= 0.55:
        return False
    if score >= 0.4 and _containment(block_a.words, block_b.words) >= 0.8:
        return False
    bad_length_mismatch = min(block_a.token_count, block_b.token_count) / max(1, max(block_a.token_count, block_b.token_count)) < 0.45
    return score <= abs(gap_penalty) or bad_length_mismatch


def rescue_orphans_with_context(matches: list[AlignmentMatch]) -> AlignmentResult:
    rescued, metrics = _reanchor_local_windows(matches, allow_contextual_windows=True)
    compacted = _compact_duplicate_orphans(rescued)
    metrics["orphan_rows_prevented"] += max(0, len(rescued) - len(compacted))
    return AlignmentResult(matches=compacted, metrics=metrics)


def _compact_duplicate_orphans(matches: list[AlignmentMatch]) -> list[AlignmentMatch]:
    compacted: list[AlignmentMatch] = []
    index = 0
    while index < len(matches):
        match = matches[index]
        if not compacted:
            compacted.append(match)
            index += 1
            continue
        prev = compacted[-1]
        if prev.block_a and match.block_b and prev.block_b is None and match.block_a is None:
            prev_norm = " ".join(prev.block_a.words)
            next_norm = " ".join(match.block_b.words)
            if prev_norm and prev_norm == next_norm:
                compacted.pop()
                index += 1
                while index < len(matches) and matches[index].block_a is None and matches[index].block_b is not None and " ".join(matches[index].block_b.words) == prev_norm:
                    index += 1
                continue
        if prev.block_b and match.block_a and prev.block_a is None and match.block_b is None:
            prev_norm = " ".join(prev.block_b.words)
            next_norm = " ".join(match.block_a.words)
            if prev_norm and prev_norm == next_norm:
                compacted.pop()
                index += 1
                while index < len(matches) and matches[index].block_b is None and matches[index].block_a is not None and " ".join(matches[index].block_a.words) == prev_norm:
                    index += 1
                continue
        compacted.append(match)
        index += 1
    return compacted


def _new_alignment_metrics() -> dict[str, int]:
    return {
        "reanchors_attempted": 0,
        "reanchors_successful": 0,
        "orphan_rows_prevented": 0,
    }


def _merge_alignment_metrics(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def _reanchor_local_windows(matches: list[AlignmentMatch], *, allow_contextual_windows: bool = False) -> tuple[list[AlignmentMatch], dict[str, int]]:
    if len(matches) < 2:
        return matches, _new_alignment_metrics()

    metrics = _new_alignment_metrics()
    reanchored: list[AlignmentMatch] = []
    index = 0
    while index < len(matches):
        best_candidate: tuple[int, AlignmentMatch] | None = None
        for window_size in range(min(4, len(matches) - index), 1, -1):
            window = matches[index:index + window_size]
            if not _should_attempt_reanchor_window(window, allow_contextual_windows=allow_contextual_windows):
                continue
            metrics["reanchors_attempted"] += 1
            candidate = _best_window_reanchor(window)
            if candidate is None:
                continue
            if best_candidate is None or candidate.score > best_candidate[1].score:
                best_candidate = (window_size, candidate)
        if best_candidate is None:
            reanchored.append(matches[index])
            index += 1
            continue
        window_size, candidate = best_candidate
        metrics["reanchors_successful"] += 1
        metrics["orphan_rows_prevented"] += max(0, window_size - 1)
        reanchored.append(candidate)
        index += window_size
    return reanchored, metrics


def _should_attempt_reanchor_window(window: list[AlignmentMatch], *, allow_contextual_windows: bool) -> bool:
    blocks_a = [match.block_a for match in window if match.block_a is not None]
    blocks_b = [match.block_b for match in window if match.block_b is not None]
    if not (1 <= len(blocks_a) <= 2 and 1 <= len(blocks_b) <= 2 and (len(blocks_a) > 1 or len(blocks_b) > 1)):
        return False

    direct_gap_swap = any(
        (
            left.block_a is not None
            and left.block_b is None
            and right.block_a is None
            and right.block_b is not None
        )
        or (
            left.block_b is not None
            and left.block_a is None
            and right.block_b is None
            and right.block_a is not None
        )
        for left, right in zip(window, window[1:])
    )
    consecutive_gaps = sum(1 for match in window if match.block_a is None or match.block_b is None) >= 2
    medium_score = any(
        match.block_a
        and match.block_b
        and 0.35 <= match.score <= 0.78
        and _containment(match.block_a.words, match.block_b.words) >= 0.72
        for match in window
    )
    context_signal = allow_contextual_windows and any(match.block_a is None or match.block_b is None for match in window) and any(
        match.block_a
        and match.block_b
        and 0.45 <= match.score <= 0.9
        and _containment(match.block_a.words, match.block_b.words) >= 0.68
        for match in window
    )
    return direct_gap_swap or consecutive_gaps or medium_score or context_signal


def _best_window_reanchor(window: list[AlignmentMatch]) -> AlignmentMatch | None:
    blocks_a = [match.block_a for match in window if match.block_a is not None]
    blocks_b = [match.block_b for match in window if match.block_b is not None]
    if not blocks_a or not blocks_b:
        return None

    candidates: list[tuple[SemanticBlock, SemanticBlock, str]] = []
    if len(blocks_a) >= 1 and len(blocks_b) >= 2:
        candidates.append((blocks_a[0], _merge_block_sequence(blocks_b[:2]), "1to2_merge"))
    if len(blocks_a) >= 2 and len(blocks_b) >= 1:
        candidates.append((_merge_block_sequence(blocks_a[:2]), blocks_b[0], "2to1_merge"))
    if len(blocks_a) >= 2 and len(blocks_b) >= 2:
        candidates.append((_merge_block_sequence(blocks_a[:2]), _merge_block_sequence(blocks_b[:2]), "2to2_merge"))

    scored: list[tuple[float, AlignmentMatch]] = []
    for candidate_a, candidate_b, strategy in candidates:
        mixed_score, similarity_score, containment, length_ratio = _many_to_many_score(candidate_a, candidate_b)
        if not _passes_reanchor_threshold(candidate_a, candidate_b, mixed_score, similarity_score, containment, length_ratio):
            continue
        scored.append((
            mixed_score,
            AlignmentMatch(
                block_a=candidate_a,
                block_b=candidate_b,
                score=round(mixed_score, 4),
                strategy=f"window_reanchor_{strategy}",
                reanchored=True,
                reanchor_strategy=strategy,
            ),
        ))

    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    best = scored[0][1]
    if best.reanchor_strategy == "2to2_merge":
        best.strategy = "window_reanchor"
    return best


def _many_to_many_score(block_a: SemanticBlock, block_b: SemanticBlock) -> tuple[float, float, float, float]:
    similarity_score, _ = _similarity(block_a, block_b)
    lexical = SequenceMatcher(None, block_a.canonical_text, block_b.canonical_text, autojunk=False).ratio()
    containment = _containment(block_a.words, block_b.words)
    length_ratio = min(block_a.token_count, block_b.token_count) / max(1, max(block_a.token_count, block_b.token_count))
    order_preserved = 1.0 if _source_ids_are_ordered(block_a) and _source_ids_are_ordered(block_b) else 0.0
    mixed = 0.4 * similarity_score + 0.25 * containment + 0.2 * lexical + 0.1 * length_ratio + 0.05 * order_preserved
    return mixed, similarity_score, containment, length_ratio


def _passes_reanchor_threshold(
    block_a: SemanticBlock,
    block_b: SemanticBlock,
    mixed_score: float,
    similarity_score: float,
    containment: float,
    length_ratio: float,
) -> bool:
    lexical = SequenceMatcher(None, block_a.canonical_text, block_b.canonical_text, autojunk=False).ratio()
    if mixed_score >= 0.78 and containment >= 0.7 and lexical >= 0.72 and length_ratio >= 0.5:
        return True
    if similarity_score >= 0.72 and containment >= 0.82 and lexical >= 0.7 and length_ratio >= 0.48:
        return True
    return similarity_score >= 0.66 and containment >= 0.9 and lexical >= 0.82 and length_ratio >= 0.58 and len(block_a.words) + len(block_b.words) >= 8


def _merge_block_sequence(blocks: list[SemanticBlock]) -> SemanticBlock:
    merged = blocks[0]
    for block in blocks[1:]:
        merged = _merge_blocks(merged, block)
    return merged


def _source_ids_are_ordered(block: SemanticBlock) -> bool:
    return block.source_ids == sorted(block.source_ids)