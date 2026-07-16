#!/usr/bin/env python3
"""Extract compact review context for Claude — no file reads needed."""

import os, sys, re, json, sqlite3

DB = os.path.expanduser('~/.hermes/affiliate-crons/db/viator_cli.db')

def review_context(html_file):
    with open(html_file) as f:
        html = f.read()
    
    # Site detection
    site_id = None
    for sid, sd in [('porto','porto-wine-tours'),('tenerife','tenerife-outdoor-guide'),
                    ('lapland','lapland-adventure-guide'),('sanjuan','san-juan-excursions'),
                    ('yogyakarta','yogyakarta-temple-tours'),('madeira','madeira-trail-guide')]:
        if f'/sites/{sd}' in html_file:
            site_id = sid
            site_dir = os.path.expanduser(f'~/sites/{sd}')
            break
    
    # 1. H1 + title
    h1_m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.DOTALL)
    title_m = re.search(r'<title>([^<]+)', html)
    h1 = re.sub(r'<[^>]+>', '', h1_m.group(1)).strip() if h1_m else 'MISSING'
    title = title_m.group(1).strip() if title_m else 'MISSING'
    
    # 2. Viator products — query DB
    viator_codes = {}
    for url in re.findall(r'href="https?://(?:www\.)?viator\.com[^"]+', html):
        cm = re.search(r'/d\d+-(\w+)', url)
        if cm:
            code = cm.group(1)
            if code not in viator_codes:
                viator_codes[code] = True
    
    conn = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    products = []
    for code in viator_codes:
        row = conn.execute('SELECT title,rating,review_count,destination_name FROM products WHERE product_code=?',(code,)).fetchone()
        if row:
            products.append(f'{code}: {row["title"][:70]} ({row["rating"]:.1f}★ {row["review_count"]}rev) @ {row["destination_name"] or "unknown"}')
        else:
            products.append(f'{code}: NOT IN DB')
    conn.close()
    
    # 3. First 200 words of main
    main_m = re.search(r'<main[^>]*>(.*?)</main>', html, re.DOTALL)
    if main_m:
        main_text = re.sub(r'<[^>]+>', ' ', main_m.group(1))
        words = main_text.split()
        first_200 = ' '.join(words[:200])
        has_cta_early = 'viator.com' in first_200
        # Sample of first ~80 words for topic context
        opening = ' '.join(words[:80])
    else:
        first_200 = ''
        has_cta_early = False
        opening = ''
    
    # 4. Product cards count
    pc_count = len(re.findall(r'class="product-card"', html))
    
    # 5. Verdict box linked?
    verdict_linked = bool(re.search(
        r'(?:Verdict|My Top Pick|Winner).{0,200}viator\.com',
        html, re.DOTALL | re.IGNORECASE
    ))
    
    # 6. Anti-words
    anti_list = ['spectacular','iconic','breathtaking','unforgettable','hidden gem',
                 'world-class','magical','pristine','enchanting','paradise','curated',
                 'seamless','life-changing','unparalleled']
    anti_found = [w for w in anti_list if w.lower() in html.lower()]
    
    # 7. Placeholders + missing-P
    ph = []
    for p in ['XXXXX','YYYYY','XXXX','YYYY','12345','67890']:
        if p in html: ph.append(p)
    mp = [m.group(1) for m in re.finditer(r'/d\d+-(\d{4,})(?=[/\"\'?&])', html)]
    
    # 8. Internal links — just count, Claude doesn't need to check
    internal_count = len(re.findall(r'href="(/[^"]*)"', html))
    
    return {
        'site': site_id,
        'slug': os.path.relpath(html_file, site_dir) if site_dir else html_file,
        'title': title[:100],
        'h1': h1[:100],
        'opening': opening[:400],
        'products': products,
        'product_cards': pc_count,
        'first_200_cta': has_cta_early,
        'verdict_linked': verdict_linked,
        'anti_words': anti_found,
        'placeholders': ph,
        'missing_p': mp,
        'internal_links': internal_count,
    }

if __name__ == '__main__':
    result = review_context(sys.argv[1])
    # Print compact prompt for Claude
    print(f'Page: [{result["site"]}] {result["slug"]}')
    print(f'H1: {result["h1"]}')
    print(f'Opening: {result["opening"]}')
    print(f'Products:')
    for p in result['products']:
        print(f'  - {p}')
    print(f'Placement: CTA in 1st 200 words={result["first_200_cta"]}, cards={result["product_cards"]}, verdict_linked={result["verdict_linked"]}')
    print(f'Anti-words found: {result["anti_words"] or "NONE"}')
    print(f'Placeholders: {result["placeholders"] or "NONE"}')
    print(f'Missing-P: {result["missing_p"] or "NONE"}')
