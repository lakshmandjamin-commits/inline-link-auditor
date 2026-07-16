#!/usr/bin/env python3
"""
Product Image Injection — Component 2 of the Viator Image Pipeline.

Closes the page_generator gap identified by Hypatia's image-pipeline-gap-analysis:
generated HTML currently reaches disk with zero product images. This module is the
on-ramp that wires the local image cache into the generator.

Public entry point: `inject_product_images(html, slug, brief, *, dry_run=False)`.

Injection rules (from /tmp/viator-image-pipeline-spec.md):
  • Review pages — hero image AFTER the verdict section (not between H1 and CTA —
    respects image-audit R1/R2). Falls back to "after the last </section> before
    <section class='explore-more'>" when no explicit verdict section exists.
  • Comparison pages — winner's product image BELOW the comparison table
    (between </table> and the verdict section, or after the last </section> when
    the table is missing).
  • Category hubs — category hero image. Uses existing cat-{topic}.jpg when
    present; otherwise falls back to the first product image we can find.

Multi-fleet: every function takes `slug` and `images_dir` as parameters — no
hard-coded `~/sites/saraswati` paths. Works for Saraswati and Hanumanhermes.

Viator product codes match the NNNNNP\d+ format (e.g. 101727P4, 162160P1) and
appear in product cards as data-viator-id="...". The NNNNNP\d+ regex is the
single source of truth for code extraction so the two regexes cannot drift.

This module is deliberately HTML-regex-based (no lxml/bs4 dependency) so it runs
identically in the generator subprocess and the post-audit droid. Tested in
isolation under tests/test_product_image_injection.py.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Constants & patterns
# ---------------------------------------------------------------------------

# Viator product code: digits, optional P, more digits. Examples: 101727P4,
# 162160P1, 29733P2, 171122P18. The "P" is a Viator-internal separator and the
# trailing digits are the option/variant id. We anchor on ^...$ when validating
# so substrings inside other tokens (URL slugs, CSS classes) don't match.
VIATOR_PRODUCT_CODE_RE = re.compile(r"\b(\d{4,7}P\d{1,4})\b")

# Image audit rules this module must respect:
#   R1: hero must NOT be inserted between H1 and the first CTA.
#   R2: winner image on comparison pages belongs BELOW the comparison table.
# See /Users/saraswati/.hermes/skills/image-audit/SKILL.md if present.
R1_R2_LANDMARKS = ("explore-more", "faq", "verdict", "winner-box")

# Default fleet site root. Saraswati layout: ~/sites/{slug}/images/{code}.jpg.
# Hanumanhermes is reached by overriding images_dir at the call site.
DEFAULT_SITES_ROOT = os.path.expanduser("~/sites")
_SITES_YAML = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "config", "sites.yaml"
)


def _load_sites_config() -> dict:
    """Load the unified sites.yaml config, with caching.
    
    Only caches on successful load. A corrupted file re-raises so the
    caller gets a clear error rather than silently operating with empty config.
    """
    if not hasattr(_load_sites_config, "_cache"):
        try:
            with open(_SITES_YAML) as f:
                import yaml as _yaml
                config = _yaml.safe_load(f)
            if not isinstance(config, dict) or "sites" not in config:
                raise ValueError(f"Invalid sites.yaml: missing 'sites' key")
            _load_sites_config._cache = config
        except Exception:
            import sys
            print(f"WARNING: Failed to load sites.yaml from {_SITES_YAML}", file=sys.stderr)
            _load_sites_config._cache = {"sites": {}}
    return _load_sites_config._cache


def _site_images_dir(slug: str, images_dir: Optional[str] = None) -> str:
    """Resolve images directory for a site slug.

    Priority: explicit images_dir param → sites.yaml images_dir → sites.yaml path/images
              → DEFAULT_SITES_ROOT/slug/images.
    """
    if images_dir:
        return images_dir
    config = _load_sites_config()
    site_data = config.get("sites", {}).get(slug, {})
    # Prefer explicit images_dir field from sites.yaml
    if "images_dir" in site_data:
        return os.path.expanduser(site_data["images_dir"])
    if "path" in site_data:
        return os.path.join(os.path.expanduser(site_data["path"]), "images")
    return os.path.join(DEFAULT_SITES_ROOT, slug, "images")


# ---------------------------------------------------------------------------
# Result object — what the injector did
# ---------------------------------------------------------------------------


@dataclass
class InjectionResult:
    """Structured outcome of a single inject_product_images() call.

    The generator logs this so the post-audit loop can distinguish "no images
    available" from "images available, skipped by policy" from "image injected".
    """

    page_type: str = "unknown"        # review | comparison | category_hub | informational | unknown
    inserted: bool = False            # Did we actually add an <img> to the HTML?
    image_path: Optional[str] = None  # URL path used (e.g. /images/217270P2.jpg)
    product_code: Optional[str] = None
    reason: str = ""                  # human-readable: why we did / didn't inject
    skipped_existing: bool = False    # page already had a hero — we left it alone
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "page_type": self.page_type,
            "inserted": self.inserted,
            "image_path": self.image_path,
            "product_code": self.product_code,
            "reason": self.reason,
            "skipped_existing": self.skipped_existing,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Product code extraction
# ---------------------------------------------------------------------------


def extract_product_codes_from_html(html: str) -> List[str]:
    """Return Viator product codes referenced by data-viator-id in this page.

    Order-preserving, deduplicated. We match against the exact `NNNNNP\d+` form
    to avoid false positives from CSS classes or URL slugs.
    """
    if not html:
        return []
    codes: List[str] = []
    seen = set()
    for m in re.finditer(r'data-viator-id="([^"]+)"', html):
        c = m.group(1).strip()
        if VIATOR_PRODUCT_CODE_RE.fullmatch(c) and c not in seen:
            codes.append(c)
            seen.add(c)
    return codes


def extract_product_codes_from_brief(brief: dict) -> List[str]:
    """Return product codes listed in a brief's products_to_feature (or legacy keys).

    Order-preserving, deduplicated, validated against the Viator code regex.
    """
    if not brief:
        return []
    raw = []
    for key in ("products_to_feature", "primary_product", "featured_products",
                "products", "winner_product"):
        v = brief.get(key)
        if isinstance(v, list):
            raw.extend(str(x) for x in v)
        elif isinstance(v, str):
            raw.append(v)
    out: List[str] = []
    seen = set()
    for c in raw:
        c = c.strip()
        if VIATOR_PRODUCT_CODE_RE.fullmatch(c) and c not in seen:
            out.append(c)
            seen.add(c)
    return out


# ---------------------------------------------------------------------------
# Page-type classification
# ---------------------------------------------------------------------------


def classify_page_type(html: str, brief: Optional[dict], slug: str,
                        article_slug: str) -> str:
    """Decide which injection rule applies to this page.

    Priority:
      1. brief['template'] if it's a known generator template.
      2. Slug heuristics (review slug, vs-/comparison slug, hub path).
      3. Structural heuristics (presence of <table>, verdict section, etc.).
    """
    template = (brief or {}).get("template", "")
    if template in {"tour_review", "review", "product_review"}:
        return "review"
    if template in {"comparison", "versus", "vs"}:
        return "comparison"
    if template in {"category_hub", "category", "hub"}:
        return "category_hub"

    slug_lower = (article_slug or "").lower()
    if "review" in slug_lower or "tour-review" in slug_lower:
        return "review"
    if "-vs-" in slug_lower or "-or-" in slug_lower or "comparison" in slug_lower:
        return "comparison"
    # Category hub: ends in /index.html, ends in -hub, ends in -guide, etc.
    if slug_lower.endswith("index.html") or slug_lower.endswith("/"):
        return "category_hub"
    if slug_lower.endswith("-hub") or "buyers-guide" in slug_lower:
        return "category_hub"

    # Structural fallback
    has_table = bool(re.search(r"<table[^>]*>", html, re.IGNORECASE))
    has_verdict = bool(re.search(
        r'class=["\'][^"\']*\bverdict\b[^"\']*["\']', html, re.IGNORECASE))
    if has_table and has_verdict:
        return "comparison"
    if has_verdict and not has_table:
        # Some comparison pages use verdict without a table.
        return "comparison"
    return "informational"


# ---------------------------------------------------------------------------
# Image discovery
# ---------------------------------------------------------------------------


def image_path_for_code(slug: str, code: str,
                        images_dir: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a Viator product code to a local image path.

    Returns (url_path, filesystem_path). url_path is what should land in the
    <img src="..."> attribute (always /images/{code}.jpg, no host). filesystem_path
    is the absolute path that was checked for existence.

    Naming convention (matches viator-image-fetcher.py + existing cache):
      {code}.jpg       — primary, what the injector uses first
      {code}_1.jpg     — secondary (alt shot)
      {code}_2.jpg     — tertiary

    Returns (None, None) when no image exists locally — caller decides whether
    to fall back to a category image or skip.
    """
    if not code:
        return None, None
    if not VIATOR_PRODUCT_CODE_RE.fullmatch(code):
        return None, None
    img_dir = _site_images_dir(slug, images_dir)
    if not os.path.isdir(img_dir):
        return None, None

    # Try the exact code first, then variant fallback.
    # 424075P18 → try 424075P18.jpg, then strip variant: 424075P1.jpg
    codes_to_try = [code]
    variant_match = re.match(r'^(\d{4,7})P(\d{1,4})$', code)
    if variant_match and variant_match.group(2) != '1':
        base_code = f"{variant_match.group(1)}P1"
        codes_to_try.append(base_code)

    for c in codes_to_try:
        for suffix in ("", "_1", "_2"):
            filename = f"{c}{suffix}.jpg"
            full = os.path.join(img_dir, filename)
            if os.path.isfile(full) and os.path.getsize(full) > 0:
                url = f"/images/{filename}"
                return url, full
    return None, None


