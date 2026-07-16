#!/usr/bin/env python3
"""
Strip all invalid images from editorial pages across the fleet.
Uses the audit CSV from audit_existing_page_images.py to identify which images to remove.
Also removes images from the filesystem if they don't belong to the site's destination.

Usage:
  python3 strip_invalid_page_images.py              # report what would be stripped
  python3 strip_invalid_page_images.py --fix        # strip invalid images from pages
  python3 strip_invalid_page_images.py --fix --purge # also delete cross-destination files from disk
"""
import os, sys, re, csv, json
from collections import defaultdict

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(SCRIPTS_DIR, "..", "reports")
AUDIT_CSV = os.path.join(REPORTS_DIR, "page_image_audit.csv")

SITES = {
    "tenerife-outdoor-guide": "/Users/saraswati/sites/tenerife-outdoor-guide",
    "madeira-trail-guide": "/Users/saraswati/sites/madeira-trail-guide/sites/madeira-hiking",
    "porto-sommelier": "/Users/saraswati/sites/porto-wine-tours",
    "lapland-adventure-guide": "/Users/saraswati/sites/lapland-adventure-guide",
    "san-juan-excursions": "/Users/saraswati/sites/san-juan-excursions",
    "yogyakarta-temple-tours": "/Users/saraswati/sites/yogyakarta-temple-tours",
}

DESTINATION_MAP = {
    "tenerife-outdoor-guide": "tenerife",
    "madeira-trail-guide": "madeira",
    "porto-sommelier": "porto",
    "lapland-adventure-guide": "lapland",
    "san-juan-excursions": "san-juan",
    "yogyakarta-temple-tours": "yogyakarta",
}


def strip_invalid_images_from_page(site_dir, rel_path, invalid_imgs):
    """Remove specific invalid <img> tags from a page's <main> content."""
    fpath = os.path.join(site_dir, rel_path)
    try:
        html = open(fpath).read()
    except Exception:
        return False, "read_error"
    
    modified = False
    
    for img_src in invalid_imgs:
        basename = os.path.basename(img_src)
        # Match <img ... src="/images/{basename}" ...> — case insensitive
        pattern = re.compile(
            r'<img\s+[^>]*src\s*=\s*["\']/images/' + re.escape(basename) + r'["\'][^>]*>',
            re.IGNORECASE
        )
        # Also match the exact src value in case different path format
        pattern2 = re.compile(
            r'<img\s+[^>]*src\s*=\s*["\']' + re.escape(img_src) + r'["\'][^>]*>',
            re.IGNORECASE
        )
        
        new_html = pattern.sub('', html)
        new_html = pattern2.sub('', new_html)
        
        if new_html != html:
            html = new_html
            modified = True
    
    if modified:
        with open(fpath, 'w') as f:
            f.write(html)
        return True, "stripped"
    
    return False, "not_found"


def purge_cross_destination_files(site_dir, destination):
    """Delete image files from disk that belong to other destinations."""
    img_dir = os.path.join(site_dir, "images")
    if not os.path.isdir(img_dir):
        return 0, 0
    
    prod_map = {}
    map_path = os.path.join(SCRIPTS_DIR, "..", "data", "product_topic_map.json")
    if os.path.exists(map_path):
        prod_map = json.load(open(map_path))
    
    deleted = 0
    skipped = 0
    
    for f in os.listdir(img_dir):
        if not f.endswith('.jpg'): continue
        if f.startswith(('stock-', 'viator-', 'alejandro', 'sofia', 'tiago', 'hero-')):
            continue
        
        # Extract product code
        code = re.sub(r'(_\d+)?\.jpg$', '', f)
        prod_info = prod_map.get(code, {})
        prod_dest = prod_info.get("destination_name", "")
        
        if prod_dest and destination and prod_dest.lower() != destination.lower():
            fpath = os.path.join(img_dir, f)
            os.remove(fpath)
            deleted += 1
        else:
            skipped += 1
    
    return deleted, skipped


def main():
    do_fix = "--fix" in sys.argv
    do_purge = "--purge" in sys.argv
    
    if not os.path.exists(AUDIT_CSV):
        print("ERROR: Run audit_existing_page_images.py first to generate page_image_audit.csv")
        sys.exit(1)
    
    # Read audit results
    rows = []
    with open(AUDIT_CSV) as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    
    invalid = [r for r in rows if r['is_valid'].strip().lower() == 'false']
    print(f"Audit: {len(rows)} images total, {len(invalid)} invalid\n")
    
    if not invalid:
        print("No invalid images to strip.")
        return
    
    # Group by site + page
    by_page = defaultdict(list)
    for r in invalid:
        key = (r['site'], r['page_path'])
        by_page[key].append(r['img_src'])
    
    print(f"Affected: {len(by_page)} pages across {len(set(k[0] for k in by_page))} sites")
    
    if not do_fix:
        # Report mode
        violation_counts = defaultdict(int)
        for r in invalid:
            violation_counts[r['violation_reason']] += 1
        
        print("\nViolation breakdown:")
        for reason, count in sorted(violation_counts.items(), key=lambda x: -x[1]):
            print(f"  {reason}: {count}")
        
        print("\nSample pages (first 15):")
        for i, ((site, page), imgs) in enumerate(sorted(by_page.items())[:15]):
            print(f"  {site}/{page}: {len(imgs)} invalid images")
            for img in imgs[:3]:
                print(f"    - {img}")
        return
    
    # Fix mode: strip invalid images
    stripped = 0
    errors = 0
    
    for (site, page), imgs in sorted(by_page.items()):
        site_dir = SITES.get(site)
        if not site_dir:
            print(f"SKIP {site}: unknown site")
            continue
        
        success, reason = strip_invalid_images_from_page(site_dir, page, imgs)
        if success:
            stripped += 1
            print(f"OK   {site}/{page}: stripped {len(imgs)} images")
        else:
            errors += 1
            print(f"FAIL {site}/{page}: {reason}")
    
    print(f"\nStripped: {stripped} pages, Errors: {errors}")
    
    # Purge cross-destination files from disk
    if do_purge:
        print("\n=== Purging cross-destination image files ===\n")
        for site_slug, site_dir in SITES.items():
            dest = DESTINATION_MAP.get(site_slug, "")
            deleted, skipped = purge_cross_destination_files(site_dir, dest)
            print(f"  {site_slug}: {deleted} deleted, {skipped} kept")


if __name__ == '__main__':
    main()
