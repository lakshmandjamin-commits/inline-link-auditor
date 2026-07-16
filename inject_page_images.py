#!/usr/bin/env python3
"""
Inline Image Injector — Fleet-Wide

Scans editorial pages, finds natural breakpoints between paragraphs,
and inserts 4 types of images: establishing, what-you'll-see, experience, aspirational.

Images come from:
  - /images/{productCode}.jpg (downloaded Viator product images)
  - /images/stock-{topic}-{purpose}.jpg (downloaded free stock)

Usage:
  python3 inject_page_images.py /path/to/site              # scan + report
  python3 inject_page_images.py /path/to/site --fix        # inject images
  python3 inject_page_images.py /path/to/site --fix --dry  # show what would change

Pid: P00303273
Mcid: 42383
"""

import os, re, sys, subprocess, json

# Load product→topic mapping from Viator API categories
PRODUCT_TOPIC_MAP_PATH = os.path.expanduser("~/.hermes/affiliate-crons/data/product_topic_map.json")
_product_topic_map = None

def load_product_topic_map():
    """Load cached Viator product category map."""
    global _product_topic_map
    if _product_topic_map is not None:
        return _product_topic_map
    if os.path.exists(PRODUCT_TOPIC_MAP_PATH):
        with open(PRODUCT_TOPIC_MAP_PATH) as f:
            _product_topic_map = json.load(f)
    else:
        _product_topic_map = {}
    return _product_topic_map

def get_product_topic(product_code):
    """Get the Viator category topic for a product code. Returns None if unknown."""
    topic_map = load_product_topic_map()
    info = topic_map.get(product_code)
    if info:
        return info.get("topic")
    return None

UTILITY_PAGES = {
    "about.html", "contact.html", "privacy.html", "privacy",
    "datenschutz", "privacidad", "privacidade",
    "kontakt", "contacto", "contact", "acerca-de", "ueber-uns",
}
# Pages where the user is known to exist but has no images
STARGAZING_CODE = "63452P1"  # Teide by Night: Sunset and Stargazing
WHALE_CODE = "45458P1"       # Eco Respectful Whale Safari

VISUAL_STRATEGY_CACHE = {}  # site_dir → strategy dict


def load_visual_strategy(site_dir):
    """Load visual_strategy.json if it exists for this site."""
    if site_dir in VISUAL_STRATEGY_CACHE:
        return VISUAL_STRATEGY_CACHE[site_dir]
    strategy_path = os.path.join(site_dir, "..", "..", "generated", os.path.basename(site_dir), "visual_strategy.json")
    # Also try: GENERATED_DIR/{site_slug}/visual_strategy.json via known paths
    alt_paths = [
        os.path.expanduser(f"~/.hermes/affiliate-crons/generated/{os.path.basename(site_dir)}/visual_strategy.json"),
        os.path.join(site_dir, "visual_strategy.json"),
    ]
    for path in [strategy_path] + alt_paths:
        if os.path.exists(path):
            with open(path) as f:
                VISUAL_STRATEGY_CACHE[site_dir] = json.load(f)
                return VISUAL_STRATEGY_CACHE[site_dir]
    VISUAL_STRATEGY_CACHE[site_dir] = {}
    return {}


def is_editorial_page(rel_path):
    """Check if a page should get images (not utility)."""
    basename = os.path.basename(rel_path)
    if basename in UTILITY_PAGES:
        return False
    # Check parent directory names too
    parts = rel_path.replace("\\", "/").lower().split("/")
    for p in parts:
        if p in UTILITY_PAGES:
            return False
    return True


