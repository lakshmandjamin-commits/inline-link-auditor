#!/usr/bin/env python3
"""
Page Image Audit — scans all editorial pages across all 4 sites.
For every <img> in <main>, determines if the image is valid for that page.
Outputs reports/page_image_audit.csv and reports/site_image_inventory.csv.

Checks: destination match, topic match, product active, planning-on-non-planning,
unknown product, stale auto-injected markers.

Usage:
  python3 audit_existing_page_images.py           # audit all sites
  python3 audit_existing_page_images.py --fix     # audit + flag for strip
"""
import os, sys, json, re, csv
from collections import defaultdict

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPTS_DIR, "..", "data")
REPORTS_DIR = os.path.join(SCRIPTS_DIR, "..", "reports")

SITE_CONFIG = json.load(open(os.path.join(DATA_DIR, "site_config.json")))
PRODUCT_OVERRIDES = json.load(open(os.path.join(DATA_DIR, "product_overrides.json")))

# Try to load existing product catalog or build from topic map
PRODUCT_MAP_PATH = os.path.join(DATA_DIR, "product_topic_map.json")
if os.path.exists(PRODUCT_MAP_PATH):
    PRODUCT_MAP = json.load(open(PRODUCT_MAP_PATH))
else:
    PRODUCT_MAP = {}


def get_page_topic(rel_path, site_slug):
    """Infer page topic from URL path."""
    p = rel_path.lower()
    # Explicit rules first
    if "whale" in p or re.search(r'(?:^|/)wal(?:beobacht|fisch|fang)', p) or "dolphin" in p or "cetaceo" in p:
        return "whale-watching"
    if "stargaz" in p or "sternbeobachtung" in p or "observacion-de-estrellas" in p or re.search(r'(?:^|/)astronom', p) or re.search(r'(?:^|/)nacht', p):
        return "stargazing"
    if "food" in p or "tapas" in p or "kochen" in p or "comida" in p or "gastro" in p or "restaurant" in p or "essen" in p:
        return "food-wine"
    if "wine" in p or "cellar" in p or "bodega" in p or "vinho" in p or "port" in p:
        return "wine-cellars"
    if "schnorchel" in p or "snorkel" in p or "buceo" in p:
        return "snorkeling"
    if "husky" in p or "dog" in p or "schlittenhund" in p:
        return "husky"
    if "snowmobile" in p or "schneemobil" in p or "moto" in p:
        return "snowmobile"
    if "reindeer" in p or "rentier" in p or "reno" in p:
        return "reindeer"
    if "aurora" in p or "northern" in p or "borealis" in p or "nordlicht" in p:
        return "aurora"
    if "santa" in p or "weihnachtsmann" in p or "navidad" in p:
        return "santa"
    if "ice" in p and ("hotel" in p or "floating" in p or "fishing" in p or "fish" in p or "eis" in p):
        if "fishing" in p or "fish" in p:
            return "ice-fishing"
        if "hotel" in p:
            return "ice-hotel"
        if "floating" in p:
            return "ice-floating"
    if "4x4" in p or "jeep" in p or "offroad" in p:
        return "4x4-tours"
    if "boat" in p or "catamaran" in p or "cruise" in p or "barco" in p or "boot" in p:
        return "boat-tours"
    if "hiking" in p or "hike" in p or "trail" in p or "levada" in p or "wander" in p or "senderismo" in p or "trek" in p or "anaga" in p or "masca" in p or "teide" in p:
        return "hiking"
    if "adventure" in p or "abenteuer" in p or "aventura" in p or "canyoning" in p or "coasteering" in p or "kayak" in p or "paragliding" in p:
        return "adventure"
    if "transfer" in p or "airport" in p or "flughafen" in p or "aeropuerto" in p:
        return "planning"
    if "plan" in p or "planung" in p or "pack" in p or "budget" in p or "best-time" in p:
        return "planning"
    # Comparison pages: inherit parent directory's topic instead of forcing "comparison"
    # Moved to end — only returns "comparison" if parent has no clear topic
    if "douro" in p or "duero" in p or "braga" in p or "guimaraes" in p:
        return "day-trips"
    return "unknown"


