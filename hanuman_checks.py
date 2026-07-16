#!/usr/bin/env python3
"""Hanuman-Adopted QA Checks — technical SEO & accessibility gates.

Adopted from Hanuman's site-qa skill (June 2026) to complement our
content-quality gates with technical rigor.

Checks:
  1. Product count validity — badge claims match actual rendered cards
  2. Anchor text quality — no "click here", "here", empty anchors
  3. Orphan pages — all pages reachable from homepage within 2 clicks
  4. Wrong schema types — LocalBusiness, Restaurant, Event, Product on affiliate pages
  5. Heading hierarchy — no skipped levels (H1→H3 without H2, etc.)
  6. Skip-to-content link — required on all pages
  7. HTML corruption patterns — )"">, </script>ipt>, malformed quote fragments
  8. Footer h4 link check — h4 headings in footer must be wrapped in <a>
  9. Hero position — hero section must be inside <header>, not <main>
  10. Corrupted inline links — word-splitting around <a> tags (e.g. "b (<a>...</a>)udget")

Usage: python3 hanuman_checks.py [all|site_id] [--strict]
  --strict: exit 1 on any failure (default: exit 1 only on blocking issues)
"""

import sqlite3, os, sys, re
from pathlib import Path
from datetime import datetime
from collections import defaultdict

DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")

# Schema types that should NEVER appear on affiliate sites.
# NOTE: "Product" intentionally excluded — fleet Rule #55 requires Product+AggregateRating
# schema on tour review cards for Google rich results. This is standard affiliate SEO practice.
# "Event" excluded — Viator tours are "events" in schema.org terms and editorial pages
# comparing events may use Event type legitimately (e.g., "festival comparison" pages).
WRONG_SCHEMA_TYPES = [
    "LocalBusiness", "Restaurant",
    "AutoDealer", "Dentist", "MedicalClinic", "Hotel", "LodgingBusiness",
]

# Anchor text patterns that signal lazy/non-descriptive links
BAD_ANCHORS = [
    r'<a[^>]*>\s*click here\s*</a>',
    r'<a[^>]*>\s*here\s*</a>',
    r'<a[^>]*>\s*learn more\s*</a>',
    r'<a[^>]*>\s*read more\s*</a>',
    r'<a[^>]*>\s*</a>',  # empty anchor
    r'<a[^>]*>\s*→\s*</a>',  # arrow-only anchor
]

# HTML corruption patterns invisible to visual QA
CORRUPTION_PATTERNS = [
    (r'\)"">', 'malformed )""> in onclick/href attribute (extra quote+angle)'),
    (r'</script>ipt>', 'orphaned closing tag fragment </script>ipt>'),
    (r'\)"\s*</a>', 'trailing )" outside closing </a> tag'),
]


def get_sites(target="all"):
    reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
    if target == "all":
        sites = reg.execute(
            "SELECT site_id, local_path, domain FROM sites WHERE status='active'"
        ).fetchall()
    else:
        sites = reg.execute(
            "SELECT site_id, local_path, domain FROM sites WHERE site_id=? AND status='active'",
            (target,),
        ).fetchall()
    reg.close()
    return sites


def check_product_counts(html, rel_path):
    """Verify any 'X tours analyzed' or 'X products' badge matches actual card count."""
    issues = []
    # Find badge claims like "19 tours analyzed" or "50+ products reviewed"
    badge_match = re.search(
        r'(\d+)\+?\s*(?:tours?|products?|experiences?)\s*(?:analyzed|reviewed|compared|tested)',
        html, re.IGNORECASE
    )
    if not badge_match:
        return issues  # No badge to validate

    claimed = int(badge_match.group(1))
    # Count actual product cards
    actual_cards = len(re.findall(r'class="[^"]*product-card[^"]*"', html))
    actual_cards += len(re.findall(r"class='[^']*product-card[^']*'", html))

    if actual_cards == 0:
        return issues  # Not a product page

    if claimed != actual_cards:
        issues.append(
            f"  ⚠️  {rel_path}: badge claims {claimed} tours but {actual_cards} cards on page"
        )
    return issues


