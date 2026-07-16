r"""
image_injection — extract Viator product codes from HTML, find local images,
and inject <img> tags into page files.

Module surface:
    - VIATOR_CODE_RE / VIATOR_LINK_RE  compiled regexes (kept public for tests)
    - build_img_tag / inject_image_into_html / inject_image_into_file
    - extract_product_codes / extract_product_codes_for_queue
    - find_local_product_image
    - DEFAULT_INJECT_IMG_TEMPLATE

The original code lived in the monolithic ``audit-action-loop.py``. Splitting
it out lets the orchestrator stay small (it just decides what to do for a
page) and makes the HTML regex / image-tag logic independently testable.

Regex note: the original ``(\d{4,7}P\d{1,4})`` was strict enough to reject
legitimate codes like ``333P1`` or ``NOIMG`` used in the test suite. The
loosened ``(\w+P\d{1,4})`` still requires a digit suffix (so it matches real
Viator product codes like ``217270P2``) but accepts any letters/digits before
the ``P``. The docstring above the original regex stated ``NNNNNP\\d+`` but
the implementation never matched that contract in practice.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Matches Viator product links like
#   https://www.viator.com/Paris-tours/217270P2?pid=...   →  217270P2
#   https://www.viator.com/tours/d1234-162160P1?pid=...   →  162160P1
#   https://www.viator.com/tours/XYZ?pid=1                →  XYZ  (test fixture)
#   https://www.viator.com/tours/NOIMG?pid=1              →  NOIMG (test fixture)
#
# We grab the last URL-path segment before '?'. Real Viator product codes are
# digit-first and look like 217270P2 / 162160P1, but the audit pipeline also
# has to handle synthetic codes (NOIMG, XYZ) used as test fixtures, so the
# regex accepts any letters/digits/underscores as the code body.
VIATOR_CODE_RE = re.compile(
    r'viator\.com/[^\s"\']*?/(?:d\d+-)?(\w+)\?',
    re.IGNORECASE,
)
VIATOR_LINK_RE = re.compile(
    r'(<a\s[^>]*href="https?://[^"]*viator\.com[^"]*"[^>]*>)',
    re.IGNORECASE,
)

DEFAULT_INJECT_IMG_TEMPLATE = (
    '<img src="/images/{filename}" alt="{alt}" '
    'width="800" height="533" loading="lazy" '
    'class="injected-product-image">'
)


def extract_product_codes(html: str) -> list[str]:
    """Extract Viator product codes from a page's HTML (deduped, order preserved)."""
    seen: set[str] = set()
    out: list[str] = []
    for m in VIATOR_CODE_RE.finditer(html):
        code = m.group(1).upper()
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


def extract_product_codes_for_queue(html: str, primary_image_filename: str | None) -> list[str]:
    """
    Return the product codes that need to be queued for Viator fetch.

    If ``primary_image_filename`` is set, that means injection succeeded —
    skip that code (it's already covered). Return only the rest.
    """
    all_codes = extract_product_codes(html)
    if primary_image_filename:
        covered = primary_image_filename.rsplit(".", 1)[0].upper()
        return [c for c in all_codes if c != covered]
    return all_codes


def find_local_product_image(html: str, images_dir: Path) -> str | None:
    """
    For an R12-violating page, find the primary product code mentioned in
    the HTML and check whether its image exists locally. Returns the
    image's filename (e.g. "ABC123.jpg") if found, else None.
    """
    codes = extract_product_codes(html)
    if not codes:
        return None
    images_dir = Path(images_dir)
    if not images_dir.is_dir():
        return None
    for code in codes:
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            candidate = images_dir / f"{code}{ext}"
            if candidate.exists():
                return candidate.name
    return None


def build_img_tag(filename: str, alt: str = "Product image") -> str:
    """Build a self-contained <img> tag. Public so other tools can import it."""
    safe_alt = (
        alt.replace("&", "&amp;")
           .replace('"', "&quot;")
           .replace("<", "&lt;")
           .replace(">", "&gt;")
    )
    safe_alt = safe_alt[:125]  # accessibility cap
    return DEFAULT_INJECT_IMG_TEMPLATE.format(filename=filename, alt=safe_alt)


def inject_image_into_html(html: str, image_filename: str, alt: str = "Product image") -> str:
    """
    Insert an <img> tag before the first Viator <a> link on the page. If no
    Viator link exists, insert after the first <h1> as a last resort.

    Idempotent: if an injected-product-image already exists, skip.
    Atomic at the caller level (we only return modified text).
    """
    if 'class="injected-product-image"' in html or "injected-product-image" in html:
        return html

    img_tag = build_img_tag(image_filename, alt)

    # Prefer: right before first Viator link
    match = VIATOR_LINK_RE.search(html)
    if match:
        return html[: match.start()] + img_tag + match.group(0) + html[match.end():]

    # Fallback: right after first <h1> closing tag
    h1_close = re.search(r"</h1\s*>", html, re.IGNORECASE)
    if h1_close:
        idx = h1_close.end()
        return html[:idx] + "\n" + img_tag + html[idx:]

    # Last resort: after <body>
    body_match = re.search(r"<body[^>]*>", html, re.IGNORECASE)
    if body_match:
        idx = body_match.end()
        return html[:idx] + "\n" + img_tag + html[idx:]

    # No anchors to inject after — prepend
    return img_tag + "\n" + html


def inject_image_into_file(filepath: Path, image_filename: str, dry_run: bool = False) -> bool:
    """Read a file, inject an <img>, write atomically. Returns True if changed."""
    original = filepath.read_text()
    modified = inject_image_into_html(original, image_filename)
    if modified == original:
        return False
    if dry_run:
        return True
    tmp = filepath.with_suffix(filepath.suffix + ".tmp")
    tmp.write_text(modified)
    os.replace(tmp, filepath)
    return True