def extract_product_code_from_filename(filename):
    """Extract product code from image filename like '12964P17_2.jpg' → '12964P17'."""
    name = filename.replace('.jpg', '').replace('.jpeg', '').replace('.png', '')
    for suffix in ['_2', '_1', '_0']:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    # Check if it looks like a product code
    if re.match(r'^[A-Za-z0-9]{4,}$', name):
        return name
    return None


def get_product_info(code):
    """Get product metadata from catalog + overrides."""
    info = dict(PRODUCT_MAP.get(code, {}))
    
    # Apply overrides
    if code in PRODUCT_OVERRIDES:
        for k, v in PRODUCT_OVERRIDES[code].items():
            if k not in ('reason',):
                info[k] = v
    
    info.setdefault('topic', 'unknown')
    info.setdefault('title', info.get('title', ''))
    info.setdefault('destination_name', '')
    info.setdefault('destination_id', None)
    info.setdefault('canonical_topic', info.get('topic', 'unknown'))
    info.setdefault('status', 'active')  # Products in catalog without explicit status are active
    
    return info


def check_image_validity(img_src, product_code, product_info, page_topic, site_config):
    """Check if an image is valid for a given page."""
    img_topic = product_info.get('canonical_topic', product_info.get('topic', 'unknown'))
    img_destination = product_info.get('destination_name', '')
    
    # 1. Product not in catalog at all
    if not product_code or product_code not in PRODUCT_MAP:
        return False, "unknown_product", f"Product {product_code} not in catalog"
    
    # 2. Product inactive
    if product_info.get('status') == 'inactive':
        return False, "inactive_product", f"Product {product_code} is inactive/404"
    
    # 3. Cross-destination
    if img_destination:
        blocked = [d.lower() for d in site_config.get('blocked_destination_names', [])]
        if img_destination.lower() in blocked:
            return False, "invalid_destination", f"Image from {img_destination}, blocked for {site_config['destination_slug']}"
    
    # 4. Cross-topic: explicit mismatch
    img_topic = img_topic.lower()
    page_topic = page_topic.lower() if page_topic else "unknown"
    
    if img_topic == 'unknown':
        return False, "unknown_topic", "Product topic is unknown"
    
    if img_topic == 'planning' and page_topic != 'planning':
        return False, "planning_on_non_planning", f"Planning/transfer image on {page_topic} page"
    
    # 5. Explicit topic mismatch
    if img_topic != page_topic:
        # Check secondary topics
        secondary = [s.lower() for s in product_info.get('secondary_topics', [])]
        if page_topic not in secondary:
            return False, "invalid_topic", f"Image topic '{img_topic}' ≠ page topic '{page_topic}'"
    
    # 6. Eligible for inline check
    if not product_info.get('eligible_for_general_inline_images', True):
        return False, "not_inline_eligible", f"Product {product_code} marked as not eligible for general inline images"
    
    return True, "valid", ""


def audit_site(site_slug, site_config):
    """Audit all editorial pages for a site."""
    site_dir = site_config['site_dir']
    rows = []
    stats = defaultdict(int)
    
    UTILITY_PAGES = {'about.html', 'contact.html', 'privacy.html', 'privacy',
                     'datenschutz', 'privacidad', 'kontakt', 'contacto',
                     'acerca-de', 'ueber-uns', 'impressum',
                     'acerca-de.html', 'guia-de-viaje.html'}
    
    for root, dirs, files in os.walk(site_dir):
        dirs[:] = [d for d in dirs if 'backup' not in d and not d.startswith('.')
                   and d not in ('css', 'images', 'node_modules', '.git')]
        for fn in files:
            if not fn.endswith('.html'):
                continue
            fpath = os.path.join(root, fn)
            rel = os.path.relpath(fpath, site_dir)
            
            # Skip utility pages
            if os.path.basename(rel) in UTILITY_PAGES:
                continue
            
            try:
                html = open(fpath).read()
            except Exception:
                continue
            
            # Extract <main>
            main_match = re.search(r'<main[^>]*>(.*?)</main>', html, re.DOTALL)
            if not main_match:
                continue
            main_content = main_match.group(1)
            
            page_topic = get_page_topic(rel, site_slug)
            
            # Find all <img> in main
            for img_match in re.finditer(r'<img[^>]*src="([^"]*\.(?:jpg|jpeg|png))"[^>]*>', main_content, re.IGNORECASE):
                src = img_match.group(1)
                fname = os.path.basename(src)
                code = extract_product_code_from_filename(fname)
                
                if not code:
                    # Non-product image (author photo, hero, etc.) — skip
                    continue
                
                info = get_product_info(code)
                is_valid, reason, detail = check_image_validity(src, code, info, page_topic, site_config)
                
                stats[reason] += 1
                
                row = {
                    'site': site_slug,
                    'page_path': rel,
                    'img_src': src,
                    'product_code': code,
                    'image_topic': info.get('canonical_topic', info.get('topic', 'unknown')),
                    'image_destination': info.get('destination_name', ''),
                    'page_topic': page_topic,
                    'page_destination': site_config['destination_slug'],
                    'is_valid': is_valid,
                    'violation_reason': reason,
                    'detail': detail,
                    'product_title': info.get('title', '')[:100],
                }
                rows.append(row)
    
    return rows, stats


