#!/usr/bin/env python3
"""
Phase 3 — Product Card & CTA Injection for DE/ES Sites

For every DE/ES page, this script:
1. Reads the EN equivalent to find the product-grid sections
2. Extracts product card data (titles, prices, ratings, Viator URLs, images)
3. Injects product cards into the DE/ES page (before FAQ or before </main>)
4. Adds Product @graph JSON-LD

Usage:
  python3 phase3-inject.py --site porto --lang es
  python3 phase3-inject.py --site madeira --lang de
"""

import os, re, json, sys

SITES = {
    "porto": {
        "root": os.path.expanduser("~/sites/porto-wine-tours"),
        "domain": "porto-sommelier.com",
    },
    "madeira": {
        "root": os.path.expanduser("~/sites/madeira-trail-guide/sites/madeira-hiking"),
        "domain": "madeira-trail-guide.com",
    },
}

MADEIRA_MAP = [
    ("index.html", "de/index.html"),
    ("25-fontes-vs-alecrim-vs-risco-comparison/index.html", "de/25-fontes-vs-alecrim-vs-risco-vergleich/index.html"),
    ("pr1-vs-pr1-2-pico-ruivo-comparison.html", "de/pr1-vs-pr1-2-pico-ruivo-vergleich/index.html"),
    ("hiking/index.html", "de/wanderungen/index.html"),
    ("hiking/first-time-hiker.html", "de/wanderungen/erstes-mal/index.html"),
    ("hiking/hiking-for-families.html", "de/wanderungen/familien/index.html"),
    ("hiking/photographer-hikes.html", "de/wanderungen/fotografen/index.html"),
    ("hiking/pico-do-arieiro-sunrise.html", "de/wanderungen/pico-do-arieiro-sonnenaufgang/index.html"),
    ("levada-walks/index.html", "de/levada-wanderungen/index.html"),
    ("levada-walks/25-fontes-vs-risco.html", "de/levada-wanderungen/25-fontes-vs-risco/index.html"),
    ("levada-walks/beginners.html", "de/levada-wanderungen/anfaenger/index.html"),
    ("levada-walks/comparison.html", "de/levada-wanderungen/vergleich/index.html"),
    ("levada-walks/guided-vs-self-guided.html", "de/levada-wanderungen/gefuehrt-vs-selbst/index.html"),
    ("levada-walks/pr1-vs-pr1-2.html", "de/levada-wanderungen/pr1-vs-pr1-2/index.html"),
    ("planning/index.html", "de/planung/index.html"),
    ("planning/best-time.html", "de/planung/beste-reisezeit/index.html"),
    ("planning/packing-list.html", "de/planung/packliste/index.html"),
    ("planning/transfers.html", "de/planung/transfers/index.html"),
    ("planning/where-to-stay.html", "de/planung/unterkunft/index.html"),
    ("4x4-tours/index.html", "de/4x4-touren/index.html"),
    ("4x4-tours/east-vs-west.html", "de/4x4-touren/osten-vs-westen/index.html"),
    ("4x4-tours/nuns-valley.html", "de/4x4-touren/nonnental/index.html"),
    ("4x4-tours/private-vs-group.html", "de/4x4-touren/privat-vs-gruppe/index.html"),
    ("adventure/index.html", "de/abenteuer/index.html"),
    ("adventure/canyoning-vs-coasteering.html", "de/abenteuer/canyoning-vs-coasteering/index.html"),
    ("adventure/kayaking.html", "de/abenteuer/kajak/index.html"),
    ("adventure/whale-watching.html", "de/abenteuer/walbeobachtung/index.html"),
    ("about.html", None),  # utility
    ("contact.html", None),
    ("privacy.html", None),
]

PORTO_MAP = [
    ("index.html", "es/index.html"),
    ("wine-tours.html", "es/rutas-del-vino.html"),
    ("douro-valley.html", "es/valle-del-duero.html"),
    ("boat-cruises.html", "es/paseos-en-barco.html"),
    ("douro-river-cruises.html", "es/cruceros-por-el-duero.html"),
    ("day-trips.html", "es/excursiones.html"),
    ("douro-valley-day-trips.html", "es/excursiones-valle-del-duero.html"),
    ("planning.html", "es/planificacion.html"),
    ("wine-travel-guide.html", "es/guia-de-viaje.html"),
    ("food-tours.html", "es/tours-gastronomicos.html"),
    ("porto-food-tours.html", "es/tours-gastronomicos-porto.html"),
    ("port-wine-cellars.html", "es/bodegas-de-porto.html"),
    ("best-port-cellar-first-timers.html", "es/mejor-bodega-principiantes.html"),
    ("budget-port-tasting.html", "es/cata-de-porto-economica.html"),
    ("serious-wine-drinkers-cellar.html", "es/bodega-para-expertos.html"),
    ("braga-day-trip.html", "es/excursion-braga.html"),
    ("porto-vs-lisbon-wine-comparison.html", "es/porto-vs-lisboa.html"),
    ("port-wine-styles-explained-ruby-tawny-lbv-vintage/index.html", "es/estilos-de-porto.html"),
    ("douro-valley/douro-valley-vs-porto.html", "es/valle-del-duero/valle-vs-porto.html"),
    ("douro-valley/small-group-vs-private-douro.html", "es/valle-del-duero/grupo-pequeno-vs-privado.html"),
    ("douro-valley/douro-train-vs-guided.html", "es/valle-del-duero/tren-vs-guiado.html"),
    ("douro-valley/harvest-vs-non-harvest-douro.html", "es/valle-del-duero/cosecha-vs-no-cosecha.html"),
    ("wine-tours/port-wine-cellars-comparison.html", "es/rutas-del-vino/comparacion-de-bodegas.html"),
    ("about.html", None),
    ("contact.html", None),
    ("privacy.html", None),
]

