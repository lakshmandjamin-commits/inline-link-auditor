#!/usr/bin/env python3
"""
Inline Viator Link Injector — Fleet-Wide

Scans all editorial pages on a site. For each page missing an inline
Viator affiliate link in <p> text within the first 400 words,
auto-injects one using product-card tour data from the same page.

Usage:
  python3 inject_inline_links.py /path/to/site         # scan + report only
  python3 inject_inline_links.py /path/to/site --fix    # inject missing links
  python3 inject_inline_links.py /path/to/site --fix --dry-run  # show what would happen

Requirements:
  - Pages must have product-card <div> sections with <h3> tour names
    and <a href="...viator.com..."> CTA links (standard fleet template)

Pid: P00303273
Mcid: 42383
"""

import os, re, sys, sqlite3

PID = "P00303273"
MCID = "42383"
UTILITY_PAGES = {'about.html', 'contact.html', 'privacy.html'}
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'db')


def get_product_ratings():
    """Get {product_code: rating} from viator_cli.db."""
    try:
        db_path = os.path.join(DB_DIR, 'viator_cli.db')
        db = sqlite3.connect(db_path)
        rows = db.execute("SELECT product_code, rating FROM products WHERE rating IS NOT NULL").fetchall()
        db.close()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def pick_best_tour(mapping):
    """Pick the highest-rated tour from the mapping. Falls back to first if no ratings."""
    if not mapping:
        return None, None
    if len(mapping) == 1:
        return list(mapping.items())[0]
    
    ratings = get_product_ratings()
    
    # Score each tour: rating (higher=better), prefer tours with known ratings
    def score(item):
        name, url = item
        # Extract product code from URL
        code_match = re.search(r'/d\d+-?(\w+)(?:[?/]|$)', url)
        if not code_match:
            code_match = re.search(r'/tours/(\w+)(?:[?/]|$)', url)
        code = code_match.group(1) if code_match else None
        rating = ratings.get(code, 0)  # Unknown = 0
        return (rating, len(name))  # Higher rating, shorter name as tiebreaker
    
    sorted_tours = sorted(mapping.items(), key=score, reverse=True)
    return sorted_tours[0]


def extract_tour_mapping(html):
    """Build {tour_name_lower: viator_url} from product-card sections."""
    mapping = {}
    # Find product-card divs and extract h3 + nearest Viator URL
    for card_match in re.finditer(
        r'<div[^>]*class="[^"]*product-card[^"]*"[^>]*>(.*?)</div>',
        html, re.DOTALL
    ):
        card_html = card_match.group(1)
        # Get the h3 name
        h3_match = re.search(r'<h3[^>]*>(.*?)</h3>', card_html, re.DOTALL)
        if not h3_match:
            continue
        name = re.sub(r'<[^>]+>', '', h3_match.group(1)).strip()
        name = name.replace('&amp;', '&').replace('&nbsp;', ' ')
        if len(name) < 5:
            continue
        
        # Find the nearest Viator URL in this card
        url_match = re.search(
            r'href="(https?://(?:www\.)?viator\.com/tours/[^"]+?)"',
            card_html
        )
        if url_match:
            url = url_match.group(1)
            # Ensure tracking params
            if 'pid=' not in url:
                sep = '&' if '?' in url else '?'
                url += f'{sep}pid={PID}&mcid={MCID}'
            mapping[name.lower()] = url
    
    return mapping


