#!/usr/bin/env python3
"""
Build a product→topic mapping from the Viator API for all fleet product codes.
Saves to ~/.hermes/affiliate-crons/data/product_topic_map.json

Usage: python3 build_product_topic_map.py
"""

import os, sys, json, time, re
from urllib.request import Request, urlopen

# Dynamically discover all product codes from content banks + site products
def discover_product_codes():
    """Scan all content banks and site product DB for unique product codes."""
    codes = set()
    content_banks_dir = os.path.expanduser("~/.hermes/affiliate-crons/content-banks")
    
    # Scan content banks
    if os.path.isdir(content_banks_dir):
        for fname in os.listdir(content_banks_dir):
            if not fname.endswith('.yaml') or fname.startswith('_'):
                continue
            fpath = os.path.join(content_banks_dir, fname)
            try:
                import yaml
                with open(fpath) as f:
                    cb = yaml.safe_load(f)
                for p in cb.get('products', []):
                    code = p.get('code') or p.get('viator_id', '')
                    if code and re.match(r'^[A-Za-z0-9]{4,}$', code):
                        codes.add(code)
            except Exception:
                pass
    
    # Scan product DB
    db_path = os.path.expanduser("~/.hermes/affiliate-crons/db/viator_cli.db")
    if os.path.exists(db_path):
        try:
            import sqlite3
            db = sqlite3.connect(db_path)
            for row in db.execute("SELECT product_code FROM products"):
                if row[0] and re.match(r'^[A-Za-z0-9]{4,}$', row[0]):
                    codes.add(row[0])
            db.close()
        except Exception:
            pass
    
    return sorted(codes)

PRODUCT_CODES = discover_product_codes()

OUTPUT_PATH = os.path.expanduser("~/.hermes/affiliate-crons/data/product_topic_map.json")

# Viator category keywords → our topic names
CATEGORY_TO_TOPIC = {
    # Water activities
    'whale watching': 'whale-watching',
    'dolphin watching': 'whale-watching',
    'wildlife watching': 'whale-watching',
    'snorkeling': 'snorkeling',
    'scuba diving': 'snorkeling',
    'boat tours': 'boat-tours',
    'boat hire': 'boat-tours',
    'sunset cruise': 'boat-tours',
    'jet skiing': 'adventure',
    'parasailing': 'adventure',
    'kayaking': 'adventure',
    'rafting': 'adventure',
    'canyoning': 'adventure',
    'coasteering': 'adventure',
    # Hiking
    'hiking': 'hiking',
    'hiking tours': 'hiking',
    'walking tours': 'hiking',
    'nature walks': 'hiking',
    'nature & wildlife': 'hiking',
    'national parks': 'hiking',
    'mountain': 'hiking',
    'teide': 'hiking',
    'masca': 'hiking',
    'volcano': 'hiking',
    'volcanic': 'hiking',
    'national park': 'hiking',
    'night hike': 'hiking',
    # Wine
    'wine tasting': 'wine-cellars',
    'wine tours': 'wine-cellars',
    'port wine': 'wine-cellars',
    'cellar': 'wine-cellars',
    'bodega': 'wine-cellars',
    'food & wine': 'food-wine',
    'cooking classes': 'food-wine',
    'food tours': 'food-wine',
    'gastronomy': 'food-wine',
    'tapas': 'food-wine',
    'market tour': 'food-wine',
    # Stargazing
    'astronomy': 'stargazing',
    'stargazing': 'stargazing',
    'observatory': 'stargazing',
    # Day trips / city
    'day trips': 'day-trips',
    'day cruises': 'day-trips',
    'city tours': 'day-trips',
    'historical tours': 'day-trips',
    'cultural tours': 'day-trips',
    'private tours': 'day-trips',
    'full-day tours': 'day-trips',
    'half-day tours': 'day-trips',
    # Lapland
    'husky': 'husky',
    'dog sledding': 'husky',
    'snowmobile': 'snowmobile',
    'reindeer': 'reindeer',
    'santa claus': 'santa',
    'ice hotel': 'ice-hotel',
    'ice fishing': 'ice-fishing',
    'northern lights': 'aurora',
    'aurora': 'aurora',
    'winter sports': 'winter',
    'snow': 'winter',
    # 4x4
    '4x4': '4x4-tours',
    'jeep': '4x4-tours',
    'off-road': '4x4-tours',
    'safari': '4x4-tours',
    # Paragliding / air
    'paragliding': 'adventure',
    'hang gliding': 'adventure',
    'helicopter': 'day-trips',
    'flightseeing': 'day-trips',
    # Generic fallback
    'sightseeing': 'other',
    'attraction': 'other',
    'private transfer': 'planning',
    'transfers': 'planning',
    'port transfers': 'planning',
}


