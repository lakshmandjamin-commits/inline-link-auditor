#!/usr/bin/env python3
"""
Winner Box Link Injector — Fleet-Wide

Finds "🏆 My Top Pick" / "Quick Verdict" / "Top Pick" sections that
mention a tour name and price but have NO hyperlink to Viator.
Links the tour name in the winner box using the matching Viator URL
from the page's own tour review cards.

Usage:
  python3 winner_box_linker.py /path/to/site           # scan + report
  python3 winner_box_linker.py /path/to/site --fix      # inject links
  python3 winner_box_linker.py /path/to/site --fix --dry-run  # preview

PID: P00303273
MCID: 42383
"""

import os, re, sys

PID = "P00303273"
MCID = "42383"


def extract_card_urls(html):
    """Extract {tour_name_lower: viator_url} from tour-review-card/product-card sections."""
    mapping = {}
    
    # Tour review cards
    for card_match in re.finditer(
        r'<div[^>]*class="[^"]*(?:tour-review-card|product-card|comp-card)[^"]*"[^>]*>(.*?)(?:</div>\s*</div>|</article>)',
        html, re.DOTALL
    ):
        card_html = card_match.group(1)
        
        # Get h3 name
        h3_match = re.search(r'<h3[^>]*>(.*?)</h3>', card_html, re.DOTALL)
        if not h3_match:
            continue
        
        name = re.sub(r'<[^>]+>', '', h3_match.group(1)).strip()
        name = name.replace('&amp;', '&').replace('&nbsp;', ' ')
        # Remove emoji/icon prefixes
        name = re.sub(r'^[\U0001F000-\U0001FFFF\U00002000-\U00002BFF\U0000E000-\U0000FFFF]+\s*', '', name)
        name = name.strip()
        
        if len(name) < 8:
            continue
        
        # Find the nearest Viator URL
        url_match = re.search(
            r'href="(https?://(?:www\.)?viator\.com/tours/[^"]+?)"',
            card_html
        )
        if url_match:
            url = url_match.group(1)
            # Ensure tracking
            if 'pid=' not in url:
                sep = '&' if '?' in url else '?'
                url += f'{sep}pid={PID}&mcid={MCID}'
            mapping[name.lower()] = url
    
    return mapping


def find_winner_boxes(html):
    """Find winner-box/verdict sections that have a tour name but no Viator link."""
    boxes = []
    
    patterns = [
        r'<div[^>]*class="[^"]*winner-box[^"]*"[^>]*>(.*?)</div>',
        r'<div[^>]*class="[^"]*verdict[^"]*"[^>]*>(.*?)</div>',
    ]
    
    for pat in patterns:
        for m in re.finditer(pat, html, re.DOTALL):
            section = m.group(1)
            full_match = m.group(0)
            
            # Skip if already has a Viator link
            if 'viator.com' in section:
                continue
            
            # Extract the tour name from the strong tag or first phrase
            strong_match = re.search(r'<strong>(.*?)</strong>', section)
            if not strong_match:
                continue
            
            strong_text = re.sub(r'<[^>]+>', '', strong_match.group(1)).strip()
            strong_text = re.sub(r'\s+', ' ', strong_text)
            
            # Remove price for matching (we'll match on tour name only)
            name_only = re.sub(r'\s*\([€£$]\s*\d+.*?\)', '', strong_text).strip()
            name_only = re.sub(r'^[\U0001F000-\U0001FFFF\U00002000-\U00002BFF\U0000E000-\U0000FFFF]+\s*', '', name_only).strip()
            
            if len(name_only) < 8:
                continue
            
            boxes.append({
                'section': section,
                'full_match': full_match,
                'strong_text': strong_text,
                'name_only': name_only,
                'strong_match': strong_match,
            })
    
    return boxes


def find_best_url(tour_name, card_urls):
    """Find the best matching Viator URL for a tour name."""
    name_lower = tour_name.lower().strip()
    
    # Direct match
    if name_lower in card_urls:
        return card_urls[name_lower]
    
    # Partial match — find the card whose name is most similar
    best_score = 0
    best_url = None
    
    # Remove common suffixes and normalize
    for card_name, url in card_urls.items():
        # Count matching words
        name_words = set(name_lower.split())
        card_words = set(card_name.split())
        common = name_words & card_words
        
        if len(common) >= 2:
            score = len(common)
            if score > best_score:
                best_score = score
                best_url = url
    
    return best_url


def scan_winner_boxes(site_path, fix=False, dry_run=False):
    """Scan pages and inject links into winner boxes."""
    results = {}  # rel -> [actions]
    
    for root, dirs, files in os.walk(site_path):
        dirs[:] = [d for d in dirs if 'backup' not in d and not d.startswith('.') and d not in ('css', 'images', '__pycache__')]
        for fn in files:
            if not fn.endswith('.html'):
                continue
            
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, site_path)
            
            # Skip utility pages
            base = os.path.basename(fp)
            if base in ('about.html', 'contact.html', 'privacy.html'):
                continue
            
            c = open(fp).read()
            
            # Skip pages without Viator content
            if 'viator.com' not in c:
                continue
            
            # Extract card URLs for reference
            card_urls = extract_card_urls(c)
            if not card_urls:
                continue
            
            # Find winner boxes
            boxes = find_winner_boxes(c)
            if not boxes:
                continue
            
            modified = False
            actions = []
            
            for box in boxes:
                url = find_best_url(box['name_only'], card_urls)
                
                if not url:
                    actions.append(f'    ⚠ No URL match for: "{box["name_only"][:60]}"')
                    continue
                
                # Build the linked strong text
                linked_strong = f'<strong><a href="{url}" rel="sponsored" target="_blank">{box["strong_text"]}</a></strong>'
                
                if dry_run:
                    actions.append(f'    💡 Would link: "{box["strong_text"][:60]}"')
                    modified = True
                else:
                    # Replace the <strong>...</strong> inside the winner-box
                    old_strong = box['strong_match'].group(0)
                    new_box = box['section'].replace(old_strong, linked_strong, 1)
                    old_full = box['full_match']
                    new_full = old_full.replace(box['section'], new_box, 1)
                    
                    # Replace in full HTML
                    new_c = c.replace(old_full, new_full, 1)
                    if new_c != c:
                        c = new_c
                        modified = True
                        actions.append(f'    ✅ Linked: "{box["strong_text"][:60]}"')
                    else:
                        actions.append(f'    ❌ Failed: "{box["strong_text"][:60]}"')
            
            if modified:
                if not dry_run:
                    open(fp, 'w').write(c)
                results[rel] = actions
    
    return results


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
    print(f'Mode: {"FIX" if fix else "DETECT"}', end='')
    if dry_run:
        print(' (dry run)', end='')
    print('\n')
    
    results = scan_winner_boxes(site_path, fix=fix, dry_run=dry_run)
    
    total_pages = len(results)
    total_boxes = sum(len(v) for v in results.values())
    
    if total_pages == 0:
        print('✅ No unlinked winner boxes found.')
        sys.exit(0)
    
    for rel, actions in sorted(results.items()):
        print(f'  📄 {rel}')
        for a in actions:
            print(a)
        print()
    
    print(f'\n{"="*50}')
    print(f'  {total_pages} pages with {total_boxes} winner box(es)')
    if fix and not dry_run:
        print(f'  ✅ All linked')
    elif dry_run:
        print(f'  Run without --dry-run to fix')
    print(f'  {"="*50}')
    
    sys.exit(0 if not fix else (0 if total_boxes > 0 else 0))


if __name__ == '__main__':
    main()
