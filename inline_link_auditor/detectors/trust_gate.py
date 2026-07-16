"""Detector 3 — trust-gate flags (Framework 2026, Gate 6).

A Viator link must not be placed in safety-sensitive copy.  For every Viator
anchor, this detector examines the visible text from the 50 characters before
the anchor through the 50 characters after it.  HTML markup, comments, and
script/style/noscript contents do not count toward that text window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from ..models import Violation

TRUST_KEYWORDS = (
    "safety",
    "danger",
    "warning",
    "emergency",
    "medical",
    "hospital",
    "insurance",
    "visa",
    "passport",
    "immigration",
    "customs",
    "border",
    "cancel",
    "refund policy",
    "accident",
    "injury",
    "rescue",
    "evacuation",
)

_TRUST_RE = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(keyword) for keyword in TRUST_KEYWORDS) + r")(?!\w)",
    re.IGNORECASE,
)
_ANCHOR_RE = re.compile(
    r"<a\b(?P<attrs>[^>]*)>(?P<body>.*?)</a\s*>",
    re.IGNORECASE | re.DOTALL,
)
_HREF_RE = re.compile(
    r"\bhref\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))",
    re.IGNORECASE,
)
_VIATOR_RE = re.compile(r"viator\.com", re.IGNORECASE)


@dataclass(frozen=True)
class _AnchorSpan:
    start: int
    end: int
    href: str
    body: str


def _href(attrs: str) -> str | None:
    match = _HREF_RE.search(attrs)
    if not match:
        return None
    return next(value for value in match.group("double", "single", "bare") if value is not None)


def _viator_anchors(html: str) -> list[_AnchorSpan]:
    """Return real-looking Viator anchor spans in document order.

    The invisible-block mask preserves offsets while preventing an ``<a>``
    string embedded in a comment or script from becoming an audit target.
    """
    masked = _mask_invisible_blocks(html)
    anchors: list[_AnchorSpan] = []
    for match in _ANCHOR_RE.finditer(masked):
        href = _href(match.group("attrs"))
        if href is not None and _VIATOR_RE.search(href):
            anchors.append(_AnchorSpan(match.start(), match.end(), href, match.group("body")))
    return anchors


def _mask_invisible_blocks(html: str) -> str:
    block_re = re.compile(
        r"<!--.*?-->|<(?:script|style|noscript)\b[^>]*>.*?</(?:script|style|noscript)\s*>",
        re.IGNORECASE | re.DOTALL,
    )

    def mask(match: re.Match[str]) -> str:
        # Preserve newlines so offsets and line numbers remain valid.
        return "".join("\n" if char == "\n" else " " for char in match.group(0))

    return block_re.sub(mask, html)


def _visible_text(fragment: str) -> str:
    soup = BeautifulSoup(fragment, "html.parser")
    for tag in soup(("script", "style", "noscript")):
        tag.decompose()
    # A separator keeps text from adjacent elements from being merged into a
    # different word when tags are removed.
    return soup.get_text(" ", strip=False)


def _context(html: str, anchor: _AnchorSpan) -> tuple[str, str]:
    before = _visible_text(html[: anchor.start])[-50:]
    anchor_text = _visible_text(anchor.body).strip()
    after = _visible_text(html[anchor.end :])[:50]
    return anchor_text, f"{before}{anchor_text}{after}".strip()


def detect(html: str, filepath: str, url: str) -> list[Violation]:
    """Return one critical violation per Viator link near trust-gate copy."""
    violations: list[Violation] = []
    for anchor in _viator_anchors(html):
        anchor_text, snippet = _context(html, anchor)
        match = _TRUST_RE.search(snippet)
        if match is None:
            continue
        violations.append(
            Violation(
                detector="trust_gate",
                rule="trust-keyword",
                url=url,
                file=filepath,
                line=html[: anchor.start].count("\n") + 1,
                severity="critical",
                anchor_text=anchor_text,
                matched_keyword=match.group(1).lower(),
                context_snippet=snippet,
            )
        )
    return violations