def get_topic(path_rel):
    """Determine the page's topic from its path for stock image selection."""
    p = path_rel.lower()
    if "sternbeobachtung" in p or "stargazing" in p or "observacion" in p or "estrellas" in p or "sunset-vs-stargazing" in p:
        return "stargazing"
    # Yogyakarta temple keywords — match before generic rules
    if "yogyakarta" in p or "borobudur" in p or "temple" in p or "prambanan" in p or "merapi" in p:
        return "hiking"
    if "jomblang" in p or "cave" in p:
        return "hiking"
    if "walbeobachtung" in p or "whale-watching" in p or "avistamiento" in p or "cetaceos" in p or "whale" in p:
        return "whale-watching"
    if "bootsausfluege" in p or "boat-tours" in p or "paseos-en-barco" in p or "cruceros" in p or "cruises" in p or "boat" in p:
        return "boat-tours"
    if "schnorcheln" in p or "snorkeling" in p or "buceo" in p:
        return "snorkeling"
    if "essen-wein" in p or "food-wine" in p or "comida-vino" in p or "food" in p or "wine-tours" in p or "tours-gastronomicos" in p:
        return "food-wine"
    if "abenteuer" in p or "adventure" in p or "aventura" in p:
        return "adventure"
    if "teide" in p or "masca" in p:
        return "hiking"
    if "senderismo" in p or "wanderung" in p or "hiking" in p or "levada" in p or "anaga" in p or "wandern" in p:
        return "hiking"
    if "husky" in p:
        return "husky"
    if "snowmobile" in p or "schneemobil" in p:
        return "snowmobile"
    if "northern-lights" in p or "aurora" in p:
        return "aurora"
    if "ice-fishing" in p or "eisfischen" in p:
        return "ice-fishing"
    if "reindeer" in p or "rentier" in p:
        return "reindeer"
    if "ice-hotel" in p or "eishotel" in p:
        return "ice-hotel"
    if "ice-floating" in p:
        return "ice-floating"
    if "santa" in p or "weihnachtsmann" in p:
        return "santa"
    if "4x4" in p or "jeep" in p or "nuns-valley" in p or "nonnental" in p:
        return "4x4-tours"
    if "canyoning" in p or "coasteering" in p or "kayak" in p or "paragliding" in p:
        return "adventure"
    if "douro" in p or "duero" in p or "braga" in p or "guimaraes" in p:
        return "day-trips"
    if "cellar" in p or "bodega" in p or "port-wine" in p or "porto" in p:
        return "wine-cellars"
    if "best-time" in p or "planning" in p or "planung" in p or "planificacion" in p or "packing" in p or "packliste" in p or "getting-there" in p or "anreise" in p:
        return "planning"
    if "budget" in p or "cost" in p or "kosten" in p:
        return "planning"
    if "kids" in p or "familien" in p or "familia" in p or "famil" in p:
        return "planning"
    if "compare" in p or "vergleich" in p or "compar" in p or "vs-" in p:
        return "comparison"
    if "transfer" in p or "anreise" in p:
        return "planning"
    if "winter" in p or "snow" in p or "ice" in p:
        return "winter"
    return "other"


def find_product_codes(html):
    """Extract bare Viator product codes from product cards and editorial content only.
    Only searches within <main> to avoid picking up codes from author bios, footers, or sidebars."""
    # Extract the <main> section first
    main_match = re.search(r'<main[^>]*>.*?</main>', html, re.DOTALL)
    if not main_match:
        return []
    main_content = main_match.group(0)
    
    codes = set()
    # Viator product code pattern: digits + letter + digits (e.g., 12964P16, 424075P51)
    VIATOR_CODE_RE = re.compile(r'^\d+[A-Z]+\d*$')
    # Extract from product-card <a href> patterns: /dXXXXX-CODE or /tours/CODE
    for m in re.finditer(r"/d\d+-([A-Za-z0-9]+)", main_content):
        code = m.group(1)
        if VIATOR_CODE_RE.match(code):
            codes.add(code)
    for m in re.finditer(r'viator\.com/tours/([A-Za-z0-9]+)', main_content):
        code = m.group(1)
        if VIATOR_CODE_RE.match(code):
            codes.add(code)
    return sorted(codes)


def image_exists(site_dir, fname):
    """Check if an image file exists in the site's /images/ directory."""
    img_path = os.path.join(site_dir, "images", fname)
    return os.path.exists(img_path)


