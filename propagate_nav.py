#!/usr/bin/env python3
"""
Propagate nav HTML across all pages in a site.

Usage: python3 propagate_nav.py <site_path> [options]

Extracts the full <nav> block + mobile overlay from the source page and
replaces the old nav on every HTML page.

Options:
  --source FILE    Source page for new nav (default: index.html)
  --no-mobile      Site has no mobile overlay (flat nav)
  --force          Re-propagate pages that already have new nav
  --backup         Create timestamped backup of each file before modifying
  --css FILE       Inject nav CSS snippet into css/style.css
  --dry-run        Preview changes without writing

Handles:
- DE/ES page label translation automatically
- Duplicate-application guard (skips pages already updated)
- 4 closing divs for mobile overlay (fixes the nav-redesign-pattern.md regex bug)
- Sites without mobile overlay (--no-mobile)
"""

import os, re, sys, argparse, shutil
from datetime import datetime
from pathlib import Path

# --- Label maps for DE/ES translation ---
DE_LABELS = {
    '>Activities <': '>Aktivitäten <',
    '>Plan Trip <': '>Reise planen <',
    '>Plan Your Trip <': '>Reise planen <',
    '>About<': '>Über<',
    '>About <': '>Über <',
    '"About"': '"Über"',
}

ES_LABELS = {
    '>Activities <': '>Actividades <',
    '>Plan Trip <': '>Planificar viaje <',
    '>Plan Your Trip <': '>Planificar viaje <',
    '>About<': '>Sobre<',
    '>About <': '>Sobre <',
    '"About"': '"Sobre"',
}


def extract_nav_and_mobile(html, has_mobile=True):
    """Extract the <nav> block and optional mobile overlay from source HTML."""
    nav_match = re.search(r'(<nav[^>]*>.*?</nav>)', html, re.DOTALL)
    if not nav_match:
        raise ValueError("Source page has no <nav> block")
    
    nav_block = nav_match.group(0)
    mobile_block = None
    
    if has_mobile:
        mobile_start = html.find('<!-- Mobile menu overlay -->')
        if mobile_start == -1:
            raise ValueError("Source page has no mobile overlay comment — use --no-mobile")
        
        # Count 4 closing divs (NOT 2 — fixes the nav-redesign-pattern.md bug)
        rest = html[mobile_start:]
        div_count = 0
        end_pos = 0
        for m in re.finditer(r'</div>', rest):
            div_count += 1
            if div_count == 4:
                end_pos = m.end()
                break
        
        if div_count < 4:
            raise ValueError(f"Mobile overlay has only {div_count} closing divs — need 4")
        
        mobile_block = rest[:end_pos]
    
    return nav_block, mobile_block


def detect_language(html):
    """Detect page language from <html lang= attribute."""
    lang_match = re.search(r'<html[^>]*lang="([^"]*)"', html[:500])
    if lang_match:
        lang = lang_match.group(1)
        if lang.startswith('de'):
            return 'de'
        elif lang.startswith('es'):
            return 'es'
    return 'en'


def apply_language_labels(nav_html, mobile_html, lang):
    """Apply language-specific label translations."""
    labels = None
    if lang == 'de':
        labels = DE_LABELS
    elif lang == 'es':
        labels = ES_LABELS
    
    if labels and nav_html:
        for en, translated in labels.items():
            nav_html = nav_html.replace(en, translated)
        if mobile_html:
            for en, translated in labels.items():
                mobile_html = mobile_html.replace(en, translated)
    
    return nav_html, mobile_html


def has_new_nav(html):
    """Check if page already has the new nav (prevent double-application).

    Uses flexible class-name matching (not hardcoded 'nav-dropdown-trigger')
    so it works with any dropdown-trigger naming convention.
    """
    # Any dropdown-trigger class pattern (nav-dropdown-trigger, nav-trigger, etc.)
    if re.search(r'class="[^"]*dropdown-trigger[^"]*"', html):
        return True
    # Mobile overlay presence
    if 'mobile-menu-overlay' in html:
        return True
    return False


def remove_old_mobile_overlay(content):
    """Remove old mobile overlay using counted-div approach (4 closes)."""
    mobile_start = content.find('<!-- Mobile menu overlay -->')
    if mobile_start == -1:
        return content  # No old overlay to remove
    
    rest = content[mobile_start:]
    div_count = 0
    end_pos = 0
    for m in re.finditer(r'</div>', rest):
        div_count += 1
        if div_count == 4:
            end_pos = m.end()
            break
    
    if div_count >= 2:
        # Remove at least 2 closes worth (old buggy regex captured 2)
        # Try to remove 4 if available, else remove what we found
        remove_len = end_pos if div_count >= 4 else rest.find('</div>') + 10
        return content[:mobile_start] + content[mobile_start + remove_len:]
    
    return content


