#!/usr/bin/env python3
"""
Topic Research Collector — Phase 1 of topic planning.
Calls Brave Search API to gather SERP data, competitor content, and keyword gaps.
Outputs JSON that the agent consumes for Phase 2 (topic brief generation).

Usage: python3 topic_research.py [site_slug|all]
"""
import sys, os, json, yaml, time
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.parse import urlencode
import gzip

BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
CONTENT_BANKS_DIR = os.path.expanduser("~/.hermes/affiliate-crons/content-banks")
OUTPUT_DIR = os.path.expanduser("~/.hermes/affiliate-crons/research")
REQUEST_DELAY = 1.1  # respect rate limits


def load_content_bank(slug):
    path = os.path.join(CONTENT_BANKS_DIR, f"{slug}.yaml")
    if not os.path.exists(path):
        print(f"  SKIP: no content bank for {slug}")
        return None
    with open(path) as f:
        return yaml.safe_load(f)


def brave_search(query, count=10):
    """Call Brave Search API. Returns list of results."""
    params = urlencode({"q": query, "count": count, "search_lang": "en"})
    req = Request(f"{BRAVE_SEARCH_URL}?{params}")
    req.add_header("Accept", "application/json")
    req.add_header("Accept-Encoding", "gzip")
    req.add_header("X-Subscription-Token", BRAVE_API_KEY)

    try:
        with urlopen(req) as resp:
            raw = resp.read()
            # Brave returns gzip-compressed responses
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            data = json.loads(raw)
        results = []
        for r in data.get("web", {}).get("results", []):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "description": r.get("description", "")
            })
        return results
    except Exception as e:
        print(f"    Brave API error: {e}")
        return []


def get_sites(target):
    """List all sites with content banks, or a single one."""
    if target != "all":
        slug = target
        cb = load_content_bank(slug)
        return [(slug, cb)] if cb else []

    sites = []
    for f in sorted(os.listdir(CONTENT_BANKS_DIR)):
        if f.endswith(".yaml") and not f.startswith("_"):
            slug = f.replace(".yaml", "")
            cb = load_content_bank(slug)
            if cb:
                sites.append((slug, cb))
    return sites


def research_site(slug, cb):
    """Gather SERP data for a site using its content bank keywords + niche."""
    niche = cb["site"]["niche"]
    destination = cb["site"].get("destination", "")
    keywords = cb.get("seo", {}).get("primary_keywords", [])
    competitors = cb.get("seo", {}).get("competitor_urls", [])

    location = f" in {destination}" if destination else ""
    research = {
        "slug": slug,
        "niche": niche,
        "destination": destination,
        "searched_at": datetime.now().isoformat(),
        "keyword_results": {},
        "competitor_results": {},
        "gap_queries": {}
    }

    # 1. Keyword SERP analysis
    print(f"  Keywords ({len(keywords)}):", end=" ", flush=True)
    for kw in keywords[:10]:  # top 10 keywords
        print(".", end="", flush=True)
        results = brave_search(f"{kw}{location}", count=5)
        research["keyword_results"][kw] = results
        time.sleep(REQUEST_DELAY)
    print()

    # 2. Competitor analysis
    if competitors:
        print(f"  Competitors ({len(competitors)}):", end=" ", flush=True)
        for url in competitors[:5]:
            print(".", end="", flush=True)
            results = brave_search(f"site:{url} {niche}", count=5)
            research["competitor_results"][url] = results
            time.sleep(REQUEST_DELAY)
        print()

    # 3. Gap queries — what are people searching for?
    gap_queries = [
        f"best {niche} {destination} for beginners",
        f"{niche} {destination} tips",
        f"{niche} {destination} guide",
        f"what to know before {niche} {destination}",
        f"{niche} {destination} vs",
    ]
    print(f"  Gap queries:", end=" ", flush=True)
    for q in gap_queries:
        print(".", end="", flush=True)
        results = brave_search(q, count=5)
        research["gap_queries"][q] = results
        time.sleep(REQUEST_DELAY)
    print()

    return research


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    print(f"Topic Research Collector — {target} — {datetime.now().isoformat()[:19]}\n")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    sites = get_sites(target)

    if not sites:
        print("No sites found with content banks.")
        sys.exit(0)

    for slug, cb in sites:
        print(f"{slug}:")
        try:
            research = research_site(slug, cb)
            out_path = os.path.join(OUTPUT_DIR, f"{slug}.json")
            with open(out_path, "w") as f:
                json.dump(research, f, indent=2, default=str)
            kw_count = len(research["keyword_results"])
            print(f"  Saved: {out_path} ({kw_count} keywords, {len(research['gap_queries'])} gap queries)")
        except Exception as e:
            print(f"  ERROR: {e}")

    print(f"\nDone. Research files → {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
