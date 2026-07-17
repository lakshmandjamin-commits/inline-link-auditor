#!/usr/bin/env python3
"""
CTA audit v2.0: link density, HCU safety, placement position, and format checks.
Research-backed: Hypatia gbrain (travel-affiliate-link-placement-deepthink-2026).
Usage: python3 cta_audit.py [all|site_id] [--density-only]
"""
import sqlite3, os, sys, re
from datetime import datetime
from pathlib import Path
from bs4 import BeautifulSoup

DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")

# v2.0: Case-insensitive product code regex — handles lowercase, extra params, fragments, &amp;
PRODUCT_CODE_RE = re.compile(r'/d\d+-([A-Z0-9]+)', re.IGNORECASE)
GENERIC_VIATOR_RE = re.compile(r'href="https?://www\.viator\.com/(?!tours/)[^"]*\?pid=P00303273', re.IGNORECASE)

EXPECTED_CTR = {
    "comparison-table": 0.068,
    "verdict-cta": 0.056,
    "main-content": 0.041,
    "faq": 0.015,
    "sidebar": 0.0015,
    "footer": 0.002,
    "unknown": 0.020,
}

def get_sites(target="all"):
    reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
    if target == "all":
        sites = reg.execute(
            "SELECT site_id, local_path, domain FROM sites WHERE status='active'").fetchall()
    else:
        sites = reg.execute(
            "SELECT site_id, local_path, domain FROM sites WHERE site_id=? AND status='active'",
            (target,)).fetchall()
    reg.close()
    return sites

def _count_product_links(html):
    """Count Viator product links — scoped to href attributes only.
    
    v2.1: Excludes data-goatcounter-click attributes that also contain
    product codes. Double-counting inflated links by 2x (Jul 2026 subagent audit).
    """
    # Extract all Viator hrefs first, then count product codes within them
    hrefs = re.findall(r'href=["\'](https?://[^"\']*viator\.com[^"\']*/d\d+-[A-Z0-9]+)', html, re.IGNORECASE)
    return len(hrefs)

def _count_words(html):
    """Count words excluding HTML tags (v2.0)."""
    text = re.sub(r'<[^>]+>', ' ', html)
    return len(text.split())

def _is_exempt(page_path):
    """Skip utility/non-money pages (v2.0)."""
    path_lower = str(page_path).lower()
    exempt = ['contact', 'privacy', 'datenschutz', 'privacidad', 'kontakt',
              'contacto', 'impressum', 'terms', '404', '/about', 'about.html',
              'about/index', 'ueber-uns', 'acerca-de', 'planning', 'planificacion']
    return any(p in path_lower for p in exempt)

def _detect_generic_links(html):
    """Find Viator category URLs (no product code). Convert 3-5x lower (v2.0)."""
    matches = GENERIC_VIATOR_RE.findall(html)
    generic = [m for m in matches if not PRODUCT_CODE_RE.search(m)]
    return generic

def _classify_position(soup, a_tag):
    """Classify where a Viator link sits in the DOM (v2.0)."""
    parent = a_tag.parent
    depth = 0
    while parent and depth < 10:
        tag = parent.name if hasattr(parent, 'name') else ''
        if tag == 'table' and parent.get('class') and 'comparison' in str(parent.get('class')):
            return "comparison-table"
        cls = str(parent.get('class', ''))
        if 'verdict' in cls:
            return "verdict-cta"
        if tag in ('aside',):
            return "sidebar"
        if tag in ('footer',):
            return "footer"
        if tag == 'section' and 'faq' in cls:
            return "faq"
        parent = parent.parent if hasattr(parent, 'parent') else None
        depth += 1
    if a_tag.find_parent('main'):
        return "main-content"
    return "unknown"

def _classify_page_type(url_path, soup):
    """Classify page by URL pattern (v2.0)."""
    path_lower = str(url_path).lower()
    if any(x in path_lower for x in ['comparison', '-vs-', 'versus']):
        return "comparison"
    if 'review' in path_lower or '-review' in path_lower:
        return "review"
    if any(x in path_lower for x in ['best-', 'top-', 'buyers-guide', '-guide']):
        return "listicle"
    if any(x in path_lower for x in ['itinerary', 'day-trip', 'day-']):
        return "itinerary"
    if path_lower.endswith('index.html') or path_lower.endswith('/'):
        return "category-hub"
    return "informational"