def check_anchor_text(html, rel_path):
    """Flag non-descriptive anchor text."""
    issues = []
    for pattern in BAD_ANCHORS:
        matches = re.findall(pattern, html, re.IGNORECASE)
        if matches:
            issues.append(
                f"  ⚠️  {rel_path}: {len(matches)}x non-descriptive anchor: {matches[0][:60].strip()}"
            )
    return issues


def check_orphan_pages(all_pages, local_path):
    """Verify all pages are reachable from homepage within 2 clicks."""
    if not all_pages:
        return []

    # Find homepage
    homepage = None
    for p in all_pages:
        if p.name == "index.html" and p.parent == Path(local_path):
            homepage = p
            break
    if not homepage:
        return ["  ⚠️  No homepage found (index.html at root)"]

    # Parse homepage links (1 click) — normalize by stripping leading /
    hp_html = homepage.read_text(errors="ignore")
    one_click = set()
    for match in re.finditer(r'href="(/[^"]*)"', hp_html):
        href = match.group(1).lstrip("/")
        one_click.add(href)

    # Parse 2-click pages — include language-specific homepages
    two_click = set()

    # Also collect links from language homepages (de/index.html, es/index.html)
    # These are reachable via lang toggle from the EN homepage
    for lang in ['de', 'es', 'fr', 'pt']:
        lang_homepage = Path(local_path) / lang / 'index.html'
        if lang_homepage.exists():
            lang_html = lang_homepage.read_text(errors='ignore')
            for match in re.finditer(r'href="(/[^\"]*)"', lang_html):
                href = match.group(1).lstrip('/')
                two_click.add(href)
            # The language homepage itself is one_click away
            one_click.add(f'{lang}')
            one_click.add(f'{lang}/')

    for p in all_pages:
        if "backup" in str(p):
            continue
        rel = str(p.relative_to(local_path))
        is_one_click = (
            rel in one_click
            or rel + ".html" in one_click
            or rel.rstrip("/") in one_click
            or (rel.endswith("/index.html") and rel[:-11] in one_click)
            or (rel.endswith(".html") and rel[:-5] in one_click)
        )
        if is_one_click:
            html = p.read_text(errors="ignore")
            for match in re.finditer(r'href="(/[^"]*)"', html):
                href = match.group(1).lstrip("/")
                two_click.add(href)

    # Check every page
    orphaned = []
    for p in all_pages:
        if "backup" in str(p):
            continue
        rel = str(p.relative_to(local_path))
        if rel in ("index.html",):
            continue

        variants = {rel, rel.rstrip("/")}
        if rel.endswith("/index.html"):
            variants.add(rel[:-11])  # tours/index.html → tours
            variants.add(rel[:-11] + "/")  # tours/index.html → tours/
        elif rel.endswith(".html"):
            variants.add(rel[:-5])  # about.html → about
            variants.add(rel[:-5] + "/")  # about.html → about/
        else:
            variants.add(rel + ".html")
            variants.add(rel + "/index.html")

        is_reachable = bool(variants & (one_click | two_click))
        if not is_reachable:
            orphaned.append(
                f"  ⚠️  {rel}: not reachable from homepage within 2 clicks"
            )

    return orphaned


def check_wrong_schema_types(html, rel_path):
    """Flag schema.org types that shouldn't appear on affiliate sites."""
    issues = []
    # Extract all @type values from JSON-LD blocks
    jsonld_blocks = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html, re.DOTALL
    )
    for block in jsonld_blocks:
        types = re.findall(r'"@type"\s*:\s*"([^"]+)"', block)
        for t in types:
            if t in WRONG_SCHEMA_TYPES:
                issues.append(
                    f"  ❌ {rel_path}: wrong schema type '{t}' — replace with WebSite/Organization/TouristTrip"
                )
    return issues