def deduplicate_page_images(html, available_imgs, site_dir):
    """Post-injection guard: detect and fix duplicate product images in <main>.
    
    If the same product image filename appears 2+ times in main content,
    replace extras with unused variants from available_imgs or disk.
    Returns potentially modified HTML.
    """
    from collections import Counter
    
    main_match = re.search(r'<main[^>]*>(.*?)</main>', html, re.DOTALL)
    if not main_match:
        return html
    
    main_content = main_match.group(1)
    main_start = main_match.start()
    main_end = main_match.end()
    
    # Find all product image srcs (not stock, not author, not OG)
    product_imgs = re.findall(r'src="(/images/([^"]+\.jpg))"', main_content)
    counts = Counter(f[1] for f in product_imgs)
    dups = {k: v for k, v in counts.items() if v >= 2}
    
    if not dups:
        return html
    
    # Filter to actual product images (skip stock-, sofia, tiago, etc.)
    IGNORE = ('stock-', 'sofia', 'tiago', 'alejandro', 'og-', 'hero-', 'viator-topic', 'favicon')
    real_dups = {k: v for k, v in dups.items() if not any(k.startswith(p) for p in IGNORE)}
    
    if not real_dups:
        return html
    
    modified = html
    for dup_img, count in real_dups.items():
        # Extract base code (e.g., "192925P1_8.jpg" → "192925P1")
        base_match = re.match(r'^([A-Za-z0-9]+?)(?:_\d+)?\.jpg$', dup_img)
        if not base_match:
            continue
        base_code = base_match.group(1)
        
        # All variants on disk for this product
        all_variants = []
        fname = f"{base_code}.jpg"
        if image_exists(site_dir, fname):
            all_variants.append(fname)
        for s in range(0, 11):
            fname = f"{base_code}_{s}.jpg"
            if image_exists(site_dir, fname):
                all_variants.append(fname)
        
        # What's already used on the page (all variants of this product)
        used = set(re.findall(r'src="/images/(' + re.escape(base_code) + r'[^"]*)"', modified))
        unused = [v for v in all_variants if v not in used]
        
        if not unused:
            continue
        
        # Replace all but the first occurrence
        positions = [m.start() for m in re.finditer(
            re.escape(f'src="/images/{dup_img}"'), modified)]
        
        for pos in reversed(positions[1:]):  # Keep first, replace rest
            if not unused:
                break
            alt = unused.pop(0)
            old = f'src="/images/{dup_img}"'
            new = f'src="/images/{alt}"'
            modified = modified[:pos] + new + modified[pos + len(old):]
    
    return modified


def get_available_product_images(site_dir, codes, page_topic=None, site_destination=None):
    """Return sorted list of available image filenames (including variants like {code}_1.jpg, {code}_2.jpg).
    
    If page_topic is provided, only includes images whose Viator category topic matches.
    ALL images must also belong to the correct destination (no cross-destination).
    Products with unknown or wrong topics are NEVER included — we'd rather show fewer
    images than show a Madeira hiking photo on a Tenerife whale-watching page.
    """
    topic_matched = []
    
    for code in codes:
        # Skip non-product-code strings (URL slugs like "Costa", "Tenerife")
        if not re.match(r'^[A-Za-z0-9]{4,}$', code):
            continue
        
        # Check if we have any image files for this code (all variant suffixes)
        has_images = False
        img_names = []
        # Base image: {code}.jpg (no variant suffix)
        fname = f"{code}.jpg"
        if image_exists(site_dir, fname):
            has_images = True
            img_names.append(fname)
        # Variant suffixes: {code}_0.jpg through {code}_10.jpg
        for suffix in range(0, 11):
            fname = f"{code}_{suffix}.jpg"
            if image_exists(site_dir, fname):
                has_images = True
                img_names.append(fname)
        
        if not has_images:
            continue
        
        # Destination filter: reject cross-destination products
        if site_destination:
            prod_info = load_product_topic_map().get(code, {})
            prod_dest = prod_info.get("destination_name", "")
            if prod_dest and prod_dest.lower() != site_destination.lower():
                # Cross-destination product — skip entirely
                continue
        
        # Topic filtering
        # Lapland activity cluster: all winter/nordic activities are cross-relevant
        LAPLAND_TOPICS = {"aurora", "snowmobile", "reindeer", "husky", "santa",
                          "ice-fishing", "ice-hotel", "ice-floating", "winter", "planning", "other"}
        is_lapland = site_destination and site_destination.lower() == "lapland"
        
        if page_topic and page_topic != "comparison":
            prod_topic = get_product_topic(code)
            if prod_topic == page_topic or prod_topic == "other":
                # "other" is a valid Viator category — uncategorized products
                # are still relevant, just not mapped to a specific topic yet.
                topic_matched.extend(img_names)
            elif is_lapland and page_topic in LAPLAND_TOPICS and prod_topic in LAPLAND_TOPICS:
                # Lapland activities are cross-relevant: aurora images work on snowmobile pages, etc.
                topic_matched.extend(img_names)
            # ALL non-matching, non-"other" topics are excluded
            # Better to have fewer images than wrong ones
        else:
            # Comparison pages: include ALL product images regardless of topic.
            # Comparison content contextualizes the products, so cross-topic images
            # are always relevant to the comparison being made.
            topic_matched.extend(img_names)
    
    return topic_matched


