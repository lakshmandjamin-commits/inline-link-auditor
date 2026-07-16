#!/usr/bin/env python3
"""
HTML Structure Integrity Scanner
Detects: hero-in-main, corrupted inline links, empty <strong> tags, stray punctuation.
Usage: python3 html_structure_check.py [all|tenerife|porto|madeira|lapland]
"""
import os, re, sys, sqlite3
from pathlib import Path

DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")

def get_sites(target="all"):
    """Get active site paths from registry."""
    reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
    if target == "all":
        sites = reg.execute("SELECT site_id, local_path FROM sites WHERE status='active'").fetchall()
    else:
        sites = reg.execute("SELECT site_id, local_path FROM sites WHERE site_id=? AND status='active'", (target,)).fetchall()
    reg.close()
    return sites

# --- DETECTION PATTERNS ---

def check_hero_in_main(content, rel_path):
    """Check if hero section is inside <main> instead of <header>."""
    main_open = content.find('<main')
    main_close = content.find('</main>')
    hero_pos = content.find('class="hero"')
    if hero_pos != -1 and main_open != -1:
        if hero_pos > main_open and hero_pos < main_close:
            # Count hero paragraphs
            hero_chunk = content[hero_pos:hero_pos+2000]
            p_count = len(re.findall(r'<p[>\s]', hero_chunk))
            return f"hero inside <main> ({p_count} paragraphs)"
    return None

def check_corrupted_links(content, rel_path):
    """Check for inline links that split words: letter(</a>)letter without space."""
    # Original pattern flagged parenthetical links as corrupted (false positive).
    # True corruption: <a> tags that break a word mid-character sequence,
    # e.g. "tri</a>ps", "gu</a>ided", "<a>leva</a>da" — letters adjacent
    # to </a> or <a> with no space/punctuation separating them from the same word.
    pattern = re.compile(r'(\w)</a>(\w)|(\w)<a\b[^>]*>(\w)')
    matches = pattern.findall(content)
    if matches:
        return f"{len(matches)} corrupted inline link(s) — word split by <a> tag"
    return None

def check_empty_strong(content, rel_path):
    """Check for <strong></strong> with no content."""
    count = content.count('<strong></strong>')
    if count > 0:
        return f"{count} empty <strong> tag(s)"
    return None

def check_stray_punctuation(content, rel_path):
    """Check for stray/repeated punctuation."""
    patterns = [
        (r',,,', 'triple comma'),
        (r'\.\.\.\.', 'quadruple period'),
    ]
    findings = []
    for pat, desc in patterns:
        count = len(re.findall(pat, content))
        if count:
            findings.append(f"{desc} x{count}")
    return "; ".join(findings) if findings else None

def check_lang_toggle_position(content, rel_path):
    """Check that .lang-toggle is inside .nav-inner, not orphaned outside the flex container."""
    toggle_pos = content.find('lang-toggle')
    if toggle_pos < 0:
        return None  # No toggle = nothing to check
    
    # Find nav-inner close by counting div depth
    nav_match = re.search(r'<nav.*?</nav>', content, re.S)
    if not nav_match:
        return None
    
    nav_html = nav_match.group()
    first_div = nav_html.find('<div')
    if first_div < 0:
        return None
    
    depth = 0
    nav_inner_close = -1
    i = first_div
    while i < len(nav_html):
        if nav_html[i:i+4] == '<div':
            depth += 1
        elif nav_html[i:i+6] == '</div>':
            depth -= 1
            if depth == 0:
                nav_inner_close = i
                break
        i += 1
    
    # toggle_pos is relative to content, need offset from nav start
    nav_start = nav_match.start()
    toggle_in_nav = toggle_pos - nav_start
    
    if nav_inner_close > 0 and toggle_in_nav > nav_inner_close:
        return "lang-toggle OUTSIDE nav-inner — invisible on page"
    return None

def check_lang_toggle_order(content, rel_path):
    """Check that EN comes first in lang-toggle (before ES/DE)."""
    toggle_match = re.search(r'class="lang-toggle">(.*?)</div>', content, re.S)
    if not toggle_match:
        return None
    
    inner = toggle_match.group(1)
    lang_order = re.findall(r'hreflang="([^"]+)"', inner)
    
    if len(lang_order) < 2:
        return None
    
    # EN must be first
    if lang_order[0] != 'en':
        return f"lang-toggle order: {' → '.join(lang_order)} (EN should be first)"
    return None

