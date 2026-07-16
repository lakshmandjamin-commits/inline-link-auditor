#!/usr/bin/env python3
"""
Download curated free stock images for scene-setting on affiliate sites.

These serve as establishing/scenic shots that Viator product images don't cover.
All images are from Pexels (preferred, stable IDs) with Unsplash fallback.
Free for commercial use, no attribution required.

Usage:
  python3 fetch_free_stock.py --all          # download all to all sites
  python3 fetch_free_stock.py --site=porto   # download for one site only
  python3 fetch_free_stock.py --list         # show available images
  python3 fetch_free_stock.py --pexels       # use Pexels only (skip Unsplash)
  python3 fetch_free_stock.py --unsplash     # use Unsplash only (skip Pexels)

Images are downloaded at 800px width (~60-100KB) for hero/establishing use,
and 600px width (~40-60KB) for inline editorial use.
Stored under /images/stock-{topic}-{purpose}.jpg per site.

Multiple variants per topic support rotation: stock-{topic}-establishing-{0,1,2,3}.jpg
"""

import os, sys, time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

SITES = {
    "porto": "/Users/saraswati/sites/porto-wine-tours",
    "madeira": "/Users/saraswati/sites/madeira-trail-guide/sites/madeira-hiking",
    "tenerife": "/Users/saraswati/sites/tenerife-outdoor-guide",
    "lapland": "/Users/saraswati/sites/lapland-adventure-guide",
    "san-juan": "/Users/saraswati/sites/san-juan-excursions",
    "yogyakarta": "/Users/saraswati/sites/yogyakarta-temple-tours",
}

# Pexels photo IDs — stable, free for commercial use
# Format: {topic: {"establishing": [id1, id2, ...], "inline": [id1, ...]}}
# Multiple establishing IDs enable rotation across pages of the same topic.
PEXELS_PHOTOS = {
    # Site-neutral scenic images
    "landscape-mountains": {
        "establishing": [259209, 1261728, 257092, 733162],
        "inline": [733162, 1261728],
    },
    "landscape-coast": {
        "establishing": [994605, 2387417, 2918700, 3357024],
        "inline": [994605, 3357024],
    },
    "landscape-forest": {
        "establishing": [259209, 1261728, 2422265],
        "inline": [2422265],
    },
    "landscape-sunset": {
        "establishing": [2387417, 2918700, 994605],
        "inline": [2387417],
    },
    "travel-group": {
        "establishing": [2339009],
        "inline": [],
    },
    "food-wine": {
        "establishing": [994605],
        "inline": [],
    },
    "wine-cellars": {
        "establishing": [994605],
        "inline": [],
    },
    "city-view": {
        "establishing": [2339009, 1822605, 994605],
        "inline": [1822605],
    },
    "aurora": {
        "establishing": [1933239],
        "inline": [],
    },
    "winter-snow": {
        "establishing": [688660, 918744],
        "inline": [688660],
    },
    "dogsled-husky": {
        "establishing": [4388611, 3804329],
        "inline": [4388611],
    },
    "ocean-whale": {
        "establishing": [994605],
        "inline": [994605],
    },
    "boat-sailing": {
        "establishing": [994605, 1822605],
        "inline": [1822605],
    },
    "sunset-stars": {
        "establishing": [1933239],
        "inline": [],
    },
    "levada-forest": {
        "establishing": [259209, 2422265, 1261728],
        "inline": [2422265],
    },
    "adventure-cliff": {
        "establishing": [1638726, 2604520, 257092],
        "inline": [2604520],
    },
    "ice-snow": {
        "establishing": [688660, 918744],
        "inline": [918744],
    },
    # Topic-specific establishing shot variants (for rotation in injector)
    "stargazing": {
        "establishing": [1933239, 2387417],
        "inline": [],
    },
    "whale-watching": {
        "establishing": [994605],
        "inline": [],
    },
    "boat-tours": {
        "establishing": [994605, 1822605],
        "inline": [1822605],
    },
    "snorkeling": {
        "establishing": [994605, 2918700],
        "inline": [],
    },
    "hiking": {
        "establishing": [259209, 1261728, 257092, 733162],
        "inline": [733162, 1261728],
    },
    "adventure": {
        "establishing": [1638726, 2604520, 257092],
        "inline": [2604520],
    },
    "4x4-tours": {
        "establishing": [847483, 1787805, 1638726],
        "inline": [847483],
    },
    "husky": {
        "establishing": [4388611, 3804329],
        "inline": [4388611],
    },
    "snowmobile": {
        "establishing": [4895415, 2111013, 688660],
        "inline": [4895415],
    },
    "reindeer": {
        "establishing": [688660],
        "inline": [],
    },
    "santa": {
        "establishing": [3616956, 688660],
        "inline": [3616956],
    },
    "ice-hotel": {
        "establishing": [688660],
        "inline": [],
    },
    "ice-fishing": {
        "establishing": [268849, 688660, 918744],
        "inline": [268849],
    },
    "ice-floating": {
        "establishing": [4345373, 5028446, 688660],
        "inline": [4345373],
    },
    "winter": {
        "establishing": [688660, 918744],
        "inline": [688660],
    },
    "day-trips": {
        "establishing": [2339009, 1822605, 994605],
        "inline": [],
    },
    "planning": {
        "establishing": [994605],
        "inline": [],
    },
    "comparison": {
        "establishing": [2339009, 994605],
        "inline": [],
    },
    "other": {
        "establishing": [994605],
        "inline": [],
    },
}

