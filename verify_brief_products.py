#!/usr/bin/env python3
"""
verify_brief_products.py — Guard: cross-checks brief product codes against viator_cli.db.

Usage:
    python3 verify_brief_products.py                  # Check all briefs, report issues
    python3 verify_brief_products.py --fix            # Check + auto-fix titles
    python3 verify_brief_products.py madeira-hiking   # Check single site
    python3 verify_brief_products.py --fix --briefs-dir /custom/path  # Custom dir

Exit codes:
    0 — All briefs clean
    1 — Issues found (--fix was NOT used, or --fix produced zero fixes)
    2 — Issues found AND ALL were fixed (--fix used successfully)
    3 — File/directory errors
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# Configuration
DEFAULT_BRIEFS_DIR = os.path.expanduser("~/.hermes/affiliate-crons/briefs")
DEFAULT_DB_PATH = os.path.expanduser("~/.hermes/affiliate-crons/db/viator_cli.db")

# Destination keyword map — product title must contain at least one keyword.
# Keywords are matched as whole words where possible; short keywords use
# word-boundary checks in is_product_for_site().
SITE_KEYWORDS = {
    "madeira-trail-guide": [
        "madeira", "funchal", "levada", "pico", "raba\u00e7al",
        "calheta", "porto moniz", "santana", "fanal", "seixal",
        "cabo gir\u00e3o", "arieiro", "ruivo", "pr1", "pr6",
    ],
    "porto-sommelier": ["porto", "port wine", "douro", "oporto"],
    "lapland-adventure-guide": [
        "rovaniemi", "lapland", "aurora", "northern light",
        "husky", "santa claus", "reindeer", "snowmobile", "borealis",
    ],
    "san-juan-excursions": [
        "san juan", "puerto rico", "yunque", "bioluminescent",
        "fajardo", "culebra", "icacos", "vieques",
    ],
    "tenerife-outdoor-guide": [
        "tenerife", "teide", "masca",
        "costa adeje", "anaga", "garachico",
    ],
    "yogyakarta-temple-tours": [
        "yogyakarta", "jogja", "borobudur", "prambanan",
        "merapi", "jomblang", "malioboro",
    ],
}

# Keywords that need word-boundary matching to avoid false positives
# (e.g., "java" matches "JavaScript", "canary" matches "Canary Wharf")
AMBIGUOUS_KEYWORDS = {"java", "canary", "rio", "porto"}


def load_products(db_path: str) -> dict:
    """Load all active products from viator_cli.db, keyed by product_code."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT product_code, title, rating, review_count "
        "FROM products WHERE active = 1"
    ).fetchall()
    conn.close()
    return {r["product_code"]: dict(r) for r in rows}


def is_product_for_site(code: str, site: str, products: dict, keywords: list) -> bool:
    """Check if a product's title contains destination keywords."""
    prod = products.get(code)
    if not prod or not prod["title"]:
        return False
    title_lower = prod["title"].lower()
    for kw in keywords:
        if kw in AMBIGUOUS_KEYWORDS:
            # Word-boundary match for ambiguous short keywords
            import re
            if re.search(r"\b" + re.escape(kw) + r"\b", title_lower):
                return True
        elif kw in title_lower:
            return True
    return False


def find_best_product(
    site: str,
    brief_title: str,
    products: dict,
    keywords: list,
    exclude_codes: set,
) -> dict | None:
    """Find the highest-rated product for a site that matches the brief's topic."""
    best, best_score = None, 0.0
    brief_words = set(brief_title.lower().split())

    for code, prod in products.items():
        if code in exclude_codes:
            continue

        title = prod.get("title")
        if not title:
            continue

        title_lower = title.lower()

        # Must match site destination
        if not is_product_for_site(code, site, products, keywords):
            continue

        # Score by word overlap with brief title
        score = sum(
            2 for w in brief_words
            if len(w) > 3 and w in title_lower
        )
        # Rating bonus
        if prod.get("rating") is not None:
            score += float(prod["rating"]) * 0.5
        # Review count bonus (popular products preferred)
        if prod.get("review_count"):
            score += min(float(prod["review_count"]) / 100, 5)

        if score > best_score:
            best_score, best = score, prod

    return best


def build_brief_title(products_to_feature: list, products_db: dict) -> str:
    """Build a brief title from actual Viator product names. Never truncates."""
    names = []
    for code in products_to_feature:
        prod = products_db.get(code)
        if prod and prod.get("title"):
            names.append(prod["title"])

    if len(names) <= 0:
        return ""
    if len(names) == 1:
        return f"{names[0]}: Honest Review & Tips"
    if len(names) == 2:
        return " vs ".join(names)
    # Multi-product: use count + comma-separated (no truncation)
    return f'{len(names)} Best: {", ".join(names)}'


