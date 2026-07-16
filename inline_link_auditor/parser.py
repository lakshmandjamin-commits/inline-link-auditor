"""HTML parsing and Viator link extraction — shared by all detectors."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

from bs4 import BeautifulSoup, Tag

VIATOR_CODE_RE = re.compile(
    r"viator\.com[^\"'\s]*/?(?:d\d+-)?(\d{4,7}P\d{1,4})(?:\?|/|&|$)",
    re.IGNORECASE,
)
VIATOR_DOMAIN = re.compile(r"viator\.com", re.IGNORECASE)

# How many characters of surrounding text to expose on each side of an anchor.
_CONTEXT_CHARS = 50

_CARD_CLASSES = {"product-card", "comp-card", "card", "product-grid", "article-card"}


@dataclass
class ViatorLink:
    tag: object
    href: str
    anchor_text: str
    product_code: str = ""
    line: int = 0
    context_before: str = ""
    context_after: str = ""
    is_card: bool = False


def _sibling_text_before(tag: Tag, max_chars: int) -> str:
    """Concatenate text from sibling nodes preceding ``tag``, up to ``max_chars``."""
    buf: list[str] = []
    sibling = tag.previous_sibling
    while sibling is not None and sum(len(s) for s in buf) < max_chars:
        text = sibling.get_text() if hasattr(sibling, "get_text") else str(sibling)
        buf.append(text)
        sibling = sibling.previous_sibling
    return "".join(buf)[-max_chars:]


def _sibling_text_after(tag: Tag, max_chars: int) -> str:
    """Concatenate text from sibling nodes following ``tag``, up to ``max_chars``."""
    buf: list[str] = []
    sibling = tag.next_sibling
    while sibling is not None and sum(len(s) for s in buf) < max_chars:
        text = sibling.get_text() if hasattr(sibling, "get_text") else str(sibling)
        buf.append(text)
        sibling = sibling.next_sibling
    return "".join(buf)[:max_chars]


def _line_of_tag(tag: Tag, html: str) -> int:
    """Best-effort 1-based line number from BeautifulSoup sourcepos.

    The stdlib ``html.parser`` always reports sourcepos=0, so we fall back
    to counting ``\\n`` characters before the tag's href in the original
    source. lxml users get accurate per-line positions from sourcepos.
    """
    if not html:
        return 0
    sp = getattr(tag, "sourcepos", None)
    if isinstance(sp, tuple) and sp and sp[0]:
        return int(sp[0])
    if isinstance(sp, int) and sp > 0:
        return int(sp)
    # Fallback: count newlines before the href.
    href = tag.get("href", "")
    if href:
        idx = html.find(href)
        if idx >= 0:
            return html[:idx].count("\n") + 1
    return 1


def _is_inside_card(tag: Tag) -> bool:
    for parent in tag.parents:
        if parent.name == "body":
            break
        classes = set(parent.get("class", []) or [])
        if classes and classes & _CARD_CLASSES:
            return True
    return False


def extract_viator_links(html: str, filepath: str = "") -> List[ViatorLink]:
    """Return one ``ViatorLink`` per Viator <a> tag, with line + context."""
    soup = BeautifulSoup(html, "html.parser")
    for el in soup(["script", "style", "noscript"]):
        el.decompose()

    links: list[ViatorLink] = []
    for a_tag in soup.find_all("a", href=True):
        href = a_tag.get("href", "")
        if not VIATOR_DOMAIN.search(href):
            continue

        anchor_text = a_tag.get_text(strip=True)
        code_match = VIATOR_CODE_RE.search(href)
        product_code = code_match.group(1) if code_match else ""

        ctx_before = _sibling_text_before(a_tag, _CONTEXT_CHARS).strip()
        ctx_after = _sibling_text_after(a_tag, _CONTEXT_CHARS).strip()
        line = _line_of_tag(a_tag, html)
        is_card = _is_inside_card(a_tag)

        links.append(ViatorLink(
            tag=a_tag,
            href=href,
            anchor_text=anchor_text,
            product_code=product_code,
            line=line,
            context_before=ctx_before,
            context_after=ctx_after,
            is_card=is_card,
        ))
    return links


def load_pages(site_path) -> list[tuple]:
    """Load all ``*.html`` files under ``site_path`` and return ``[(filepath, html), ...]``."""
    pages: list[tuple[str, str]] = []
    site = Path(site_path)
    if not site.exists():
        return pages
    for html_file in site.rglob("*.html"):
        if html_file.name.startswith("."):
            continue
        try:
            html = html_file.read_text(encoding="utf-8")
            if len(html) > 100:
                pages.append((str(html_file), html))
        except Exception:
            continue
    return pages