def find_insertion_point(body, max_editorial_words=400):
    """Find the best paragraph to insert an inline link, within first N editorial words.
    
    Returns (p_match, word_pos) or (None, None).
    Prefers paragraphs that are long enough to have a natural insertion point.
    Skips paragraphs inside hero, trust, or author sections.
    """
    # Hero/trust section patterns (look-behind to detect enclosing section)
    SECTION_EXCLUSIONS = {
        r'<div[^>]*class="[^"]*hero[^"]*"[^>]*>',
        r'<div[^>]*class="[^"]*why-i-built[^"]*"[^>]*>',
        r'<div[^>]*class="[^"]*trust[^"]*"[^>]*>',
        r'<div[^>]*class="[^"]*author-box[^"]*"[^>]*>',
        r'<div[^>]*class="[^"]*about-author[^"]*"[^>]*>',
    }
    
    candidates = []
    for p_match in re.finditer(r'<p[^>]*>(.*?)</p>', body, re.DOTALL):
        p_content = p_match.group(1)
        p_start = p_match.start()
        
        # Skip if this paragraph already has a Viator link
        if 'viator.com' in p_content.lower():
            continue
    
        # Skip if inside a category-card (nested <a> is invalid HTML)
        before_p = body[:p_start]
        card_match = re.search(r'<a[^>]*class="[^"]*category-card[^"]*"[^>]*>', before_p[::-1])
        if card_match:
            after_open = before_p[-(card_match.start()):]
            close_pos = after_open.find('</a>')
            if close_pos == -1:
                continue
        
        # Skip if inside hero, trust, or author section
        in_excluded = False
        for exclusion_pattern in SECTION_EXCLUSIONS:
            # Find opening tag before this paragraph, then check if we're inside it
            for section_open in re.finditer(exclusion_pattern, before_p):
                after_open = body[section_open.start():p_start]
                # Count <div> opens vs </div> closes — if opens > closes, we're inside
                opens = len(re.findall(r'<div[^>]*>', after_open))
                closes = len(re.findall(r'</div>', after_open))
                if opens > closes:
                    in_excluded = True
                    break
            if in_excluded:
                break
        if in_excluded:
            continue
        
        # Skip short or non-editorial paragraphs
        clean_text = re.sub(r'<[^>]+>', '', p_content).strip()
        words = clean_text.split()
        if len(words) < 15:
            continue
        
        # Calculate word position of this paragraph in the body
        before_text = re.sub(r'<[^>]+>', ' ', body[:p_start])
        word_pos = len(before_text.split())
        
        if word_pos < max_editorial_words:
            candidates.append((p_match, word_pos, len(words), len(clean_text)))
    
    if not candidates:
        return None, None
    
    # Prefer earlier paragraphs that are longer (more room to insert)
    candidates.sort(key=lambda c: (c[1], -c[3]))
    return candidates[0][0], candidates[0][1]


def insert_editorial_link(html, tour_name, tour_url, p_match, body_start, body_end, rel_path):
    """Insert a natural editorial link into the paragraph.
    
    Returns modified HTML or None if insertion failed.
    """
    p_html = p_match.group(0)
    p_content = p_match.group(1)
    
    # Clean the paragraph text to find natural insertion point
    clean_text = re.sub(r'<[^>]+>', '', p_content).strip()
    
    # Build the link HTML
    link_html = f'<a href="{tour_url}" rel="sponsored">{tour_name}</a>'
    
    # Strategy: find the end of the first sentence and insert after it
    # "I've done X. [Insert: If you want to book it, here's the link.] The next section..."
    # OR: replace a generic mention with the linked version
    
    # Strategy A: Find first sentence end (period followed by space or end)
    sentence_end = re.search(r'\.(\s+[A-Z]|\.\.\.|<\/)', p_content)
    if sentence_end and sentence_end.start() > 20:
        insert_pos = sentence_end.start()
        before = p_content[:insert_pos]
        after = p_content[insert_pos:]
        natural_prefixes = [
            ' I recommend booking the ',
            ' Book the ',
            ' If you want to try it, I recommend the ',
        ]
        prefix = natural_prefixes[hash(tour_name) % len(natural_prefixes)]
        new_p = before + prefix + link_html + '.' + after
    else:
        # Strategy B: Find the first natural verb phrase
        verb_matches = re.finditer(
            r'(I\s+(recommend|suggest|booked|took|joined|went on|tried)\s+[\w\s]+)',
            clean_text, re.IGNORECASE
        )
        verb_positions = [(m.start(), m.group()) for m in verb_matches]
        if verb_positions and verb_positions[0][0] > 10:
            pos = verb_positions[0][0]
            new_p = p_content[:pos] + f' {link_html} ' + p_content[pos:]
        else:
            # Strategy C: Append to end of paragraph
            new_p = p_content + f' {link_html}.'
    
    new_p_html = p_html.replace(p_content, new_p, 1)
    
    # Build the full modified HTML — p_match positions are relative to <main> body,
    # so offsets must be adjusted by body_start to target the correct position in the full document
    modified = html[:body_start + p_match.start()] + new_p_html + html[body_start + p_match.end():]
    return modified