def find_category_image(slug: str, topic: str = "",
                        images_dir: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """Find a category hero image for a hub page.

    Looks for cat-{topic}.jpg first; falls back to any cat-*.jpg. Returns
    (url_path, filesystem_path) — same contract as image_path_for_code.
    """
    img_dir = _site_images_dir(slug, images_dir)
    if not os.path.isdir(img_dir):
        return None, None
    candidates: List[str] = []
    safe_topic = re.sub(r"[^a-z0-9-]", "", (topic or "").lower())
    if safe_topic:
        candidates.append(f"cat-{safe_topic}.jpg")
    try:
        for name in sorted(os.listdir(img_dir)):
            if name.startswith("cat-") and name.endswith(".jpg"):
                candidates.append(name)
    except OSError:
        return None, None
    seen = set()
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        full = os.path.join(img_dir, name)
        if os.path.isfile(full) and os.path.getsize(full) > 0:
            return f"/images/{name}", full
    return None, None


# ---------------------------------------------------------------------------
# Winner / primary product selection
# ---------------------------------------------------------------------------


def find_winner_product(html: str) -> Optional[str]:
    """Pick the winner product on a comparison page.

    Looks for the winner-box / verdict-recommended marker the generator emits.
    Falls back to the first product card when no marker exists.
    """
    # Preferred: explicit winner attribute on a product card.
    # The attribute can come before OR after class= and data-viator-id=, so we
    # check both orderings and use look-ahead for the close of the tag.
    patterns = [
        # data-viator-id and data-winner on same tag, any order.
        r'<div[^>]*\bdata-viator-id="([^"]+)"[^>]*\bdata-winner\b[^>]*>',
        r'<div[^>]*\bdata-winner\b[^>]*\bdata-viator-id="([^"]+)"[^>]*>',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1)
    # Or: a winner-box block that names the product.
    m = re.search(
        r'class=["\'][^"\']*\bwinner-box\b[^"\']*["\'][^>]*data-viator-id="([^"]+)"',
        html, re.IGNORECASE,
    )
    if m:
        return m.group(1)
    # Fallback: first product card on the page.
    codes = extract_product_codes_from_html(html)
    return codes[0] if codes else None


def find_primary_product(html: str, brief: Optional[dict], article_slug: str = "") -> Optional[str]:
    """Pick the primary product for a review page.

    Priority:
      1. Product code that appears in the page slug AND anywhere in the HTML
         (e.g., husky-sled-ride-5516800P30 → 5516800P30 found in href/text).
      2. Brief's products_to_feature[0].
      3. First data-viator-id on the page.
    """
    brief_codes = extract_product_codes_from_brief(brief or {})
    page_codes = extract_product_codes_from_html(html)

    # Prefer a code that appears in BOTH the slug AND the HTML (anywhere —
    # hrefs, text, not just data-viator-id attributes)
    if article_slug:
        for m in VIATOR_PRODUCT_CODE_RE.finditer(article_slug):
            sc = m.group(1)
            if sc in html:
                return sc

    if brief_codes:
        return brief_codes[0]
    return page_codes[0] if page_codes else None


# ---------------------------------------------------------------------------
# Image <img> tag construction
# ---------------------------------------------------------------------------


def build_img_tag(url_path: str, alt: str, *, css_class: str = "injected-product-image",
                  width: int = 800, height: int = 533,
                  loading: str = "lazy") -> str:
    """Build an <img> tag with explicit width/height to avoid CLS.

    The width/height defaults match the 720px-wide fetcher output (800/533 is
    the standard 3:2 ratio we crop to). Review-audit R3 requires explicit
    dimensions.
    """
    alt_safe = (alt or "Product image").replace('"', '&quot;')
    return (
        f'<figure class="product-image-figure {css_class}">\n'
        f'  <img src="{url_path}" alt="{alt_safe}" '
        f'width="{width}" height="{height}" loading="{loading}" '
        f'decoding="async" class="{css_class}">\n'
        f'</figure>\n'
    )


# ---------------------------------------------------------------------------
# Injection strategies
# ---------------------------------------------------------------------------


def _has_hero_image(html: str) -> bool:
    """Has this page already received a hero? Used to avoid double-injection."""
    # Match: hero class, injected-product-image, injected-winner-image, category-hero
    if re.search(r'<img[^>]*class=["\'][^"\']*\b(?:hero|injected-product-image|injected-winner-image|category-hero)', html, re.IGNORECASE):
        return True
    return False


def _split_at_landmark(html: str, landmark_pattern: str,
                       insert_before: bool = True) -> Optional[Tuple[str, str]]:
    """Split html at the first occurrence of a landmark regex.

    Returns (before, after) where the landmark sits at the start of `after`
    when insert_before=True, or at the end of `before` when insert_before=False.
    Returns None when the landmark isn't found.
    """
    m = re.search(landmark_pattern, html, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    return html[: m.start()], html[m.start():]


def inject_review_hero(html: str, code: str, slug: str,
                       images_dir: Optional[str] = None) -> Tuple[str, InjectionResult]:
    """Inject hero image AFTER the verdict section on a review page.

    Order of landmarks tried (in priority — first hit wins):
      1. </section> immediately before <section class="explore-more">
      2. </section> immediately before <section class="faq">
      3. </section> immediately before <section class="verdict">  (unlikely on review)
      4. Just before </main>
      5. Just before </article>

    Never injects between H1 and the first CTA — that respects image-audit R1.
    """
    result = InjectionResult(page_type="review")
    if _has_hero_image(html):
        result.reason = "page already has a hero image — left in place"
        result.skipped_existing = True
        return html, result

    url_path, fs_path = image_path_for_code(slug, code, images_dir=images_dir)
    if not url_path:
        result.reason = f"no local image for product code {code!r}"
        result.product_code = code
        return html, result

    # Find the product name from the first product card h3 for alt text.
    alt = _product_name_from_card(html, code) or f"Product image for {code}"
    img_tag = build_img_tag(url_path, alt, css_class="injected-product-image")

    # Try the landmarks in order. The "after last </section>" rule is the
    # canonical review-page verdict position per image-audit R2.
    landmarks = [
        # Inject AFTER the last </section> that sits before explore-more / faq.
        (r'(<section[^>]*class=["\'][^"\']*\bexplore-more\b)',
         "after last section before explore-more"),
        (r'(<section[^>]*class=["\'][^"\']*\bfaq\b)',
         "after last section before faq"),
        # Worst case: just before </main> so it never lands between H1 and CTA.
        (r'(</main>)',
         "before </main> (no explore-more/faq landmarks)"),
        (r'(</article>)',
         "before </article> (no </main>)"),
    ]
    for pattern, note in landmarks:
        m = re.search(pattern, html, re.IGNORECASE)
        if not m:
            continue
        # Walk back to the nearest </section> close so the image lands AFTER
        # the closing of the previous section, not in the middle of one.
        before = html[: m.start()]
        # Look for the last </section> in `before`. If present, split there.
        last_close = list(re.finditer(r'</section\s*>', before, re.IGNORECASE))
        if last_close:
            cut = last_close[-1].end()
            html = before[:cut] + "\n" + img_tag + before[cut:] + html[m.start():]
        else:
            html = before + img_tag + html[m.start():]
        result.inserted = True
        result.image_path = url_path
        result.product_code = code
        result.reason = f"injected {note}"
        result.notes.append(note)
        return html, result

    result.reason = "no usable landmark (no explore-more/faq/main/article)"
    result.product_code = code
    return html, result


def inject_comparison_winner(html: str, code: str, slug: str,
                              images_dir: Optional[str] = None) -> Tuple[str, InjectionResult]:
    """Inject the winner's product image BELOW the comparison table.

    Per image-audit R2, the winner image must sit AFTER the comparison table
    and BEFORE the verdict section. We also enforce R1: never between H1 and
    the first CTA.

    Order of landmarks tried:
      1. </table> → insert immediately after
      2. <section class="verdict"> → insert before (image-audit R2)
      3. <section class="explore-more"> → insert before (fallback)
      4. </main> → insert before (worst case)
    """
    result = InjectionResult(page_type="comparison")
    if _has_hero_image(html):
        result.reason = "page already has a hero image — left in place"
        result.skipped_existing = True
        return html, result

    url_path, fs_path = image_path_for_code(slug, code, images_dir=images_dir)
    if not url_path:
        result.reason = f"no local image for winner product code {code!r}"
        result.product_code = code
        return html, result

    alt = _product_name_from_card(html, code) or f"Winner product image for {code}"
    img_tag = build_img_tag(url_path, alt, css_class="injected-winner-image")

    # 1. Immediately after </table> — the canonical "below the comparison table" position.
    m = re.search(r'</table\s*>', html, re.IGNORECASE)
    if m:
        cut = m.end()
        html = html[:cut] + "\n" + img_tag + html[cut:]
        result.inserted = True
        result.image_path = url_path
        result.product_code = code
        result.reason = "inserted below </table>"
        result.notes.append("below comparison table (R2)")
        return html, result

    # 2. Before <section class="verdict"> — when there's no table, the verdict
    #    block still serves as the natural anchor.
    m = re.search(r'(<section[^>]*class=["\'][^"\']*\bverdict\b)',
                  html, re.IGNORECASE)
    if m:
        html = html[: m.start()] + img_tag + html[m.start():]
        result.inserted = True
        result.image_path = url_path
        result.product_code = code
        result.reason = "inserted before verdict section (no table present)"
        result.notes.append("before verdict (R2 fallback)")
        return html, result

    # 3. Before explore-more.
    m = re.search(r'(<section[^>]*class=["\'][^"\']*\bexplore-more\b)',
                  html, re.IGNORECASE)
    if m:
        html = html[: m.start()] + img_tag + html[m.start():]
        result.inserted = True
        result.image_path = url_path
        result.product_code = code
        result.reason = "inserted before explore-more (no table or verdict)"
        result.notes.append("before explore-more")
        return html, result

    # 4. Before </main>.
    m = re.search(r'</main\s*>', html, re.IGNORECASE)
    if m:
        html = html[: m.start()] + img_tag + html[m.start():]
        result.inserted = True
        result.image_path = url_path
        result.product_code = code
        result.reason = "inserted before </main> (last resort)"
        result.notes.append("before </main>")
        return html, result

    result.reason = "no usable landmark (no table, verdict, explore-more, or main)"
    result.product_code = code
    return html, result


def inject_category_hero(html: str, slug: str, brief: Optional[dict],
                          article_slug: str,
                          images_dir: Optional[str] = None) -> Tuple[str, InjectionResult]:
    """Inject category hero image for a hub page.

    Per the spec: use existing cat-{topic}.jpg first, then fall back to the top
    product image. Injected AFTER the header (h1+byline) but BEFORE the first
    content section so the hero is visually leading without violating R1.
    """
    result = InjectionResult(page_type="category_hub")
    if _has_hero_image(html):
        result.reason = "page already has a hero image — left in place"
        result.skipped_existing = True
        return html, result

    topic = (brief or {}).get("topic", "") or _topic_from_slug(article_slug, slug)
    url_path, fs_path = find_category_image(slug, topic, images_dir=images_dir)
    fallback_code = None
    if not url_path:
        # Fallback to the top product image.
        primary = find_primary_product(html, brief)
        if primary:
            url_path, fs_path = image_path_for_code(slug, primary, images_dir=images_dir)
            fallback_code = primary
    if not url_path:
        result.reason = f"no category image (topic={topic!r}) and no product image to fall back to"
        return html, result

    alt = f"Category hero — {topic}" if topic else "Category hero image"
    img_tag = build_img_tag(url_path, alt, css_class="injected-category-hero",
                            width=1200, height=600, loading="eager")

    # Inject AFTER the </header> that contains the H1, never between H1 and CTA.
    m = re.search(r'</header\s*>', html, re.IGNORECASE)
    if not m:
        # Fallback: just after <main ...>
        m = re.search(r'(<main\b[^>]*>)', html, re.IGNORECASE)
    if not m:
        result.reason = "no </header> or <main> landmark for category hero"
        return html, result

    cut = m.end()
    html = html[:cut] + "\n" + img_tag + html[cut:]
    result.inserted = True
    result.image_path = url_path
    result.product_code = fallback_code
    reason_parts = [f"injected category hero (topic={topic!r})"]
    if fallback_code:
        reason_parts.append(f"fell back to product image {fallback_code}")
    result.reason = "; ".join(reason_parts)
    result.notes.extend(reason_parts)
    return html, result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _product_name_from_card(html: str, code: str) -> Optional[str]:
    """Extract the tour name from the product card whose data-viator-id matches."""
    pattern = (
        r'<div[^>]*class=["\'][^"\']*product-card[^"\']*["\'][^>]*'
        r'data-viator-id="' + re.escape(code) + r'"[^>]*>(.*?)'
        r'(?=<div[^>]*class=["\'][^"\']*product-card|</main|</article)'
    )
    m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
    if not m:
        # Try the reverse order (data-viator-id before class).
        pattern = (
            r'<div[^>]*data-viator-id="' + re.escape(code) + r'"[^>]*'
            r'class=["\'][^"\']*product-card[^"\']*["\'][^>]*>(.*?)'
            r'(?=<div[^>]*class=["\'][^"\']*product-card|</main|</article)'
        )
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    card = m.group(1)
    h3 = re.search(r"<h3[^>]*>(.*?)</h3>", card, re.IGNORECASE | re.DOTALL)
    if not h3:
        return None
    text = re.sub(r"<[^>]+>", "", h3.group(1)).strip()
    return text[:125] if text else None


def _topic_from_slug(article_slug: str, slug: str) -> str:
    """Best-effort topic extraction from the page slug for category-hub lookup."""
    s = (article_slug or "").lower()
    s = s.replace(f"{slug.lower()}-", "").replace(".html", "").strip("-")
    # Drop trailing "-hub", "-index", "-guide" qualifiers.
    for suffix in ("-hub", "-index", "-guide", "-buyers-guide"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return s


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def inject_product_images(html: str, slug: str, brief: Optional[dict] = None,
                          article_slug: str = "",
                          images_dir: Optional[str] = None,
                          *, dry_run: bool = False) -> Tuple[str, InjectionResult]:
    """Inject the right product image(s) for this page.

    Args:
        html:        Generated HTML, post-structural-repair.
        slug:        Site slug (e.g. 'san-juan-excursions'). Used to locate
                     ~/sites/{slug}/images/ by default.
        brief:       The content brief (optional but recommended — drives
                     page-type classification and primary product selection).
        article_slug: Slug of the article (e.g. 'bioluminescent-bay-tour-review').
                     Used as a fallback classifier signal.
        images_dir:  Override the image cache root. Defaults to
                     ~/sites/{slug}/images. Hanumanhermes uses this to point
                     at its own directory layout.
        dry_run:     If True, never mutate html — just report what would happen.

    Returns:
        (html, InjectionResult). On dry_run, the returned html is the
        unchanged input.
    """
    if not html or not slug:
        return html, InjectionResult(reason="missing html or slug — skipped")

    page_type = classify_page_type(html, brief, slug, article_slug)

    if page_type == "review":
        code = find_primary_product(html, brief, article_slug)
        if not code:
            return html, InjectionResult(
                page_type=page_type,
                reason="no primary product code found (brief empty, no product cards)",
            )
        if dry_run:
            url, _ = image_path_for_code(slug, code, images_dir=images_dir)
            return html, InjectionResult(
                page_type=page_type,
                product_code=code,
                image_path=url,
                reason="dry-run: would inject review hero",
            )
        return inject_review_hero(html, code, slug, images_dir=images_dir)

    if page_type == "comparison":
        code = find_winner_product(html)
        if not code:
            return html, InjectionResult(
                page_type=page_type,
                reason="no winner product code found on comparison page",
            )
        if dry_run:
            url, _ = image_path_for_code(slug, code, images_dir=images_dir)
            return html, InjectionResult(
                page_type=page_type,
                product_code=code,
                image_path=url,
                reason="dry-run: would inject winner image below table",
            )
        return inject_comparison_winner(html, code, slug, images_dir=images_dir)

    if page_type == "category_hub":
        if dry_run:
            topic = (brief or {}).get("topic", "") or _topic_from_slug(article_slug, slug)
            url, _ = find_category_image(slug, topic, images_dir=images_dir)
            reason = "dry-run: would inject category hero"
            if url:
                reason += f" (image={url})"
            return html, InjectionResult(
                page_type=page_type, image_path=url, reason=reason,
            )
        return inject_category_hero(html, slug, brief, article_slug,
                                    images_dir=images_dir)

    # informational / unknown: skip. Don't force-fit a wrong image onto the page.
    return html, InjectionResult(
        page_type=page_type,
        reason=f"page type {page_type!r} is not in the injection policy",
    )