def replace_nav(content, new_nav, new_mobile=None):
    """Replace old nav and old mobile overlay with new ones."""
    # Replace old <nav> block
    old_nav_match = re.search(r'<nav[^>]*>.*?</nav>', content, re.DOTALL)
    if not old_nav_match:
        return None  # No nav to replace
    
    # Remove old mobile overlay first (if any)
    if new_mobile:
        content = remove_old_mobile_overlay(content)
    
    content = content[:old_nav_match.start()] + new_nav + content[old_nav_match.end():]
    
    # Insert new mobile overlay after </nav> (if provided)
    if new_mobile:
        nav_end = content.find('</nav>')
        if nav_end != -1:
            nav_end += len('</nav>')
            content = content[:nav_end] + '\n' + new_mobile + content[nav_end:]
    
    return content


def inject_css(site_path, css_path):
    """Inject nav CSS snippet into style.css with validation."""
    css_file = os.path.join(site_path, 'css', 'style.css')
    if not os.path.exists(css_file):
        print(f"ERROR: {css_file} not found")
        return False
    if not os.path.exists(css_path):
        print(f"ERROR: CSS snippet {css_path} not found")
        return False
    
    with open(css_file) as f:
        existing = f.read()
    with open(css_path) as f:
        snippet = f.read()
    
    # Validate CSS snippet: check brace balance
    if snippet.count('{') != snippet.count('}'):
        print(f"ERROR: CSS snippet has unbalanced braces ({{={snippet.count('{')}, }}={snippet.count('}')})")
        return False
    
    # Normalized duplicate detection: strip whitespace/comment prefix, compare first 200 non-blank chars
    def normalize(css):
        # Strip comments and normalize whitespace
        css = re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)
        return re.sub(r'\s+', ' ', css).strip()
    
    norm_existing = normalize(existing)
    norm_snippet = normalize(snippet)
    if norm_snippet[:200] in norm_existing:
        print("CSS already contains this snippet — skipping")
        return True
    
    # Append to end
    with open(css_file, 'w') as f:
        f.write(existing + '\n\n/* Nav upgrade — ' + datetime.now().strftime('%Y-%m-%d') + ' */\n' + snippet)
    
    print(f"Injected CSS into {css_file}")
    return True


def add_missing_js(content, has_mobile=True):
    """Add nav JS if not already present. Uses rfind for safe insertion.

    When has_mobile=False (--no-mobile flag), mobile menu + Escape key
    handlers are excluded since the DOM elements they reference won't exist.
    """
    # Check for any dropdown-trigger class pattern
    if not re.search(r'class="[^"]*dropdown-trigger[^"]*"', content):
        return content  # New nav not applied yet

    if '// ── Desktop dropdowns' in content:
        return content  # JS already present

    js_block = '''
<script>
(function(){
  // ── Desktop dropdowns ──────────────────────────────────────────
  document.querySelectorAll('.nav-dropdown-trigger').forEach(function(btn){
    btn.addEventListener('click', function(e){
      e.stopPropagation();
      var panel = this.nextElementSibling;
      var wasOpen = panel.classList.contains('open');
      document.querySelectorAll('.dropdown-panel.open').forEach(function(p){ p.classList.remove('open'); });
      document.querySelectorAll('.nav-dropdown-trigger').forEach(function(b){ b.setAttribute('aria-expanded','false'); });
      if (!wasOpen) {
        panel.classList.add('open');
        this.setAttribute('aria-expanded','true');
      }
    });
  });
  document.addEventListener('click', function(e){
    if (e.target.closest('.dropdown-panel')) return;
    document.querySelectorAll('.dropdown-panel.open').forEach(function(p){ p.classList.remove('open'); });
    document.querySelectorAll('.nav-dropdown-trigger').forEach(function(b){ b.setAttribute('aria-expanded','false'); });
  });'''

    mobile_js = '''
  // ── Mobile menu ───────────────────────────────────────────────
  var toggle = document.getElementById('nav-toggle');
  var menu = document.getElementById('mobile-menu');
  var body = document.body;
  var scrollY = 0;
  if (toggle && menu) {
    toggle.addEventListener('click', function(){
      var isOpen = menu.classList.toggle('open');
      toggle.setAttribute('aria-expanded', isOpen);
      toggle.classList.toggle('open', isOpen);
      if (isOpen) {
        scrollY = window.scrollY;
        body.style.overflow = 'hidden';
        body.style.position = 'fixed';
        body.style.top = '-' + scrollY + 'px';
        body.style.width = '100%';
      } else {
        body.style.overflow = '';
        body.style.position = '';
        body.style.top = '';
        body.style.width = '';
        window.scrollTo(0, scrollY);
      }
    });
  }
  // Mobile accordion
  document.querySelectorAll('.mobile-section-header').forEach(function(header){
    header.addEventListener('click', function(){
      var section = this.nextElementSibling;
      var isOpen = section.classList.toggle('open');
      this.setAttribute('aria-expanded', isOpen);
      this.querySelector('.chevron').classList.toggle('rotated', isOpen);
    });
  });
  // Escape key
  document.addEventListener('keydown', function(e){
    if (e.key === 'Escape') {
      document.querySelectorAll('.dropdown-panel.open').forEach(function(p){ p.classList.remove('open'); });
      document.querySelectorAll('.nav-dropdown-trigger').forEach(function(b){ b.setAttribute('aria-expanded','false'); });
      if (menu && menu.classList.contains('open')) {
        menu.classList.remove('open');
        toggle.classList.remove('open');
        toggle.setAttribute('aria-expanded','false');
        body.style.overflow = '';
        body.style.position = '';
        body.style.top = '';
        body.style.width = '';
        window.scrollTo(0, scrollY);
      }
    }
  });'''

    footer = '''
  // ── Reduced motion ─────────────────────────────────────────────
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    var s = document.createElement('style');
    s.textContent = '* { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }';
    document.head.appendChild(s);
  }
})();
</script>
'''

    if has_mobile:
        js_block += mobile_js
    js_block += footer

    # Safe insertion: use rfind to find LAST </body>
    body_close = content.rfind('</body>')
    if body_close != -1:
        content = content[:body_close] + js_block + '\n' + content[body_close:]

    return content