def main():
    do_fix = '--fix' in sys.argv
    
    os.makedirs(REPORTS_DIR, exist_ok=True)
    
    all_rows = []
    all_stats = defaultdict(int)
    site_inventory = {}
    
    for site_slug, site_config in SITE_CONFIG.items():
        if not os.path.isdir(site_config['site_dir']):
            print(f"SKIP {site_slug}: dir not found")
            continue
        
        print(f"\n{'='*60}")
        print(f"Auditing: {site_slug}")
        print(f"{'='*60}")
        
        rows, stats = audit_site(site_slug, site_config)
        all_rows.extend(rows)
        for k, v in stats.items():
            all_stats[k] += v
        
        # Per-site summary
        valid = stats.get('valid', 0)
        invalid = sum(v for k, v in stats.items() if k != 'valid')
        print(f"  Pages checked: {len(set(r['page_path'] for r in rows))}")
        print(f"  Images found: {len(rows)}")
        print(f"  Valid: {valid}")
        print(f"  Invalid: {invalid}")
        for reason, count in sorted(stats.items()):
            if reason != 'valid':
                print(f"    {reason}: {count}")
        
        # Site image inventory
        img_dir = os.path.join(site_config['site_dir'], site_config.get('public_image_dir', 'images'))
        if os.path.isdir(img_dir):
            imgs = [f for f in os.listdir(img_dir) if f.endswith('.jpg')]
            site_inventory[site_slug] = {
                'total_images': len(imgs),
                'product_images': len([f for f in imgs if not f.startswith('stock-') and not f.startswith('viator-')]),
                'samples': sorted(imgs)[:10]
            }
    
    # Write page_image_audit.csv
    audit_path = os.path.join(REPORTS_DIR, 'page_image_audit.csv')
    if all_rows:
        with open(audit_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=all_rows[0].keys())
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"\n✅ Audit written: {audit_path} ({len(all_rows)} images)")
    
    # Write site_image_inventory.csv
    inv_path = os.path.join(REPORTS_DIR, 'site_image_inventory.csv')
    with open(inv_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['site', 'total_images', 'product_images'])
        for slug, inv in site_inventory.items():
            writer.writerow([slug, inv['total_images'], inv['product_images']])
    print(f"✅ Inventory written: {inv_path}")
    
    # Print fleet-wide summary
    print(f"\n{'='*60}")
    print(f"FLEET SUMMARY")
    print(f"{'='*60}")
    print(f"Total images audited: {len(all_rows)}")
    print(f"Total valid: {all_stats.get('valid', 0)}")
    total_invalid = sum(v for k, v in all_stats.items() if k != 'valid')
    print(f"Total invalid: {total_invalid}")
    for reason, count in sorted(all_stats.items()):
        print(f"  {reason}: {count}")
    
    if do_fix:
        invalid_rows = [r for r in all_rows if not r['is_valid']]
        print(f"\n⚠️  {len(invalid_rows)} invalid images to strip (run strip_invalid_page_images.py)")
    
    return 0 if total_invalid == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