# Unsplash IDs — fallback only. Many are dead as of June 2026.
UNSPLASH_PHOTOS = {
    "landscape-mountains": {
        "establishing": "1469474968028-56623f02e42e",
        "inline": "1506905925343-315869b1e230",
    },
    "landscape-coast": {
        "establishing": "1500382017468-9049fed747ef",
        "inline": "1468476398531-1b5c7e215f2d",
    },
    "landscape-forest": {
        "establishing": "1441974231531-c6227db76b6e",
        "inline": "1518531930633-e8c86d0cb135",
    },
    "landscape-sunset": {
        "establishing": "1504893524553-b855bce32c67",
        "inline": "1475920956731-e3015c1f0e3c",
    },
    "travel-group": {
        "establishing": "1551672740-871540c9a8f3",
        "inline": "1527631746610-b59ff91a7c72",
    },
    "food-wine": {
        "establishing": "1504672510290-0881e3d0a90f",
        "inline": "1414235077428-338989a2e8c0",
    },
    "wine-cellars": {
        "establishing": "1510810939930-73a4ba5f64dd",
        "inline": "1506374322096-602d171a4b48",
    },
    "city-view": {
        "establishing": "1499856871958-5b9627545d1a",
        "inline": "1512452518020-6f59e5c0a0ae",
    },
    "aurora": {
        "establishing": "1504893524553-b855bce32c67",
        "inline": "1531366284886-c5e39c4f1e7d",
    },
    "winter-snow": {
        "establishing": "1483664851311-e0abf48c8ab5",
        "inline": "1500531279541-fb0d28676f03",
    },
    "dogsled-husky": {
        "establishing": "1551967551-32e5a4a2e4e4",
        "inline": "1516205651412-e1c31b063932",
    },
    "ocean-whale": {
        "establishing": "1505222018886-e8e42d0e1aef",
        "inline": "1569390503676-507f9b2554c5",
    },
    "boat-sailing": {
        "establishing": "1505222018886-e8e42d0e1aef",
        "inline": "1464776832229-5b23c67e88e4",
    },
    "sunset-stars": {
        "establishing": "1462337701798-6a2b67f5d3b7",
        "inline": "1519681393784-d120267933ba",
    },
    "levada-forest": {
        "establishing": "1441974231531-c6227db76b6e",
        "inline": "1501785888041-af3ef285b470",
    },
    "adventure-cliff": {
        "establishing": "1464822759023-3e6a6fbcf080",
        "inline": "1558618666-8c4caf06d50e",
    },
    "ice-snow": {
        "establishing": "1531366284886-c5e39c4f1e7d",
        "inline": "1483664851311-e0abf48c8ab5",
    },
}

# Map sites to their relevant stock categories
SITE_STOCK_MAP = {
    "porto": ["city-view", "food-wine", "wine-cellars", "travel-group", "landscape-sunset", "day-trips"],
    "madeira": ["levada-forest", "landscape-coast", "landscape-mountains", "adventure-cliff", "travel-group", "landscape-forest", "ocean-whale", "hiking", "4x4-tours", "adventure"],
    "tenerife": ["landscape-mountains", "landscape-coast", "landscape-sunset", "sunset-stars", "ocean-whale", "boat-sailing", "travel-group", "food-wine", "adventure-cliff", "stargazing", "whale-watching", "snorkeling", "hiking"],
    "lapland": ["aurora", "winter-snow", "dogsled-husky", "ice-snow", "landscape-forest", "travel-group", "husky", "snowmobile", "reindeer", "santa", "ice-hotel", "ice-fishing", "ice-floating", "winter"],
    "san-juan": ["snorkeling", "landscape-coast", "landscape-mountains", "city-view", "day-trips", "travel-group", "ocean-whale", "adventure"],
    "yogyakarta": ["landscape-mountains", "landscape-sunset", "city-view", "travel-group", "hiking", "adventure", "food-wine"],
}


def download_pexels(photo_id, width, dest_path):
    """Download a Pexels image. Returns (success, bytes)."""
    url = f"https://images.pexels.com/photos/{photo_id}/pexels-photo-{photo_id}.jpeg?auto=compress&cs=tinysrgb&w={width}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        resp = urlopen(req, timeout=30)
        data = resp.read()
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(data)
        return True, len(data)
    except Exception as e:
        return False, 0


def download_unsplash(photo_id, width, dest_path):
    """Download an Unsplash image. Returns (success, bytes)."""
    url = f"https://images.unsplash.com/photo-{photo_id}?w={width}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        resp = urlopen(req, timeout=30)
        data = resp.read()
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(data)
        return True, len(data)
    except Exception as e:
        return False, 0


