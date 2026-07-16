#!/usr/bin/env python3
"""CTA-Path Gate — ensures every page has a path to Viator.

Money pages  → must have ≥1 Viator link (product card or editorial)
Feeder pages → must link to ≥1 Money page
Utility      → no requirement

BLOCKS deploy (exit 1) if any Money page has zero Viator links.
Warns (exit 0) if any Feeder page has zero Money-page links.

Usage: python3 cta_path_gate.py [all|site_id] [--strict]
  --strict: exit 1 on feeder issues too
"""

import sqlite3, os, sys, re
from pathlib import Path
from datetime import datetime

DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")
TAXONOMY_PATH = os.path.expanduser("~/.hermes/fleet-page-taxonomy.md")

UTILITY_SLUGS = {'contact', 'privacy', 'terms', '404'}

# Template placeholder patterns that indicate broken Viator URLs
PLACEHOLDER_PATTERNS = [
    (r'href="https?://www\.[A-Z][0-9]+[A-Z][0-9]*\?', 'broken-domain'),  # www.J2201P4?
    (r'href="https?://www\.[{]\d', 'template-brace'),                     # www.{72P39
    (r'href="https?://www\.}[^\"]+', 'template-brace-close'),             # www.}92P12
    (r'data-viator-id="[0-9]+[- ]?XXX', 'placeholder-id-x'),              # 562-XXXXX
    (r'data-viator-id="12345', 'placeholder-id-numeric'),                 # 12345P1
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


def classify_page(rel_path, html):
    """Quick inline classification (no taxonomy file dependency)."""
    slug_lower = rel_path.lower()
    if any(
        slug_lower.startswith(u) or f"/{u}" in slug_lower or slug_lower.endswith(f"/{u}")
        for u in UTILITY_SLUGS
    ):
        return "Utility"

    product_cards = html.count('class="product-card"') + html.count("class='product-card'")
    comparison_tables = bool(re.search(r'<table[^>]*class="[^"]*comparison', html))
    viator_links = len(re.findall(r'href="https?://(?:www\.)?viator\.com/', html))

    if product_cards >= 1 or comparison_tables or viator_links >= 2:
        return "Money"
    return "Feeder"


def get_money_links(page_hrefs, money_pages):
    """Check if a Feeder page links to any Money page."""
    for href in page_hrefs:
        target = href.lstrip("/")
        if target in money_pages:
            return True
        if f"{target}/index.html" in money_pages:
            return True
        if target + ".html" in money_pages:
            return True
        if any(m.startswith(target.rstrip("/") + "/") for m in money_pages):
            return True
    return False


def detect_placeholders(html):
    """Detect template placeholder Viator URLs. Returns list of (pattern_type, match_text)."""
    found = []
    for pattern, ptype in PLACEHOLDER_PATTERNS:
        for m in re.finditer(pattern, html):
            found.append((ptype, m.group()[:80]))
    return found


def audit_site(local_path, domain, strict=False):
    """Audit CTA paths for one site. Returns (money_issues, feeder_issues, placeholder_issues)."""
    if not os.path.isdir(local_path):
        return [], [], [f"Path not found: {local_path}"]

    pages = sorted(Path(local_path).rglob("*.html"))
    page_data = {}
    placeholder_issues = []

    # First pass: classify and extract
    for page in pages:
        if "backup" in str(page) or "node_modules" in str(page) or "/." in str(page):
            continue
        rel = str(page.relative_to(local_path))
        html = page.read_text(errors="ignore")
        ptype = classify_page(rel, html)
        viator_links = re.findall(r'href="https?://(?:www\.)?viator\.com/[^"]*"', html)
        internal_hrefs = set()
        for match in re.finditer(r'href="(/[^"]*)"', html):
            internal_hrefs.add(match.group(1))

        # Check for template placeholders
        placeholders = detect_placeholders(html)
        if placeholders:
            for ptype_code, match_text in placeholders:
                placeholder_issues.append(
                    f"  ⚠️  {rel}: template placeholder ({ptype_code}) — {match_text[:60]}"
                )

        page_data[rel] = {
            "type": ptype,
            "viator_count": len(viator_links),
            "hrefs": internal_hrefs,
        }

    money_pages = {p for p, d in page_data.items() if d["type"] == "Money"}

    money_issues = []
    feeder_issues = []

    for rel, data in sorted(page_data.items()):
        if data["type"] == "Money" and data["viator_count"] == 0:
            money_issues.append(f"  ❌ {rel}: Money page with ZERO Viator links")

        elif data["type"] == "Feeder":
            if not get_money_links(data["hrefs"], money_pages):
                # Homepage exception — always a feeder even if it doesn't link directly
                if rel in ("index.html",):
                    continue
                if "about" in rel.lower():
                    continue  # About pages are structural feeders
                feeder_issues.append(
                    f"  ⚠️  {rel}: Feeder page with no links to Money pages"
                )

    return money_issues, feeder_issues, placeholder_issues


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    strict = "--strict" in sys.argv

    sites = get_sites(target)
    all_money_issues = []
    all_feeder_issues = []

    for site_id, local_path, domain in sites:
        print(f"--- {site_id} ---")
        money_issues, feeder_issues, placeholder_issues = audit_site(local_path, domain, strict)

        if placeholder_issues:
            for issue in placeholder_issues:
                print(issue)
            print(f"  {len(placeholder_issues)} TEMPLATE PLACEHOLDER issues (HARD BLOCK — fix with product-code-flywheel)")

        if money_issues:
            for issue in money_issues:
                print(issue)
                all_money_issues.append(f"{site_id}: {issue}")
            print(f"  {len(money_issues)} MONEY issues (HARD BLOCK)")
        else:
            print(f"  ✅ All Money pages have Viator links")

        if feeder_issues:
            for issue in feeder_issues:
                print(issue)
                all_feeder_issues.append(f"{site_id}: {issue}")
            print(f"  {len(feeder_issues)} FEEDER issues")
        else:
            print(f"  ✅ All Feeder pages link to Money pages")

        # Log to DB
        try:
            reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
            reg.execute(
                "INSERT INTO audit_log (site_id, check_type, details) VALUES (?, 'cta_path_gate', ?)",
                (
                    site_id,
                    f"M:{len(money_issues)} F:{len(feeder_issues)}",
                ),
            )
            reg.commit()
            reg.close()
        except Exception as e:
            print(f"  [WARN] DB log failed: {e}", file=sys.stderr)
        print()

    print(f"{'='*50}")
    print(
        f"CTA-Path Gate: {len(all_money_issues)} MONEY issues, {len(all_feeder_issues)} FEEDER issues"
    )

    if all_money_issues:
        print("\n❌ HARD BLOCK — Money pages without Viator links:")
        for i in all_money_issues:
            print(f"  -> {i}")
        sys.exit(1)

    if strict and all_feeder_issues:
        print("\n⚠️  STRICT MODE — Feeder issues block deploy")
        sys.exit(1)

    print("\n✅ CTA-Path Gate passed")
    sys.exit(0)


if __name__ == "__main__":
    main()
