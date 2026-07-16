"""Detector 4: disclosure position.

FTC Endorsement Guides + ASA/CAP require a clear, conspicuous, NON-hyperlink
disclosure BEFORE the first affiliate link on a page. Footer-only and
hyperlink-only disclosures both fail.

Detection rule
--------------
1. Strip <script>/<style>/<noscript> content (it's not visible disclosure).
2. Find the byte offset of the first Viator link in the *stripped* HTML.
3. Scan HTML before that offset for disclosure language (case-insensitive).
4. If no match → violation with rule "missing-disclosure".
5. If match but the matched disclosure text is inside an <a> tag → violation
   with rule "hyperlink-disclosure" (the FTC requires the disclosure to be
   clearly readable, not just present as a hyperlink).

Severity: ``critical``.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..models import Violation

_DISCLOSURE_RE = re.compile(
    r"\b(affiliate|commission|monetize|may earn|paid link|advertising|sponsored)\b",
    re.IGNORECASE,
)
_VIATOR_HREF_RE = re.compile(r"viator\.com", re.IGNORECASE)


def detect(html: str, filepath: str, url: str) -> list[Violation]:
    """Return a critical violation when disclosure precedes no first Viator link."""
    visible_html = _mask_invisible_blocks(html)
    first_link_pos = _first_viator_offset(visible_html)
    if first_link_pos < 0:
        return []  # no viator links → not our problem

    before = visible_html[:first_link_pos]
    match = _DISCLOSURE_RE.search(before)
    if not match:
        return [_make(visible_html, filepath, url, first_link_pos,
                      disclosure_found=False, is_hyperlink=False, rule="missing-disclosure")]

    if _disclosure_is_hyperlinked(visible_html, match.start()):
        # Check if a non-hyperlink disclosure also exists further ahead
        remaining = before[match.end():]
        second = _DISCLOSURE_RE.search(remaining)
        if second and not _disclosure_is_hyperlinked(visible_html, match.end() + second.start()):
            return []  # plain-text disclosure exists further ahead
        return [_make(visible_html, filepath, url, first_link_pos,
                      disclosure_found=True, is_hyperlink=True, rule="hyperlink-disclosure")]

    return []


def _first_viator_offset(html: str) -> int:
    m = _VIATOR_HREF_RE.search(html)
    return m.start() if m else -1


def _mask_invisible_blocks(html: str) -> str:
    block_re = re.compile(
        r"<!--.*?-->|<(?:script|style|noscript)\b[^>]*>.*?</(?:script|style|noscript)\s*>",
        re.IGNORECASE | re.DOTALL,
    )

    def mask(match: re.Match[str]) -> str:
        return "".join("\n" if char == "\n" else " " for char in match.group(0))

    return block_re.sub(mask, html)


def _disclosure_is_hyperlinked(visible_html: str, char_offset: int) -> bool:
    """True when the disclosure text at ``char_offset`` is wrapped in <a>."""
    for anchor in re.finditer(
        r"<a\b[^>]*>.*?</a\s*>", visible_html, re.IGNORECASE | re.DOTALL
    ):
        if _DISCLOSURE_RE.search(anchor.group(0)) and anchor.start() <= char_offset < anchor.end():
            return True
    return False


def _make(html, filepath, url, pos, *, disclosure_found, is_hyperlink, rule) -> Violation:
    line = html[:pos].count("\n") + 1 if pos >= 0 else 0
    return Violation(
        detector="disclosure",
        rule=rule,
        url=url,
        file=filepath or "",
        line=line,
        severity="critical",
        extra={
            "first_viator_line": line,
            "disclosure_found": disclosure_found,
            "disclosure_is_hyperlink": is_hyperlink,
        },
    )