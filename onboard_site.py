#!/usr/bin/env python3
"""
Register a new site into the fleet database.
Usage: python3 onboard_site.py <site_id> <name> <domain> <local_path> [viator_pid]

Extracts Viator product codes from the site's HTML, seeds them into the product
catalog, and registers the site for all automated crons.
"""
import sqlite3, os, sys, re, glob
from pathlib import Path
from datetime import datetime

DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")

def extract_products(local_path):
    """Scan all HTML files for Viator product codes.

    Extracts bare product codes (e.g., '383935P5', '42204P1') from URL fragments
    like '/d26879-383935P5'. The old regex only captured digits after the hyphen,
    missing letter suffixes (P5, P1, P12, P21, etc.), causing all API calls to
    return HTTP 400 'Invalid product code'.
    """
    products = {}
    for f in Path(local_path).rglob("*.html"):
        if "node_modules" in str(f):
            continue
        html = f.read_text(errors="ignore")
        rel = str(f.relative_to(local_path))
        # Match /d{DESTID}-{PRODUCTCODE} where PRODUCTCODE is alphanumeric
        # e.g. /d26879-383935P5 -> code=383935P5
        matches = re.findall(r'/d\d+-([A-Za-z0-9]+)', html)
        for code in matches:
            if code not in products:
                products[code] = []
            products[code].append("/" + rel.replace("/index.html", "").replace(".html", ""))
    return products

def main():
    if len(sys.argv) < 5:
        print("Usage: onboard_site.py <site_id> <name> <domain> <local_path> [viator_pid]")
        sys.exit(1)

    site_id = sys.argv[1]
    name = sys.argv[2]
    domain = sys.argv[3]
    local_path = sys.argv[4]
    viator_pid = sys.argv[5] if len(sys.argv) > 5 else "P00303273"

    if not os.path.isdir(local_path):
        print(f"ERROR: {local_path} is not a directory")
        sys.exit(1)

    print(f"Onboarding: {site_id} ({name})")

    # ── Extract products ──
    products = extract_products(local_path)
    print(f"  Found {len(products)} unique Viator product codes")

    # ── Register site ──
    reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
    reg.execute("""INSERT OR REPLACE INTO sites
        (site_id, name, domain, local_path, viator_pid, status, total_products)
        VALUES (?, ?, ?, ?, ?, 'active', ?)""",
        (site_id, name, domain, local_path, viator_pid, len(products)))

    # ── Seed products ──
    prod_db = sqlite3.connect(os.path.join(DB_DIR, "viator_cli.db"))
    for code in products:
        prod_db.execute("INSERT OR IGNORE INTO products (product_code, title) VALUES (?, ?)",
                       (code, code))  # Title placeholder — sync fills real data

    # ── Link products to site ──
    for code, pages in products.items():
        for page in pages:
            reg.execute("""INSERT OR IGNORE INTO site_products
                (site_id, product_code, page_url) VALUES (?, ?, ?)""",
                (site_id, code, page))

    reg.commit()
    prod_db.commit()
    reg.close()
    prod_db.close()

    print(f"  Registered: {site_id} — {len(products)} products across {len(list(Path(local_path).rglob('*.html')))} pages")
    print(f"  Domain: https://{domain}")
    print(f"  Ready for crons.")

if __name__ == "__main__":
    main()
