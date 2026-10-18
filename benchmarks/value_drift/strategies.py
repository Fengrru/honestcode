"""Matching strategies used to establish whether value drift is a real risk.

Two things live here:

1. ``GUARDED_*`` — a faithful Python port of the eight matching strategies from
   ``fuzzy-patch`` (``packages/fuzzy-patch/src/index.ts``), *including* its
   value-literal guard. This is the reference implementation whose claims the
   benchmark exists to test.

2. ``UNGUARDED_*`` — the same strategies with the guard removed, plus a
   generic similarity matcher in the shape used by mainstream edit tools
   (a raised similarity floor with a uniqueness check, which is what the
   2026 ecosystem converged on: Aider-style cascades, Cursor apply, and the
   ``_FUZZY_CONTENT_FLOOR`` guards in Hermes-Agent/pawwork).

The point of keeping both is that the guard is the *only* difference between
rows 1 and 2. If a scenario drifts under ``UNGUARDED_*`` and does not under
``GUARDED_*``, the guard is carrying the entire result.

Nothing here is imported by the shipped package; this is benchmark-only code.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from dataclasses import dataclass

__all__ = [
    "MatchResult",
    "Strategy",
    "GUARDED_STRATEGIES",
    "UNGUARDED_STRATEGIES",
    "COMPETITOR_STRATEGIES",
    "ALL_STRATEGY_SETS",
    "extract_value_tokens",
    "preserves_value_tokens",
]

# ── primitives (ported from fuzzy-patch/src/index.ts) ───────────────────────


def lev(a: str, b: str) -> int:
    """Levenshtein distance, two-row DP (index.ts:52)."""
    m, n = len(a), len(b)
    if m == 0:
        return n
    if n == 0:
        return m
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        curr = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[n]


def tokenize(s: str) -> list[str]:
    """index.ts:79 — word tokens for token_match."""
    return [t for t in re.split(r"""[\s,;:.\-()\[\]{}"'`~?!@#$%^&*+=/\\<>|]+""", s.lower()) if t]