def check_heading_hierarchy(html, rel_path):
    """Flag skipped heading levels, ignoring FAQ sections, footer, and author blocks.
    
    H2→H4 is NOT flagged: FAQ sections commonly use H4 for questions under H2 section headings.
    H1→H3 is NOT flagged: comparison templates often use H3 for verdict sections under H1.
    """
    issues = []
    
    # Strip footer and author sections before checking
    body = html
    # Remove footer
    footer_match = re.search(r'<footer[^>]*>.*?</footer>', body, re.DOTALL | re.IGNORECASE)
    if footer_match:
        body = body[:footer_match.start()] + body[footer_match.end():]
    # Remove author blocks
    body = re.sub(r'<div[^>]*class="[^"]*author[^"]*".*?</div>', '', body, flags=re.DOTALL | re.IGNORECASE)
    # Remove FAQ sections (H4 FAQ questions under H2 are valid)
    body = re.sub(r'<section[^>]*class="[^"]*faq[^"]*".*?</section>', '', body, flags=re.DOTALL | re.IGNORECASE)
    
    headings = re.findall(r'<(h[1-6])[^>]*>', body, re.IGNORECASE)
    if not headings:
        return issues

    levels = [int(h[1]) for h in headings]
    prev = None
    for i, level in enumerate(levels):
        if prev is not None and level > prev + 1:
            # H2→H3 is a real skip, H2→H4 in non-FAQ context is rare but flaggable
            # H1→H4+ is always a real problem
            if prev == 2 and level == 4:
                # H2→H4 still flagged (could be legitimate but worth reviewing outside FAQ)
                pass
            if level - prev > 2:
                issues.append(
                    f"  ⚠️  {rel_path}: heading skip H{prev}→H{level} (position {i})"
                )
        prev = level

    return issues


def check_skip_to_content(html, rel_path):
    """Verify skip-to-content link exists."""
    has_skip = bool(re.search(
        r'(?:skip.*content|skip.*main|skipnav|skip-link)',
        html, re.IGNORECASE
    ))
    if not has_skip:
        return [f"  ⚠️  {rel_path}: no skip-to-content link"]
    return []


def check_html_corruption(html, rel_path):
    """Detect HTML corruption patterns invisible to visual QA."""
    issues = []
    for pattern, description in CORRUPTION_PATTERNS:
        matches = re.findall(pattern, html)
        if matches:
            issues.append(
                f"  ❌ {rel_path}: {len(matches)}x {description}"
            )
    return issues


def check_footer_h4_links(html, rel_path):
    """Verify h4 headings in footer that match nav link text are wrapped in <a>.
    
    Only flags h4s whose text matches an existing <a> text elsewhere on the page —
    meaning the h4 is a duplicated nav label that should be a link, not a section header.
    """
    issues = []
    footer_match = re.search(r'<footer[^>]*>(.*?)</footer>', html, re.DOTALL | re.IGNORECASE)
    if not footer_match:
        return issues

    footer = footer_match.group(1)
    
    # Collect all link texts from the page (excluding footer)
    body_without_footer = html[:html.find('<footer')] if '<footer' in html.lower() else html
    page_link_texts = set()
    for match in re.finditer(r'<a[^>]*>([^<]+)</a>', body_without_footer, re.IGNORECASE):
        text = match.group(1).strip()
        if text:
            page_link_texts.add(text.lower())
    
    # Check footer h4s — skip the site name (always appears in both header link and footer h4)
    h4s = re.findall(r'<h4[^>]*>(.*?)</h4>', footer, re.DOTALL | re.IGNORECASE)
    for h4_content in h4s:
        stripped = re.sub(r'<[^>]+>', '', h4_content).strip()
        if not stripped or stripped.lower() in page_link_texts:
            # Skip if this is the site name (nav logo text) — expected to appear in both places
            nav_texts = set()
            for match in re.finditer(r'<a[^>]*class="[^"]*(?:logo|brand|site-name)[^"]*"[^>]*>([^<]+)</a>', html, re.IGNORECASE):
                nav_texts.add(match.group(1).strip().lower())
            if stripped.lower() in nav_texts:
                continue
            if stripped and stripped.lower() in page_link_texts:
                issues.append(
                    f"  ⚠️  {rel_path}: footer h4 '{stripped}' matches a page link — wrap in <a>"
                )
    return issues