CTA_TEXT = {"de": "Jetzt auf Viator buchen →", "es": "Reservar en Viator →"}

SECTION_HEADERS = {
    "de": {
        "h2": "Touren im Vergleich",
        "p": "Wir haben die top-bewerteten Touren recherchiert und verglichen. Alle Preise pro Person, sofern nicht anders angegeben."
    },
    "es": {
        "h2": "Tours en Comparación",
        "p": "Hemos investigado y comparado los tours mejor valorados. Todos los precios son por persona, a menos que se indique lo contrario."
    }
}


def extract_card_data(en_html):
    """Extract product card data from EN page by finding all product-grid sections."""
    # Find all product-grid sections using div-nesting-aware extraction
    all_cards = []
    idx = 0
    
    while True:
        grid_start = en_html.find('<div class="product-grid">', idx)
        if grid_start == -1:
            break
        
        # Track nesting to find the matching closing </div>
        depth = 1
        pos = grid_start + len('<div class="product-grid">')
        
        while depth > 0 and pos < len(en_html):
            next_open = en_html.find('<div', pos)
            next_close = en_html.find('</div>', pos)
            
            if next_close == -1:
                break
            
            if next_open != -1 and next_open < next_close:
                # Opening tag
                depth += 1
                pos = next_open + 4  # skip '<div'
            else:
                # Closing tag
                depth -= 1
                pos = next_close + 6  # skip '</div>'
        
        grid_end = pos
        grid_html = en_html[grid_start:grid_end]
        
        # Now extract card data from within this grid
        # Handle both formats:
        # Format A (Porto): <div class="product-card"> with <div class="product-card-body">, <div class="price">, structured rating
        # Format B (Madeira): <div class="product-card" itemscope> with <h3 class="product-card-title"> and <div class="product-card-specs">
        
        # Try Format A first
        for cm in re.finditer(
            r'<div class="product-card">\s*<img src="([^"]+)" alt="([^"]*)"[^>]*>\s*<div class="product-card-body">\s*<h3>(.*?)</h3>\s*<div class="rating"[^>]*>(.*?)<span itemprop="aggregateRating"[^>]*><meta itemprop="ratingValue" content="([^"]+)"><meta itemprop="reviewCount" content="([^"]+)"></span></div>\s*<div class="price">(.*?)</div>\s*<p>(.*?)</p>\s*<a href="(https://www\.viator\.com[^"]+)"[^>]*>([^<]+)</a>\s*</div>\s*</div>',
            grid_html, re.DOTALL
        ):
            all_cards.append({
                "img_src": cm.group(1),
                "img_alt": cm.group(2),
                "title": cm.group(3),
                "rating_display": cm.group(4).strip(),
                "rating_value": cm.group(5),
                "rating_count": cm.group(6),
                "price_text": cm.group(7),
                "description": cm.group(8).strip(),
                "viator_url": cm.group(9),
                "cta_text": cm.group(10),
            })
        
        # Try Format B (Madeira homepage style — title in <a> tag)
        for cm in re.finditer(
            r'<div class="product-card"[^>]*>\s*<img[^>]+src="([^"]+)"[^>]+alt="([^"]*)"[^>]*>\s*<div class="product-card-body">\s*<h3 class="product-card-title"><a href="([^"]+)"[^>]*>(.*?)</a></h3>\s*(?:<p>.*?</p>\s*)?<div class="product-card-specs"[^>]*>.*?<span[^>]*itemprop="ratingValue"[^>]*>\D*([\d.]+)</span>\s*<span[^>]*itemprop="reviewCount"[^>]*>\s*\(?(\d+(?:,\d+)?)\)?\s*</span>\s*<span>\s*[~$£€]*\s*([\d.]+)</span>',
            grid_html, re.DOTALL
        ):
            title = cm.group(4).strip()
            title_clean = re.sub(r'^[🏆🥇⭐★✨]\s*', '', title)
            viator_url = cm.group(3)
            if not viator_url.startswith('https://www.viator.com'):
                continue
            all_cards.append({
                "img_src": cm.group(1),
                "img_alt": cm.group(2),
                "title": title_clean,
                "rating_display": f"★ {cm.group(5)} ({cm.group(6)} reviews)",
                "rating_value": cm.group(5),
                "rating_count": cm.group(6).replace(",", ""),
                "price_text": f"~${cm.group(7)}",
                "description": f"Rated {cm.group(5)} out of 5 with {cm.group(6)} reviews.",
                "viator_url": viator_url,
                "cta_text": "Book This Tour →",
            })
        
        # Try Format C (Madeira sub-page style — title in <h3>, specs spans, cta-btn in card-actions)
        # Use separate extraction for img src/alt since attribute order varies
        for card_block in re.finditer(
            r'(<div class="product-card"[^>]*>.*?<a class="cta-btn" href="(https://www\.viator\.com[^"]+)"[^>]*>([^<]+)</a>.*?</div>\s*</div>)',
            grid_html, re.DOTALL
        ):
            card_text = card_block.group(1)
            viator_url = card_block.group(2)
            cta_text = card_block.group(3)
            
            # Extract img src/alt position-independently
            img_src_m = re.search(r'<img[^>]*src="([^"]+)"', card_text)
            img_alt_m = re.search(r'<img[^>]*alt="([^"]*)"', card_text)
            if not img_src_m:
                continue
            
            # Extract title
            title_m = re.search(r'<h3 class="product-card-title">(.*?)</h3>', card_text)
            if not title_m:
                continue
            
            # Extract rating
            rating_m = re.search(r'itemprop="ratingValue"[^>]*>\D*([\d.]+)</span>', card_text)
            # Extract review count
            review_m = re.search(r'<span>\s*\(?(\d[\d,]*(?:\.\d+)?)\)?\s*[Rr]eviews?\s*</span>', card_text)
            if not review_m:
                # Try just numbers
                review_m = re.search(r'<span>\s*([\d,]+)\s*</span>', card_text)
            
            # Extract price
            price_m = re.search(r'<div class="product-card-price">(.*?)</div>', card_text)
            
            rating_value = rating_m.group(1) if rating_m else "0"
            review_count = review_m.group(1).replace(",", "") if review_m else "0"
            price_text = price_m.group(1).strip() if price_m else "~$0"
            
            all_cards.append({
                "img_src": img_src_m.group(1),
                "img_alt": img_alt_m.group(1) if img_alt_m else "",
                "title": title_m.group(1).strip(),
                "rating_display": f"★ {rating_value} ({review_count} reviews)",
                "rating_value": rating_value,
                "rating_count": review_count,
                "price_text": price_text,
                "description": f"Rated {rating_value} out of 5 with {review_count} reviews.",
                "viator_url": viator_url,
                "cta_text": cta_text,
            })
        
        idx = grid_end
    
    return all_cards