def normalize_whitespace(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def normalize_line_endings(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _tolerant_regex(s: str) -> re.Pattern[str]:
    out = []
    for ch in s:
        if ch in ".*+?^${}()|[]\\":
            out.append("\\" + ch)
        elif ch == " ":
            out.append(r"\s+")
        else:
            out.append(ch)
    return re.compile("".join(out))


# ── the value guard (index.ts:100-136) ──────────────────────────────────────

# Numeric literals (hex or decimal), and single/double/backtick quoted strings.
_VALUE_RE = re.compile(r"""0[xX][0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?|"[^"\n]*"|'[^'\n]*'|`[^`]*`""")


def extract_value_tokens(s: str) -> list[str]:
    """Value-bearing tokens, whitespace-normalized and sorted (index.ts:100)."""
    return sorted(normalize_whitespace(m.group(0)) for m in _VALUE_RE.finditer(s))


def preserves_value_tokens(region: str, old_str: str) -> bool:
    """True when *region* carries exactly the literals of *old_str* (index.ts:117).

    Multiset equality over numeric and quoted-string literals is the whole
    check. Formatting may drift; values may not.
    """
    region_tokens = extract_value_tokens(region)
    old_tokens = extract_value_tokens(old_str)
    if len(region_tokens) != len(old_tokens):
        return False
    return region_tokens == old_tokens


# ── types ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MatchResult:
    index: int
    length: int

    @property
    def end(self) -> int:
        return self.index + self.length


StrategyFn = Callable[[str, str], "MatchResult | None"]


@dataclass(frozen=True)
class Strategy:
    name: str
    fn: StrategyFn


# ── the eight strategies ────────────────────────────────────────────────────


def _find_in_original_via_anchors(content: str, norm_match: str) -> MatchResult | None:
    """index.ts:153 — map a whitespace-normalized match back onto the original."""
    trimmed = norm_match.strip()
    head_len = min(20, len(trimmed))
    tail_len = min(20, len(trimmed))
    head = trimmed[:head_len]
    tail = trimmed[len(trimmed) - tail_len :]
    if len(head) < 3:
        return None

    m = _tolerant_regex(head).search(content)
    if not m:
        return None
    start = m.start()
    expected_end = start + round(len(norm_match) * 1.3)
    tail_re = _tolerant_regex(tail)

    best_tail_idx = -1
    search_idx = min(expected_end, max(0, len(content) - tail_len * 2))
    while search_idx < len(content):
        m2 = tail_re.search(content, search_idx)
        if m2:
            best_tail_idx = m2.start()
            tail_end = m2.end()
            break
        search_idx += 1

    if best_tail_idx < 0:
        approx_len = min(len(norm_match) + 10, len(content) - start)
        return MatchResult(start, approx_len)
    return MatchResult(start, tail_end - start)


def strategy_exact(content: str, old: str) -> MatchResult | None:
    idx = content.find(old)
    return None if idx == -1 else MatchResult(idx, len(old))


def strategy_whitespace_norm(content: str, old: str) -> MatchResult | None:
    norm_content = normalize_whitespace(content)
    norm_old = normalize_whitespace(old)
    if not norm_old:
        return None
    norm_idx = norm_content.find(norm_old)
    if norm_idx == -1:
        return None
    return _find_in_original_via_anchors(content, norm_content[norm_idx : norm_idx + len(norm_old)])


def strategy_indent_norm(content: str, old: str) -> MatchResult | None:
    content_lines = content.split("\n")
    old_lines = old.split("\n")
    if not old_lines or not content_lines:
        return None
    first_old = old_lines[0].lstrip()
    for i in range(len(content_lines) - len(old_lines) + 1):
        if content_lines[i].lstrip() != first_old:
            continue
        if all(
            content_lines[i + j].lstrip() == old_lines[j].lstrip() for j in range(1, len(old_lines))
        ):
            start = sum(len(line) + 1 for line in content_lines[:i])
            leading = len(content_lines[i]) - len(content_lines[i].lstrip())
            adjusted = start + leading
            end = sum(len(line) + 1 for line in content_lines[: i + len(old_lines)]) - 1
            return MatchResult(adjusted, end - adjusted)
    return None


def strategy_line_ending_norm(content: str, old: str) -> MatchResult | None:
    norm_content = normalize_line_endings(content)
    norm_old = normalize_line_endings(old)
    norm_idx = norm_content.find(norm_old)
    if norm_idx == -1:
        return None

    orig_pos = norm_pos = 0
    while norm_pos < norm_idx and orig_pos < len(content):
        if (
            content[orig_pos] == "\r"
            and orig_pos + 1 < len(content)
            and content[orig_pos + 1] == "\n"
        ):
            orig_pos += 2
        else:
            orig_pos += 1
        norm_pos += 1

    match_end = orig_pos
    norm_pos = 0
    while norm_pos < len(norm_old) and match_end < len(content):
        if (
            content[match_end] == "\r"
            and match_end + 1 < len(content)
            and content[match_end + 1] == "\n"
        ):
            match_end += 2
        else:
            match_end += 1
        norm_pos += 1
    return MatchResult(orig_pos, match_end - orig_pos)


def strategy_head_tail_anchor(content: str, old: str) -> MatchResult | None:
    if len(old) < 20:
        return None
    head_len = min(15, int(len(old) * 0.3))
    tail_len = min(15, int(len(old) * 0.3))
    head, tail = old[:head_len], old[len(old) - tail_len :]

    candidates, idx = [], content.find(head)
    while idx != -1:
        candidates.append(idx)
        idx = content.find(head, idx + 1)

    for start in candidates:
        expected_tail_pos = start + len(old) - tail_len
        if not (
            expected_tail_pos <= len(content)
            and content[expected_tail_pos : expected_tail_pos + tail_len] == tail
        ):
            continue
        middle = content[start + head_len : expected_tail_pos]
        old_middle = old[head_len : len(old) - tail_len]
        max_edit = max(len(middle), len(old_middle)) * 0.4
        if lev(middle, old_middle) > max_edit:
            continue
        return MatchResult(start, expected_tail_pos + tail_len - start)
    return None


def strategy_context_anchor(content: str, old: str) -> MatchResult | None:
    old_lines = old.split("\n")
    if len(old_lines) < 3:
        return None
    first_line, last_line = old_lines[0], old_lines[-1]
    content_lines = content.split("\n")
    for i in range(len(content_lines)):
        if content_lines[i].strip() == first_line.strip():
            expected_last = i + len(old_lines) - 1
            if (
                expected_last < len(content_lines)
                and content_lines[expected_last].strip() == last_line.strip()
            ):
                start = sum(len(line) + 1 for line in content_lines[:i])
                end = sum(len(line) + 1 for line in content_lines[: expected_last + 1]) - 1
                return MatchResult(start, end - start)
    return None


_OPEN_TO_CLOSE = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = frozenset(_OPEN_TO_CLOSE.values())


def _bracket_balanced_end(content: str, start: int, end: int) -> int:
    """Extend a word-token span through its closing brackets.

    ``token_match`` stops at the last word token, so for a multi-line call it
    matched ``return client.get(..., timeout=30`` and left the trailing
    ``\\n    )`` outside the span. Replacing that span with the (balanced)
    replacement duplicated the closing paren and produced invalid code. Walk
    the existing span with a small bracket/quote scanner and keep consuming
    until every bracket it opened is closed.
    """
    stack: list[str] = []
    quote: str | None = None
    escaped = False

    def consume(
        ch: str, stack: list[str], quote: str | None, escaped: bool
    ) -> tuple[str | None, bool]:
        if quote is not None:
            if escaped:
                return quote, False
            if ch == "\\":
                return quote, True
            return (None if ch == quote else quote), False
        if ch in "\"'`":
            return ch, False
        if ch in _OPEN_TO_CLOSE:
            stack.append(_OPEN_TO_CLOSE[ch])
        elif ch in _CLOSERS and stack and stack[-1] == ch:
            stack.pop()
        return None, False

    i = start
    while i < end:
        quote, escaped = consume(content[i], stack, quote, escaped)
        i += 1
    if not stack:
        return end

    limit = min(len(content), end + 400)
    while i < limit and stack:
        quote, escaped = consume(content[i], stack, quote, escaped)
        i += 1
    return i if not stack else end


def strategy_token_match(content: str, old: str) -> MatchResult | None:
    old_tokens = tokenize(old)
    if len(old_tokens) < 3:
        return None
    positions = [(m.group(0).lower(), m.start(), m.end()) for m in re.finditer(r"\w+", content)]
    for i in range(len(positions) - len(old_tokens) + 1):
        if all(positions[i + j][0] == old_tokens[j] for j in range(len(old_tokens))):
            start = positions[i][1]
            end = positions[i + len(old_tokens) - 1][2]
            while end < len(content) and not re.match(r"\w", content[end]):
                if content[end] in "\n\r":
                    break
                end += 1
            end = _bracket_balanced_end(content, start, end)
            return MatchResult(start, end - start)
    return None


def strategy_levenshtein_fuzzy(content: str, old: str) -> MatchResult | None:
    """index.ts:354 — relative edit budget only, winning window width reported."""
    if len(old) < 10:
        return None
    threshold = max(len(old) * 0.2 // 1, 2)
    threshold = int(threshold)
    window_size = len(old)
    variation = int(len(old) * 0.2)
    step = max(1, int(len(old) / 10))

    best: MatchResult | None = None
    best_dist: int | None = None
    for i in range(0, len(content) - window_size + variation + 1, step):
        for ws in range(window_size - variation, window_size + variation + 1):
            if i + ws > len(content) or ws <= 0:
                continue
            dist = lev(content[i : i + ws], old)
            if dist <= threshold and (best_dist is None or dist < best_dist):
                best, best_dist = MatchResult(i, ws), dist
    return best


_STRATEGY_FNS: list[tuple[str, StrategyFn]] = [
    ("exact", strategy_exact),
    ("whitespace_normalized", strategy_whitespace_norm),
    ("indentation_normalized", strategy_indent_norm),
    ("line_ending_normalized", strategy_line_ending_norm),
    ("token_match", strategy_token_match),
    ("head_tail_anchor", strategy_head_tail_anchor),
    ("context_anchor", strategy_context_anchor),
    ("levenshtein_fuzzy", strategy_levenshtein_fuzzy),
]


def _guarded(fn: StrategyFn) -> StrategyFn:
    """index.ts:128 — refuse a match that would change a value literal."""

    def wrapper(content: str, old: str) -> MatchResult | None:
        match = fn(content, old)
        if match is None or match.index < 0 or match.length <= 0:
            return None
        region = content[match.index : match.index + match.length]
        return match if preserves_value_tokens(region, old) else None

    return wrapper


GUARDED_STRATEGIES: list[Strategy] = [Strategy(name, _guarded(fn)) for name, fn in _STRATEGY_FNS]
UNGUARDED_STRATEGIES: list[Strategy] = [Strategy(name, fn) for name, fn in _STRATEGY_FNS]


# ── mainstream competitor behaviour ─────────────────────────────────────────


def _similarity_matcher(floor: float) -> StrategyFn:
    """Shape of the guard the 2026 ecosystem converged on.

    Raise the similarity floor, require a *unique* best candidate, and refuse
    matches whose span is wildly larger than the search text (the
    ``_FUZZY_CONTENT_FLOOR`` / ``isDisproportionateMatch`` pattern). Note what
    this protects: *where* the match lands. It has no opinion on *which values*
    the matched region carries.
    """

    def match(content: str, old: str) -> MatchResult | None:
        lines = content.split("\n")
        old_lines = old.split("\n")
        span = max(len(old_lines), 1)

        best: MatchResult | None = None
        best_ratio = 0.0
        second_ratio = 0.0
        for i in range(len(lines)):
            candidate = "\n".join(lines[i : i + span])
            if not candidate:
                continue
            if len(candidate) > len(old) * 2.5:  # disproportionate-span rejection
                continue
            ratio = difflib.SequenceMatcher(
                None, normalize_whitespace(candidate), normalize_whitespace(old)
            ).ratio()
            if ratio > best_ratio:
                second_ratio = best_ratio
                best_ratio = ratio
                start = sum(len(line) + 1 for line in lines[:i])
                best = MatchResult(start, len(candidate))
            elif ratio > second_ratio:
                second_ratio = ratio

        if best is None or best_ratio < floor:
            return None
        # ambiguity = hard failure; never fall through to a looser matcher
        if best_ratio - second_ratio < 0.05:
            return None
        return best

    return match


COMPETITOR_STRATEGIES: list[Strategy] = [
    Strategy("similarity_floor_0.65", _similarity_matcher(0.65)),
    Strategy("similarity_floor_0.80", _similarity_matcher(0.80)),
]

ALL_STRATEGY_SETS: dict[str, list[Strategy]] = {
    "guarded (fuzzy-patch)": GUARDED_STRATEGIES,
    "unguarded (8 strategies, no value guard)": UNGUARDED_STRATEGIES,
    "competitor: similarity floor + uniqueness": COMPETITOR_STRATEGIES,
}


def match_at_all(
    strategies: list[Strategy], content: str, old: str
) -> tuple[MatchResult | None, str | None]:
    """First strategy to match wins, mirroring ``fuzzyFindAndReplace``."""
    for strategy in strategies:
        m = strategy.fn(content, old)
        if m is not None and m.index >= 0 and m.length > 0:
            return m, strategy.name
    return None, None