# Per-run rotation tracking for topics with many pages
TOPIC_ESTABLISHING_COUNTER = {}

def find_stock_image(site_dir, topic, purpose):
    """Find a stock image for the given topic and purpose. Returns filename or None.
    
    For 'establishing' images, supports rotation via stock-{topic}-establishing-{n}.jpg
    so that topics with many pages (whale-watching: 17 pages) alternate between 
    different establishing shots instead of all using the same one.
    """
    imgs = os.path.join(site_dir, "images")
    
    # 1. Try topic-specific establishing shot with rotation counter
    if purpose == "establishing":
        count = TOPIC_ESTABLISHING_COUNTER.get(topic, 0)
        TOPIC_ESTABLISHING_COUNTER[topic] = count + 1
        
        # Try the rotated variant first: stock-{topic}-establishing-{n}.jpg
        fname = f"stock-{topic}-establishing-{count}.jpg"
        if image_exists(site_dir, fname):
            return fname
        
        # If rotation ran past available images, cycle back to 0
        fname = f"stock-{topic}-establishing-0.jpg"
        if image_exists(site_dir, fname):
            TOPIC_ESTABLISHING_COUNTER[topic] = 1  # reset for next call
            return fname
    
    # 2. Try exact purpose match: stock-{topic}-{purpose}.jpg
    fname = f"stock-{topic}-{purpose}.jpg"
    if image_exists(site_dir, fname):
        return fname
    
    # 3. Site-neutral fallback (last resort)
    for fallback in ["landscape-coast", "landscape-mountains", "landscape-sunset"]:
        fname = f"stock-{fallback}-{purpose}.jpg"
        if image_exists(site_dir, fname):
            return fname
    
    return None


def find_insertion_points(html, main_start, main_end):
    """Find natural insertion points BETWEEN <p> tags in <main> content.
    Returns list of (position_relative_to_main_content, paragraph_index) sorted by position.
    Works on original HTML — no stripping that would shift character positions.
    Skips paragraphs inside product-card, tour-review-card, faq, hero sections
    by tracking the nesting depth of forbidden elements.
    Only considers <p> with enough text (100+ chars) on one line.
    """
    main_content = html[main_start:main_end]
    
    # Build a set of character indices that are inside forbidden elements
    forbidden_ranges = set()
    for pattern in [
        r'<div[^>]*class="[^"]*(?:product-card|tour-review-card|faq)[^"]*"[^>]*>.*?</div>\s*</div>',
        r'<section[^>]*class="[^"]*faq[^"]*"[^>]*>.*?</section>',
        r'<header[^>]*class="[^"]*hero[^"]*"[^>]*>.*?</header>',
        r'<a[^>]*class="[^"]*category-card[^"]*"[^>]*>.*?</a>',
        r'<div[^>]*class="[^"]*author-block[^"]*"[^>]*>.*?</div>',
    ]:
        for m in re.finditer(pattern, main_content, re.DOTALL):
            for pos in range(m.start(), m.end()):
                forbidden_ranges.add(pos)
    
    points = []
    para_idx = 0
    for m in re.finditer(r'<p[^>]*>.*?</p>', main_content, re.DOTALL):
        # Skip if the <p> opening is inside a forbidden section
        p_start = m.start()
        if p_start in forbidden_ranges:
            continue
        
        text = re.sub(r'<[^>]+>', '', m.group(0)).strip()
        # Only consider substantial paragraphs
        sentences = text.count('.')
        if len(text) > 100 and sentences >= 1:
            para_idx += 1
            # Insert after paragraphs at indices: 2, 5, 8, 11 (1-indexed)
            if para_idx in (2, 5, 8, 11):
                # Position is right AFTER the </p> tag of this paragraph (relative to main_content)
                abs_pos = m.end()
                points.append((abs_pos, para_idx))
    
    return points


