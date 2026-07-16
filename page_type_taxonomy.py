#!/usr/bin/env python3
"""Page Type Taxonomy — classifies every page as Money/Feeder/Utility.

Money  = has Viator product cards OR comparison tables OR ≥2 Viator booking links
Feeder = no booking links but links to Money pages (hubs, about, info)
Utility = contact, privacy, terms, 404

Usage: python3 page_type_taxonomy.py [all|site_id]
Output: Writes ~/.hermes/fleet-page-taxonomy.md and exits 0.
         Any classification change vs prior run is flagged.
"""

import sqlite3, os, sys, re, json
from pathlib import Path
from datetime import datetime
from collections import defaultdict

DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")
TAXONOMY_PATH = os.path.expanduser("~/.hermes/fleet-page-taxonomy.md")

UTILITY_SLUGS = {'contact', 'privacy', 'terms', '404'}


PRODUCT_CODE_RE = re.compile(r'/d\d+-([A-Z0-9]+)', re.IGNORECASE)


def classify_format(page_path):
    """v2.0: Sub-classify money pages by format type using URL patterns."""
    path_lower = page_path.lower()
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


def write_json_taxonomy(all_results, json_path):
    """v2.0: Write JSON taxonomy with format subclassification."""
    output = {}
    for site_name, (money, feeder, utility) in sorted(all_results.items()):
        for page in money:
            key = f"{site_name}/{page}"
            output[key] = {"function": "Money", "format": classify_format(page)}
        for page in feeder:
            key = f"{site_name}/{page}"
            output[key] = {"function": "Feeder", "format": "feeder"}
        for page in utility:
            key = f"{site_name}/{page}"
            output[key] = {"function": "Utility", "format": "utility"}
    with open(json_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"✅ JSON taxonomy written to {json_path} ({len(output)} entries)")


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


def get_money_pages(site_html_dir):
    """Return set of relative paths that are Money pages (have Viator links in product cards)."""
    money = set()
    feeder = set()
    utility = set()

    pages = sorted(Path(site_html_dir).rglob("*.html"))
    # Build a map of page path -> set of hrefs for feeder detection
    page_hrefs = {}

    for page in pages:
        if "backup" in str(page) or "node_modules" in str(page):
            continue
        rel = str(page.relative_to(site_html_dir))
        html = page.read_text(errors="ignore")

        # --- Utility check (first — contact, privacy, terms) ---
        slug_lower = rel.lower()
        is_utility = any(
            slug_lower.startswith(u) or f"/{u}" in slug_lower or slug_lower.endswith(f"/{u}")
            for u in UTILITY_SLUGS
        )
        if is_utility:
            utility.add(rel)
            continue

        # --- Money check ---
        product_cards = html.count('class="product-card"') + html.count("class='product-card'")
        comparison_tables = bool(re.search(r'<table[^>]*class="[^"]*comparison', html))
        viator_links = len(re.findall(r'href="https?://(?:www\.)?viator\.com/', html))

        # Extract all internal links for feeder detection
        internal_hrefs = set()
        for match in re.finditer(r'href="(/[^"]*)"', html):
            internal_hrefs.add(match.group(1))
        page_hrefs[rel] = internal_hrefs

        if product_cards >= 1 or comparison_tables or viator_links >= 2:
            money.add(rel)
        else:
            feeder.add(rel)  # Tentative, refined below

    # --- Refine feeder: remove index.html / overlay with hub logic ---
    # A feeder page must link to at least one Money page
    true_feeders = set()
    for rel_path in sorted(feeder):
        hrefs = page_hrefs.get(rel_path, set())
        links_to_money = False
        for href in hrefs:
            # Normalize href to match money set keys
            target = href.lstrip("/")
            if target in money or f"{target}/index.html" in money or target + ".html" in money:
                links_to_money = True
                break
            # Check if any money page starts with this path
            if any(m.startswith(target.rstrip("/") + "/") for m in money):
                links_to_money = True
                break

        if links_to_money:
            true_feeders.add(rel_path)
        elif rel_path in ("index.html",):
            true_feeders.add(rel_path)  # Homepage is always feeder
        elif "about" in rel_path.lower():
            true_feeders.add(rel_path)
        else:
            # No links to money and not about/home — likely a thin utility
            utility.add(rel_path)

    return money, true_feeders, utility


