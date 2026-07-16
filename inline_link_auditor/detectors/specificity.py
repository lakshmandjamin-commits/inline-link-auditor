"""Detector 1: specificity (Gate 2).

Anchor text must contain a named entity. Generic phrases like "this tour"
or "click here" don't satisfy WCAG 2.4.4 (link purpose) and Google's anchor-
text quality guidance.

Detection rule
--------------
For every Viator <a>:
  * Vague if anchor is empty, <8 chars, OR matches the generic-phrase list
    (``this|that|these|here|click|book now|...``).
  * OR anchor has no proper noun (no capitalised word >2 chars).

The ``reason`` field in the violation extra is one of:
  * ``"vague-phrase"`` — anchor matched the generic-phrase list.
  * ``"anchor-too-short"`` — anchor has fewer than 8 characters.
  * ``"anchor-empty"`` — anchor has no text at all.
  * ``"no-proper-noun"`` — anchor has no capitalised word >2 chars.

Severity: ``major``.
"""

from __future__ import annotations

import re
from typing import Iterable

from ..models import Violation

# Generic demonstrative / CTA tokens that should never appear as standalone
# anchor text. Matched as whole-word tokens so "this tour" still trips
# "this" → violation; "historic" (which contains "his") does not.
_VAGUE_TOKENS = [
    "this", "that", "these", "those",
    "here", "click", "book", "now",
    "learn", "read", "find", "out", "more",
    "check", "it", "tap", "see",
]
_VAGUE_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _VAGUE_TOKENS) + r")\b",
    re.IGNORECASE,
)
# Proper noun: capitalised word, > 2 chars (matches "Porto", "Marina"; rejects "NY").
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")


def detect(
    html: str,
    filepath: str = "",
    url: str = "",
    page_url: str = "",
    links: Iterable | None = None,
    **_unused,
) -> list[Violation]:
    page_url = url or page_url
    from ..parser import extract_viator_links
    if links is None:
        links = extract_viator_links(html, filepath)

    violations: list[Violation] = []
    for link in links:
        anchor = (link.anchor_text or "").strip()
        if not anchor:
            violations.append(_make(link, html, filepath, page_url, "anchor-empty"))
            continue
        if _VAGUE_RE.search(anchor):
            violations.append(_make(link, html, filepath, page_url, "vague-phrase"))
            continue
        if len(anchor) < 8:
            violations.append(_make(link, html, filepath, page_url, "anchor-too-short"))
            continue
        if not _PROPER_NOUN_RE.search(anchor):
            violations.append(_make(link, html, filepath, page_url, "no-proper-noun"))

    return violations


def _make(link, html, filepath, page_url, reason: str) -> Violation:
    return Violation(
        detector="specificity",
        rule="vague-anchor",
        url=page_url,
        file=filepath or "",
        line=link.line or 0,
        severity="major",
        anchor_text=link.anchor_text or "",
        extra={"reason": reason},
    )