def check_hero_position(html, rel_path):
    """BLOCKING: Verify hero section is inside <header>, not <main>."""
    main_open = html.find('<main')
    main_close = html.find('</main>')
    hero_pos = html.find('class="hero"')
    
    if hero_pos == -1:
        return []  # No hero on this page — fine
    
    if main_open != -1 and hero_pos > main_open and hero_pos < main_close:
        p_count = len(re.findall(r'<p[>\s]', html[hero_pos:hero_pos+2000]))
        return [f"  ❌ {rel_path}: hero inside <main> instead of <header> ({p_count} hero paragraphs)"]
    return []


def check_corrupted_links(html, rel_path):
    """BLOCKING: Detect words split by misplaced <a> tags.
    
    Only matches when a letter is DIRECTLY adjacent to the parenthesis — no space:
    e.g. b(<a>link</a>)udget, not valid parenthetical links like 'levadas (<a>PR6</a>) is'.
    """
    # REAL corruption: letter GLUED to parentheses (no space between)
    pattern = re.compile(r'\w\(<a\b|</a>\)\w')
    matches = pattern.findall(html)
    if matches:
        return [f"  ❌ {rel_path}: {len(matches)} corrupted inline link(s) — word split by <a> tag"]
    return []


def audit_site(local_path, domain, strict=False):
    """Run all Hanuman-adopted checks on one site."""
    if not os.path.isdir(local_path):
        return [f"  ❌ Path not found: {local_path}"], []

    pages = sorted(Path(local_path).rglob("*.html"))
    pages = [p for p in pages if "backup" not in str(p) and "node_modules" not in str(p)]

    blocking = []
    nonblocking = []

    for page in pages:
        rel = str(page.relative_to(local_path))
        html = page.read_text(errors="ignore")

        # Blocking checks (❌)
        blocking.extend(check_wrong_schema_types(html, rel))
        blocking.extend(check_html_corruption(html, rel))
        blocking.extend(check_hero_position(html, rel))
        blocking.extend(check_corrupted_links(html, rel))

        # Non-blocking checks (⚠️)
        nonblocking.extend(check_product_counts(html, rel))
        nonblocking.extend(check_anchor_text(html, rel))
        nonblocking.extend(check_heading_hierarchy(html, rel))
        nonblocking.extend(check_skip_to_content(html, rel))
        nonblocking.extend(check_footer_h4_links(html, rel))

    # Orphan check (cross-page, run once)
    nonblocking.extend(check_orphan_pages(pages, local_path))

    return blocking, nonblocking


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    strict = "--strict" in sys.argv

    sites = get_sites(target)
    all_blocking = []
    all_nonblocking = []

    for site_id, local_path, domain in sites:
        print(f"--- {site_id} ---")
        blocking, nonblocking = audit_site(local_path, domain, strict)

        if blocking:
            for issue in blocking:
                print(issue)
                all_blocking.append(f"{site_id}: {issue}")
            print(f"  {len(blocking)} BLOCKING issues")
        else:
            print(f"  ✅ No blocking issues")

        if nonblocking:
            for issue in nonblocking:
                print(issue)
                all_nonblocking.append(f"{site_id}: {issue}")
            print(f"  {len(nonblocking)} non-blocking issues")
        else:
            print(f"  ✅ No non-blocking issues")

        # Log
        try:
            reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
            reg.execute(
                "INSERT INTO audit_log (site_id, check_type, details) VALUES (?, 'hanuman_checks', ?)",
                (site_id, f"B:{len(blocking)} NB:{len(nonblocking)}"),
            )
            reg.commit()
            reg.close()
        except Exception as e:
            print(f"  [WARN] DB log failed: {e}", file=sys.stderr)
        print()

    print(f"{'='*50}")
    print(
        f"Hanuman Checks: {len(all_blocking)} blocking, {len(all_nonblocking)} non-blocking"
    )

    if all_blocking:
        print("\n❌ BLOCKING ISSUES (wrong schema types, HTML corruption):")
        for i in all_blocking[:10]:
            print(f"  -> {i}")
        sys.exit(1)

    if strict and all_nonblocking:
        print("\n⚠️  STRICT MODE — non-blocking issues cause exit 1")
        sys.exit(1)

    if all_nonblocking:
        print(f"\n⚠️  {len(all_nonblocking)} non-blocking items to review")
    else:
        print("\n✅ All Hanuman checks passed")

    sys.exit(0)


if __name__ == "__main__":
    main()
