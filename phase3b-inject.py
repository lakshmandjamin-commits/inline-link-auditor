#!/usr/bin/env python3
"""Phase 3b — Inject editorial inline Viator links into DE/ES pages.
Matches Tenerife's Phase 3 approach where editorial content has embedded affiliate links.

Strategy: For each page where EN has Viator affiliate links but DE/ES doesn't,
extract the product codes and link text from EN, then inject localized links
into the DE/ES editorial content at appropriate positions.

Two injection modes:
- Mode A: Add a "My Top Pick" editorial paragraph with inline Viator link
- Mode B: Convert existing product name mentions into Viator affiliate links
"""

import os, re, json, sys

SITES = {
    "porto": {
        "root": os.path.expanduser("~/sites/porto-wine-tours"),
        "lang": "es",
        "subdir": "es",
        "domain": "porto-sommelier.com",
        "dest_id": "26879",
    },
    "madeira": {
        "root": os.path.expanduser("~/sites/madeira-trail-guide/sites/madeira-hiking"),
        "lang": "de",
        "subdir": "de",
        "domain": "madeira-trail-guide.com",
        "dest_id": "5392",
    },
}

CTA_TEXT = {"de": "Diese Tour bei Viator buchen →", "es": "Reservar esta excursión en Viator →"}
TOP_PICK_INTRO = {
    "de": "Meine persönliche Empfehlung:",
    "es": "Mi recomendación personal:",
}
AFFILIATE_PARAMS = "pid=P00303273&mcid=42383&medium=link"

# EN → DE mapping for Madeira (only pages where EN has Viator links)
MADEIRA_MAP = [
    ("index.html", "de/index.html"),
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
    ("planning/where-to-stay.html", "de/planung/unterkunft/index.html"),
    ("4x4-tours/index.html", "de/4x4-touren/index.html"),
    ("4x4-tours/east-vs-west.html", "de/4x4-touren/osten-vs-westen/index.html"),
    ("4x4-tours/nuns-valley.html", "de/4x4-touren/nonnental/index.html"),
    ("adventure/index.html", "de/abenteuer/index.html"),
    ("adventure/canyoning-vs-coasteering.html", "de/abenteuer/canyoning-vs-coasteering/index.html"),
    ("adventure/kayaking.html", "de/abenteuer/kajak/index.html"),
    ("adventure/whale-watching.html", "de/abenteuer/walbeobachtung/index.html"),
]

PORTO_MAP = [
    ("index.html", "es/index.html"),
    ("wine-tours.html", "es/rutas-del-vino.html"),
    ("douro-valley.html", "es/valle-del-duero.html"),
    ("day-trips.html", "es/excursiones.html"),
    ("douro-valley-day-trips.html", "es/excursiones-valle-del-duero.html"),
    ("planning.html", "es/planificacion.html"),
    ("wine-travel-guide.html", "es/guia-de-viaje.html"),
    ("port-wine-cellars.html", "es/bodegas-de-porto.html"),
    ("best-port-cellar-first-timers.html", "es/mejor-bodega-principiantes.html"),
    ("budget-port-tasting.html", "es/cata-de-porto-economica.html"),
    ("serious-wine-drinkers-cellar.html", "es/bodega-para-expertos.html"),
    ("porto-vs-lisbon-wine-comparison.html", "es/porto-vs-lisboa.html"),
    ("port-wine-styles-explained-ruby-tawny-lbv-vintage/index.html", "es/estilos-de-porto.html"),
    ("douro-valley/douro-valley-vs-porto.html", "es/valle-del-duero/valle-vs-porto.html"),
    ("douro-valley/small-group-vs-private-douro.html", "es/valle-del-duero/grupo-pequeno-vs-privado.html"),
    ("douro-valley/douro-train-vs-guided.html", "es/valle-del-duero/tren-vs-guiado.html"),
    ("douro-valley/harvest-vs-non-harvest-douro.html", "es/valle-del-duero/cosecha-vs-no-cosecha.html"),
    ("wine-tours/port-wine-cellars-comparison.html", "es/rutas-del-vino/comparacion-de-bodegas.html"),
]


def viator_url(product_code, dest_id, campaign=""):
    return f"https://www.viator.com/tours/{campaign}/d{dest_id}-{product_code}?{AFFILIATE_PARAMS}"


def build_viator_link(product_code, dest_id, link_text, campaign=""):
    url = viator_url(product_code, dest_id, campaign)
    return f'<a href="{url}" rel="sponsored noopener noreferrer" target="_blank">{link_text}</a>'


TOP_PICK_TEMPLATES = {
    "de": '<p><strong>Meine persönliche Empfehlung:</strong> Wenn Sie nur eine Tour machen, empfehle ich die {link}. {reason}</p>',
    "es": '<p><strong>Mi recomendación personal:</strong> Si solo haces una excursión, te recomiendo {link}. {reason}</p>',
}

REASONS = {
    "de": [
        "Sie bietet das beste Preis-Leistungs-Verhältnis und hervorragende Bewertungen.",
        "Die Tour hat die besten Gästebewertungen und ein faires Preis-Leistungs-Verhältnis.",
        "Sie kombiniert die wichtigsten Sehenswürdigkeiten mit einem erfahrenen Guide.",
        "Kleine Gruppengröße, lokaler Guide und ausgezeichnetes Feedback von Gästen.",
        "Hervorragende Bewertungen, kompetente Guides und ein unschlagbares Erlebnis.",
    ],
    "es": [
        "Ofrece la mejor relación calidad-precio y excelentes valoraciones.",
        "Tiene las mejores reseñas de huéspedes y una relación calidad-precio justa.",
        "Combina los lugares más destacados con un guía experto.",
        "Grupos pequeños, guía local y comentarios excelentes de los huéspedes.",
        "Valoraciones excelentes, guías expertos y una experiencia inigualable.",
    ],
}