def download_pexels_variant(photo_id, dest_path):
    """Download a Pexels variant for rotation. Returns (success, bytes)."""
    return download_pexels(photo_id, 800, dest_path)


def main():
    args = set(sys.argv[1:])

    if "--list" in args:
        print("Available stock images (Pexels):")
        for topic, sizes in sorted(PEXELS_PHOTOS.items()):
            purpose_strs = []
            for purpose, ids in sizes.items():
                purpose_strs.append(f"{purpose}: {ids}")
            print(f"  {topic}: {'; '.join(purpose_strs)}")
        print("\nAvailable stock images (Unsplash fallback):")
        for topic, sizes in sorted(UNSPLASH_PHOTOS.items()):
            purpose_strs = []
            for purpose, pid in sizes.items():
                purpose_strs.append(f"{purpose}: {pid}")
            print(f"  {topic}: {'; '.join(purpose_strs)}")
        return

    do_all = "--all" in args
    use_pexels = "--pexels" in args or "--all" in args
    use_unsplash = "--unsplash" in args or "--all" in args or (not args & {"--pexels", "--unsplash"})
    site_filter = None
    for a in sys.argv:
        if a.startswith("--site="):
            site_filter = a.split("=", 1)[1].lower()

    targets = list(SITES.keys()) if do_all or site_filter is None else [site_filter]

    stats = {"downloaded": 0, "skipped": 0, "failed": 0}

    for site_name in targets:
        site_path = SITES.get(site_name)
        if not site_path or not os.path.isdir(site_path):
            print(f"ERROR: Site '{site_name}' not found at {site_path}")
            continue

        img_dir = os.path.join(site_path, "images")
        os.makedirs(img_dir, exist_ok=True)

        stock_categories = SITE_STOCK_MAP.get(site_name, ["landscape-mountains", "travel-group", "landscape-sunset"])
        print(f"\n--- {site_name} ---")

        for category in stock_categories:
            # Download establishing variants (multiple per topic for rotation)
            pexels_establishing = PEXELS_PHOTOS.get(category, {}).get("establishing", [])
            if use_pexels and pexels_establishing:
                for vi, pid in enumerate(pexels_establishing):
                    fname = f"stock-{category}-establishing-{vi}.jpg"
                    dest = os.path.join(img_dir, fname)
                    if os.path.exists(dest):
                        stats["skipped"] += 1
                        continue
                    success, size = download_pexels(pid, 800, dest)
                    if success:
                        stats["downloaded"] += 1
                        print(f"  ✓ {fname} ({size//1024}KB) [Pexels {pid}]")
                    else:
                        stats["failed"] += 1
                        print(f"  ✗ {fname} — Pexels download failed (id {pid})")
                    time.sleep(0.2)
            elif use_unsplash and category in UNSPLASH_PHOTOS:
                # Fallback: single Unsplash establishing
                pid = UNSPLASH_PHOTOS[category].get("establishing", "")
                if pid:
                    fname = f"stock-{category}-establishing-0.jpg"
                    dest = os.path.join(img_dir, fname)
                    if not os.path.exists(dest):
                        success, size = download_unsplash(pid, 800, dest)
                        if success:
                            stats["downloaded"] += 1
                            print(f"  ✓ {fname} ({size//1024}KB) [Unsplash {pid}]")
                        else:
                            stats["failed"] += 1
                            print(f"  ✗ {fname} — Unsplash download failed (id {pid})")
                        time.sleep(0.2)
                    else:
                        stats["skipped"] += 1

            # Download inline variants
            pexels_inline = PEXELS_PHOTOS.get(category, {}).get("inline", [])
            if use_pexels and pexels_inline:
                for vi, pid in enumerate(pexels_inline):
                    fname = f"stock-{category}-inline-{vi}.jpg"
                    dest = os.path.join(img_dir, fname)
                    if os.path.exists(dest):
                        stats["skipped"] += 1
                        continue
                    success, size = download_pexels(pid, 600, dest)
                    if success:
                        stats["downloaded"] += 1
                        print(f"  ✓ {fname} ({size//1024}KB) [Pexels {pid}]")
                    else:
                        stats["failed"] += 1
                        print(f"  ✗ {fname} — Pexels download failed (id {pid})")
                    time.sleep(0.2)
            elif use_unsplash and category in UNSPLASH_PHOTOS:
                pid = UNSPLASH_PHOTOS[category].get("inline", "")
                if pid:
                    fname = f"stock-{category}-inline-0.jpg"
                    dest = os.path.join(img_dir, fname)
                    if not os.path.exists(dest):
                        success, size = download_unsplash(pid, 600, dest)
                        if success:
                            stats["downloaded"] += 1
                            print(f"  ✓ {fname} ({size//1024}KB) [Unsplash {pid}]")
                        else:
                            stats["failed"] += 1
                            print(f"  ✗ {fname} — Unsplash download failed (id {pid})")
                        time.sleep(0.2)
                    else:
                        stats["skipped"] += 1

    print(f"\nDone. Downloaded: {stats['downloaded']}, Skipped: {stats['skipped']}, Failed: {stats['failed']}")


if __name__ == "__main__":
    main()
