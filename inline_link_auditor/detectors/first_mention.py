"""Detector 2: first-mention counter (Placement rule).

A product should be linked once in prose, then surfaced through cards /
buttons. Repeated prose links for the same product signal over-optimization
(Google "link spam" patterns).

Detection rule
--------------
For every Viator link per file, group by ``product_code``. Positions 2..N in
prose (non-card) are flagged.

Severity: ``major``.
"""

from __future__ import annotations

import re
from typing import Iterable

from ..models import Violation

_PRODUCT_CODE_RE = re.compile(r"(\d{4,7}P\d{1,4})", re.IGNORECASE)


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

    seen: dict[str, int] = {}
    violations: list[Violation] = []
    for link in links:
        code = link.product_code
        if not code:
            m = _PRODUCT_CODE_RE.search(link.href or "")
            code = m.group(1) if m else ""
        if not code:
            continue
        # Skip links inside cards/buttons — they're the recommended pattern.
        if getattr(link, "is_card", False):
            continue
        seen[code] = seen.get(code, 0) + 1
        if seen[code] > 1:
            violations.append(
                Violation(
                    detector="first_mention",
                    rule="repeated-link",
                    url=page_url,
                    file=filepath or "",
                    line=link.line or 0,
                    severity="major",
                    product_code=code,
                    anchor_text=link.anchor_text or "",
                    extra={"occurrence": seen[code]},
                )
            )
    return violations