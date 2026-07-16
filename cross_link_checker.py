#!/usr/bin/env python3
"""
cross_link_checker.py — Audit internal cross-links across a site.

Checks:
  1. Every editorial page has ≥2 outbound internal links (to other pages on same site)
  2. Every page has ≥2 inbound links FROM other pages
  3. Reports orphan pages (0 inbound links)
  4. Reports pages below threshold

Usage:
  python3 cross_link_checker.py <site_path> [--fix]
"""

import os, re, sys, argparse
from collections import defaultdict

UTILITY_NAME_MAP = {
    'about.html', 'contact.html', 'privacy.html', '404.html',
    'ueber-uns.html', 'kontakt.html', 'datenschutz.html',
    'acerca-de.html', 'contacto.html', 'privacidad.html',
}

SKIP_DIRS = {'templates', 'css', 'images', 'js', 'assets', '.git', '.vercel', '__pycache__'}

def is_utility_page(fn, dirpath, root):
    if fn in UTILITY_NAME_MAP:
        return True
    # Also skip index.html in utility directories
    parent = os.path.basename(dirpath)
    if parent in {'ueber-uns', 'kontakt', 'datenschutz', 'acerca-de', 'contacto', 'privacidad'}:
        return True
    if fn == 'index.html' and dirpath == root:
        return True
    return False

def extract_links(html, domain_filter=None):
    """Extract all internal <a href> links from main content area."""
    main_match = re.search(r'<main[^>]*>(.*?)</main>', html, re.DOTALL)
    content = main_match.group(1) if main_match else html
    
    links = []
    for m in re.finditer(r'<a[^>]*href="(/[^"#][^"]*)"[^>]*>', content):
        href = m.group(1)
        if not href.startswith(('http://', 'https://')):
            links.append(href)
    
    return links

def extract_page_title(html):
    """Extract page title or H1."""
    m = re.search(r'<title>([^<]+)</title>', html)
    if m:
        title = m.group(1).strip()
        title = re.sub(r'\s*[-–|].*', '', title).strip()
        return title
    m = re.search(r'<h1[^>]*>([^<]+)</h1>', html)
    return m.group(1).strip() if m else 'Unknown'

def main():
    parser = argparse.ArgumentParser(description='Audit internal cross-links across a site')
    parser.add_argument('site_path', help='Path to site directory')
    parser.add_argument('--fix', action='store_true', help='Auto-fix by running inject_cross_links.py')
    parser.add_argument('--threshold', type=int, default=2, help='Minimum outbound links per page')
    args = parser.parse_args()
    
    root = os.path.abspath(args.site_path)
    
    # Build URL → page mapping
    pages = {}  # url_path → {title, filepath, outbound_links, inbound_count}
    
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if 'backup' not in d and not d.startswith('.') and d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith('.html'):
                continue
            if is_utility_page(fn, dirpath, root):
                continue
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, root)
            url_path = '/' + rel.replace('.html', '').replace('/index', '')
            
            with open(fp, 'r') as f:
                html = f.read()
            
            title = extract_page_title(html)
            outbound = extract_links(html)
            # Normalize outbound URLs (strip trailing slash, index)
            outbound = list(set(
                re.sub(r'/index$', '', u).rstrip('/') or '/'
                for u in outbound
            ))
            
            pages[url_path] = {
                'title': title,
                'filepath': fp,
                'rel': rel,
                'outbound': outbound,
                'inbound_count': 0,
            }
    
    # Count inbound links
    for url, data in pages.items():
        for other_url, other_data in pages.items():
            if url in other_data['outbound']:
                data['inbound_count'] += 1
    
    # Analyze
    below_threshold = []
    orphans = []
    
    for url, data in pages.items():
        out_count = len(data['outbound'])
        in_count = data['inbound_count']
        
        if out_count < args.threshold:
            below_threshold.append((url, out_count, in_count, data['title']))
        if in_count == 0:
            orphans.append((url, out_count, data['title']))
    
    # Report
    print(f"Site: {os.path.basename(root)}")
    print(f"Pages scanned: {len(pages)}")
    print(f"Threshold: ≥{args.threshold} outbound links")
    print()
    
    if below_threshold:
        print(f"❌ Below threshold ({len(below_threshold)} pages):")
        for url, out, in_count, title in sorted(below_threshold, key=lambda x: x[1]):
            print(f"  {url}")
            print(f"    Outbound: {out} | Inbound: {in_count} | {title[:60]}")
        print()
    else:
        print(f"✅ All pages meet ≥{args.threshold} outbound links")
        print()
    
    if orphans:
        print(f"⚠️  Orphan pages — 0 inbound links ({len(orphans)}):")
        for url, out, title in orphans:
            print(f"  {url} ({out} outbound) — {title[:60]}")
    else:
        print(f"✅ No orphan pages")
    
    # Summary stats
    out_counts = [len(d['outbound']) for d in pages.values()]
    in_counts = [d['inbound_count'] for d in pages.values()]
    print(f"\nStats:")
    print(f"  Outbound: min={min(out_counts)} max={max(out_counts)} avg={sum(out_counts)/len(out_counts):.1f}")
    print(f"  Inbound:  min={min(in_counts)} max={max(in_counts)} avg={sum(in_counts)/len(in_counts):.1f}")
    
    # Auto-fix
    if args.fix and below_threshold:
        injector = os.path.join(os.path.dirname(__file__), 'inject_cross_links.py')
        if os.path.exists(injector):
            print(f"\nRunning auto-fix via {injector}...")
            os.system(f"python3 {injector} {root}")
    
    # Exit code
    sys.exit(1 if below_threshold or orphans else 0)

if __name__ == '__main__':
    main()