def check_briefs(briefs_dir: str, db_path: str, fix: bool = False) -> dict:
    """Check all brief files for product code issues. Returns report dict."""
    products = load_products(db_path)
    report = {"sites": {}, "total_issues": 0, "total_fixed": 0}

    for fn in sorted(os.listdir(briefs_dir)):
        if not fn.endswith(".json"):
            continue
        # Case-insensitive site matching
        site_lower = fn.replace(".json", "").lower()
        keywords = SITE_KEYWORDS.get(site_lower)
        if not keywords:
            continue

        fp = os.path.join(briefs_dir, fn)
        try:
            briefs_data = json.load(open(fp))
        except (json.JSONDecodeError, FileNotFoundError) as e:
            print(f"ERROR: Cannot parse {fn}: {e}", file=sys.stderr)
            report["sites"][site_lower] = {
                "issues": [f"JSON parse error: {e}"],
                "fixes": [],
            }
            report["total_issues"] += 1
            continue

        if not isinstance(briefs_data, dict):
            print(f"ERROR: {fn} is not a JSON object", file=sys.stderr)
            report["sites"][site_lower] = {
                "issues": ["Not a JSON object"],
                "fixes": [],
            }
            report["total_issues"] += 1
            continue

        site_issues = []
        site_fixes = []

        used_codes = set()
        for brief in briefs_data.get("briefs", []):
            products_list = brief.get("products_to_feature")
            if products_list is None:
                continue  # key exists but is null
            if not isinstance(products_list, list):
                continue

            new_products = []

            for code in products_list:
                # Issue: product not in DB
                if code not in products:
                    site_issues.append(f"{code}: not in viator_cli.db")
                    if fix:
                        alt = find_best_product(
                            site_lower, brief.get("title", ""),
                            products, keywords, used_codes | set(new_products),
                        )
                        if alt:
                            site_fixes.append(
                                f"{code} -> {alt['product_code']} ({alt['title'][:50]})"
                            )
                            new_products.append(alt["product_code"])
                            used_codes.add(alt["product_code"])
                        else:
                            new_products.append(code)
                    else:
                        new_products.append(code)
                    continue

                # Issue: product for wrong destination
                if not is_product_for_site(code, site_lower, products, keywords):
                    prod_title = products[code].get("title") or "(null title)"
                    site_issues.append(
                        f"{code}: '{prod_title[:50]}' "
                        f"not a {site_lower} product"
                    )
                    if fix:
                        alt = find_best_product(
                            site_lower, brief.get("title", ""),
                            products, keywords, used_codes | set(new_products),
                        )
                        if alt:
                            site_fixes.append(
                                f"{code} -> {alt['product_code']} ({alt['title'][:50]})"
                            )
                            new_products.append(alt["product_code"])
                            used_codes.add(alt["product_code"])
                        else:
                            new_products.append(code)
                    else:
                        new_products.append(code)
                    continue

                new_products.append(code)
                used_codes.add(code)

            if fix and new_products != products_list:
                brief["products_to_feature"] = new_products
                # Update title to use actual product names
                new_title = build_brief_title(new_products, products)
                if new_title and new_title != brief.get("title", ""):
                    brief["title"] = new_title

        if fix and site_fixes:
            # Atomic write: write to temp file, then replace
            tmp = fp + ".tmp"
            with open(tmp, "w") as f:
                json.dump(briefs_data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, fp)

        if site_issues:
            report["sites"][site_lower] = {
                "issues": site_issues,
                "fixes": site_fixes if fix else [],
            }
            report["total_issues"] += len(site_issues)
            if fix:
                report["total_fixed"] += len(site_fixes)

    return report


def main():
    fix_mode = "--fix" in sys.argv
    briefs_dir = DEFAULT_BRIEFS_DIR
    db_path = DEFAULT_DB_PATH

    # Parse --briefs-dir
    for i, arg in enumerate(sys.argv):
        if arg == "--briefs-dir" and i + 1 < len(sys.argv):
            briefs_dir = sys.argv[i + 1]

    # Single site check
    site_filter = None
    for arg in sys.argv[1:]:
        if arg.lower() in SITE_KEYWORDS:
            site_filter = arg.lower()
            break

    if not os.path.isdir(briefs_dir):
        print(f"ERROR: briefs directory not found: {briefs_dir}", file=sys.stderr)
        sys.exit(3)

    if not os.path.isfile(db_path):
        print(f"ERROR: viator_cli.db not found: {db_path}", file=sys.stderr)
        sys.exit(3)

    # If single site, create temp dir with just that file
    check_dir = briefs_dir
    tmpdir = None
    if site_filter:
        tmpdir = tempfile.mkdtemp()
        src = os.path.join(briefs_dir, f"{site_filter}.json")
        if not os.path.isfile(src):
            shutil.rmtree(tmpdir, ignore_errors=True)
            print(f"ERROR: brief file not found: {src}", file=sys.stderr)
            sys.exit(3)
        shutil.copy2(src, os.path.join(tmpdir, f"{site_filter}.json"))
        check_dir = tmpdir

    report = check_briefs(check_dir, db_path, fix=fix_mode)

    # If we used a temp dir and fixed, copy back atomically
    if site_filter and fix_mode and report["total_fixed"] > 0:
        src = os.path.join(check_dir, f"{site_filter}.json")
        dst = os.path.join(briefs_dir, f"{site_filter}.json")
        tmp_dst = dst + ".tmp"
        shutil.copy2(src, tmp_dst)
        os.replace(tmp_dst, dst)

    # Cleanup temp dir
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # Print report
    if report["total_issues"] == 0:
        print("ALL CLEAN: 0 product code issues across fleet")
        sys.exit(0)

    for site, data in sorted(report["sites"].items()):
        print(f"\n=== {site}: {len(data['issues'])} issues ===")
        for issue in data["issues"]:
            print(f"  {issue}")
        if data["fixes"]:
            print(f"  FIXES APPLIED ({len(data['fixes'])}):")
            for f in data["fixes"]:
                print(f"    {f}")

    if fix_mode:
        if report["total_fixed"] == report["total_issues"]:
            print(f"\nFIXED: all {report['total_fixed']} issues across "
                  f"{len(report['sites'])} sites")
            sys.exit(2)
        else:
            unfixed = report["total_issues"] - report["total_fixed"]
            print(f"\nPARTIALLY FIXED: {report['total_fixed']} of "
                  f"{report['total_issues']} issues fixed. "
                  f"{unfixed} issues remaining — manual review needed.")
            sys.exit(1)
    else:
        print(f"\n{report['total_issues']} issues found. "
              "Run with --fix to auto-correct.")
        sys.exit(1)


if __name__ == "__main__":
    main()
