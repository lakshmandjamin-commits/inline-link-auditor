import os, re

SITE = os.path.expanduser('~/sites/lapland-adventure-guide')
DOMAIN = 'lapland-adventure-guide.com'

# EN → DE slug mapping (both with and without trailing slash)
SLUG_MAP = {
    '/': '/de/',
    '/about': '/de/ueber-uns',
    '/contact': '/de/kontakt',
    '/privacy': '/de/datenschutz',
    '/northern-lights': '/de/nordlichter',
    '/aurora-beginners': '/de/nordlichter-fuer-anfaenger',
    '/aurora-photography-rovaniemi-guide': '/de/nordlichter-fotografie-rovaniemi',
    '/northern-lights-tours-rovaniemi-comparison': '/de/nordlichter-touren-rovaniemi-vergleich',
    '/husky-safari': '/de/husky-safari',
    '/husky-safari-5km-vs-7-5km-vs-10km-comparison': '/de/husky-safari-5km-vs-7-5km-vs-10km-vergleich',
    '/husky-vs-snowmobile-lapland-comparison': '/de/husky-vs-snowmobil-lappland-vergleich',
    '/snowmobile': '/de/schneemobil',
    '/reindeer-farm': '/de/rentierfarm',
    '/santa-claus': '/de/weihnachtsmann',
    '/ice-hotel': '/de/eishotel',
    '/ice-fishing': '/de/eisfischen',
    '/ice-floating': '/de/eisschwimmen',
    '/ice-floating-raw': '/de/eisschwimmen-pur',
    '/best-time': '/de/beste-reisezeit',
    '/best-time/march-value-trips': '/de/beste-reisezeit/maerz-spar-tipps',
    '/packing-list': '/de/packliste',
    '/lapland-cost': '/de/lappland-kosten',
    '/lapland-with-kids': '/de/lappland-mit-kindern',
    '/getting-there': '/de/anreise',
    '/winter-holiday-planner': '/de/winterurlaub-planer',
    '/animal-welfare-checklist': '/de/tierschutz-checkliste',
    '/index': '/de/',  # index.html maps to DE homepage
}

def get_en_slug(rel_path):
    """Convert any relative file path to URL slug"""
    # Handle index.html in subdirectory
    if rel_path.endswith('/index.html'):
        slug = '/' + rel_path[:-len('index.html')].rstrip('/')
        return slug if slug != '/' else '/'
    # Handle flat .html files in root
    if '/' not in rel_path and rel_path.endswith('.html'):
        slug = '/' + rel_path[:-5]  # strip .html
        if slug == '/index':
            return '/'
        return slug
    # Handle nested .html files (e.g. best-time/march-value-trips.html)
    if rel_path.endswith('.html'):
        slug = '/' + rel_path[:-5]
        return slug
    return rel_path

def find_lang_toggle_position(html):
    """Find where to insert lang-toggle in the nav"""
    # Find all closing </div> tags and their positions
    # We need the one that closes nav-inner (the outermost div inside nav)
    nav_match = re.search(r'<nav[^>]*>(.*?)</nav>', html, re.DOTALL)
    if not nav_match:
        return None, None
    
    nav_content = nav_match.group(0)
    nav_start = nav_match.start()
    
    # Count div depth inside nav
    # We want to insert BEFORE the </div> that closes nav-inner
    # nav-inner is the first div inside nav
    # The structure is: <nav> <div class="nav-inner"> ... <div class="nav-links"> ... </div> [INSERT HERE] </div> </nav>
    
    # Find the position of each </div> in nav
    depth = 0
    div_close_positions = []
    pos = 0
    while True:
        open_match = re.search(r'<div[ >]', nav_content[pos:])
        close_match = re.search(r'</div>', nav_content[pos:])
        
        if not open_match and not close_match:
            break
        
        next_open = open_match.start() + pos if open_match else float('inf')
        next_close = close_match.start() + pos if close_match else float('inf')
        
        if next_open < next_close:
            depth += 1
            pos = next_open + 4
        else:
            if depth == 1:
                # This is the nav-inner closing div — insert before it
                actual_pos = nav_start + next_close
                return actual_pos, actual_pos + 6  # start of </div>, end+len('</div>')
            depth -= 1
            div_close_positions.append(nav_start + next_close)
            pos = next_close + 6
    
    return None, None

def insert_lang_toggle(html, de_slug, en_slug):
    """Insert lang-toggle inside nav-inner, before its closing </div>"""
    toggle_html = f'''
  <div class="lang-toggle">
    <a href="https://www.{DOMAIN}{en_slug}" class="active" hreflang="en">EN</a>
    <a href="https://www.{DOMAIN}{de_slug}" hreflang="de">DE</a>
  </div>'''
    
    start, end = find_lang_toggle_position(html)
    if start is None:
        # Fallback: insert before </nav>
        nav_end = html.rfind('</nav>')
        if nav_end > 0:
            return html[:nav_end] + toggle_html + '\n' + html[nav_end:]
        return html
    
    return html[:start] + toggle_html + '\n' + html[start:]

# Collect all HTML files
pages = []
for root, dirs, files in os.walk(SITE):
    dirs[:] = [d for d in dirs if 'backup' not in d and d != '.git' and d != 'css' and d != 'images' and not d.startswith('.')]
    for fn in files:
        if fn.endswith('.html'):
            pages.append(os.path.relpath(os.path.join(root, fn), SITE))

modified_href = 0
modified_toggle = 0
for p in pages:
    fp = os.path.join(SITE, p)
    html = open(fp).read()
    
    en_slug = get_en_slug(p)
    de_slug = SLUG_MAP.get(en_slug)
    if not de_slug:
        print(f"  WARNING: No DE slug for {p} (EN slug: {en_slug})")
        continue
    
    already_has_hreflang = 'hreflang="de"' in html
    already_has_toggle = 'lang-toggle' in html
    
    if already_has_hreflang and already_has_toggle:
        print(f"  SKIP: {p}")
        continue
    
    print(f"  FIX: {p} (EN: {en_slug} → DE: {de_slug})")
    
    # Add hreflang if missing
    if not already_has_hreflang:
        hreflang_block = f'''  <link rel="alternate" hreflang="de" href="https://www.{DOMAIN}{de_slug}">
  <link rel="alternate" hreflang="x-default" href="https://www.{DOMAIN}{en_slug}">'''
        html = re.sub(
            r'(<link rel="canonical"[^>]*>)',
            r'\1\n' + hreflang_block,
            html
        )
        modified_href += 1
    
    # Add lang toggle if missing
    if not already_has_toggle:
        html = insert_lang_toggle(html, de_slug, en_slug)
        modified_toggle += 1
    
    with open(fp, 'w') as f:
        f.write(html)

print(f"\nAdded hreflang: {modified_href} pages")
print(f"Added lang-toggle: {modified_toggle} pages")

# Final verification
href_count = 0
toggle_count = 0
for p in pages:
    html = open(os.path.join(SITE, p)).read()
    if 'hreflang="de"' in html:
        href_count += 1
    if 'lang-toggle' in html:
        toggle_count += 1
print(f"Final: hreflang={href_count}/{len(pages)}, lang-toggle={toggle_count}/{len(pages)}")
