#!/usr/bin/env python3
"""
inject_cross_links.py — Auto-inject internal cross-links between related pages.

Strategy (from cross-linking-strategy.md):
  Layer 2 — "Explore Related" sections on every editorial page
  Layer 3 — Cross-comparison links between comparison pages

Rules:
  - Same language (EN only links to EN, DE to DE, ES to ES)
  - Same destination (site)
  - Same topic (from path segments)
  - Descriptive anchor text using page H1
  - 2-4 links per "Explore More" section
  - Idempotent — replaces existing section, doesn't duplicate

Usage:
  python3 inject_cross_links.py <site_path> [--dry-run]
"""

import os, re, sys, argparse
from collections import defaultdict

SECTION_TEMPLATES = {
    'en': {
        'heading': 'Explore More',
        'intro': 'Related comparisons and guides:',
    },
    'de': {
        'heading': 'Mehr entdecken',
        'intro': 'Verwandte Vergleiche und Ratgeber:',
    },
    'es': {
        'heading': 'Explora más',
        'intro': 'Comparaciones y guías relacionadas:',
    },
}

UTILITY_NAME_MAP = {
    # EN
    'about.html', 'contact.html', 'privacy.html', '404.html',
    # DE
    'ueber-uns.html', 'kontakt.html', 'datenschutz.html',
    # ES
    'acerca-de.html', 'contacto.html', 'privacidad.html',
}
SKIP_DIRS = {'templates', 'css', 'images', 'js', 'assets', '.git', '.vercel', '__pycache__'}

def is_utility_page(rel_path):
    """Only skip the ROOT index.html (homepage). Subdirectory index.html files are editorial."""
    fn = os.path.basename(rel_path)
    if fn in UTILITY_NAME_MAP:
        return True
    # Also catch index.html in utility directories (de/ueber-uns/index.html etc.)
    parts = rel_path.rsplit('/', 2)
    if len(parts) >= 2 and parts[-2] in {'ueber-uns', 'kontakt', 'datenschutz', 'acerca-de', 'contacto', 'privacidad'}:
        return True
    # Only skip index.html at the root (homepage), not in subdirectories
    # 'index.html' → root homepage. 'category/index.html' → editorial page.
    if fn == 'index.html' and rel_path == 'index.html':
        return True
    return False

def detect_lang(path):
    """Detect language from path."""
    if '/de/' in path or path.startswith('de/'):
        return 'de'
    if '/es/' in path or path.startswith('es/'):
        return 'es'
    return 'en'

def extract_page_metadata(root, rel_path):
    """Extract title, topic, and language from a page."""
    fp = os.path.join(root, rel_path)
    with open(fp, 'r') as f:
        html = f.read()
    
    title = ''
    h1 = ''
    m = re.search(r'<title>([^<]+)</title>', html)
    if m:
        title = m.group(1).strip()
        # Strip site name suffix
        title = re.sub(r'\s*[-–|]\s*(Porto Wine Tours|Tenerife Outdoor Guide|Madeira Trail Guide|Lapland Adventure Guide).*', '', title).strip()
    
    m = re.search(r'<h1[^>]*>([^<]+)</h1>', html)
    if m:
        h1 = m.group(1).strip()
    
    # Determine topic from path AND content
    path_parts = rel_path.replace('.html', '').split('/')
    # Remove language prefix and 'index'
    path_parts = [p for p in path_parts if p not in ('de', 'es', 'index')]
    
    # Primary topic: directory name (e.g., 'levada-walks', 'adventure')
    if len(path_parts) > 1:
        topic = path_parts[0]  # First directory = topic
    else:
        # Root-level page: extract topic from filename keywords
        filename = path_parts[0] if path_parts else rel_path
        # Extract topic from common keywords in the filename
        keywords = ['canyoning', 'hiking', 'whale', 'kayak', 'boat', 'wine', 
                     'food', 'adventure', 'teide', 'stargaz', 'douro', 'porto',
                     'safari', 'husky', 'snowmobile', 'aurora', 'reindeer', 'lapland',
                     'snorkel', 'masca', 'trail', 'levada', '4x4', 'planning']
        for kw in keywords:
            if kw in filename.lower():
                topic = kw
                break
        else:
            # Fallback: first word of filename
            topic = filename.split('-')[0] if '-' in filename else 'general'
    
    # Derive URL path
    url_path = '/' + rel_path.replace('.html', '').replace('/index', '')
    
    lang = detect_lang(rel_path)
    
    # Check if page has product cards (is a money/comparison page)
    has_cards = bool(re.search(r'class="(product-card|comp-card|tour-review-card)"', html))
    
    # Check if it's a comparison page
    is_comparison = bool(re.search(r'vs[.-]|comparison|compare|vergleich|comparaci', rel_path.lower()))
    
    # Extract existing internal links in editorial content
    main_match = re.search(r'<main[^>]*>(.*?)</main>', html, re.DOTALL)
    main_html = main_match.group(1) if main_match else html
    internal_links = set()
    for m in re.finditer(r'href="(/[^"#][^"]*)"', main_html):
        internal_links.add(m.group(1))
    
    return {
        'rel_path': rel_path,
        'url_path': url_path,
        'title': title or h1 or rel_path,
        'h1': h1 or title,
        'topic': topic,
        'lang': lang,
        'has_cards': has_cards,
        'is_comparison': is_comparison,
        'existing_links': internal_links,
    }

