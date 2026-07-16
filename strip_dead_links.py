#!/usr/bin/env python3
"""Strip dead internal links from generated HTML pages.

Scans all <a href> in <main>, checks that internal links (/path) 
resolve to an actual file on disk. Replaces dead <a> tags with 
their anchor text (preserving the content, removing only the link).

Usage: python3 strip_dead_links.py <site_dir> [--fix]
"""

import sys, os, re
from pathlib import Path


def find_internal_links(html):
    """Find all internal <a href> links in <main>."""
    main_match = re.search(r'<main[^>]*>(.*?)</main>', html, re.DOTALL)
    if not main_match:
        return []
    content = main_match.group(1)
    links = []
    for m in re.finditer(r"""<a[^>]*href=["']([^"']+)['""][^>]*>""", content):
        href = m.group(1)
        if href.startswith('/') and not href.startswith('//'):
            links.append((m.group(0), href))
    return links


def check_link_exists(site_dir, href):
    """Check if an internal link resolves to a file."""
    # Strip query params and anchors
    path = href.split('?')[0].split('#')[0]
    # Map to actual file
    filepath = os.path.join(site_dir, path.lstrip('/'))
    if os.path.isfile(filepath):
        return True
    # Try with .html extension
    if os.path.isfile(filepath + '.html'):
        return True
    # Try index.html
    if os.path.isdir(filepath) and os.path.isfile(os.path.join(filepath, 'index.html')):
        return True
    return False


def strip_dead_links(html, site_dir, dry_run=True):
    """Find and optionally remove dead internal links."""
    links = find_internal_links(html)
    dead = []
    for anchor_html, href in links:
        if check_link_exists(site_dir, href):
            continue
        dead.append((anchor_html, href))
    
    if not dead:
        return html, []
    
    if not dry_run:
        for anchor_html, href in dead:
            # Replace dead <a> with just the anchor text (no link)
            text_match = re.search(r'<a[^>]*>(.*?)</a>', anchor_html)
            replacement = text_match.group(1) if text_match else anchor_html
            html = html.replace(anchor_html, replacement, 1)
    
    return html, dead


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 strip_dead_links.py <site_dir> [--fix]")
        sys.exit(1)
    
    site_dir = sys.argv[1]
    do_fix = '--fix' in sys.argv
    
    if not os.path.isdir(site_dir):
        print(f"ERROR: not a directory: {site_dir}")
        sys.exit(1)
    
    pages = []
    for root, dirs, files in os.walk(site_dir):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'node_modules']
        for f in files:
            if f.endswith('.html'):
                pages.append(os.path.join(root, f))
    
    total_dead = 0
    for page in sorted(pages):
        rel = os.path.relpath(page, site_dir)
        with open(page, encoding='utf-8') as fh:
            html = fh.read()
        
        new_html, dead = strip_dead_links(html, site_dir, dry_run=not do_fix)
        
        if dead:
            total_dead += len(dead)
            action = "FIX" if do_fix else "FOUND"
            print(f"{action} {rel}: {len(dead)} dead links")
            for _, href in dead:
                print(f"  DEAD: {href}")
            
            if do_fix and new_html != html:
                with open(page, 'w') as fh:
                    fh.write(new_html)
    
    if total_dead == 0:
        print("No dead internal links found.")
    else:
        print(f"\nTotal: {total_dead} dead links {'fixed' if do_fix else 'found'}.")
        if not do_fix:
            print("Run with --fix to remove dead links.")


if __name__ == '__main__':
    main()