def build_gallery_html(site_dir, codes, topic, destination=None):
    """Build a photo gallery section from available product images."""
    available = get_available_product_images(site_dir, codes, page_topic=topic, site_destination=destination)
    if not available:
        return ""

    rows = []
    for fname in available[:8]:  # max 8 images in gallery
        # Get a title from the product DB if available
        alt = f"Tour experience"
        rows.append(f'      <img src="/images/{fname}" alt="{alt}" loading="lazy">')

    gallery = (
        '\n  <section class="photo-gallery">\n'
        '    <h2>Photo Gallery</h2>\n'
        '    <div class="gallery-grid">\n'
        + '\n'.join(rows) +
        '\n    </div>\n'
        '  </section>\n'
    )
    return gallery


def generate_image_html(src, alt, caption=None):
    """Generate HTML for an inline image with optional caption."""
    parts = [f'<div class="inline-image">']
    parts.append(f'  <img src="{src}" alt="{alt}" loading="lazy">')
    if caption:
        parts.append(f'  <p class="image-caption">{caption}</p>')
    parts.append('</div>')
    return '\n\n' + '\n'.join(parts) + '\n\n'


def inject_images(site_dir, rel_path, images_root=None, do_fix=False, dry_run=False):
    """Process a single page: report and optionally inject images."""
    if images_root is None:
        images_root = site_dir  # if not passed, assume site_dir is the root
    full_path = os.path.join(site_dir, rel_path)
    with open(full_path, encoding="utf-8", errors="ignore") as f:
        original = f.read()

    html = original
    topic = get_topic(rel_path)
    destination = get_site_destination(site_dir)

    # ── Visual Strategy: use pre-planned images if available ──
    strategy = load_visual_strategy(site_dir)
    page_slug = rel_path.rstrip('/').split('/')[-1]
    if page_slug == 'index.html':
        page_slug = rel_path.rstrip('/').split('/')[-2] if '/' in rel_path.rstrip('/') else 'home'
    page_slug = page_slug.replace('.html', '')
    visual_plan = strategy.get(page_slug) if strategy else None

    # Find main content
    main_match = re.search(r'<main[^>]*>', html)
    if not main_match:
        return f"SKIP {rel_path}: no <main>"

    main_start = main_match.end()
    main_end_match = re.search(r'</main>', html[main_start:])
    if not main_end_match:
        return f"SKIP {rel_path}: unclosed <main>"

    main_end = main_start + main_end_match.start()
    main_content = html[main_start:main_end]

    # Get product codes on this page
    product_codes = find_product_codes(html)
    available_imgs = get_available_product_images(images_root, product_codes, page_topic=topic, site_destination=destination)

    # Count existing <img> in main
    existing_img_count = len(re.findall(r'<img\b', main_content))

    if not available_imgs and not find_stock_image(images_root, topic, "establishing"):
        return f"SKIP {rel_path}: no images available ({topic}, codes={product_codes[:3]})"

    # If already has 3+ editorial images, still run dedup then skip injection
    if existing_img_count >= 3:
        html = deduplicate_page_images(html, available_imgs, images_root)
        if html != original:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(html)
            return f"FIX  {rel_path}: dedup'd product images"
        return f"OK   {rel_path}: already has {existing_img_count} images"

    # Find insertion points
    points = find_insertion_points(html, main_start, main_end)
    if not points:
        return f"SKIP {rel_path}: no suitable insertion points"

    # Plan 3-4 images max (fewer for short pages)
    max_imgs = min(len(points), max(1, 4 - existing_img_count))

    if max_imgs == 0:
        return f"OK   {rel_path}: has {existing_img_count} images, no more needed"

    if not do_fix:
        return f"NEED {rel_path}: {existing_img_count} imgs → insert {max_imgs} ({topic}, {len(available_imgs)} product imgs)"

    # Build images to insert (sorted by position)
    inserts = []
    used_images = set()  # Track full filenames to prevent duplicates on same page

    # RC1: Seed with images already on the page — prevents injector from
    # re-inserting images the LLM already placed in inline-image blocks.
    for m in re.finditer(r'<img[^>]*src="[^"]*/([^/"]+)"', main_content):
        used_images.add(m.group(1))

    # ── Visual Strategy override: use pre-planned images ──
    if visual_plan and do_fix:
        products = visual_plan.get("products", {})
        hero_code = visual_plan.get("hero_product", "")
        # Hero image from the primary product
        if hero_code and hero_code in products:
            hero = products[hero_code].get("hero", {})
            if hero.get("url"):
                hero_src = f"/images/{hero_code}.jpg"
                # Use first insertion point for hero
                inserts.append((points[0][0], generate_image_html(
                    hero_src, hero.get("alt", "Tour experience")
                ), hero_src))

        # Card/inline images from remaining products (skip hero product)
        remaining_codes = [c for c in products if c != hero_code]
        for i, code in enumerate(remaining_codes):
            if i + (1 if hero_code in products else 0) >= len(points):
                break
            pick = products[code]
            card = pick.get("card", {})
            src = f"/images/{code}.jpg"
            if card.get("url") and os.path.basename(src) not in used_images:
                point_idx = i + (1 if hero_code in products else 0)
                ai_src = src.replace("/images/", "/images/viator/")
                actual_src = ai_src if os.path.exists(os.path.join(images_root, "viator", os.path.basename(src))) else src
                inserts.append((points[point_idx][0], generate_image_html(
                    actual_src, card.get("alt", "Tour highlight")
                ), os.path.basename(actual_src)))
                used_images.add(os.path.basename(actual_src))

    # Fallback: heuristic image selection
    if not inserts:
        for i, (pos, para_idx) in enumerate(points[:max_imgs]):
            if i == 0:
                # First image: establishing shot — use topic-specific stock image with rotation
                stock_fname = find_stock_image(images_root, topic, "establishing")
                if stock_fname and stock_fname not in used_images:
                    inserts.append((pos, generate_image_html(
                        f"/images/{stock_fname}",
                        f"{topic.replace('-', ' ').title()} establishing shot"
                    ), stock_fname))
                    used_images.add(stock_fname)
                    continue
                # Fallback: use first available product image
                if available_imgs:
                    fname = available_imgs[0]
                    inserts.append((pos, generate_image_html(
                        f"/images/{fname}",
                        f"{topic.replace('-', ' ').title()} experience"
                    ), fname))
                    used_images.add(fname)
                    continue
                # No images available — skip establishing shot
                continue

            if i == max_imgs - 1:
                # Last image: aspirational — best unused product image
                for fname in available_imgs:
                    if fname not in used_images:
                        inserts.append((pos, generate_image_html(
                            f"/images/{fname}",
                            "Top-rated tour experience"
                        ), fname))
                        used_images.add(fname)
                        break
                else:
                    # All products used — skip this slot (no stock fallback)
                    pass
                continue

            # Middle images: product images (skip if already used)
            found = False
            for fname in available_imgs:
                if fname not in used_images:
                    used_images.add(fname)
                    inserts.append((pos, generate_image_html(
                        f"/images/{fname}",
                        "Tour experience"
                    ), fname))
                    found = True
                    break
            if not found:
                # No more unused topic-matched images — skip this slot instead of using stock
                pass


    # Sort by position (reverse so we insert bottom-up and don't shift positions)
    inserts.sort(key=lambda x: x[0], reverse=True)

    # Apply inserts (bottom-up to keep positions valid)
    for pos, img_html, src in inserts:
        # Insert after the </p> at this position
        main_content = main_content[:pos] + img_html + main_content[pos:]

    # Rebuild HTML
    html = html[:main_start] + main_content + html[main_end:]

    # Add gallery section before product cards (if not already present)
    gallery_html = None
    if '<section class="photo-gallery">' not in html:
        gallery_html = build_gallery_html(images_root, product_codes, topic, destination)
    if gallery_html:
        if dry_run:
            print(f"  Would add gallery ({len(available_imgs)} images)")
        else:
            # Find last product card div and insert gallery before it
            last_card = max(html.rfind('<div class="product-card'), html.rfind('<div class="tour-review-card'))
            if last_card > 0:
                # Insert gallery right before the product cards section
                section_start = html.rfind('<section', 0, last_card)
                if section_start > 0:
                    html = html[:section_start] + gallery_html + '\n' + html[section_start:]
                else:
                    # Fallback: insert before last product card
                    html = html[:last_card] + gallery_html + '\n' + html[last_card:]

    # Post-injection dedup: ensure no same product image appears 2+ times in main
    html = deduplicate_page_images(html, available_imgs, images_root)

    # Write if changed
    if html != original:
        if dry_run:
            print(f"  Would modify {rel_path}")
            return f"DRY  {rel_path}: would insert {len(inserts)} images + gallery"
        else:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(html)
            return f"FIX  {rel_path}: inserted {len(inserts)} images + gallery"
    else:
        return f"OK   {rel_path}: no changes needed"