def audit_page(html, page_path, domain):
    """Check CTA best practices + v2.0 research-backed checks."""
    issues = []

    # --- v1.x: Legacy CTA checks (preserved) ---
    text_ctas = len(re.findall(
        r'(?:Check\s+(?:price|availability)|Book\s+now|Learn\s+more\s*→|'
        r'Jetzt\s+buchen|Preis\s+prüfen|Verfügbarkeit\s+prüfen|Reservieren|'
        r'Mehr\s+erfahren\s*→|Reservar\s+ahora|Consultar\s+precio|'
        r'Más\s+información\s*→)', html, re.IGNORECASE))
    class_ctas = len(re.findall(r'<a\s[^>]*class="[^"]*cta[^"]*"[^>]*>', html, re.IGNORECASE))
    cta_count = text_ctas + class_ctas
    is_utility = _is_exempt(page_path)
    viator_hrefs = len(re.findall(r'href=["\'][^"\']*viator\.com', html, re.IGNORECASE))

    if cta_count == 0 and viator_hrefs == 0 and not is_utility:
        issues.append(f"  ⚠️  No CTAs on {page_path}")
    elif cta_count < 2 and "comparison" in str(page_path).lower():
        issues.append(f"  ⚠️  Only {cta_count} CTA on comparison page {page_path}")

    first_card = html.find('product-card')
    if first_card > -1:
        snippet = html[first_card:first_card+2500]
        if '<a href=' in snippet.lower() and 'viator.com' not in snippet.lower():
            pass
        elif 'viator.com' not in snippet.lower():
            issues.append(f"  ⚠️  First product card has no CTA link on {page_path}")

    if 'affiliate-disclosure' in html and 'style=' in html[html.find('affiliate-disclosure'):html.find('affiliate-disclosure')+200]:
        issues.append(f"  ⚠️  Disclosure uses inline style on {page_path}")

    # --- v2.0: Research-backed checks ---
    if is_utility:
        return issues  # Skip density/placement checks for utility pages

    links = _count_product_links(html)
    words = _count_words(html)
    page_type = _classify_page_type(page_path, None)

    # 1. Link density (v2.0 reasonable thresholds)
    if links == 0 and not is_utility:
        issues.append(f"  🔴 ZERO-LINKS: {page_path} — page is broken (hard gate)")
    elif links >= 10 and not is_utility:
        issues.append(f"  🔴 OVER-DENSITY: {page_path} — {links} links, max 9 (cannibalisation)")
    elif links < 2 and not is_utility:
        issues.append(f"  🟡 LOW-DENSITY: {page_path} — {links} links, advisory (target 3-5)")

    # 2. HCU-safe ratio (<1 link per 150 words)
    if links > 0 and words / links < 150:
        issues.append(f"  🔴 HCU-UNSAFE: {page_path} — {words/links:.0f} words/link, need ≥150")

    # 3. Generic link detection
    generic = _detect_generic_links(html)
    if generic:
        issues.append(f"  🟡 GENERIC: {page_path} — {len(generic)} category URL(s), convert 3-5x lower")

    # 4. Format check (comparison pages need ≥2 cards)
    if page_type == "comparison" and links < 2:
        issues.append(f"  🔴 FORMAT: {page_path} — comparison page needs ≥2 products")

    # 5. Placement position check
    try:
        soup = BeautifulSoup(html, 'html.parser')
        viator_links = soup.find_all('a', href=re.compile(r'viator\.com'))
        positions = {}
        for a in viator_links:
            pos = _classify_position(soup, a)
            positions[pos] = positions.get(pos, 0) + 1
        low_ctr_positions = [p for p in positions if EXPECTED_CTR.get(p, 0) < 0.015]
        if low_ctr_positions and len(positions) == len(low_ctr_positions):
            pos_str = ", ".join(low_ctr_positions)
            issues.append(f"  🟡 POSITION: {page_path} — all links in low-CTR position(s): {pos_str}")
    except Exception:
        pass  # Malformed HTML — skip position check

    return issues

def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    density_only = "--density-only" in sys.argv
    placement_only = "--placement-only" in sys.argv
    mode = "placement" if placement_only else ("density" if density_only else "full")
    print(f"CTA Audit v2.0 — {target} — {mode} mode — {datetime.now().isoformat()[:19]}\n")

    sites = get_sites(target)
    all_issues = []

    for site_id, local_path, domain in sites:
        print(f"--- {site_id} ---")
        site_path = Path(local_path)
        pages = sorted(site_path.rglob("*.html"))
        pages = [p for p in pages
                 if 'backup' not in str(p).lower()
                 and 'node_modules' not in str(p)
                 and '/.' not in str(p)]
        site_issues = []

        for page in pages:
            html = page.read_text(errors="ignore")
            rel = page.relative_to(site_path)
            issues = audit_page(html, rel, domain)
            site_issues.extend(issues)

        if site_issues:
            if placement_only:
                placement_issues = [i for i in site_issues if any(
                    x in i for x in ['POSITION', 'GENERIC', 'FORMAT'])]
                for issue in placement_issues:
                    print(f"  {issue}")
                print(f"  {len(placement_issues)} placement issues")
                all_issues.extend([f"{site_id}: {i}" for i in placement_issues])
            elif density_only:
                # Only show hard-gate density/HCU issues
                density_issues = [i for i in site_issues if any(
                    x in i for x in ['ZERO-LINKS', 'OVER-DENSITY', 'HCU-UNSAFE'])]
                for issue in density_issues:
                    print(f"  {issue}")
                print(f"  {len(density_issues)} density issues")
                all_issues.extend([f"{site_id}: {i}" for i in density_issues])
            else:
                for issue in site_issues:
                    print(f"  {issue}")
                    all_issues.append(f"{site_id}: {issue}")
                print(f"  {len(site_issues)} total issues")
        else:
            print(f"  All {len(pages)} pages pass")

        try:
            reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
            reg.execute(
                "INSERT INTO audit_log (site_id, check_type, details) VALUES (?, 'cta_audit_v2', ?)",
                (site_id, f"{len(site_issues)} issues across {len(pages)} pages"))
            reg.commit()
            reg.close()
        except Exception as e:
            print(f"  [WARN] DB update failed: {e}", file=sys.stderr)
        print()

    print(f"{'='*50}")
    print(f"CTA Audit v2.0 complete: {len(all_issues)} total issues")
    if all_issues:
        for i in all_issues[:15]:
            print(f"  -> {i}")
    sys.exit(0)

if __name__ == "__main__":
    main()
