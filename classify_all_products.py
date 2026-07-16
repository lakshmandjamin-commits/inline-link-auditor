#!/usr/bin/env python3
"""
Classify all product codes in the fleet image directories.
Uses Viator API when available, falls back to title-based keyword matching.
Merges results into product_topic_map.json.

Usage: python3 classify_all_products.py [--force]
"""
import json, os, sys, re, time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
MAP_PATH = os.path.join(DATA_DIR, "product_topic_map.json")

SITE_IMAGES = {
    "tenerife-outdoor-guide": "/Users/saraswati/sites/tenerife-outdoor-guide/images",
    "madeira-trail-guide": "/Users/saraswati/sites/madeira-trail-guide/sites/madeira-hiking/images",
    "porto-sommelier": "/Users/saraswati/sites/porto-wine-tours/images",
    "lapland-adventure-guide": "/Users/saraswati/sites/lapland-adventure-guide/images",
}

# Title keyword → topic mapping (used as fallback)
TITLE_TOPIC_RULES = [
    # Pattern (case-insensitive), Topic, Destination hint
    (r'whale|dolphin|cetaceo|\\bwal\\b|delfin', 'whale-watching', 'tenerife'),
    (r'snorkel|scuba|diving|buceo', 'snorkeling', None),
    (r'stargaz|observatory|astronom|stars|estrellas|nacht', 'stargazing', None),
    (r'teide|masca|anaga|levada|pico|rabacal|hiking|walk|trek|hike|wandern|wanderung|sendero', 'hiking', None),
    (r'wine|port tasting|cellar|bodega|vinho|porto|douro|cockburn', 'wine-cellars', 'porto'),
    (r'food|tapas|cooking|gourmet|gastro', 'food-wine', None),
    (r'boat|catamaran|cruise|sailing|yacht|barco|lancha', 'boat-tours', None),
    (r'4x4|jeep|offroad|quad|buggy', '4x4-tours', None),
    (r'paragliding|paraglide|canyoning|coasteering|kayak|rafting|climbing|abseil', 'adventure', None),
    (r'husky|dog sled|dog sledding|schlittenhund', 'husky', 'lapland'),
    (r'snowmobile|snow mobile|snowmobil|moto nieve|motosnow', 'snowmobile', 'lapland'),
    (r'reindeer|reino|rentier|poro', 'reindeer', 'lapland'),
    (r'santa claus|santa\'s|joulupukki|navidad|weihnachtsmann', 'santa', 'lapland'),
    (r'aurora|northern light|borealis|arctic light|revontulet', 'aurora', 'lapland'),
    (r'ice hotel|snow hotel|icehotel|eishotel', 'ice-hotel', 'lapland'),
    (r'ice fish|ice fishing|icefish|eisfischen', 'ice-fishing', 'lapland'),
    (r'ice float|ice floating|icefloat|eisschwimmen', 'ice-floating', 'lapland'),
    (r'transfer|airport|flughafen|pickup|shuttle', 'planning', None),
    (r'braga|guimaraes|porto day|day trip|day tour', 'day-trips', 'porto'),
    (r'catamaran.*whale|whale.*catamaran|dolphin.*boat|boat.*dolphin|whale.*cruise|dolphin.*cruise', 'whale-watching', None),
    (r'canoe|rafting|river', 'adventure', None),
    (r'bus|coach|train|railway', 'day-trips', None),
    (r'snowshoe|snow shoe|raqueta|winter walk', 'hiking', 'lapland'),
]

# Destination keywords in titles
DESTINATION_KEYWORDS = {
    'tenerife': ['tenerife', 'teide', 'los gigantes', 'costa adeje', 'los cristianos',
                 'adeje', 'masca', 'anaga', 'la gomera', 'garachico', 'canary',
                 'teneriffa', 'canarias'],
    'madeira': ['madeira', 'funchal', 'rabacal', 'rabacal', 'pico ruivo', 'pico do arieiro',
                'levada', 'machico', 'calheta', 'santana', 'sao vicente', 'porto moniz',
                'garajau', 'curral'],
    'porto': ['porto', 'douro', 'braga', 'guimaraes', 'cockburn', 'gaia', 'port wine',
              'vinho do porto', 'vila nova de gaia', 'ribeira', 'matosinhos'],
    'lapland': ['rovaniemi', 'lapland', 'arctic', 'santa claus', 'korvatunturi',
                'snowhotel', 'icehotel', 'ranua', 'saariselkä', 'levi', 'ylläs',
                'luosto', 'pyhä', 'inari', 'ivalo', 'kemijärvi', 'sodankylä'],
}


