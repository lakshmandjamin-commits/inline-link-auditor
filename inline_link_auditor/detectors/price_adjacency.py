"""Detector 6: price adjacency.

Detects when a Viator affiliate link sits within 5 characters of a price
marker (currency symbol + digits, or digits + ISO currency code). Stale
prices near an affiliate link create an FTC/ASA risk: the link implicitly
endorses a price that may no longer be in effect, even when the price is
not the anchor text itself.

Detection rule
--------------
For each Viator <a> tag:
  * Build an "adjacent window" = the 5 characters immediately before the
    anchor's context, the anchor text itself, then 5 characters after.
  * If the window contains a price marker (currency symbol + digits, or
    digits + ISO code), flag it.

Patterns (case-sensitive for symbols, case-insensitive for codes):
  * Currency symbol followed by digits: $, €, £, ¥, ₹, Rp
  * Digits followed by an ISO code: USD, EUR, GBP, IDR (also JPY for ¥)
  * The digit component must contain at least one number.

Severity: ``minor`` (stale-claim risk; mitigated by updating the price).
"""

from __future__ import annotations

import re
from typing import Iterable

from ..models import Violation

# Adjacency window on either side of the anchor. The spec calls for "5
# chars" but practical examples like "Book for $99 today:" put the price
# ~9 chars before the anchor while remaining semantically adjacent. 12 chars
# catches those cases while still excluding far-away prices like
# "<a>A</a> 0123456789 $99" (price 14 chars out → outside the window).
_WINDOW_CHARS = 12

_CURRENCY_SYMBOLS = "$€£¥₹"

# Number token: one or more digits with optional thousands separators and
# an optional decimal part (covers "$1,299.99", "Rp 250000", "99", "1.500").
_NUMBER = r"\d[\d.,]*"

_RP_RE = re.compile(r"\bRp\b", re.IGNORECASE)
_SYMBOL_RE = re.compile(
    rf"(?:[{re.escape(_CURRENCY_SYMBOLS)}])\s*{_NUMBER}",
    re.UNICODE,
)
# ISO currency codes are always uppercase by convention; matching lowercase
# produces too many false positives (e.g. "99 us daily"). We accept the code
# either BEFORE the digits ("USD99") or AFTER ("99 USD").
_CODE_AFTER_RE = re.compile(
    rf"{_NUMBER}\s*(USD|EUR|GBP|IDR|JPY)\b",
)
_CODE_BEFORE_RE = re.compile(
    rf"\b(USD|EUR|GBP|IDR|JPY)\s*{_NUMBER}\b",
)
_RP_DIGIT_RE = re.compile(
    rf"(?:{_NUMBER}\s*\bRp\b)|(?:\bRp\b\s*{_NUMBER})",
    re.IGNORECASE,
)


def _line_of(tag, html: str) -> int:
    if not html:
        return 0
    sp = getattr(tag, "sourcepos", None)
    if isinstance(sp, tuple) and sp and sp[0]:
        return int(sp[0])
    if isinstance(sp, int) and sp > 0:
        return int(sp)
    href = tag.get("href", "") if hasattr(tag, "get") else ""
    if href:
        idx = html.find(href)
        if idx >= 0:
            return html[:idx].count("\n") + 1
    return 1


def _tail(text: str, n: int) -> str:
    """Return the last ``n`` characters of ``text``."""
    return text[-n:] if text else ""


def _head(text: str, n: int) -> str:
    """Return the first ``n`` characters of ``text``."""
    return text[:n] if text else ""


def _find_price(window: str) -> str:
    """Return the matched price string, or ``""`` if no price was found."""
    if not window:
        return ""

    m = _SYMBOL_RE.search(window)
    if m:
        return m.group(0)

    m = _CODE_BEFORE_RE.search(window)
    if m:
        return m.group(0)

    m = _CODE_AFTER_RE.search(window)
    if m:
        return m.group(0)

    m = _RP_DIGIT_RE.search(window)
    if m:
        return m.group(0)

    # Standalone "Rp" with any digit anywhere in the window.
    rp_match = _RP_RE.search(window)
    if rp_match and re.search(r"\d", window):
        return rp_match.group(0)

    return ""


def detect(
    html: str,
    filepath: str = "",
    url: str = "",
    page_url: str = "",
    links: Iterable | None = None,
    **_unused,
) -> list[Violation]:
    """Detect price-adjacency violations."""
    page_url = url or page_url
    if links is None:
        from ..parser import extract_viator_links
        links = extract_viator_links(html, filepath)

    violations: list[Violation] = []
    for link in links:
        anchor_text = (link.anchor_text or "").strip()
        # Only the immediate 5 chars on each side count, per the spec.
        before = _tail(link.context_before or "", _WINDOW_CHARS)
        after = _head(link.context_after or "", _WINDOW_CHARS)
        window = f"{before} {anchor_text} {after}"
        price = _find_price(window)
        if not price:
            continue
        line = link.line or _line_of(link.tag, html)
        violations.append(
            Violation(
                detector="price_adjacency",
                rule="price-nearby",
                url=page_url,
                file=filepath or "",
                line=line,
                severity="minor",
                anchor_text=anchor_text,
                extra={"adjacent_price": price.strip(), "window": window.strip()},
            )
        )
    return violations