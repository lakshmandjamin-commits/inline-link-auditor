#!/usr/bin/env python3
"""
Auto-fix: remove disappeared Viator products from site pages.

Runs after viator_cli.py sync: reads viator_cli.db for is_available=0
products, finds the pages referencing them, removes the product card divs,
and writes fixed HTML. Reports what was done.

Usage: python3 product_sync_fix.py [all|site_id]
If run without args, processes all sites.

The removed product cards are logged so the deploy process knows what changed.
"""
import sqlite3, os, sys, re, json
from datetime import datetime
from pathlib import Path

DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")
LOG_FILE = os.path.expanduser("~/.hermes/affiliate-crons/logs/product_sync_fix.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

# Card div patterns — the opening tag patterns for each known card type
CARD_PATTERNS = [
    re.compile(r'<div[^>]*class="[^"]*(?:comp-card|product-card|tour-review-card)[^"]*"[^>]*>'),
]

def log(msg):
    ts = datetime.now().isoformat()[:19]
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] {msg}\n")
    print(msg)

def get_disappeared_products():
    """Get list of (product_code, title) from viator_cli where is_available=0."""
    db = os.path.join(DB_DIR, "viator_cli.db")
    if not os.path.exists(db):
        log(f"  SKIP: viator_cli.db not found at {db}")
        return []
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT product_code, title FROM products WHERE is_available=0"
    ).fetchall()
    conn.close()
    return [(r[0], r[1] or "Unknown") for r in rows]

def get_affected_pages(product_code, site_id=None):
    """Find all (site_id, page_url, local_path) for pages referencing a product code.

    First checks site_registry.db (registered pages), then scans all HTML files
    in the site directory for unregistered references (e.g. DE/ES multilingual pages).
    """
    reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
    if site_id:
        rows = reg.execute(
            """SELECT sp.site_id, sp.page_url, s.local_path
               FROM site_products sp
               JOIN sites s ON sp.site_id=s.site_id
               WHERE sp.product_code=? AND sp.site_id=? AND s.status='active'""",
            (product_code, site_id)
        ).fetchall()
        # Also get local_path for unregistered scan
        site_info = reg.execute(
            "SELECT local_path FROM sites WHERE site_id=? AND status='active'",
            (site_id,)
        ).fetchone()
        local_path = site_info[0] if site_info else None
    else:
        rows = reg.execute(
            """SELECT sp.site_id, sp.page_url, s.local_path
               FROM site_products sp
               JOIN sites s ON sp.site_id=s.site_id
               WHERE sp.product_code=? AND s.status='active'""",
            (product_code,)
        ).fetchall()
        site_info = None
    reg.close()

    # Convert to set of (site_id, page_url, local_path) — dedup by page
    known = set()
    for r in rows:
        known.add((r[0], r[1], r[2]))

    # If site_id given, also scan all HTML files for unregistered references
    if site_id and local_path:
        html_dir = local_path
        for dirpath, dirnames, filenames in os.walk(html_dir):
            # Skip backup directories
            dirnames[:] = [d for d in dirnames if d != 'backup_pre_fixes']
            for fn in filenames:
                if not fn.endswith('.html'):
                    continue
                # Skip if already known
                full_path = os.path.join(dirpath, fn)
                rel_path = os.path.relpath(full_path, html_dir)
                # Convert to page_url format (no .html extension)
                page_url = '/' + rel_path
                if page_url.endswith('.html'):
                    page_url = page_url[:-5]
                if page_url.endswith('/index'):
                    page_url = page_url[:-6] or '/'
                if (site_id, page_url, local_path) in known:
                    continue
                # Check if file contains the product code
                try:
                    with open(full_path, errors='ignore') as f:
                        content = f.read()
                    if product_code in content:
                        known.add((site_id, page_url, local_path))
                        log(f"  +UNREGISTERED: {site_id} ({page_url})")
                except Exception:
                    continue

    # Return as list sorted by page_url
    result = list(known)
    result.sort(key=lambda x: x[1])
    return result

def resolve_html_path(local_path, page_url):
    """Resolve the HTML file path: handles cleanUrls (.html extension)."""
    rel = page_url.lstrip("/")
    if not rel:
        return None
    base = os.path.join(local_path, rel)
    if os.path.isfile(base):
        return base
    base_html = base + ".html"
    if os.path.isfile(base_html):
        return base_html
    if os.path.isdir(base):
        index = os.path.join(base, "index.html")
        if os.path.isfile(index):
            return index
    return None

