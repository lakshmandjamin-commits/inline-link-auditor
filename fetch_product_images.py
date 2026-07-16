#!/usr/bin/env python3
"""
Fetch Viator product images via the API and download to site image directories.

Usage:
  python3 fetch_product_images.py                          # scan + report
  python3 fetch_product_images.py --fetch                  # download missing images
  python3 fetch_product_images.py --fetch --all            # re-download all (force)

Images are downloaded from TripAdvisor media CDN (Viator API returns these URLs).
Stored per-site under /images/{productCode}.jpg using the 720x480 variant.
Uses Rule #46a: pick the LARGEST variant by area, not the last one listed.

Pid: P00303273
Mcid: 42383
"""

import os, sys, re, sqlite3, json, time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

PID = "P00303273"
MCID = "42383"
DB_DIR = "/Users/saraswati/.hermes/affiliate-crons/db"
PRODUCT_DB = os.path.join(DB_DIR, "viator_cli.db")
SITE_DB = os.path.join(DB_DIR, "site_registry.db")

# Viator API base — same pattern used by viator_cli.py
VIATOR_API = "https://api.viator.com/partner"

SITES = {
    "porto-sommelier": "/Users/saraswati/sites/porto-wine-tours",
    "madeira-trail-guide": "/Users/saraswati/sites/madeira-trail-guide",
    "tenerife-outdoor-guide": "/Users/saraswati/sites/tenerife-outdoor-guide",
    "lapland-adventure-guide": "/Users/saraswati/sites/lapland-adventure-guide",
    "frasercoastadventures": "/Users/saraswati/sites/frasercoastadventures",
    "san-juan-excursions": "/Users/saraswati/sites/san-juan-excursions",
    "yogyakarta-temple-tours": "/Users/saraswati/sites/yogyakarta-temple-tours",
}

# Destination mapping + per-site destination filter
SITE_DESTINATIONS = {
    "tenerife-outdoor-guide": "tenerife",
    "madeira-trail-guide": "madeira",
    "porto-sommelier": "porto",
    "lapland-adventure-guide": "lapland",
    "frasercoastadventures": "fraser",
    "san-juan-excursions": "san-juan",
    "yogyakarta-temple-tours": "yogyakarta",
}

# Block cross-destination image copies
BLOCKED_DESTINATIONS = {
    "tenerife-outdoor-guide": ["madeira", "porto", "lapland"],
    "madeira-trail-guide": ["tenerife", "porto", "lapland"],
    "porto-sommelier": ["tenerife", "madeira", "lapland"],
    "lapland-adventure-guide": ["tenerife", "madeira", "porto"],
    "san-juan-excursions": [],
    "yogyakarta-temple-tours": [],
}


