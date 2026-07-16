#!/usr/bin/env python3
"""2d - Disclosure Check: Every page with viator.com links must have affiliate disclosure."""
import re
import sys
from pathlib import Path

DISCLOSURE_PATTERNS = [
    r'affiliate',
    r'disclosure',
    r'commission',
    r'we earn',
    r'ganamos comisiones',
    r'comisión',
    r'divulgación',
]

def check_site(site_path):
    site = Path(site_path)
    html_files = list(site.rglob('*.html'))
    
    pages_with_viator = []
    pages_no_disclosure = []
    
    for fpath in sorted(html_files):
        try:
            content = fpath.read_text(encoding='utf-8', errors='ignore')
        except:
            continue
        
        # Check for Viator links
        has_viator = bool(re.search(r'viator\.com', content))
        if not has_viator:
            continue
        
        pages_with_viator.append(fpath)
        
        # Check for disclosure
        has_disclosure = False
        matched_pattern = None
        for pattern in DISCLOSURE_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                has_disclosure = True
                matched_pattern = pattern
                break
        
        if not has_disclosure:
            pages_no_disclosure.append(fpath)
    
    rel = site.name
    print(f"\n{'='*70}")
    print(f"Site: {rel}")
    print(f"Total HTML files: {len(html_files)}")
    print(f"Pages with Viator links: {len(pages_with_viator)}")
    print(f"Pages MISSING disclosure: {len(pages_no_disclosure)}")
    
    for p in pages_no_disclosure:
        print(f"  ❌ {p.relative_to(site)}")
    
    return {
        'site': rel,
        'viator_pages': len(pages_with_viator),
        'missing': [str(p.relative_to(site)) for p in pages_no_disclosure]
    }

def main():
    results = []
    for site_path in sys.argv[1:]:
        r = check_site(site_path)
        if r:
            results.append(r)
    
    print(f"\n\n{'='*70}")
    print("SUMMARY - Disclosure Check (2d)")
    print(f"{'='*70}")
    total_missing = sum(len(r['missing']) for r in results)
    print(f"Total sites: {len(results)}")
    print(f"Total pages missing disclosure: {total_missing}")
    for r in results:
        print(f"  {r['site']}: {len(r['missing'])} missing / {r['viator_pages']} viator pages")

if __name__ == '__main__':
    main()
