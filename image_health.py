#!/usr/bin/env python3
"""
image_health.py — Fleet image health instrumentation
DB-driven discovery via site_registry.db. Two modes: slim (daily) / full (weekly).
Output: JSON to stdout (--json) or human summary to stderr.

Usage:
  image_health.py [--site SITE] [--mode slim|full] [--json]
    --site: omit for all active sites
    --mode: slim (broken+alt+bloat) or full (+coverage+freshness+distribution)
    --json: machine JSON to stdout, human summary to stderr
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: requests library required. pip install requests", file=sys.stderr)
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: beautifulsoup4 required. pip install beautifulsoup4", file=sys.stderr)
    sys.exit(1)

# ── Paths ──────────────────────────────────────────────────────────────────
HERMES_DIR = os.path.expanduser("~/.hermes")
CRONS_DIR = os.path.join(HERMES_DIR, "affiliate-crons")
REGISTRY_PATH = os.path.join(CRONS_DIR, "db", "site_registry.db")
IMAGE_HEALTH_JSON = os.path.join(CRONS_DIR, "data", "image-health.json")
SITES_DIR = os.path.expanduser("~/sites")

# ── Thresholds ─────────────────────────────────────────────────────────────
SIZE_BLOAT_KB = 500
STALE_VIATOR_DAYS = 30
MAX_IMAGES_PER_SITE = 100
NETWORK_TIMEOUT = 5
GLOBAL_DEADLINE = 30
CONCURRENCY = 8

# ── DB helpers ─────────────────────────────────────────────────────────────
def get_sites(site_slug=None):
    """DB-driven site discovery."""
    conn = sqlite3.connect(REGISTRY_PATH)
    conn.row_factory = sqlite3.Row
    if site_slug:
        rows = conn.execute(
            "SELECT site_id, domain, local_path FROM sites WHERE site_id=? AND status='active'",
            (site_slug,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT site_id, domain, local_path FROM sites WHERE status='active'"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_site_products(site_slug):
    """Get product codes for a site."""
    conn = sqlite3.connect(REGISTRY_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT product_code FROM site_products WHERE site_id=?",
        (site_slug,)
    ).fetchall()
    conn.close()
    return [r["product_code"] for r in rows]


def get_last_product_sync(site_slug):
    """Get last_product_sync_at for freshness check."""
    conn = sqlite3.connect(REGISTRY_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT last_product_sync_at FROM sites WHERE site_id=?",
        (site_slug,)
    ).fetchone()
    conn.close()
    return row["last_product_sync_at"] if row else None


# ── Image discovery ────────────────────────────────────────────────────────
def discover_images(site_dir, max_images=MAX_IMAGES_PER_SITE):
    """
    Walk site_dir for HTML files, extract <img src> tags.
    Returns list of (src, page_path, is_product_card) tuples.
    Deduplicated by (src, page_path). Sorted alphabetically. Capped at max_images.
    """
    site_dir = Path(site_dir)
    images = []

    for html_file in sorted(site_dir.rglob("*.html")):
        # Skip .git, node_modules, backup dirs
        parts = set(html_file.parts)
        if parts & {".git", "node_modules", "_site", ".vercel", "__pycache__", "backup"}:
            continue

        try:
            soup = BeautifulSoup(html_file.read_text(), "html.parser")
        except Exception:
            continue

        for img in soup.find_all("img"):
            src = (img.get("src") or "").strip()
            if not src:
                continue

            # Determine if inside a product card
            parent = img.parent
            in_product_card = False
            for _ in range(5):  # check up to 5 ancestors
                if not parent or parent.name == "html":
                    break
                if parent.name == "div" and (
                    "product-card" in (parent.get("class") or [])
                    or parent.get("data-viator-id")
                ):
                    in_product_card = True
                    break
                parent = parent.parent

            key = (src, str(html_file))
            if key not in {(i[0], i[1]) for i in images}:
                images.append((src, str(html_file), in_product_card))

    # Sort alphabetically by (src, page_path)
    images.sort(key=lambda x: (x[0], x[1]))
    total = len(images)
    capped = total > max_images
    images = images[:max_images]

    return images, total, capped


# ── Classification ─────────────────────────────────────────────────────────
def classify_src(src, html_file_path, site_dir):
    """
    Classify img src into: SKIP, LOCAL (with resolved path), NETWORK (with URL).
    Classification order (per Gate A v6):
      1. empty → SKIP
      2. data: → SKIP
      3. // → NETWORK (prepend https:)
      4. http:// / https:// → NETWORK
      5. /absolute → LOCAL (site_dir + src)
      6. relative / ./ / ../ → LOCAL (resolved against html dir)
    """
    src = src.strip()

    # 1. Empty
    if not src:
        return ("SKIP", None)

    # 2. data: URI
    if src.startswith("data:"):
        return ("SKIP", None)

    # 3. Protocol-relative (//cdn...)
    if src.startswith("//"):
        return ("NETWORK", "https:" + src)

    # 4. Absolute URLs
    if src.startswith("http://") or src.startswith("https://"):
        return ("NETWORK", src)

    # Strip query/fragment for LOCAL paths only
    clean_src = src.split("?")[0].split("#")[0]

    # 5. Root-relative (/absolute/path)
    if clean_src.startswith("/"):
        resolved = site_dir.rstrip("/") + clean_src
        return ("LOCAL", resolved)

    # 6. Relative (resolved against HTML file directory)
    html_dir = os.path.dirname(html_file_path)
    resolved = os.path.normpath(os.path.join(html_dir, clean_src))
    return ("LOCAL", resolved)


# ── Checkers ───────────────────────────────────────────────────────────────
def check_local(path_str):
    """Check if local file exists. Returns (ok, size_kb)."""
    p = Path(path_str)
    if not p.exists() or not p.is_file():
        return (False, 0)
    size_kb = p.stat().st_size / 1024
    return (True, round(size_kb, 1))


def check_network(url):
    """HEAD request with GET fallback. Returns (ok, status_code)."""
    for method in ("HEAD", "GET"):
        try:
            if method == "HEAD":
                resp = requests.head(url, allow_redirects=True, timeout=NETWORK_TIMEOUT)
            else:
                resp = requests.get(url, allow_redirects=True, timeout=NETWORK_TIMEOUT, stream=True)
                resp.close()  # don't download body

            code = resp.status_code
            if code == 429:
                return (None, code)  # rate-limited → skip
            if code in (405, 501, 403) and method == "HEAD":
                continue  # fall back to GET
            return (200 <= code < 400, code)
        except requests.RequestException:
            return (False, 0)

    return (False, 0)


def check_viator_freshness(site_slug, site_dir):
    """
    Check if Viator product images exist on disk.
    Fresh = file exists. Stale = missing AND last sync > 30 days ago.
    Matches: {code}.{jpg,jpeg,png,webp} and {code}_1.{ext}, {code}-1.{ext}
    """
    product_codes = get_site_products(site_slug)
    last_sync = get_last_product_sync(site_slug)
    images_dir = Path(site_dir) / "images"

    if not images_dir.exists():
        return len(product_codes), 0, len(product_codes)  # all stale

    # Parse last_sync
    sync_age_days = None
    if last_sync:
        try:
            sync_dt = datetime.fromisoformat(last_sync)
            sync_age_days = (datetime.now(timezone.utc) - sync_dt.replace(tzinfo=timezone.utc)).days
        except (ValueError, TypeError):
            pass

    fresh = 0
    stale = 0
    for code in product_codes:
        # Check exact and variant matches
        found = False
        for suffix in [f".{ext}" for ext in ["jpg", "jpeg", "png", "webp", "JPG", "JPEG", "PNG", "WEBP"]] + \
                      [f"_{i}.{ext}" for i in range(1, 3) for ext in ["jpg", "jpeg", "png", "webp", "JPG", "JPEG", "PNG", "WEBP"]] + \
                      [f"-{i}.{ext}" for i in range(1, 3) for ext in ["jpg", "jpeg", "png", "webp", "JPG", "JPEG", "PNG", "WEBP"]]:
            if (images_dir / (code + suffix)).exists():
                found = True
                break
        if found:
            fresh += 1
        else:
            stale += 1

    # If last sync was recent (< 30d), stale might just be slow pipeline — don't penalize heavily
    if sync_age_days is not None and sync_age_days < STALE_VIATOR_DAYS:
        stale = 0

    return len(product_codes), fresh, stale


# ── Main check per site ────────────────────────────────────────────────────
def check_site(site, mode="slim"):
    """Run all image checks for one site. Returns dict."""
    site_dir = site["local_path"]
    slug = site["site_id"]

    if not os.path.isdir(site_dir):
        return {"error": f"directory not found: {site_dir}"}

    images, total_images, capped = discover_images(site_dir)

    broken = 0
    alt_missing = 0
    size_bloat = 0
    checked = 0
    alt_total = 0
    size_distribution = {"<100KB": 0, "100-500KB": 0, "500KB-1MB": 0, ">1MB": 0}

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = {}

        for src, page_path, is_product_card in images:
            classification, target = classify_src(src, page_path, site_dir)

            if classification == "SKIP":
                continue

            checked += 1

            if classification == "LOCAL":
                futures[executor.submit(check_local, target)] = (src, page_path, is_product_card, "LOCAL")
            elif classification == "NETWORK":
                futures[executor.submit(check_network, target)] = (src, page_path, is_product_card, "NETWORK")

        # Collect results with global deadline
        deadline = time.time() + GLOBAL_DEADLINE
        complete = True

        for future, (src, page_path, is_product_card, cls_type) in list(futures.items()):
            remaining = deadline - time.time()
            if remaining <= 0:
                complete = False
                break

            try:
                if cls_type == "LOCAL":
                    ok, size_kb = future.result(timeout=min(remaining, NETWORK_TIMEOUT))
                    if not ok:
                        broken += 1
                    else:
                        if size_kb > SIZE_BLOAT_KB:
                            size_bloat += 1
                        if size_kb < 100:
                            size_distribution["<100KB"] += 1
                        elif size_kb < 500:
                            size_distribution["100-500KB"] += 1
                        elif size_kb < 1024:
                            size_distribution["500KB-1MB"] += 1
                        else:
                            size_distribution[">1MB"] += 1
                else:  # NETWORK
                    ok, code = future.result(timeout=min(remaining, NETWORK_TIMEOUT))
                    if ok is None:  # 429 → skip
                        pass
                    elif not ok:
                        broken += 1

            except FuturesTimeout:
                complete = False
                break
            except Exception:
                broken += 1

        # Alt check (only on product-card images)
        for src, page_path, is_product_card in images:
            classification, _ = classify_src(src, page_path, site_dir)
            if classification == "SKIP":
                continue
            alt_total += 1
            if is_product_card and classification != "SKIP":
                # Parse again to get alt text
                try:
                    soup = BeautifulSoup(Path(page_path).read_text(), "html.parser")
                    for img in soup.find_all("img"):
                        if (img.get("src") or "").strip() == src:
                            alt = img.get("alt")
                            if alt is None or alt.strip() == "":
                                alt_missing += 1
                            break
                except Exception:
                    pass

    # Score
    score = 100
    score -= min(broken * 10, 30)
    score -= min(alt_missing * 3, 15)
    score -= min(size_bloat * 5, 15)
    # Viator freshness scored separately
    viator_score_penalty = 0

    result = {
        "score": score,
        "broken_img": broken,
        "alt_missing": alt_missing,
        "size_bloat": size_bloat,
        "checked": checked,
        "total_images": total_images,
        "complete": complete and not capped,
        "capped": capped,
    }

    # Full mode extras
    if mode == "full" and alt_total > 0:
        result["alt_coverage_pct"] = round((alt_total - alt_missing) / alt_total * 100, 1)

    if mode == "full":
        result["size_distribution"] = size_distribution

    if mode == "full":
        total_products, fresh, stale = check_viator_freshness(slug, site_dir)
        result["viator_total"] = total_products
        result["viator_fresh"] = fresh
        result["viator_stale"] = stale
        viator_score_penalty = min(stale * 5, 10)
        result["score"] = max(0, score - viator_score_penalty)

    return result


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Fleet image health check")
    parser.add_argument("--site", help="Site slug (omit for all active sites)")
    parser.add_argument("--mode", choices=["slim", "full"], default="slim",
                        help="Check depth: slim (daily) or full (weekly)")
    parser.add_argument("--json", action="store_true",
                        help="Machine JSON to stdout, human summary to stderr")
    args = parser.parse_args()

    sites = get_sites(args.site)
    if not sites:
        print(f"No active sites found" + (f" for '{args.site}'" if args.site else ""),
              file=sys.stderr)
        sys.exit(1)

    results = {}
    for site in sites:
        slug = site["site_id"]
        results[slug] = check_site(site, args.mode)

    # Build output
    output = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "sites": results
    }

    if args.json:
        print(json.dumps(output, default=str, indent=2))
    else:
        # Human summary
        for slug, data in results.items():
            if "error" in data:
                print(f"  {slug}: ERROR — {data['error']}")
                continue
            score = data["score"]
            broken = data["broken_img"]
            alt = data["alt_missing"]
            bloat = data["size_bloat"]
            complete = data["complete"]
            checked = data["checked"]
            total = data["total_images"]
            completeness = "" if complete else f" (partial: {checked}/{total})"

            print(f"  {slug}: score={score} broken={broken} alt_missing={alt} bloat={bloat}{completeness}")

            if args.mode == "full":
                print(f"    alt_coverage={data.get('alt_coverage_pct', '?')}%  "
                      f"viator: {data.get('viator_fresh', '?')}/{data.get('viator_total', '?')} fresh  "
                      f"stale: {data.get('viator_stale', '?')}")
                dist = data.get("size_distribution", {})
                if dist:
                    print(f"    sizes: {dist}")

        print(f"\n  Total: {len(results)} sites ({args.mode} mode)")

    # Write JSON to disk for fleet-health.py (regardless of --json flag)
    os.makedirs(os.path.dirname(IMAGE_HEALTH_JSON), exist_ok=True)
    with open(IMAGE_HEALTH_JSON, "w") as f:
        json.dump(output, f, default=str, indent=2)

    # Exit 1 if any broken images found
    has_broken = any(
        d.get("broken_img", 0) > 0
        for d in results.values()
        if "error" not in d
    )
    sys.exit(1 if has_broken else 0)


if __name__ == "__main__":
    main()