def get_api_key():
    env_path = os.path.expanduser("~/.hermes/.env")
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("VIATOR_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"").strip("'")
    return None


def classify_product(api_data):
    """Return the best-fit topic for this product based on its Viator categories."""
    title = (api_data.get("title") or "").lower()
    # Check primaryCategory first
    primary = api_data.get("primaryCategory", {})
    primary_name = (primary.get("name") or "").lower()
    
    # Check all categories
    categories = api_data.get("categories", [])
    all_cat_names = [primary_name]
    for cat in categories:
        all_cat_names.append((cat.get("name") or "").lower())
    
    # Also check destination
    dest_name = (api_data.get("destination", {}).get("name") or "").lower()
    all_cat_names.append(dest_name)
    
    # Score each category against our topics
    best_topic = "other"
    best_score = 0
    
    for cat_name in all_cat_names:
        for keyword, topic in CATEGORY_TO_TOPIC.items():
            if keyword in cat_name:
                score = len(keyword)  # longer match = more specific
                if cat_name == primary_name:
                    score += 10  # primary category bonus
                if score > best_score:
                    best_score = score
                    best_topic = topic
    
    # Title-based refinement for common patterns
    title_keywords = {
        'whale': 'whale-watching',
        'dolphin': 'whale-watching',
        'hike': 'hiking',
        'hiking': 'hiking',
        'trek': 'hiking',
        'levada': 'hiking',
        'trail': 'hiking',
        'wine': 'wine-cellars',
        'port wine': 'wine-cellars',
        'cellar': 'wine-cellars',
        'tasting': 'food-wine',
        'cooking': 'food-wine',
        'food': 'food-wine',
        'star': 'stargazing',
        'astronomy': 'stargazing',
        'husky': 'husky',
        'snowmobile': 'snowmobile',
        'reindeer': 'reindeer',
        'aurora': 'aurora',
        'northern light': 'aurora',
        'santa': 'santa',
        'ice hotel': 'ice-hotel',
        'ice fish': 'ice-fishing',
        '4x4': '4x4-tours',
        'jeep': '4x4-tours',
        'snorkel': 'snorkeling',
        'scuba': 'snorkeling',
        'kayak': 'adventure',
        'canyon': 'adventure',
        'paraglid': 'adventure',
        'coasteer': 'adventure',
        'catamaran': 'boat-tours',
        'cruise': 'boat-tours',
        'sail': 'boat-tours',
        'speedboat': 'boat-tours',
        'rib': 'boat-tours',
        'braga': 'day-trips',
        'guimaraes': 'day-trips',
        'douro': 'day-trips',
        'sunset': 'boat-tours',
        'teide': 'hiking',
        'masca': 'hiking',
        'volcano': 'hiking',
        'volcanic': 'hiking',
        'night hike': 'hiking',
        'jeep safari': '4x4-tours',
        'tapas': 'food-wine',
        'market': 'day-trips',
        'chef': 'food-wine',
        'eco': 'boat-tours',
        'safari': 'day-trips',
    }
    
    title_best_topic = "other"
    title_best_score = 0
    for keyword, topic in title_keywords.items():
        if keyword in title:
            score = len(keyword)
            if score > title_best_score:
                title_best_score = score
                title_best_topic = topic
    
    # If title gives a strong signal, prefer it
    if title_best_score >= 5:  # longer keyword = more specific
        return title_best_topic
    
    return best_topic


def main():
    api_key = get_api_key()
    if not api_key:
        print("ERROR: VIATOR_API_KEY not found in ~/.hermes/.env")
        sys.exit(1)
    
    BASE_URL = "https://api.viator.com/partner"
    
    # Load existing map if any
    topic_map = {}
    if os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH) as f:
            topic_map = json.load(f)
    
    stats = {"new": 0, "cached": 0, "failed": 0, "unknown": 0}
    
    for i, code in enumerate(PRODUCT_CODES):
        progress = f"[{i+1}/{len(PRODUCT_CODES)}]"
        
        # Skip if already mapped
        if code in topic_map and topic_map[code].get("topic") != "unknown":
            stats["cached"] += 1
            continue
        
        # API call
        url = f"{BASE_URL}/products/{code}"
        req = Request(url)
        req.add_header("exp-api-key", api_key)
        req.add_header("Accept", "application/json; version=2.0")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept-Language", "en-US")
        
        try:
            resp = urlopen(req, timeout=15)
            data = json.loads(resp.read().decode())
        except Exception as e:
            topic_map[code] = {"topic": "unknown", "error": str(e)[:50],
                               "title": "", "category": ""}
            stats["failed"] += 1
            if i % 10 == 0:
                print(f"{progress} {code}: API error - {str(e)[:40]}")
            time.sleep(0.3)
            continue
        
        title = data.get("title", "")
        primary_cat = data.get("primaryCategory", {}).get("name", "")
        destination = data.get("destination", {}).get("name", "")
        status = data.get("status", "")
        
        topic = classify_product(data)
        
        topic_map[code] = {
            "topic": topic,
            "title": title,
            "category": primary_cat,
            "destination": destination,
            "status": status,
        }
        stats["new"] += 1
        if i % 5 == 0:
            print(f"{progress} {code}: {topic} ({title[:50]})")
        
        time.sleep(0.3)
    
    # Save
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(topic_map, f, indent=2, ensure_ascii=False)
    
    print(f"\nDone. New: {stats['new']}, Cached: {stats['cached']}, Failed: {stats['failed']}")
    print(f"Saved to {OUTPUT_PATH}")
    
    # Summary by topic
    topics = {}
    for code, info in topic_map.items():
        t = info.get("topic", "unknown")
        topics[t] = topics.get(t, 0) + 1
    print("\nTopics distribution:")
    for t, count in sorted(topics.items(), key=lambda x: -x[1]):
        print(f"  {t}: {count}")


if __name__ == "__main__":
    main()
