#!/usr/bin/env python3
"""
Image Diversity Check — Enforces Rule #17 (max 4 pages per image).

Usage:
    python3 image_diversity_check.py <site-path>          # Check and report
    python3 image_diversity_check.py <site-path> --fix    # Auto-redistribute overused images

Scans all HTML files in a site directory, finds every establishing shot
(stock-* and viator-* establishing images), and flags any image used on
more than 4 pages. In --fix mode, redistributes overused images across
available variants.
"""
import os
import re
import sys
from collections import defaultdict

# Topic patterns for matching images to page content
TOPIC_KEYWORDS = {
    "hiking": ["hiking", "hike", "trail", "walk", "trek", "trekking", "levada", "pr1", "pr8", "pico", "anaga", "mountain", "peak", "summit", "wander", "wandern"],
    "adventure": ["adventure", "whale", "dolphin", "kayak", "kayaking", "canoe", "canyoning", "coasteering", "snorkel", "paraglide", "jet-ski", "abenteuer", "aventura"],
    "boat": ["boat", "cruise", "sail", "yacht", "catamaran", "ferry", "boot", "barco"],
    "food": ["food", "wine", "cooking", "market", "tasting", "gastronomy", "culinary", "port", "cellar", "essen", "comida"],
    "planning": ["planning", "guide", "best-time", "where-to-stay", "packing", "transfers", "budget", "cost", "getting-there", "planung", "plan"],
    "4x4": ["4x4", "jeep", "off-road", "4wd", "nuns-valley", "nuns valley", "east-west"],
    "4x4-tours": ["4x4", "jeep", "off-road", "4wd", "nuns-valley", "nuns valley", "east-west"],
    "wine-cellars": ["wine", "cellar", "port", "bodega", "tasting", "cata"],
    "day-trips": ["day-trip", "day-trips", "day trip", "braga", "guimaraes", "douro"],
    "husky": ["husky", "dog-sled", "sled", "mushing", "schlitten"],
    "snowmobile": ["snowmobile", "snow-mobile", "snow mobile"],
    "aurora": ["aurora", "northern-lights", "northern lights"],
    "ice-fishing": ["ice-fishing", "ice fishing"],
    "ice-floating": ["ice-floating", "ice floating"],
    "ice-hotel": ["ice-hotel", "ice hotel"],
    "reindeer": ["reindeer", "santa", "christmas", "weihnacht"],
    "winter": ["winter", "snow", "cold"],
    "snorkeling": ["snorkel", "scuba", "diving", "underwater", "schnorchel"],
    "stargazing": ["stargaze", "star", "astronomy", "night-sky", "observatory", "stern", "estrella"],
    "whale": ["whale", "dolphin", "wal", "ballena"],
    "whale-watching": ["whale", "dolphin", "wal", "ballena", "cetaceo", "avistamiento"],
    "city": ["city", "view", "skyline", "river", "douro"],
    "other": [],  # catch-all, matches everything
}


def get_topic_from_image(img_name):
    """Extract topic from image filename (stock-{topic}... or viator-{topic}...)."""
    m = re.match(r'(?:stock|viator)-([a-z-]+?)(?:-establishing|-\d)', img_name)
    if m:
        return m.group(1)
    return None


def get_topic_from_page(page_path):
    """Infer page topic from path keywords."""
    path_lower = page_path.lower()
    for topic, keywords in TOPIC_KEYWORDS.items():
        for kw in keywords:
            if kw in path_lower:
                return topic
    return None


def is_topic_match(img_name, page_path):
    """Check if an image's topic matches the page's topic."""
    img_topic = get_topic_from_image(img_name)
    page_topic = get_topic_from_page(page_path)
    
    if not img_topic or img_topic in ("other", "landscape"):
        return True  # generic images are always OK
    
    if not page_topic:
        return True  # can't determine page topic, assume OK
    
    # Check if page topic matches image topic
    page_kw = TOPIC_KEYWORDS.get(page_topic, [page_topic])
    img_kw = TOPIC_KEYWORDS.get(img_topic, [img_topic])
    
    # Direct match
    if img_topic == page_topic:
        return True
    
    return False