def check_header_balance(content, rel_path):
    """Check for unclosed <header> tags — common template bug where
    <header class='hero'> wraps nav+article+main without a closing </header>."""
    opens = len(re.findall(r'<header[ >]', content))
    closes = len(re.findall(r'</header>', content))
    if opens != closes:
        return "header tag imbalance: %d open, %d close" % (opens, closes)
    return None


def check_category_card_images(content, rel_path):
    """Check for <img> or .inline-image inside <a class="category-card"> elements.
    Category cards must use CSS background-image only — any HTML <img> inside
    an anchor wrapper breaks the card layout by superimposing an image over the
    background."""
    pattern = re.compile(r'<a[^>]*class="[^"]*category-card[^"]*"[^>]*>.*?</a>', re.DOTALL)
    for m in pattern.finditer(content):
        inner = m.group()
        imgs = re.findall(r'<img\b', inner)
        inline_divs = re.findall(r'<div class="inline-image"', inner)
        nested_links = re.findall(r'<a\s', inner[1:])  # skip the outer <a> itself
        issues = []
        if imgs:
            issues.append(f"{len(imgs)} <img> tag(s)")
        if inline_divs:
            issues.append(f"{len(inline_divs)} .inline-image div(s)")
        if nested_links:
            issues.append(f"{len(nested_links)} nested <a> tag(s)")
        if issues:
            # Find which card by extracting the heading
            h3 = re.search(r'<h3>(.*?)</h3>', inner)
            card_name = h3.group(1) if h3 else "unnamed card"
            return f"{card_name}: {'; '.join(issues)}"
    return None


def check_mobile_overlay_closure(content, rel_path):
    """Check mobile overlay has proper closing div count."""
    if "mobile-menu-overlay" not in content:
        return None
    issues = []
    overlay_count = content.count("mobile-menu-overlay")
    if overlay_count > 1:
        issues.append(f"DUPLICATE mobile overlay: {overlay_count} instances")
    m = re.search(r'class="mobile-menu-overlay".*?<header', content, re.DOTALL)
    if m:
        closes = m.group(0).count("</div>")
        if closes < 4:
            issues.append(f"UNDERCLOSED mobile overlay: {closes}/4 closing divs")
    return "; ".join(issues) if issues else None
def scan_site(site_path):
    """Run all checks on all HTML files in a site."""
    findings = []
    for root, dirs, files in os.walk(site_path):
        # Exact-match dir filtering (not substring 'in' — avoids false matches like 'my-backup-notes')
        SKIP_DIRS = {'backup', 'backup_pre_fixes', 'backup_fresh', 'css', 'images', '.git', 'node_modules'}
        dirs[:] = [d for d in dirs
                   if d not in SKIP_DIRS
                   and not d.startswith('.')
                   and not d.startswith('backup_nav_')]
        for fname in files:
            if not fname.endswith('.html'):
                continue
            fp = os.path.join(root, fname)
            rel = fp.replace(site_path, '')
            try:
                with open(fp, 'r') as f:
                    content = f.read()
            except:
                continue
            
            for check_name, check_fn in CHECKS:
                result = check_fn(content, rel)
                if result:
                    findings.append((rel, check_name, result))
    
    return findings

CHECKS = [
    ("hero-in-main", check_hero_in_main),
    ("corrupted-links", check_corrupted_links),
    ("empty-strong", check_empty_strong),
    ("stray-punctuation", check_stray_punctuation),
    ("lang-toggle-position", check_lang_toggle_position),
    ("lang-toggle-order", check_lang_toggle_order),
    ("header-balance", check_header_balance),
    ("category-card-images", check_category_card_images),
    ("nav-overlay", check_mobile_overlay_closure),
]


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    sites = get_sites(target)
    
    total_findings = 0
    for site_id, site_path in sites:
        if not os.path.isdir(site_path):
            print(f"  {site_id}: path not found ({site_path})")
            continue
        
        findings = scan_site(site_path)
        if findings:
            print(f"\n❌ {site_id}: {len(findings)} finding(s)")
            for rel, check, detail in findings:
                print(f"   [{check}] {rel}")
                print(f"          {detail}")
                total_findings += 1
        else:
            print(f"  ✅ {site_id}: clean")
    
    print(f"\n{'='*50}")
    print(f"  Total: {total_findings} finding(s)")
    sys.exit(1 if total_findings > 0 else 0)

if __name__ == "__main__":
    main()
