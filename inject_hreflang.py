#!/usr/bin/env python3
"""
Retroactively add hreflang tags to all pages in an affiliate site.
Yogya upgrade lesson: site launched with zero hreflang — this fixes that.
Usage: python3 inject_hreflang.py <site_path> [--domain <domain>] [--lang en]
"""
import re, sys
from pathlib import Path

def inject_hreflang(site_dir, domain, lang="en"):
    pages = list(Path(site_dir).rglob("*.html"))
    pages = [p for p in pages if ".review" not in str(p) and "node_modules" not in str(p)]
    
    fixed = 0
    skipped = 0
    for p in pages:
        content = p.read_text()
        if 'hreflang="en"' in content:
            skipped += 1
            continue
        
        canon_match = re.search(r'<link[^>]*rel="canonical"[^>]*href="([^"]+)"', content)
        if not canon_match:
            print(f"  SKIP {p.name}: no canonical")
            continue
        
        canon_url = canon_match.group(1)
        hreflang_block = (
            f'{canon_match.group(0)}\n'
            f'  <link rel="alternate" hreflang="{lang}" href="{canon_url}">\n'
            f'  <link rel="alternate" hreflang="x-default" href="{canon_url}">'
        )
        
        content = content.replace(canon_match.group(0), hreflang_block, 1)
        p.write_text(content)
        fixed += 1
        rel = p.relative_to(site_dir)
        print(f"  OK: {rel}")
    
    print(f"\nDone: {fixed} pages fixed, {skipped} already had hreflang, {len(pages)} total")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: inject_hreflang.py <site_path> [--domain <domain>] [--lang en]")
        sys.exit(1)
    
    site_dir = sys.argv[1]
    domain = None
    lang = "en"
    
    for i, arg in enumerate(sys.argv):
        if arg == "--domain" and i + 1 < len(sys.argv):
            domain = sys.argv[i + 1]
        if arg == "--lang" and i + 1 < len(sys.argv):
            lang = sys.argv[i + 1]
    
    if not domain:
        # Try to extract from sitemap or index.html
        index = Path(site_dir) / "index.html"
        if index.exists():
            m = re.search(r'canonical.*?href="https://(www\.[^"]+)"', index.read_text())
            if m:
                domain = m.group(1)
    
    if not domain:
        print("ERROR: Could not determine domain. Use --domain")
        sys.exit(1)
    
    inject_hreflang(site_dir, domain, lang)