def scan_site(site_path, fix=False, dry_run=False):
    """Scan all HTML files and inject inline links where missing.
    
    Returns stats dict.
    """
    scanned = 0
    missing = 0
    fixed = 0
    skipped_utility = 0
    skipped_no_products = 0
    errors = []
    
    for root, dirs, files in os.walk(site_path):
        dirs[:] = [d for d in dirs if 'backup' not in d and not d.startswith('.') and d not in ('css', 'images', '__pycache__')]
        for fn in files:
            if not fn.endswith('.html'):
                continue
            
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, site_path)
            
            # Skip utility pages
            if os.path.basename(fp) in UTILITY_PAGES:
                skipped_utility += 1
                continue
            
            try:
                with open(fp, 'r') as f:
                    html = f.read()
            except Exception as e:
                errors.append(f'{rel}: read error: {e}')
                continue
            
            scanned += 1
            
            # Skip pages without Viator content
            if 'viator.com' not in html:
                skipped_no_products += 1
                continue
            
            # Find <main> section
            m = re.search(r'<main[^>]*>(.*?)</main>', html, re.DOTALL)
            if not m:
                continue
            body = m.group(1)
            body_start = m.start()
            body_end = m.end()
            
            # Check if page already has an inline link in first 400 words
            already_has = False
            for p_match in re.finditer(r'<p[^>]*>(.*?)</p>', body, re.DOTALL):
                p_content = p_match.group(1)
                p_start = p_match.start()
                if 'viator.com' not in p_content:
                    continue
                # Check word position
                before_text = re.sub(r'<[^>]+>', ' ', body[:p_start])
                word_pos = len(before_text.split())
                if word_pos < 400:
                    already_has = True
                    break
            
            if already_has:
                continue
            
            missing += 1
            
            if not fix:
                continue
            
            # Extract tour mapping
            mapping = extract_tour_mapping(html)
            if not mapping:
                errors.append(f'{rel}: no product-card tour data found')
                continue
            
            # Find insertion point
            p_match, word_pos = find_insertion_point(body)
            if not p_match:
                errors.append(f'{rel}: no suitable insertion paragraph found')
                continue
            
            # Pick the highest-rated tour to link
            tour_name, tour_url = pick_best_tour(mapping)
            display_name = tour_name.title()
            
            if dry_run:
                print(f'  WOULD FIX: {rel} -> "{display_name}" at word {word_pos}')
                fixed += 1
                continue
            
            # Insert the link
            modified = insert_editorial_link(
                html, display_name, tour_url, p_match,
                body_start, body_end, rel
            )
            if modified:
                with open(fp, 'w') as f:
                    f.write(modified)
                print(f'  FIXED: {rel} -> "{display_name}" at word {word_pos}')
                fixed += 1
            else:
                errors.append(f'{rel}: injection failed')
    
    return {
        'scanned': scanned,
        'missing': missing,
        'fixed': fixed,
        'skipped_utility': skipped_utility,
        'skipped_no_products': skipped_no_products,
        'errors': errors,
        'site_path': site_path,
    }


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    
    site_path = sys.argv[1]
    fix = '--fix' in sys.argv
    dry_run = '--dry-run' in sys.argv
    
    if not os.path.isdir(site_path):
        print(f'Error: not a directory: {site_path}')
        sys.exit(1)
    
    print(f'Scanning: {site_path}')
    print(f'Mode: {"FIX" if fix else "DETECT only"}')
    if dry_run:
        print(f'  (dry run — no files will be modified)')
    print()
    
    stats = scan_site(site_path, fix=fix, dry_run=dry_run)
    
    print(f'\n--- Results ---')
    print(f'Scanned:        {stats["scanned"]}')
    print(f'Missing link:    {stats["missing"]}')
    print(f'Fixed:           {stats["fixed"]}')
    print(f'Utility skipped: {stats["skipped_utility"]}')
    print(f'No products:     {stats["skipped_no_products"]}')
    
    if stats['errors']:
        print(f'\nErrors ({len(stats["errors"])}):')
        for e in stats['errors']:
            print(f'  ⚠ {e}')
    
    if stats['fixed'] > 0 and not dry_run:
        print(f'\n✅ {stats["fixed"]} pages injected. Run link-audit.py to verify.')
    
    sys.exit(0 if stats['errors'] == 0 else 1)


if __name__ == '__main__':
    main()