def load_api_key():
    """Load Viator API key from .env."""
    env_path = os.path.expanduser("~/.hermes/.env")
    if not os.path.exists(env_path):
        print("ERROR: ~/.hermes/.env not found")
        sys.exit(1)
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("VIATOR_API_KEY="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    print("ERROR: VIATOR_API_KEY not found in .env")
    sys.exit(1)



def classify_by_title(title):
    """Classify a product by its title text using keyword rules."""
    title_lower = title.lower() if title else ""
    for pattern, topic, dest_hint in TITLE_TOPIC_RULES:
        if re.search(pattern, title_lower, re.IGNORECASE):
            return topic, dest_hint
    return "other", None


def detect_destination(title, description=""):
    """Detect destination from product title/description."""
    text = f"{title} {description}".lower()
    scores = {}
    for dest, keywords in DESTINATION_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scores[dest] = score
    if scores:
        return max(scores, key=scores.get)
    return None


def fetch_from_api(code, api_key):
    """Fetch product data from Viator API."""
    url = f"https://api.viator.com/partner/products/{code}"
    try:
        req = Request(url)
        req.add_header("exp-api-key", api_key)
        req.add_header("Accept", "application/json;version=2.0")
        req.add_header("Accept-Language", "en")
        resp = urlopen(req, timeout=15)
        data = json.loads(resp.read().decode())
        return data
    except Exception:
        return None


def main():
    force = "--force" in sys.argv
    api_key = load_api_key()
    
    # Load existing map
    if os.path.exists(MAP_PATH):
        product_map = json.load(open(MAP_PATH))
    else:
        product_map = {}
    
    # Get all product codes from image dirs
    all_codes = set()
    for site, img_dir in SITE_IMAGES.items():
        if not os.path.isdir(img_dir):
            continue
        for f in os.listdir(img_dir):
            if not f.endswith('.jpg'): continue
            if f.startswith(('stock-', 'viator-', 'alejandro', 'sofia', 'tiago', 'hero-')):
                continue
            code = re.sub(r'(_\d+)?\.jpg$', '', f)
            all_codes.add(code)
    
    # Find unknown codes
    unknown = [c for c in all_codes if c not in product_map]
    print(f"Total codes: {len(all_codes)}, Known: {len(all_codes)-len(unknown)}, Unknown: {len(unknown)}")
    
    if not unknown:
        print("All codes already classified.")
        return
    
    classified = 0
    api_failures = 0
    
    for i, code in enumerate(sorted(unknown)):
        if i % 20 == 0:
            print(f"  Progress: {i}/{len(unknown)} ({classified} classified, {api_failures} API failures)")
        
        info = {'code': code, 'topic': 'other', 'title': '', 'category': '',
                'destination_name': '', 'source': 'unknown'}
        
        # Try API first (if we have a key)
        if api_key:
            data = fetch_from_api(code, api_key)
            if data:
                title = data.get('title', '')
                categories = [c.get('name', '') for c in data.get('categories', [])]
                primary_cat = data.get('primaryCategory', {}).get('name', '')
                if primary_cat:
                    categories.insert(0, primary_cat)
                
                topic, dest_hint = classify_by_title(title)
                
                # Override topic if category is strong signal
                cat_text = ' '.join(categories).lower()
                if 'whale' in cat_text or 'dolphin' in cat_text:
                    topic = 'whale-watching'
                elif 'wine' in cat_text or 'port tasting' in cat_text:
                    topic = 'wine-cellars'
                elif 'stargazing' in cat_text or 'astronomy' in cat_text:
                    topic = 'stargazing'
                
                dest = detect_destination(title, data.get('description', ''))
                
                info.update({
                    'topic': topic,
                    'title': title,
                    'category': ' > '.join(categories[:3]) if categories else '',
                    'destination_name': dest or dest_hint or '',
                    'source': 'viator_api',
                })
                
                classified += 1
                time.sleep(0.3)  # Rate limit
            else:
                api_failures += 1
                # Fall back to title-only classification
                topic, dest_hint = classify_by_title(code)
                info.update({'topic': topic, 'source': 'code_pattern',
                             'destination_name': dest_hint or ''})
        
        product_map[code] = info
    
    # Save
    with open(MAP_PATH, 'w') as f:
        json.dump(product_map, f, indent=2, sort_keys=True)
    
    print(f"\nDone. Classified {classified} from API, {api_failures} from code patterns.")
    print(f"Updated: {MAP_PATH}")


if __name__ == '__main__':
    main()
