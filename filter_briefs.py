#!/usr/bin/env python3
"""
Pre-generation filter: validates briefs against Viator inventory before page generation.
Filters out briefs whose products don't exist in the API, and enforces intent_type gating.

Usage:
  python3 filter_briefs.py <site_slug> [--mode transactional|informational|all] [--verbose]
  
Output:
  Writes filtered briefs to /tmp/filtered-{site_slug}.json with added:
    - intent_type: derived from template
    - primary_product_code: first product in products_to_feature
    - product_status: "verified" | "partial" | "none"
    - _filtered: true/false with reason
"""
import sys, os, json, sqlite3, re
from pathlib import Path

BRIEFS_DIR = Path.home() / ".hermes" / "affiliate-crons" / "briefs"
DB_PATH = Path.home() / ".hermes" / "affiliate-crons" / "db" / "viator_cli.db"

def load_product_codes():
    """Load all known product codes from local Viator DB."""
    codes = set()
    try:
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute("SELECT product_code FROM products WHERE active=1").fetchall()
        codes = {r[0] for r in rows}
        conn.close()
    except Exception as e:
        print(f"WARNING: Could not load product DB: {e}", file=sys.stderr)
    return codes

PRODUCT_CODES = load_product_codes()

def resolve_brief_product(ref):
    """Resolve a brief product reference (e.g., 'd22388-14203') to a DB product code.
    
    Brief format: d{groupId}-{numeric} or sometimes bare product code.
    We match by: exact match, or fuzzy match on numeric suffix.
    """
    # If it's already a known product code, return it
    if ref in PRODUCT_CODES:
        return ref
    
    # Extract numeric part from d{groupId}-{numeric} format
    import re
    m = re.match(r'd\d+-(\d+)', ref)
    if m:
        numeric = m.group(1)
        # Find codes starting with this numeric prefix
        matches = [c for c in PRODUCT_CODES if c.startswith(numeric)]
        if matches:
            return matches[0]  # Return first match (most likely)
    
    return None

def check_product_exists(ref):
    """Check if a product referenced in a brief exists in the local DB."""
    resolved = resolve_brief_product(ref)
    return resolved is not None

# Template → intent_type mapping
TEMPLATE_INTENT = {
    "comparison": "transactional",
    "buyer_guide": "transactional",
    "best_of": "transactional",
    "tour_review": "transactional",
    "beginner_guide": "informational",
    "how_to": "informational",
    "destination_overview": "informational",
    "seasonal": "informational",
    "default": "informational",
}

def filter_briefs(site_slug, mode="transactional", verbose=False):
    """Filter briefs for a site."""
    briefs_path = BRIEFS_DIR / f"{site_slug}.json"
    if not briefs_path.exists():
        print(f"ERROR: No briefs file at {briefs_path}")
        sys.exit(1)
    
    # Reload product codes in case DB was updated
    global PRODUCT_CODES
    PRODUCT_CODES = load_product_codes()
    
    with open(briefs_path) as f:
        data = json.load(f)
    
    briefs = data.get("briefs", [])
    if not briefs:
        print(f"No briefs in {briefs_path}")
        sys.exit(0)
    
    filtered = []
    stats = {"total": len(briefs), "passed": 0, "skipped_no_products": 0,
             "skipped_wrong_intent": 0}
    
    for i, brief in enumerate(briefs):
        slug = brief.get("slug", f"brief-{i}")
        # Sanitize slug: strip anti-words and superlatives
        ANTI_SLUG_WORDS = ["best", "top", "ultimate", "amazing", "perfect", "cheap",
                           "unforgettable", "magical", "stunning", "breathtaking",
                           "must-see", "must-visit", "hidden-gem", "paradise"]
        for word in ANTI_SLUG_WORDS:
            slug = re.sub(rf'^{word}-|{word}$|(?<=-){word}-', '', slug)
        slug = re.sub(r'-+', '-', slug).strip('-')
        if slug != brief.get("slug"):
            print(f"  SLUG FIXED: {brief['slug']} → {slug}")
            brief["original_slug"] = brief["slug"]
            brief["slug"] = slug
        template = brief.get("template", "default")
        products = brief.get("products_to_feature", [])
        
        # Determine intent_type
        intent_type = TEMPLATE_INTENT.get(template, "informational")
        brief["intent_type"] = intent_type
        
        # Resolve product codes from brief references
        resolved = []
        for ref in products:
            code = resolve_brief_product(ref)
            if code:
                resolved.append(code)
        
        if resolved:
            brief["primary_product_code"] = resolved[0]
            brief["comparison_products"] = resolved[1:] if len(resolved) > 1 else []
        else:
            brief["primary_product_code"] = None
            brief["comparison_products"] = []
        
        # Intent type gate
        if mode != "all" and intent_type != mode:
            brief["_filtered"] = True
            brief["_filter_reason"] = f"intent_type={intent_type} (mode={mode})"
            stats["skipped_wrong_intent"] += 1
            if verbose:
                print(f"  SKIP {slug}: wrong intent ({intent_type})")
            continue
        
        # Product existence check
        if not products:
            brief["_filtered"] = True
            brief["_filter_reason"] = "no products_to_feature"
            brief["product_status"] = "none"
            stats["skipped_no_products"] += 1
            if verbose:
                print(f"  SKIP {slug}: no products listed")
            continue
        
        if not resolved:
            brief["_filtered"] = True
            brief["_filter_reason"] = f"no products matched in local DB (refs: {products})"
            brief["product_status"] = "none"
            stats["skipped_no_products"] += 1
            if verbose:
                print(f"  SKIP {slug}: 0/{len(products)} products in DB")
            continue
        
        brief["product_status"] = "verified" if len(resolved) == len(products) else "partial"
        brief["_filtered"] = False
        brief["_filter_reason"] = None
        filtered.append(brief)
        stats["passed"] += 1
        if verbose:
            print(f"  PASS {slug}: {len(resolved)}/{len(products)} products OK, intent={intent_type}")
    
    # Save filtered output
    out_path = Path(f"/tmp/filtered-{site_slug}.json")
    out_data = {**data, "briefs": filtered, "filter_stats": stats}
    with open(out_path, "w") as f:
        json.dump(out_data, f, indent=2)
    
    print(f"\n{site_slug}: {stats['passed']}/{stats['total']} briefs passed (mode={mode})")
    if stats['skipped_no_products']:
        print(f"  No matching products: {stats['skipped_no_products']}")
    if stats['skipped_wrong_intent']:
        print(f"  Wrong intent type: {stats['skipped_wrong_intent']}")
    
    return stats

if __name__ == "__main__":
    site_slug = sys.argv[1] if len(sys.argv) > 1 else None
    mode = sys.argv[2] if len(sys.argv) > 2 else "transactional"
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    
    if not site_slug:
        print("Usage: filter_briefs.py <site_slug> [transactional|informational|all] [--verbose]")
        sys.exit(1)
    
    filter_briefs(site_slug, mode, verbose)
