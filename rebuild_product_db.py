#!/usr/bin/env python3
"""
Rebuild the product DB with correct product codes extracted from live pages.

Fixes the root cause of the HTTP 400 product sync failure:
- Old regex captured incomplete codes (e.g., 'd5392-42204' instead of '42204P1')
- Old regex stored d{DESTID}- prefix which API rejects
- Backup pages with fabricated codes were included

Usage: python3 rebuild_product_db.py
"""
import sqlite3, os, sys, re
from pathlib import Path

DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")

# Known AI-fabricated codes (from skill: refs/viator-api.md)
FABRICATED = {'114919', '126582', '162308', '177041', '194043', '3592', '459292', '471901', '501192'}
# Suspicious non-standard codes
# 'All' is from /d22130-All-Tours destination pages — not a product code
EXCLUDE = FABRICATED | {'ttd', 'ttd-', 'All'}

def get_sites():
    """DB-driven site discovery — no hardcoded site lists."""
    db = os.path.join(DB_DIR, "site_registry.db")
    if not os.path.exists(db):
        return {}
    reg = sqlite3.connect(db)
    sites = {}
    for row in reg.execute(
        "SELECT site_id, local_path FROM sites WHERE status='active'"
    ).fetchall():
        if os.path.isdir(row[1]):
            sites[row[0]] = row[1]
    reg.close()
    return sites


def extract_products(local_path):
    """Extract bare product codes from live HTML pages."""
    products = {}
    for f in sorted(Path(local_path).rglob("*.html")):
        if "node_modules" in str(f):
            continue
        # Skip backup directories — contain old/fabricated codes
        rel = str(f)
        if "/backup_pre_fixes/" in rel or "/backups/" in rel:
            continue
        html = f.read_text(errors="ignore")
        rel_page = str(f.relative_to(local_path))
        # Match /d{DESTID}-{PRODUCTCODE}, capture bare code e.g. 383935P5
        matches = re.findall(r'/d\d+-([A-Za-z0-9]+)', html)
        for code in matches:
            if code in EXCLUDE:
                continue
            # Must contain at least one digit — pure-alpha codes like 'All' are
            # from destination landing pages (/d22130-All-Tours), not products
            if not re.search(r'\d', code):
                continue
            if code not in products:
                products[code] = set()
            products[code].add("/" + rel_page.replace("/index.html", "").replace(".html", ""))
    return products

def main():
    print("Rebuilding product database...\n")

    # ── Clear existing ──
    reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
    prod_db = sqlite3.connect(os.path.join(DB_DIR, "viator_cli.db"))
    reg.execute("DELETE FROM site_products")
    prod_db.execute("DELETE FROM products")
    reg.commit()
    prod_db.commit()

    total_all = 0
    sites = get_sites()
    for site_id, local_path in sites.items():
        products = extract_products(local_path)
        total_all += len(products)

        print(f"{site_id}: {len(products)} product codes")
        for code in sorted(products):
            pages = products[code]
            print(f"  {code}: {len(pages)} pages — {min(pages)}")
            for page in sorted(pages):
                prod_db.execute("INSERT OR IGNORE INTO products (product_code, title) VALUES (?, ?)", (code, code))
                reg.execute("""INSERT OR IGNORE INTO site_products
                    (site_id, product_code, page_url) VALUES (?, ?, ?)""",
                    (site_id, code, page))

        reg.execute("UPDATE sites SET total_products=? WHERE site_id=?", (len(products), site_id))
        print()

    reg.commit()
    prod_db.commit()
    reg.close()
    prod_db.close()

    print(f"✅ Re-seeded {total_all} unique product codes across {len(SITES)} sites")

if __name__ == "__main__":
    main()
