#!/usr/bin/env python3
"""Pre-extract context for Claude page review. Outputs JSON with all data Claude needs."""

import os, sys, re, json, sqlite3

DB = os.path.expanduser('~/.hermes/affiliate-crons/db/viator_cli.db')

def extract_page_context(html_file_path):
    """Given an HTML file, return all context Claude needs for review."""
    
    with open(html_file_path) as f:
        html = f.read()
    
    # Determine site from path
    site_dir = None
    site_id = None
    for sid, sd in [
        ('porto', 'porto-wine-tours'),
        ('tenerife', 'tenerife-outdoor-guide'),
        ('lapland', 'lapland-adventure-guide'),
        ('sanjuan', 'san-juan-excursions'),
        ('yogyakarta', 'yogyakarta-temple-tours'),
        ('madeira', 'madeira-trail-guide'),
    ]:
        base = os.path.expanduser(f'~/sites/{sd}')
        if html_file_path.startswith(base):
            site_dir = base
            site_id = sid
            break
    
    if not site_dir:
        return {'error': f'Cannot determine site for {html_file_path}'}
    
    # 1. Extract Viator product codes and query DB
    viator_codes = {}
    for url in re.findall(r'href="https?://(?:www\.)?viator\.com[^"]+', html):
        cm = re.search(r'/d\d+-(\w+)', url)
        if cm:
            code = cm.group(1)
            if code not in viator_codes:
                viator_codes[code] = url[:120]
    
    conn = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    
    product_data = {}
    for code in viator_codes:
        row = conn.execute(
            'SELECT product_code, title, rating, review_count, destination_name '
            'FROM products WHERE product_code=?', (code,)
        ).fetchone()
        if row:
            product_data[code] = {
                'title': row['title'],
                'rating': round(row['rating'], 1) if row['rating'] else None,
                'review_count': row['review_count'],
                'destination': row['destination_name'],
            }
        else:
            product_data[code] = {'title': 'NOT IN DB', 'rating': None, 'review_count': None, 'destination': None}
    
    conn.close()
    
    # 2. Extract internal links and verify
    internal_links = []
    broken_links = []
    for m in re.finditer(r'href="(/[^"]*)"', html):
        href = m.group(1)
        # Skip non-page links
        if any(href.startswith(x) for x in ['/images/', '/css/', '/js/', '/fonts/', '#']):
            continue
        if '.html' not in href and '.' in href.split('/')[-1]:
            continue
        
        internal_links.append(href)
        # Try to resolve
        target = site_dir + href
        if href.endswith('/'):
            target = target + 'index.html'
        elif '.' not in href.split('/')[-1]:
            target = target + '/index.html'
        
        if not os.path.exists(target):
            broken_links.append({'href': href, 'expected_path': target.replace(site_dir, '')})
    
    # 3. Site page inventory
    page_inventory = []
    for root, dirs, files in os.walk(site_dir):
        dirs[:] = [d for d in dirs if 'backup' not in d and not d.startswith('.') and d not in ('css','images')]
        for fn in files:
            if not fn.endswith('.html'): continue
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, site_dir)
            try:
                with open(fp) as f:
                    ph = f.read(4096)
                h1_m = re.search(r'<h1[^>]*>(.*?)</h1>', ph, re.DOTALL)
                title_m = re.search(r'<title>([^<]+)', ph)
                h1 = re.sub(r'<[^>]+>', '', h1_m.group(1)).strip()[:80] if h1_m else ''
                title = title_m.group(1).strip()[:80] if title_m else ''
                page_inventory.append({'path': rel, 'h1': h1, 'title': title})
            except:
                pass
    
    # 4. Page basics
    h1_m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.DOTALL)
    title_m = re.search(r'<title>([^<]+)', html)
    page_h1 = re.sub(r'<[^>]+>', '', h1_m.group(1)).strip() if h1_m else 'MISSING'
    page_title = title_m.group(1).strip() if title_m else 'MISSING'
    
    # 5. Placement checks
    main_m = re.search(r'<main[^>]*>(.*?)</main>', html, re.DOTALL)
    first_200_viator = False
    if main_m:
        main_text = re.sub(r'<[^>]+>', ' ', main_m.group(1))
        first_200 = ' '.join(main_text.split()[:200])
        first_200_viator = 'viator.com' in first_200
    
    product_cards = len(re.findall(r'class="product-card"', html))
    verdict_linked = bool(re.search(r'(Verdict|Pick|Winner).*viator\.com', html, re.DOTALL | re.IGNORECASE))
    
    # 6. Regression checks
    anti_words_found = []
    anti_list = ['spectacular','iconic','breathtaking','unforgettable','hidden gem',
                 'world-class','magical','pristine','enchanting','paradise','curated',
                 'seamless','life-changing','unparalleled']
    for w in anti_list:
        if w.lower() in html.lower():
            anti_words_found.append(w)
    
    placeholders_found = []
    for p in ['XXXXX','YYYYY','XXXX','YYYY','12345','67890']:
        if p in html:
            placeholders_found.append(p)
    
    missing_p = []
    for url in re.findall(r'href="https?://(?:www\.)?viator\.com[^"]+', html):
        cm = re.search(r'/d\d+-(\d{4,})(?=[/\"\'?&])', url)
        if cm:
            missing_p.append(cm.group(1))
    
    # Also check data-viator-id
    for m in re.finditer(r'data-viator-id="d?\d+-(\d{4,})"', html):
        missing_p.append(m.group(1))
    
    # 7. Domain
    domain = None
    for sd, dom in [('porto-wine-tours','porto-sommelier.com'),
                    ('tenerife-outdoor-guide','tenerife-outdoor-guide.com'),
                    ('lapland-adventure-guide','lapland-adventure-guide.com'),
                    ('san-juan-excursions','san-juan-excursions.com'),
                    ('yogyakarta-temple-tours','yogyakarta-temple-tours.com'),
                    ('madeira-trail-guide','madeira-trail-guide.com')]:
        if sd in site_dir:
            domain = dom
            break
    
    return {
        'site': site_id,
        'domain': domain,
        'file': html_file_path,
        'slug': os.path.relpath(html_file_path, site_dir),
        'title': page_title,
        'h1': page_h1,
        'product_data': product_data,
        'internal_links_total': len(internal_links),
        'broken_internal_links': broken_links,
        'site_pages': page_inventory,
        'placement': {
            'first_200_viator': first_200_viator,
            'product_cards': product_cards,
            'verdict_linked': verdict_linked,
        },
        'regression': {
            'anti_words': anti_words_found,
            'placeholders': placeholders_found,
            'missing_p_codes': missing_p,
        }
    }

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: pre_extract.py <html_file_path>")
        sys.exit(1)
    
    result = extract_page_context(sys.argv[1])
    print(json.dumps(result, indent=2, default=str))