def run_diversity_check(site_dir):
    """Run Rule #17 image diversity check after injection."""
    script = os.path.join(os.path.dirname(__file__), "image_diversity_check.py")
    if os.path.exists(script):
        result = subprocess.run(
            [sys.executable, script, site_dir],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            print("\n⚠️  Rule #17 violations detected. Auto-fixing...")
            subprocess.run(
                [sys.executable, script, site_dir, "--fix"],
                capture_output=True, text=True, timeout=120
            )
            # Re-verify
            result2 = subprocess.run(
                [sys.executable, script, site_dir],
                capture_output=True, text=True, timeout=60
            )
            if result2.returncode == 0:
                print("✅ Rule #17 violations resolved by auto-fix")
            else:
                print("❌ Rule #17 violations remain — see output above")
        else:
            print("✅ Rule #17 check passed — all images within diversity limits")


# Site directory → destination slug mapping
SITE_DESTINATIONS = {
    "tenerife-outdoor-guide": "tenerife",
    "madeira-trail-guide": "madeira",
    "madeira-trail-guide": "madeira",
    "porto-sommelier": "porto",
    "lapland-adventure-guide": "lapland",
    "yogyakarta-temple-tours": "yogyakarta",
}

def get_site_destination(site_path):
    """Determine destination from site directory name."""
    base = os.path.basename(os.path.abspath(site_path))
    return SITE_DESTINATIONS.get(base, "")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_fix = "--fix" in sys.argv
    dry_run = "--dry" in sys.argv

    if not args:
        site_path = os.getcwd()
    else:
        site_path = args[0]

    site_path = os.path.abspath(site_path)
    if not os.path.isdir(site_path):
        print(f"ERROR: not a directory: {site_path}")
        sys.exit(1)

    site_name = os.path.basename(site_path)

    # Resolve the actual site root directory for image lookups.
    # If site_path is a subdirectory (e.g. "what-to-pack-for-lapland-winter"),
    # walk up until we find the images/ directory.
    images_root = site_path
    while not os.path.isdir(os.path.join(images_root, "images")):
        parent = os.path.dirname(images_root)
        if parent == images_root:  # reached filesystem root — give up
            break
        images_root = parent

    # Collect all HTML files
    pages = []
    for root, dirs, files in os.walk(site_path):
        dirs[:] = [d for d in dirs if "backup" not in d and not d.startswith(".") and d not in ("css", "node_modules")]
        for fn in files:
            if fn.endswith(".html"):
                rel = os.path.relpath(os.path.join(root, fn), site_path)
                if is_editorial_page(rel):
                    pages.append(rel)

    pages.sort()
    print(f"{site_name}: {len(pages)} editorial pages\n")

    if not pages:
        print("No editorial pages found.")
        return

    if not do_fix:
        print(f"Scan mode. Pass --fix to inject images. Pass --dry for dry run.\n")

    results = {"ok": 0, "fixed": 0, "skipped": 0, "needed": 0}
    for rel in pages:
        result = inject_images(site_path, rel, images_root=images_root, do_fix=do_fix, dry_run=dry_run)
        if result.startswith("FIX"):
            results["fixed"] += 1
        elif result.startswith("OK"):
            results["ok"] += 1
        elif result.startswith("NEED"):
            results["needed"] += 1
        else:
            results["skipped"] += 1
        print(result)

    print(f"\n{'='*50}")
    print(f"Results: {results['fixed']} fixed, {results['ok']} ok, "
          f"{results['needed']} need images, {results['skipped']} skipped")
    print(f"Stock images in /images/: {len([f for f in os.listdir(os.path.join(images_root,'images')) if f.startswith('stock-')])}")
    print(f"Product images in /images/: {len([f for f in os.listdir(os.path.join(images_root,'images')) if not f.startswith('stock-') and f.endswith('.jpg') and f != 'hero.jpg'])}")

    # Run diversity check after injection
    if do_fix and results['fixed'] > 0:
        print()
        run_diversity_check(images_root)


if __name__ == "__main__":
    main()
