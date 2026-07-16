"""Detector 5: link-chain.

Finds adjacent <a> tags whose separating text is empty or only whitespace /
punctuation. Two or more consecutive links without separating prose confuse
screen readers and search-engine parsers (WCAG 2.4.4 + Google link-purpose
guidance).

Detection rule
--------------
Walk the parsed HTML and look at every pair of <a> tags that share a parent.
If the text between the close of the first and the open of the second
matches ``^[\\s\\W_]*$`` (only whitespace, punctuation, underscores, or empty),
the pair is treated as a chain. Larger runs collapse into a single Violation
listing every anchor in the chain, so a 3-link chain yields one violation,
not two.

Severity: ``major`` (accessibility + UX impact).
"""

from __future__ import annotations

import re
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from ..models import Violation

# Characters allowed in the "gap" between two adjacent anchors. Anything
# outside this set (i.e. a real word, a number, a letter) means the links
# are separated by prose and should NOT be flagged.
_GAP_PUNCT_RE = re.compile(r"^[\s\W_]*$", re.UNICODE)


def _gap_text(first: Tag, second: Tag) -> str:
    """Concatenate the text nodes sitting between ``first`` and ``second``.

    Operates at the BeautifulSoup tag level, so siblings and nested inline
    tags (e.g. <span>) all contribute their text content. Returns the
    empty string when ``second`` immediately follows ``first``.
    """
    gap = ""
    sibling = first.next_sibling
    while sibling is not None and sibling is not second:
        if isinstance(sibling, Tag):
            gap += sibling.get_text()
        elif isinstance(sibling, str):
            gap += sibling
        sibling = sibling.next_sibling
    return gap


def _is_chain_gap(text: str) -> bool:
    """True when ``text`` contains no real word content."""
    return _GAP_PUNCT_RE.match(text) is not None


def _line_of(tag: Tag, source_html: str) -> int:
    """Best-effort 1-based line number of a tag in the source HTML."""
    if not source_html:
        return 0
    sp = getattr(tag, "sourcepos", None)
    if isinstance(sp, tuple) and sp and sp[0]:
        return int(sp[0])
    if isinstance(sp, int) and sp > 0:
        return int(sp)
    href = tag.get("href", "") if hasattr(tag, "get") else ""
    if href:
        idx = source_html.find(href)
        if idx >= 0:
            return source_html[:idx].count("\n") + 1
    return 1


def detect(html: str, filepath: str = "", url: str = "") -> list[Violation]:
    """Detect link-chain violations on a single HTML document.

    Accepts both ``url`` (sibling-detector convention) and ``page_url``
    (spec / CLI convention); ``url`` wins when both are passed.
    """
    page_url = url or ""
    soup = BeautifulSoup(html, "html.parser")
    for el in soup(["script", "style", "noscript"]):
        el.decompose()

    violations: list[Violation] = []

    # Group <a> tags by their parent so chains only form between siblings.
    parents: dict[Tag, list[Tag]] = {}
    for a in soup.find_all("a"):
        parent = a.parent
        if parent is None:
            continue
        parents.setdefault(parent, []).append(a)

    for parent, anchors in parents.items():
        if len(anchors) < 2:
            continue

        # Walk consecutive anchors and collapse runs where every gap is empty.
        run_start = 0
        i = 1
        while i < len(anchors):
            gap = _gap_text(anchors[i - 1], anchors[i])
            if _is_chain_gap(gap):
                # Keep extending the current run.
                i += 1
                continue
            # Run ended at i-1. Emit if run length >= 2.
            if i - run_start >= 2:
                _emit_run(violations, anchors[run_start:i], parent, html, filepath, page_url)
            run_start = i
            i += 1

        # Tail run.
        if len(anchors) - run_start >= 2:
            _emit_run(violations, anchors[run_start:], parent, html, filepath, page_url)

    return violations


def _emit_run(
    violations: list[Violation],
    run: list[Tag],
    parent: Tag,
    html: str,
    filepath: str,
    page_url: str,
) -> None:
    adjacent = [a.get_text(strip=True) for a in run]
    first_tag = run[0]
    line = _line_of(first_tag, html)
    if line == 0 and parent is not None:
        line = _line_of(parent, html)
    rel = str(Path(filepath)) if filepath else ""
    violations.append(
        Violation(
            detector="link_chain",
            rule="link-chain",
            url=page_url,
            file=rel,
            line=line,
            severity="major",
            anchor_text=adjacent[0] if adjacent else "",
            extra={"adjacent_links": adjacent, "chain_length": len(adjacent)},
        )
    )