def load_api_key():
    """Load Viator API key from .env."""
    env_path = "/Users/saraswati/.hermes/.env"
    if not os.path.exists(env_path):
        print("ERROR: ~/.hermes/.env not found")
        sys.exit(1)
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("VIATOR_API_KEY="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    print("ERROR: VIATOR_API_KEY not found in .env")
    sys.exit(1)


def get_products_per_site():
    """Map product codes to the sites that use them by scanning HTML files."""
    site_products = {}
    for site_id, site_path in SITES.items():
        products = set()
        html_dir = site_path
        if not os.path.isdir(html_dir):
            print(f"  WARNING: Site path not found: {html_dir}")
            site_products[site_id] = products
            continue
        for root, dirs, files in os.walk(html_dir):
            dirs[:] = [d for d in dirs if "backup" not in d and not d.startswith(".")]
            for fn in files:
                if not fn.endswith(".html"):
                    continue
                fp = os.path.join(root, fn)
                try:
                    content = open(fp, errors="ignore").read()
                except:
                    continue
                # Extract bare product codes from Viator URLs
                for m in re.finditer(r"/d\d+-([A-Za-z0-9]+)", content):
                    code = m.group(1)
                    if len(code) >= 4 and not code.startswith("P") and not code.isdigit():
                        products.add(code)
                # Also catch URLs without destId prefix
                for m in re.finditer(r'viator\.com/tours/([A-Za-z0-9]+)', content):
                    code = m.group(1)
                    if len(code) >= 4 and not code.isdigit():
                        products.add(code)
        site_products[site_id] = sorted(products)
    return site_products


def get_all_products(db_path=PRODUCT_DB):
    """Get all product codes from the product catalog."""
    if not os.path.exists(db_path):
        print(f"ERROR: Product DB not found: {db_path}")
        return []
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT product_code FROM products ORDER BY product_code").fetchall()
    conn.close()
    return [r[0] for r in rows]


def fetch_product_images(api_key, product_code):
    """Fetch images for a product from the Viator API.
    Returns list of {url, width, height, is_cover} for images, or None on failure.
    """
    url = f"{VIATOR_API}/products/{product_code}"
    req = Request(url)
    req.add_header("exp-api-key", api_key)
    req.add_header("Accept", "application/json;version=2.0")
    req.add_header("Accept-Language", "en")
    req.add_header("Content-Type", "application/json")

    try:
        resp = urlopen(req, timeout=15)
        data = json.loads(resp.read().decode())
    except Exception as e:
        return None

    images = data.get("images", [])
    if not images:
        return []

    result = []
    for img in images:
        variants = img.get("variants", [])
        if not variants:
            continue
        # Rule #46a: pick largest by area
        best = max(variants, key=lambda v: v.get("height", 0) * v.get("width", 0))
        result.append({
            "url": best["url"],
            "width": best["width"],
            "height": best["height"],
            "is_cover": img.get("isCover", False),
            "source": img.get("imageSource", ""),
        })
    return result


def download_image(url, dest_path):
    """Download an image URL to a local path. Returns (success, bytes_downloaded)."""
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urlopen(req, timeout=30)
        data = resp.read()
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(data)
        return True, len(data)
    except Exception as e:
        return False, 0


def main():
    args = set(sys.argv[1:])
    do_fetch = "--fetch" in args
    force_all = "--all" in args

    api_key = load_api_key()
    all_products = get_all_products()
    site_products = get_products_per_site()

    print(f"Product DB has {len(all_products)} products")
    for sid, prods in site_products.items():
        print(f"  {sid}: {len(prods)} products referenced in pages")

    # Union of all products across sites
    all_site_products = set()
    for prods in site_products.values():
        all_site_products.update(prods)

    # Also include products from DB
    all_site_products.update(all_products)

    sorted_products = sorted(all_site_products)
    print(f"\nTotal unique products to fetch: {len(sorted_products)}")

    if not do_fetch:
        print("\nPass --fetch to download images")
        print("Pass --fetch --all to force re-download")
        return

    # For each product, fetch images and download to each site that uses it
    stats = {"downloaded": 0, "skipped": 0, "failed": 0, "api_errors": 0}

    for i, code in enumerate(sorted_products):
        # Determine which sites need this product
        target_sites = []
        for sid, prods in site_products.items():
            if code in prods:
                target_sites.append(sid)

        if not target_sites:
            target_sites = list(SITES.keys())  # DB-only products → all sites

        progress = f"[{i+1}/{len(sorted_products)}]"
        central_dir = "/Users/saraswati/.hermes/affiliate-crons/images"
        dest_path = os.path.join(central_dir, f"{code}.jpg")

        if os.path.exists(dest_path) and not force_all:
            stats["skipped"] += 1
            continue

        # Fetch from API
        images = fetch_product_images(api_key, code)
        if images is None:
            stats["api_errors"] += 1
            if i % 10 == 0:
                print(f"{progress} {code}: API error")
            continue
        if not images:
            stats["failed"] += 1
            if i % 10 == 0:
                print(f"{progress} {code}: no images")
            continue

        # Download up to 3 variants per product for diversity (cover + 2 alternates)
        all_variants = sorted(images, key=lambda x: x["is_cover"], reverse=True)
        variants_downloaded = 0
        for vi, img_data in enumerate(all_variants[:3]):
            # Naming: cover = {code}.jpg, alternates = {code}_1.jpg, {code}_2.jpg
            vpath = dest_path.replace(".jpg", f"_{vi}.jpg") if vi > 0 else dest_path
            if os.path.exists(vpath) and not force_all:
                variants_downloaded += 1
                continue
            success, size = download_image(img_data["url"], vpath)
            if success:
                stats["downloaded"] += 1
                variants_downloaded += 1
                if i % 5 == 0:
                    cover_mark = "📷" if img_data["is_cover"] else "  "
                    print(f"{progress} {code} v{vi}: {size//1024}KB {img_data['width']}x{img_data['height']} {cover_mark}")
            else:
                stats["failed"] += 1
            time.sleep(0.3)  # Rate limit

        if variants_downloaded == 0:
            stats["failed"] += 1

    # Copy images from central cache to each site's /images/ directory
    # DESTINATION FILTER: only copy if product destination matches site destination
    link_stats = {"copied": 0, "skipped": 0, "blocked": 0}
    # Load product-to-destination map
    prod_map = {}
    pmap_path = "/Users/saraswati/.hermes/affiliate-crons/data/product_topic_map.json"
    if os.path.exists(pmap_path):
        with open(pmap_path) as f:
            prod_map = json.load(open(pmap_path)) if pmap_path else {}
    
    for sid, site_path in SITES.items():
        site_img_dir = os.path.join(site_path, "images")
        os.makedirs(site_img_dir, exist_ok=True)
        site_dest = SITE_DESTINATIONS.get(sid, "")
        for code in site_products.get(sid, []):
            # Destination filter: skip if product is from a different destination
            if site_dest:
                prod_info = prod_map.get(code, {})
                prod_dest = prod_info.get("destination_name", "")
                if prod_dest and prod_dest.lower() != site_dest.lower():
                    link_stats["blocked"] += 1
                    continue
            
            for variant_num in [None, 1, 2]:  # cover + 2 alternates
                suffix = f"_{variant_num}" if variant_num is not None else ""
                src = os.path.join(central_dir, f"{code}{suffix}.jpg")
                dst = os.path.join(site_img_dir, f"{code}{suffix}.jpg")
                if not os.path.exists(src):
                    continue
                if os.path.exists(dst) and not force_all:
                    link_stats["skipped"] += 1
                    continue
                # Copy file
                with open(src, "rb") as fsrc:
                    with open(dst, "wb") as fdst:
                        fdst.write(fsrc.read())
                link_stats["copied"] += 1

    print(f"\nDone. Downloaded: {stats['downloaded']}, Skipped: {stats['skipped']}, "
          f"API errors: {stats['api_errors']}, Failed: {stats['failed']}")
    print(f"Copied to site image dirs: {link_stats['copied']} (skipped {link_stats['skipped']})")


if __name__ == "__main__":
    main()