def find_related_pages(page, all_pages, max_links=4):
    """
    Find related pages for cross-linking.
    Priority: same language + same topic + comparison pages first.
    """
    candidates = []
    
    for other in all_pages:
        if other['rel_path'] == page['rel_path']:
            continue
        if other['lang'] != page['lang']:
            continue
        
        # Calculate relevance score
        score = 0
        
        # Same topic = high relevance
        if other['topic'] == page['topic']:
            score += 10
        
        # Comparison pages are more valuable to link to
        if other['is_comparison']:
            score += 5
        
        # Pages with product cards are money pages → link to them
        if other['has_cards']:
            score += 3
        
        # Don't link to pages we already link to
        if other['url_path'] in page['existing_links']:
            score -= 20  # heavy penalty
        
        # Same broad category?
        if page['topic'] in other['topic'] or other['topic'] in page['topic']:
            score += 2
        
        # Keyword overlap in filenames (catches 'canyoning' ↔ 'adventure/canyoning-*')
        page_words = set(re.findall(r'[a-z]+', page['rel_path'].lower()))
        other_words = set(re.findall(r'[a-z]+', other['rel_path'].lower()))
        common = page_words & other_words - {'html', 'index', 'en', 'de', 'es', 'vs', 'and', 'or', 'the', 'for', 'to', 'in', 'of', 'guide', 'comparison', 'com'}
        if common:
            score += min(len(common), 3)
        
        if score > 0:
            candidates.append((score, other))
    
    candidates.sort(key=lambda x: -x[0])
    return [c[1] for c in candidates[:max_links]]

def build_explore_section(related_pages, lang, site_domain):
    """Build the 'Explore More' HTML section."""
    t = SECTION_TEMPLATES.get(lang, SECTION_TEMPLATES['en'])
    
    lines = []
    lines.append(f'\n<!-- CROSS-LINKS: auto-injected {len(related_pages)} related pages -->')
    lines.append(f'<section class="explore-more">')
    lines.append(f'  <h3>{t["heading"]}</h3>')
    lines.append(f'  <p>{t["intro"]}</p>')
    lines.append(f'  <ul>')
    
    for rp in related_pages:
        anchor_text = rp['h1'] or rp['title']
        # Truncate long titles
        if len(anchor_text) > 80:
            anchor_text = anchor_text[:77] + '...'
        lines.append(f'    <li><a href="{rp["url_path"]}">{anchor_text}</a></li>')
    
    lines.append(f'  </ul>')
    lines.append(f'</section>')
    
    return '\n'.join(lines)

