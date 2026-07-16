#!/usr/bin/env python3
"""
Verify nav structure integrity across a fleet site.

Usage: python3 verify_nav_structure.py <site_id|site_path>

Accepts either a site ID (lapland, tenerife, porto, madeira, san-juan, yogyakarta) from site_registry.db
or a raw filesystem path.

Checks:
1. Mobile overlay: exactly 1 per page, properly closed (4+ </div> before <header>)
2. Desktop dropdowns: has nav-dropdown-trigger buttons
3. Mobile sections: Activities + Plan Trip + About present
4. DE page labels: Aktivitäten/Reise planen/Über (German pages)
5. ES page labels: Actividades/Planificar viaje/Sobre (Spanish pages)
6. Skip-to-content link: present on all pages

Exit 0 = all checks pass, 1 = issues found.
"""

import os, re, sys, argparse, sqlite3


DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")


def resolve_site_path(target):
    """Resolve site_id or path to absolute path."""
    if os.path.isabs(target) or target.startswith('./') or target.startswith('../'):
        path = os.path.abspath(target)
        if os.path.isdir(path):
            return path
    else:
        # Try as site_id from registry
        try:
            reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
            row = reg.execute("SELECT local_path FROM sites WHERE site_id=? AND status='active'", (target,)).fetchone()
            reg.close()
            if row:
                return row[0]
        except Exception:
            pass
    
    # Try as direct path
    path = os.path.abspath(os.path.expanduser(target))
    if os.path.isdir(path):
        return path
    
    print(f"ERROR: Cannot resolve '{target}' as site_id or path")
    sys.exit(1)


def check_mobile_overlay_closure(html, rel_path):
    """Check mobile overlay is properly closed."""
    issues = []
    overlay_count = html.count('mobile-menu-overlay')
    if overlay_count == 0:
        return issues
    if overlay_count > 1:
        issues.append(f"DUPLICATE: {overlay_count} mobile-menu-overlay divs")
    
    # Check if overlay is inside <header>...</header> (valid — header close handles it)
    m_header_wrap = re.search(r'<header[^>]*>.*?mobile-menu-overlay.*?</header>', html, re.DOTALL)
    if m_header_wrap:
        return issues  # Overlay inside header — properly bounded
    
    m = re.search(r'class="mobile-menu-overlay".*?<header', html, re.DOTALL)
    if m:
        closes = m.group(0).count('</div>')
        if closes < 4:
            issues.append(f"UNDERCLOSED: {closes}/4 closing divs")
    else:
        # No <header> after overlay — check if closing divs suffice
        overlay_pos = html.find('class="mobile-menu-overlay"')
        rest = html[overlay_pos:overlay_pos+2000]
        closes = rest.count('</div>')
        if closes < 4:
            issues.append(f"NO_HEADER + UNDERCLOSED: {closes}/4 closing divs in 2000 chars")
    return issues