def make_product_card_html(card, lang, img_base="/images/"):
    """Generate a product card HTML block for DE/ES."""
    # The image src is already relative in EN pages (e.g. /images/douro-six-bridges-cruise.jpg)
    # Use it as-is
    cta = CTA_TEXT[lang]
    
    return f'''      <div class="product-card">
        <img src="{card['img_src']}" alt="{card['img_alt']}" loading="lazy">
        <div class="product-card-body">
          <h3>{card['title']}</h3>
          <div class="rating" itemscope itemtype="http://schema.org/Product">{card['rating_display']}<span itemprop="aggregateRating" itemscope itemtype="http://schema.org/AggregateRating"><meta itemprop="ratingValue" content="{card['rating_value']}"><meta itemprop="reviewCount" content="{card['rating_count']}"></span></div>
          <div class="price">{card['price_text']}</div>
          <p>{card['description']}</p>
          <a href="{card['viator_url']}" rel="sponsored noopener noreferrer" class="cta" target="_blank">{cta}</a>
        </div>
      </div>'''


def make_product_grid_section(cards, lang):
    """Generate the full product-grid section with header."""
    h = SECTION_HEADERS[lang]
    cards_html = "\n\n".join(make_product_card_html(c, lang) for c in cards)
    
    return f'''<section class="section" style="padding-top: 0;">
  <div class="content">
    <h2 id="comparison-grid">{h['h2']}</h2>
    <p>{h['p']}</p>

    <div class="product-grid">

{cards_html}

    </div>
  </div>
</section>'''