def find_card_boundaries(html, product_code):
    """Find the outer card div containing a Viator URL with the given product_code.

    Returns list of (card_start, card_end) boundary positions for removal,
    or empty list if no card found.

    Strategy:
    1. Find all Viator URLs containing the product_code
    2. For each URL, locate the containing card div (product-card, comp-card, tour-review-card)
    3. Return the outermost card boundary for removal
    """
    boundaries = []

    # Find all anchor tags with Viator URLs containing this product code
    url_pattern = re.compile(
        r'<a[^>]*?href="[^"]*viator\.com[^"]*' + re.escape(product_code) + r'[^"]*"[^>]*>.*?</a>',
        re.IGNORECASE
    )

    for anchor_match in url_pattern.finditer(html):
        anchor_start = anchor_match.start()

        # Find the containing card div: go backwards from anchor
        card_starts = [m.start() for m in
                       re.finditer(r'<div class="(?:comp-card|product-card|tour-review-card)(?: |")',
                                    html[:anchor_start])]
        if not card_starts:
            continue
        card_start = card_starts[-1]

        # Find where this card ends: find the matching closing div structure
        # The card has a specific ending pattern
        card_content = html[card_start:anchor_start + 2000]

        # Find the end of this specific card by looking for the next card start or EOF
        next_card = re.search(r'<div class="(?:comp-card|product-card|tour-review-card)(?: |")',
                               html[card_start + 20:])
        if next_card:
            card_end = card_start + 20 + next_card.start()
        else:
            card_end = len(html)

        # Verify the anchor is within this card
        if card_start <= anchor_start < card_end:
            boundaries.append((card_start, card_end))

    # Merge overlapping/deduplicate boundaries
    if not boundaries:
        return boundaries

    boundaries.sort()
    merged = [boundaries[0]]
    for b in boundaries[1:]:
        if b[0] < merged[-1][1]:
            # Overlapping — take the wider range
            merged[-1] = (merged[-1][0], max(merged[-1][1], b[1]))
        else:
            merged.append(b)

    return merged

def remove_card(html, card_start, card_end):
    """Remove the card div at the given boundaries and clean up excess whitespace.

    Returns the modified HTML.
    """
    before = html[:card_start]
    after = html[card_end:]

    # Clean up empty lines/gaps left by removal
    # Remove extra blank lines and trailing whitespace at the join point
    before = before.rstrip() + "\n"
    after = after.lstrip("\n")

    return before + after

def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    site_filter = None if target == "all" else target

    log(f"\n{'='*60}")
    log(f"Product Sync Fix — {target} — {datetime.now().isoformat()[:19]}")
    log(f"{'='*60}")

    # Get disappeared products
    disappeared = get_disappeared_products()
    if not disappeared:
        log("No disappeared products found. Nothing to fix.")
        return

    log(f"Found {len(disappeared)} disappeared product(s):")
    for code, title in disappeared:
        log(f"  {code}: {title}")

    total_fixed = 0
    total_pages = 0

    for product_code, title in disappeared:
        pages = get_affected_pages(product_code, site_filter)
        if not pages:
            log(f"\n{product_code}: No affected pages found")
            continue

        log(f"\n{product_code} ({title}) — {len(pages)} affected page(s)")

        for site_id, page_url, local_path in pages:
            html_path = resolve_html_path(local_path, page_url)
            if not html_path:
                log(f"  SKIP {site_id}: cannot resolve path for {page_url}")
                continue

            try:
                with open(html_path) as f:
                    html = f.read()
            except Exception as e:
                log(f"  SKIP {site_id}: cannot read {html_path} — {e}")
                continue

            boundaries = find_card_boundaries(html, product_code)
            if not boundaries:
                log(f"  SKIP {site_id} ({page_url}): no card found with {product_code}")
                continue

            # Apply removals (in reverse order to preserve positions)
            modifications = 0
            for card_start, card_end in sorted(boundaries, reverse=True):
                html = remove_card(html, card_start, card_end)
                modifications += 1

            # Write fixed HTML
            try:
                with open(html_path, "w") as f:
                    f.write(html)
                log(f"  FIXED {site_id} ({page_url}): removed {modifications} card(s)")
                total_fixed += modifications
                total_pages += 1
            except Exception as e:
                log(f"  ERROR {site_id}: failed to write {html_path} — {e}")

    # Summary
    log(f"\n{'='*60}")
    log(f"Fix complete: {total_fixed} card(s) removed across {total_pages} page(s)")
    if total_fixed > 0:
        log("REQUIRED ACTION: git commit + push (Vercel auto-deploys)")
    log(f"{'='*60}\n")

    # Exit 1 if anything was fixed (signals deploy needed)
    sys.exit(1 if total_fixed > 0 else 0)

if __name__ == "__main__":
    main()