def scan_site(site_path):
    """Scan site and return {image_name: [page_paths]} mapping."""
    image_usage = defaultdict(set)
    topic_mismatches = []
    
    for root, dirs, files in os.walk(site_path):
        # Skip backup and .git directories
        dirs[:] = [d for d in dirs if d != ".git" and "backup" not in d]
        
        for fn in files:
            if not fn.endswith(".html"):
                continue
            
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, site_path)
            
            try:
                with open(fp, encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue
            
            # Find all img tags with stock or viator references
            for img in re.finditer(r'<img[^>]+src="([^"]*/(stock-[^"]*|viator-[^"]*))"', content):
                fname = img.group(1).split("/")[-1]
                image_usage[fname].add(rel)
    
    # Find violations
    violations = {img: pages for img, pages in image_usage.items() if len(pages) > 4}
    
    # Find topic mismatches (image on a page of unrelated topic)
    mismatches = []
    for img_name, pages in sorted(image_usage.items()):
        for page in pages:
            if not is_topic_match(img_name, page):
                mismatches.append((img_name, page))
    
    return image_usage, violations, mismatches


def find_available_variants(site_path, topic):
    """Find all establishing images available for a given topic."""
    images_dir = os.path.join(site_path, "images")
    if not os.path.isdir(images_dir):
        return []
    
    variants = []
    for fn in os.listdir(images_dir):
        if re.match(rf'(?:stock|viator)-{re.escape(topic)}.*\.(?:jpg|png|webp)', fn):
            variants.append(fn)
    return sorted(variants)


def redistribute_images(site_path, violations, image_usage):
    """Fix violations by redistributing overused images across available variants."""
    total_fixes = 0
    fixed_pages = []
    
    for img_name, pages in sorted(violations.items()):
        topic = get_topic_from_image(img_name)
        if not topic:
            print(f"  ⚠️  Cannot determine topic for '{img_name}' — skipping")
            continue
        
        # Find available variants in this topic
        variants = [v for v in sorted(image_usage.keys()) if get_topic_from_image(v) == topic and v != img_name]
        
        # Also look on disk for unused variants
        disk_variants = find_available_variants(site_path, topic)
        for v in disk_variants:
            if v not in image_usage and v != img_name and v not in variants:
                variants.append(v)
        
        if not variants:
            print(f"  ❌ No variants available for topic '{topic}' — need to download more images")
            continue
        
        # Track how many times each variant is already used
        variant_usage = {v: len(image_usage.get(v, set())) for v in variants}
        
        # Keep current image on 4 pages, redistribute the rest
        excess = sorted(pages)  # sorted for deterministic behavior
        keep = 4
        redistribute = excess[keep:]
        
        print(f"\n  Fixing '{img_name}' ({len(pages)} pages → keeping {keep}, redistributing {len(redistribute)})")
        
        for page_path in redistribute:
            # Find variant with lowest usage
            variant_usage_sorted = sorted(variant_usage.items(), key=lambda x: (x[1], x[0]))
            best_variant = variant_usage_sorted[0][0]
            
            full_path = os.path.join(site_path, page_path.lstrip("/"))
            if not os.path.exists(full_path):
                print(f"    WARNING: Page not found: {full_path}")
                continue
            
            try:
                with open(full_path, encoding="utf-8") as f:
                    html = f.read()
                
                # Replace the image reference - anchor with quotes
                old_src = f'src="/images/{img_name}"'
                new_src = f'src="/images/{best_variant}"'
                
                if old_src not in html:
                    # Try without leading slash
                    old_src2 = f'src="images/{img_name}"'
                    new_src2 = f'src="images/{best_variant}"'
                    if old_src2 in html:
                        old_src = old_src2
                        new_src = new_src2
                    else:
                        print(f"    ⚠️  Cannot find '{img_name}' in {page_path} — skipping")
                        continue
                
                html = html.replace(old_src, new_src, 1)
                
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(html)
                
                variant_usage[best_variant] = variant_usage.get(best_variant, 0) + 1
                total_fixes += 1
                fixed_pages.append((page_path, img_name, best_variant))
                print(f"    ✓ {page_path}: {img_name} → {best_variant}")
                
            except Exception as e:
                print(f"    ❌ Error processing {page_path}: {e}")
    
    return total_fixes, fixed_pages


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 image_diversity_check.py <site-path> [--fix]")
        sys.exit(1)
    
    site_path = sys.argv[1]
    fix_mode = "--fix" in sys.argv
    
    if not os.path.isdir(site_path):
        print(f"Error: '{site_path}' is not a directory")
        sys.exit(1)
    
    site_name = os.path.basename(site_path)
    print(f"\n{'='*60}")
    print(f"  Image Diversity Check: {site_name}")
    print(f"{'='*60}")
    
    image_usage, violations, mismatches = scan_site(site_path)
    
    print(f"\n  HTML files scanned (excluding backup/.git)")
    print(f"  Unique establishing images: {len(image_usage)}")
    print(f"  Pages with establishing shots: {sum(len(v) for v in image_usage.values())}")
    
    # Report violations
    print(f"\n  --- Rule #17 Violations (image on >4 pages) ---")
    if violations:
        for img_name, pages in sorted(violations.items()):
            print(f"  ⚠️  {img_name}: {len(pages)} pages (limit 4)")
            for p in sorted(pages)[:5]:
                print(f"       {p}")
            if len(pages) > 5:
                print(f"       ... +{len(pages)-5} more")
        
        # Fix mode
        if fix_mode:
            print(f"\n  --- Auto-Fix Mode ---")
            total, fixed = redistribute_images(site_path, violations, image_usage)
            if total > 0:
                print(f"\n  ✅ {total} fixes applied across {len(fixed)} pages")
            else:
                print(f"  ❌ No fixes applied — check available variants")
            
            # Re-scan to verify
            print(f"\n  --- Re-Verification ---")
            _, remaining, _ = scan_site(site_path)
            if remaining:
                print(f"  ⚠️  {len(remaining)} violations remain after fix:")
                for img, pgs in sorted(remaining.items()):
                    print(f"       {img}: {len(pgs)} pages")
                sys.exit(1 if remaining else 0)
            else:
                print(f"  ✅ All violations resolved")
                sys.exit(0)
        else:
            print(f"\n  ❌ {len(violations)} violations found. Run with --fix to auto-redistribute")
            sys.exit(1)
    else:
        print(f"  ✅ No Rule #17 violations found")
    
    # Report topic mismatches
    print(f"\n  --- Topic-Image Mismatches ---")
    if mismatches:
        print(f"  ⚠️  {len(mismatches)} potential mismatches (check manually):")
        for img_name, page in mismatches[:10]:
            print(f"       '{os.path.basename(img_name)}' on {page}")
        if len(mismatches) > 10:
            print(f"       ... +{len(mismatches)-10} more")
    else:
        print(f"  ✅ No topic mismatches found")
    
    # Summary
    print(f"\n  {'='*60}")
    print(f"  Status: {'✅ PASS' if not violations else '❌ FAIL'}")
    print(f"{'='*60}")
    
    # If we had violations but user didn't use --fix, still fail
    sys.exit(1 if violations else 0)


if __name__ == "__main__":
    main()