def load_prior_taxonomy():
    """Parse the existing taxonomy markdown to detect changes."""
    if not os.path.exists(TAXONOMY_PATH):
        return {}
    text = open(TAXONOMY_PATH).read()
    prior = {}
    current_site = None
    current_type = None
    for line in text.split("\n"):
        if line.startswith("## "):
            current_site = line.strip("# ").split(" (")[0].strip()
        elif line.startswith("### Money"):
            current_type = "Money"
        elif line.startswith("### Feeder"):
            current_type = "Feeder"
        elif line.startswith("### Utility"):
            current_type = "Utility"
        elif line.startswith("- ") or (line and line[0].isalpha() and "," in line):
            # Extract page names
            for page in re.split(r",\s*", line.lstrip("- ").strip()):
                page = page.strip()
                if page:
                    prior[page] = (current_site, current_type)
    return prior


def write_taxonomy(all_results):
    """Write the fleet-page-taxonomy.md file."""
    lines = ["# Fleet Page Taxonomy — " + datetime.now().strftime("%B %Y"), ""]
    total = {"Money": 0, "Feeder": 0, "Utility": 0}

    for site_name, (money, feeder, utility) in sorted(all_results.items()):
        total["Money"] += len(money)
        total["Feeder"] += len(feeder)
        total["Utility"] += len(utility)

        lines.append(f"## {site_name} ({len(money) + len(feeder) + len(utility)} pages)")
        lines.append("")

        for label, pages in [("Money", money), ("Feeder", feeder), ("Utility", utility)]:
            lines.append(f"### {label} ({len(pages)})")
            if pages:
                lines.append(", ".join(sorted(pages)))
            else:
                lines.append("(none)")
            lines.append("")

        lines.append("---")
        lines.append("")

    lines.append("## Fleet Summary")
    lines.append("")
    lines.append("| Type | Count | QA Effort |")
    lines.append("|---|---|")
    lines.append(f"| 💰 Money | {total['Money']} | 3 loops (full) |")
    lines.append(f"| 🔗 Feeder | {total['Feeder']} | 1-2 loops |")
    lines.append(f"| 📄 Utility | {total['Utility']} | 1 loop (structural only) |")
    lines.append(f"| **Total** | **{sum(total.values())}** | |")
    lines.append("")

    open(TAXONOMY_PATH, "w").write("\n".join(lines))


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    prior = load_prior_taxonomy()

    sites = get_sites(target)
    all_results = {}
    changes = []

    for site_id, local_path, domain in sites:
        if not os.path.isdir(local_path):
            print(f"  ⚠️  {site_id}: path not found — {local_path}", file=sys.stderr)
            continue

        money, feeder, utility = get_money_pages(local_path)
        all_results[site_id] = (money, feeder, utility)

        # Detect changes vs prior taxonomy
        current = {}
        for p in money:
            current[p] = "Money"
        for p in feeder:
            current[p] = "Feeder"
        for p in utility:
            current[p] = "Utility"

        for page, ptype in current.items():
            if page in prior and prior[page][0] == site_id:
                if prior[page][1] != ptype:
                    changes.append(
                        f"  🔄 {site_id}: {page} was {prior[page][1]} → now {ptype}"
                    )

        for page, (psite, ptype) in prior.items():
            if psite == site_id and page not in current:
                changes.append(f"  ❌ {site_id}: {page} removed (was {ptype})")

        for page in current:
            if page not in prior:
                changes.append(f"  ✨ {site_id}: {page} new → {current[page]}")

        print(
            f"  {site_id}: {len(money)} Money, {len(feeder)} Feeder, {len(utility)} Utility"
        )

    write_taxonomy(all_results)
    print(f"\n✅ Taxonomy written to {TAXONOMY_PATH}")

    # v2.0: Write JSON taxonomy with format subclassification
    json_path = os.path.expanduser("~/.hermes/fleet-page-taxonomy.json")
    write_json_taxonomy(all_results, json_path)

    if changes:
        print(f"\n{len(changes)} classification changes:")
        for c in changes:
            print(c)
    else:
        print("\n✅ No classification changes")

    # Log to DB
    try:
        reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
        for site_id, (money, feeder, utility) in all_results.items():
            reg.execute(
                "INSERT INTO audit_log (site_id, check_type, details) VALUES (?, 'page_taxonomy', ?)",
                (
                    site_id,
                    f"{len(money)}M/{len(feeder)}F/{len(utility)}U",
                ),
            )
        reg.commit()
        reg.close()
    except Exception as e:
        print(f"  [WARN] DB log failed: {e}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
