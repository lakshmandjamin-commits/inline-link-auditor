#!/usr/bin/env python3
"""
Stray URL Detection & Fix — Fleet-Wide

Finds http/https URLs that appear as plain text in editorial content
(in <p> tags, between tags) instead of being wrapped in <a href="...">.
These are content generation artifacts — the AI wrote a URL as text
instead of as a hyperlink.

Usage:
  python3 stray_url_fixer.py /path/to/site          # scan + report
  python3 stray_url_fixer.py /path/to/site --fix     # convert to hyperlinks
  python3 stray_url_fixer.py /path/to/site --fix --dry-run  # preview

Strategy:
  For each bare URL found in visible text, wrap it in an <a> tag
  with rel="sponsored" if it's a Viator URL, or rel="nofollow"
  for external URLs, or noopener/noreferrer for external sites.
"""

import os, re, sys

# Known affiliate domains that should get rel="sponsored"
AFFILIATE_DOMAINS = ['viator.com', 'tiqets.com', 'getyourguide.com']

# Domains we own — internal links should use relative paths
OWNED_DOMAINS = [
    'tenerife-outdoor-guide.com',
    'porto-sommelier.com',
    'madeira-trail-guide.com',
    'lapland-adventure-guide.com',
]

# Image/file extensions to skip
SKIP_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.ico',
                   '.pdf', '.zip', '.mp3', '.mp4', '.mov', '.woff', '.woff2')


def find_stray_urls(html):
    """Find bare URLs in visible page text (not in attributes, scripts, JSON-LD)."""
    # Strip script, style, JSON-LD
    cleaned = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
    cleaned = re.sub(r'<style[^>]*>.*?</style>', '', cleaned, flags=re.DOTALL)
    cleaned = re.sub(r'<svg[^>]*>.*?</svg>', '', cleaned, flags=re.DOTALL)
    
    # Remove all HTML tags to get visible text
    text_only = re.sub(r'<[^>]+>', ' ', cleaned)
    
    results = []
    for m in re.finditer(r'(https?://[^\s<>"\')\]},;]+)', text_only):
        url = m.group(1).rstrip('.')
        pos = m.start()
        
        # Skip schema.org URLs (itemtype, etc.)
        if 'schema.org' in url:
            continue
        
        # Skip image/file extensions
        if any(url.lower().endswith(ext) for ext in SKIP_EXTENSIONS):
            continue
        
        # Get some context
        start = max(0, pos-80)
        end = min(len(text_only), pos+len(url)+80)
        context = text_only[start:end].strip()
        
        results.append((url, context))
    
    return results


def fix_bare_url(html, url):
    """Replace a bare URL with a proper <a> tag. Returns modified HTML or None."""
    # Determine rel attribute
    is_affiliate = any(domain in url.lower() for domain in AFFILIATE_DOMAINS)
    is_owned = any(domain in url.lower() for domain in OWNED_DOMAINS)
    
    if is_affiliate:
        rel = 'rel="sponsored"'
    elif is_owned:
        rel = ''  # Owned domain, no special rel
    else:
        rel = 'rel="nofollow noopener noreferrer"'
    
    target = 'target="_blank"' if rel else ''
    
    link_html = f'<a href="{url}" {rel} {target}>{url}</a>'.strip()
    link_html = re.sub(r'\s+', ' ', link_html)
    
    # Replace the URL in the HTML (first occurrence)
    if url in html:
        new_html = html.replace(url, link_html, 1)
        return new_html
    
    return None


def scan_and_fix(site_path, fix=False, dry_run=False):
    """Scan pages and optionally fix stray URLs."""
    results = {}  # rel_path -> [(url, context, action)]
    
    for root, dirs, files in os.walk(site_path):
        dirs[:] = [d for d in dirs if 'backup' not in d and not d.startswith('.') and d not in ('css', 'images', '__pycache__')]
        for fn in files:
            if not fn.endswith('.html'):
                continue
            
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, site_path)
            
            c = open(fp).read()
            urls = find_stray_urls(c)
            
            if not urls:
                continue
            
            if not fix:
                results[rel] = [(u, ctx, 'DETECTED') for u, ctx in urls]
                continue
            
            modified = False
            for url, ctx in urls:
                new_c = fix_bare_url(c, url)
                if new_c and new_c != c:
                    if dry_run:
                        if rel not in results:
                            results[rel] = []
                        results[rel].append((url, ctx, 'WOULD FIX'))
                    else:
                        c = new_c
                        modified = True
                        if rel not in results:
                            results[rel] = []
                        results[rel].append((url, ctx, 'FIXED'))
            
            if modified and not dry_run:
                open(fp, 'w').write(c)
    
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
    
    results = scan_and_fix(site_path, fix=fix, dry_run=dry_run)
    
    total_pages = len(results)
    total_urls = sum(len(v) for v in results.values())
    
    if total_urls == 0:
        print('✅ No stray URLs found.')
        sys.exit(0)
    
    for rel, entries in sorted(results.items()):
        for url, ctx, action in entries:
            symbol = {'DETECTED': '🔍', 'WOULD FIX': '💡', 'FIXED': '✅'}.get(action, '⚡')
            print(f'  {symbol} {rel}')
            print(f'     {action}: {url[:90]}')
            print(f'     Context: ...{ctx[:100]}...')
            print()
    
    print(f'\n{"="*50}')
    print(f'  Total: {total_urls} stray URLs across {total_pages} pages')
    if fix and not dry_run:
        print(f'  ✅ {total_urls} URLs wrapped in <a> tags')
    elif dry_run:
        print(f'  Run without --dry-run to fix')
    print(f'  {"="*50}')
    
    sys.exit(1 if not fix and total_urls > 0 else 0)


if __name__ == '__main__':
    main()