def make_product_jsonld(cards, domain):
    """Generate Product @graph JSON-LD."""
    graph = []
    for c in cards:
        item = {"@type": "Product", "name": c["title"], "description": c["description"]}
        # Add product image (critical for Google rich results)
        if c.get("img_src"):
            img_url = c["img_src"]
            if img_url.startswith("/"):
                img_url = f"https://www.{domain}{img_url}"
            item["image"] = img_url
        price_match = re.search(r'\$(\d+(?:\.\d+)?)', c["price_text"])
        if price_match:
            item["offers"] = {"@type": "Offer", "price": price_match.group(1), "priceCurrency": "USD"}
        item["aggregateRating"] = {
            "@type": "AggregateRating",
            "ratingValue": c["rating_value"],
            "reviewCount": c["rating_count"],
            "bestRating": "5"
        }
        graph.append(item)
    
    return json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False, indent=2)


def inject_into_page(de_html, section_html, jsonld_html):
    """Inject product grid section and JSON-LD into DE/ES page."""
    
    # 1. Add JSON-LD in <head>
    head_end = de_html.find("</head>")
    if head_end > 0:
        de_html = de_html[:head_end] + f'\n{jsonld_html}\n' + de_html[head_end:]
    
    # 2. Inject product grid before FAQ section, or before </main>
    faq_match = re.search(r'<section[^>]*class="[^"]*faq[^"]*"[^>]*>', de_html)
    if faq_match:
        de_html = de_html[:faq_match.start()] + "\n" + section_html + "\n\n" + de_html[faq_match.start():]
    else:
        main_end = de_html.rfind("</main>")
        if main_end > 0:
            de_html = de_html[:main_end] + "\n" + section_html + "\n" + de_html[main_end:]
        else:
            return None  # No injection point
    
    return de_html


def process_site(site_name, lang):
    site = SITES[site_name]
    root = site["root"]
    mapping = PORTO_MAP if site_name == "porto" else MADEIRA_MAP
    
    results = {"injected": [], "skipped_no_grid": [], "skipped_already": [], "skipped_utility": [], "failed": []}
    
    for en_rel, de_rel in mapping:
        if de_rel is None:
            results["skipped_utility"].append(en_rel)
            continue
        
        en_path = os.path.join(root, en_rel)
        de_path = os.path.join(root, de_rel)
        
        if not os.path.exists(en_path) or not os.path.exists(de_path):
            results["failed"].append(f"{en_rel} → {de_rel} (missing file)")
            continue
        
        with open(en_path) as f:
            en_html = f.read()
        with open(de_path) as f:
            de_html = f.read()
        
        # Skip if target already has product cards
        if re.search(r'class="product-card"', de_html):
            results["skipped_already"].append(de_rel)
            continue
        
        # Extract card data from EN
        cards = extract_card_data(en_html)
        if not cards:
            results["skipped_no_grid"].append(en_rel)
            continue
        
        # Build sections
        section_html = make_product_grid_section(cards, lang)
        jsonld_html = f'<script type="application/ld+json">\n{make_product_jsonld(cards, site["domain"])}\n</script>'
        
        # Inject
        result = inject_into_page(de_html, section_html, jsonld_html)
        if result is None:
            results["failed"].append(f"{de_rel} (no injection point)")
            continue
        
        with open(de_path, 'w') as f:
            f.write(result)
        
        results["injected"].append(f"{de_rel} ({len(cards)} cards)")
    
    return results


def print_results(site_name, lang, results):
    total = sum(len(v) for v in results.values())
    print(f"\n{'='*60}")
    print(f"Phase 3 Injection Results: {site_name} ({lang})")
    print(f"{'='*60}")
    print(f"\n✅ INJECTED ({len(results['injected'])}):")
    for r in results["injected"]:
        print(f"  {r}")
    
    if results["skipped_already"]:
        print(f"\n⏭️  SKIPPED (already has cards) ({len(results['skipped_already'])}):")
        for r in results["skipped_already"]:
            print(f"  {r}")
    
    if results["skipped_no_grid"]:
        print(f"\n⏭️  SKIPPED (no product grid on EN) ({len(results['skipped_no_grid'])}):")
        for r in results["skipped_no_grid"]:
            print(f"  {r}")
    
    if results["skipped_utility"]:
        print(f"\n⏭️  SKIPPED (utility pages) ({len(results['skipped_utility'])}):")
        for r in results["skipped_utility"]:
            print(f"  {r}")
    
    if results["failed"]:
        print(f"\n❌ FAILED ({len(results['failed'])}):")
        for r in results["failed"]:
            print(f"  {r}")
    
    print(f"\nTotal: {total} pages")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", choices=["porto", "madeira"], required=True)
    parser.add_argument("--lang", choices=["de", "es"], required=True)
    args = parser.parse_args()
    
    results = process_site(args.site, args.lang)
    print_results(args.site, args.lang, results)