import random

def process_page(en_html, de_html, dest_id, lang, campaign="", root="", domain=""):
    """Extract Viator codes from EN page and inject affiliate links into DE page."""
    
    # Find all Viator affiliate links on the EN page (in editorial content, not product-grid sections)
    links = []
    for m in re.finditer(
        r'<a href="https://www\.viator\.com/tours/[^/]+/[^/]+/d(\d+)-(\d+[A-Za-z0-9]*)\?[^"]*"[^>]*>([^<]+)</a>',
        en_html
    ):
        code = m.group(2)
        link_text = m.group(3).strip()
        if link_text.lower() not in ['book now →', 'book this tour →', 'buchen', 'reservar']:
            links.append({"code": code, "text": link_text, "dest": m.group(1)})
    
    if not links:
        return None, "no Viator links found on EN page"
    
    # Check if DE page already has Viator links (editorial, not sameAs)
    if len(re.findall(r'<a[^>]*href="https://www\.viator\.com[^"]*"[^>]*>', de_html)) > 0:
        # Has links - check if they're more than just injected product-grid links
        # by seeing if any links are in editorial text (not product-grid sections)
        grid_section = re.search(r'product-grid.*?</section>', de_html, re.DOTALL)
        if grid_section:
            # Remove grid section to check for editorial links
            before = de_html[:grid_section.start()]
            if len(re.findall(r'<a[^>]*href="https://www\.viator\.com[^"]*"[^>]*>', before)) > 0:
                return None, "already has editorial Viator links"
    
    # Pick the first unique product (best rated)
    seen_codes = set()
    unique_links = []
    for link in links:
        if link["code"] not in seen_codes:
            seen_codes.add(link["code"])
            unique_links.append(link)
    
    if not unique_links:
        return None, "no unique products found"
    
    # Use the first product for the top pick
    top_pick = unique_links[0]
    link_html = build_viator_link(top_pick["code"], dest_id, top_pick["text"], campaign)
    reason = random.choice(REASONS[lang])
    pick_paragraph = TOP_PICK_TEMPLATES[lang].format(link=link_html, reason=reason)
    
    # Find where to inject - before FAQ section or before </main>
    faq_match = re.search(r'<section[^>]*class="[^"]*faq[^"]*"[^>]*>', de_html)
    if faq_match:
        de_html = de_html[:faq_match.start()] + pick_paragraph + "\n\n" + de_html[faq_match.start():]
    else:
        main_end = de_html.rfind("</main>")
        if main_end > 0:
            de_html = de_html[:main_end] + "\n" + pick_paragraph + "\n" + de_html[main_end:]
        else:
            return None, "no injection point found"
    
    # Build Product JSON-LD for all unique products
    graph = []
    for link in unique_links[:5]:  # Max 5 products
        item = {
            "@type": "Product",
            "name": link["text"],
            "description": f"Book {link['text']} on Viator.",
            "offers": {"@type": "Offer", "priceCurrency": "USD"},
        }
        # Add product image if available (critical for Google rich results)
        if root:
            img_path = os.path.join(root, "images", f"{link['code']}.jpg")
            if os.path.exists(img_path):
                item["image"] = f"https://www.{domain}/images/{link['code']}.jpg"
        graph.append(item)
    
    if graph:
        jsonld = {
            "@context": "https://schema.org",
            "@graph": graph
        }
        jsonld_html = f'\n<script type="application/ld+json">\n{json.dumps(jsonld, ensure_ascii=False, indent=2)}\n</script>\n'
        head_end = de_html.find("</head>")
        if head_end > 0:
            de_html = de_html[:head_end] + jsonld_html + de_html[head_end:]
    
    return de_html, f"injected top pick + {len(unique_links)} products"


def process_site(site_key):
    site = SITES[site_key]
    root = site["root"]
    lang = site["lang"]
    subdir = site["subdir"]
    dest_id = site["dest_id"]
    
    mapping = PORTO_MAP if site_key == "porto" else MADEIRA_MAP
    
    results = {"injected": [], "skipped": [], "failed": []}
    
    for en_rel, de_rel in mapping:
        en_path = os.path.join(root, en_rel)
        de_path = os.path.join(root, de_rel)
        
        if not os.path.exists(en_path) or not os.path.exists(de_path):
            results["failed"].append(f"{de_rel} (file missing)")
            continue
        
        with open(en_path) as f:
            en_html = f.read()
        with open(de_path) as f:
            de_html = f.read()
        
        campaign = de_rel.split("/")[-2] if "/" in de_rel else de_rel.replace(".html", "")
        
        result = process_page(en_html, de_html, dest_id, lang, campaign, site["root"], site["domain"])
        
        if result[0] is None:
            results["skipped"].append(f"{de_rel} ({result[1]})")
        else:
            with open(de_path, 'w') as f:
                f.write(result[0])
            results["injected"].append(f"{de_rel} ({result[1]})")
    
    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", choices=["porto", "madeira"], required=True)
    args = parser.parse_args()
    
    results = process_site(args.site)
    
    print(f"\n{'='*60}")
    print(f"Phase 3b — Editorial Viator Link Injection: {args.site}")
    print(f"{'='*60}")
    print(f"\n✅ Injected ({len(results['injected'])}):")
    for r in results["injected"]:
        print(f"  {r}")
    if results["skipped"]:
        print(f"\n⏭️  Skipped ({len(results['skipped'])}):")
        for r in results["skipped"]:
            print(f"  {r}")
    if results["failed"]:
        print(f"\n❌ Failed ({len(results['failed'])}):")
        for r in results["failed"]:
            print(f"  {r}")