def inject_into_page(filepath, related_pages, lang, site_domain):
    """Inject cross-links into a page's HTML."""
    with open(filepath, 'r') as f:
        html = f.read()
    
    section_html = build_explore_section(related_pages, lang, site_domain)
    
    # Remove existing explore-more section (idempotent)
    html = re.sub(
        r'\n<!-- CROSS-LINKS:.*?-->.*?</section>',
        '',
        html,
        flags=re.DOTALL
    )
    
    # Find injection point: before author block (only if inside <main>), or before </main>
    author_match = re.search(r'<div[^>]*class="[^"]*author[^"]*"[^>]*>', html)
    main_end = html.rfind('</main>')
    
    if author_match and main_end > 0 and author_match.start() < main_end:
        # Author block is inside <main> — inject before it
        pos = author_match.start()
        line_start = html.rfind('\n', 0, pos) + 1
        new_html = html[:line_start] + section_html + '\n\n' + html[line_start:]
    elif main_end > 0:
        # Fallback: inject before </main>
        new_html = html[:main_end] + '\n' + section_html + '\n' + html[main_end:]
    else:
        return None, "no injection point"
    
    return new_html, f"injected {len(related_pages)} cross-links"

def main():
    parser = argparse.ArgumentParser(description='Inject internal cross-links between related pages')
    parser.add_argument('site_path', help='Path to site directory')
    parser.add_argument('--dry-run', action='store_true', help='Show what would change without writing')
    parser.add_argument('--min-links', type=int, default=2, help='Minimum cross-links per page')
    args = parser.parse_args()
    
    root = os.path.abspath(args.site_path)
    domain = os.path.basename(root)
    
    print(f"Scanning {root}...")
    
    # Collect all page metadata
    all_pages = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if 'backup' not in d and not d.startswith('.') and d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith('.html'):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), root)
            if is_utility_page(rel):
                continue
            try:
                meta = extract_page_metadata(root, rel)
                all_pages.append(meta)
            except Exception as e:
                print(f"  SKIP {rel}: {e}")
    
    print(f"  Found {len(all_pages)} editorial pages")
    
    # Group by language for stats
    by_lang = defaultdict(int)
    for p in all_pages:
        by_lang[p['lang']] += 1
    for lang, count in sorted(by_lang.items()):
        print(f"    {lang}: {count} pages")
    
    # Inject cross-links
    results = {'injected': [], 'skipped_no_related': [], 'skipped_existing': [], 'errors': []}
    
    for page in all_pages:
        related = find_related_pages(page, all_pages)
        
        if len(related) < args.min_links:
            results['skipped_no_related'].append(f"{page['rel_path']} ({len(related)} found, need {args.min_links})")
            continue
        
        # Check if page already has sufficient cross-links in explore-more
        fp = os.path.join(root, page['rel_path'])
        with open(fp, 'r') as f:
            html = f.read()
        if 'explore-more' in html:
            results['skipped_existing'].append(page['rel_path'])
            continue
        
        fp = os.path.join(root, page['rel_path'])
        result = inject_into_page(fp, related, page['lang'], domain)
        
        if result[0] is None:
            results['errors'].append(f"{page['rel_path']}: {result[1]}")
            continue
        
        if not args.dry_run:
            with open(fp, 'w') as f:
                f.write(result[0])
        
        results['injected'].append(f"{page['rel_path']} ({len(related)} links)")
    
    # Report
    print(f"\n{'='*60}")
    print(f"Results ({'DRY RUN' if args.dry_run else 'APPLIED'}):")
    print(f"{'='*60}")
    print(f"\n✅ Injected ({len(results['injected'])}):")
    for r in results['injected'][:15]:
        print(f"  {r}")
    if len(results['injected']) > 15:
        print(f"  ... and {len(results['injected']) - 15} more")
    
    if results['skipped_no_related']:
        print(f"\n⏭️  Skipped — no related pages ({len(results['skipped_no_related'])}):")
        for r in results['skipped_no_related'][:10]:
            print(f"  {r}")
    
    if results['skipped_existing']:
        print(f"\n⏭️  Skipped — already has explore-more ({len(results['skipped_existing'])}):")
    
    if results['errors']:
        print(f"\n❌ Errors ({len(results['errors'])}):")
        for r in results['errors']:
            print(f"  {r}")

if __name__ == '__main__':
    main()