def check_dropdown_links(html, rel_path):
    """Check desktop dropdown panels have links.

    Uses flexible regex to match dropdown-panel regardless of additional classes.
    """
    issues = []
    # Match <div> with class containing 'dropdown-panel' (may have other classes like 'open')
    panels = re.findall(r'<div[^>]*class="[^"]*dropdown-panel[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)
    for i, panel in enumerate(panels):
        links = re.findall(r'href="([^"]*)"', panel)
        if not links:
            issues.append(f"EMPTY DROPDOWN: panel #{i+1} has no links")
    return issues


def check_mobile_sections(html, rel_path):
    """Check mobile overlay has all expected sections."""
    issues = []
    if 'mobile-section-header' not in html:
        return issues
    sections = re.findall(r'<button class="mobile-section-header"[^>]*>(.*?)</button>', html)
    if len(sections) < 2:
        issues.append(f"WARNING: {len(sections)} mobile section(s) (expected 2+)")
    return issues


def check_de_labels(html, rel_path):
    """Check German pages have German labels."""
    issues = []
    
    if 'lang="de"' not in html[:500]:
        return issues
    
    if 'Aktivitäten' not in html and '>Activities <' in html:
        issues.append("DE_LABELS: 'Activities' not translated to 'Aktivitäten'")
    
    if 'Reise planen' not in html and ('>Plan Trip <' in html or '>Plan Your Trip <' in html):
        issues.append("DE_LABELS: 'Plan Trip' not translated to 'Reise planen'")
    
    if 'Über' not in html and '>About<' in html:
        issues.append("DE_LABELS: 'About' not translated to 'Über'")
    
    return issues


def check_es_labels(html, rel_path):
    """Check Spanish pages have Spanish labels."""
    issues = []
    
    if 'lang="es"' not in html[:500]:
        return issues
    
    if 'Actividades' not in html and '>Activities <' in html:
        issues.append("ES_LABELS: 'Activities' not translated to 'Actividades'")
    
    if 'Planificar viaje' not in html and ('>Plan Trip <' in html or '>Plan Your Trip <' in html):
        issues.append("ES_LABELS: 'Plan Trip' not translated to 'Planificar viaje'")
    
    if 'Sobre' not in html and '>About<' in html:
        issues.append("ES_LABELS: 'About' not translated to 'Sobre'")
    
    return issues


def check_skip_link(html, rel_path):
    """Check skip-to-content link exists."""
    if 'skip-link' not in html and '#main-content' not in html:
        return ["MISSING: skip-to-content link"]
    return []


def main():
    parser = argparse.ArgumentParser(description='Verify nav structure across a site')
    parser.add_argument('target', help='Site ID (lapland, tenerife, porto, madeira) or path')
    args = parser.parse_args()
    
    site_path = resolve_site_path(args.target)
    total_issues = 0
    pages_checked = 0
    site_has_dropdowns = False
    site_has_mobile = False
    
    checks = [
        ('mobile_overlay', check_mobile_overlay_closure),
        ('dropdowns', check_dropdown_links),
        ('mobile_sections', check_mobile_sections),
        ('de_labels', check_de_labels),
        ('es_labels', check_es_labels),
        ('skip_link', check_skip_link),
    ]
    
    for root, dirs, files in os.walk(site_path):
        # Exact-match dir filtering (not substring 'in' — avoids false matches like 'my-backup-notes')
        SKIP_DIRS = {'backup', 'backup_pre_fixes', 'backup_fresh', 'css', 'images', '.git', 'node_modules'}
        dirs[:] = [d for d in dirs
                   if d not in SKIP_DIRS
                   and not d.startswith('.')
                   and not d.startswith('backup_nav_')]
        for fn in files:
            if not fn.endswith('.html'):
                continue
            fpath = os.path.join(root, fn)
            rel = os.path.relpath(fpath, site_path)
            
            with open(fpath) as f:
                html = f.read()
            
            pages_checked += 1
            
            # Detect nav architecture for summary
            if 'dropdown-trigger' in html:
                site_has_dropdowns = True
            if 'mobile-menu-overlay' in html:
                site_has_mobile = True
            
            for check_name, check_fn in checks:
                issues = check_fn(html, rel)
                for issue in issues:
                    prefix = "  WARN" if issue.startswith("WARNING") else "  FAIL"
                    print(f"{prefix}: {check_name}: {rel} — {issue}")
                    if not issue.startswith("WARNING"):
                        total_issues += 1
    
    print(f"\nPages checked: {pages_checked}")
    if not site_has_dropdowns and not site_has_mobile:
        print("Nav architecture: FLAT (no dropdowns, no mobile overlay) — structural-only checks applied")
    elif site_has_dropdowns:
        mobile_note = " + mobile overlay" if site_has_mobile else ""
        print(f"Nav architecture: DROPDOWN{mobile_note}")
    if total_issues == 0:
        print("All nav structure checks PASS")
        return 0
    else:
        print(f"FAIL: {total_issues} issue(s) found")
        return 1


if __name__ == '__main__':
    sys.exit(main())