def main():
    parser = argparse.ArgumentParser(description='Propagate nav HTML across all site pages')
    parser.add_argument('site_path', help='Path to site directory')
    parser.add_argument('--source', default='index.html', help='Source page for new nav (default: index.html)')
    parser.add_argument('--no-mobile', action='store_true', help='Site has no mobile overlay')
    parser.add_argument('--force', action='store_true', help='Re-propagate pages that already have new nav')
    parser.add_argument('--backup', action='store_true', help='Create timestamped backup before modifying')
    parser.add_argument('--css', help='Path to CSS snippet to inject into style.css')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without writing')
    args = parser.parse_args()
    
    site_path = os.path.abspath(args.site_path)
    source_path = os.path.join(site_path, args.source)
    
    if not os.path.exists(source_path):
        print(f"ERROR: Source page not found: {source_path}")
        sys.exit(1)
    
    # Inject CSS if requested
    if args.css:
        inject_css(site_path, args.css)
    
    # Read source page and extract new nav
    with open(source_path) as f:
        source_html = f.read()
    
    try:
        new_nav, new_mobile = extract_nav_and_mobile(source_html, has_mobile=not args.no_mobile)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    
    mobile_label = f" + mobile overlay ({len(new_mobile)} chars)" if new_mobile else " (no mobile overlay)"
    print(f"Extracted nav ({len(new_nav)} chars){mobile_label}")
    
    # Setup backup dir if requested
    backup_dir = None
    if args.backup:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_dir = os.path.join(site_path, f'backup_nav_{ts}')
        os.makedirs(backup_dir, exist_ok=True)
    
    # Walk all HTML files
    updated = 0
    skipped_existing = 0
    skipped_no_nav = 0
    
    for root, dirs, files in os.walk(site_path):
        # Exact-match dir filtering (not substring 'in' — avoids false matches like 'my-backup-notes')
        SKIP_DIRS = {'backup', 'backup_pre_fixes', 'backup_fresh', 'css', 'images', '.git'}
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
                content = f.read()
            
            # Skip pages already updated (unless --force)
            if not args.force and has_new_nav(content):
                skipped_existing += 1
                continue
            
            # Detect language
            lang = detect_language(content)
            
            # Apply labels
            page_nav = new_nav
            page_mobile = new_mobile
            if lang != 'en':
                page_nav, page_mobile = apply_language_labels(page_nav, page_mobile, lang)
            
            # Replace old nav
            new_content = replace_nav(content, page_nav, page_mobile)
            if new_content is None:
                print(f"WARNING: {rel} — no <nav> block found, skipping")
                skipped_no_nav += 1
                continue
            
            # Add JS (with mobile handlers only if site has mobile overlay)
            new_content = add_missing_js(new_content, has_mobile=not args.no_mobile)
            
            if args.dry_run:
                print(f"WOULD UPDATE: {rel} [{lang}]")
            else:
                if args.backup:
                    backup_path = os.path.join(backup_dir, rel)
                    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
                    shutil.copy2(fpath, backup_path)
                
                with open(fpath, 'w') as f:
                    f.write(new_content)
                print(f"UPDATED: {rel} [{lang}]")
            
            updated += 1
    
    print(f"\nDone. Updated: {updated}, Skipped (already new): {skipped_existing}, Skipped (no nav): {skipped_no_nav}")
    if args.backup:
        print(f"Backups: {backup_dir}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
