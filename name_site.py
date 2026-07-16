#!/usr/bin/env python3
"""
MiniMax Naming Agent — generates site names that resonate with customers and build trust.
Uses MiniMax M2.7 to suggest brandable, trust-building names, then checks availability.

Usage: python3 name_site.py <niche> [destination]
  Example: python3 name_site.py hiking madeira
  Example: python3 name_site.py surfing "costa rica"
"""
import sys, os, json, re, socket, time
from urllib.request import Request, urlopen
from datetime import datetime

# MiniMax M2.7 (OpenAI-compatible)
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
MINIMAX_BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io")
MINIMAX_MODEL = "MiniMax-M2.7"

# Domain TLDs to check — prioritized by cost + relevance
TLDS = [
    ("vercel.app", 0, "Always available — deploy instantly"),
    ("com", 12, "Most trusted by consumers"),
    ("info", 8, "Informational credibility"),
    ("co", 12, "Short, modern"),
    ("guide", 15, "Trustworthy for activity niches"),
    ("travel", 14, "Relevant for destination sites"),
]

# Retry utility
def call_api(url, payload, api_key, max_retries=3, timeout=60):
    """Call an API with retry. Returns (data, error)."""
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            data_bytes = json.dumps(payload).encode("utf-8")
            req = Request(url, data=data_bytes, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", f"Bearer {api_key}")
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read()), None
        except Exception as e:
            last_error = str(e)
            if attempt < max_retries:
                time.sleep(2 * (2 ** attempt))
                continue
    return None, last_error


def generate_names(niche, destination):
    """Ask MiniMax M2.7 to generate customer-trusting site names."""
    prompt = f"""You are a brand strategist for niche affiliate travel/activity websites. 
Your task: generate 12-15 site names that build trust and resonate with customers.

NICHE: {niche}
DESTINATION: {destination or 'N/A (general interest niche)'}

CRITERIA:
1. Each name must sound trustworthy — like a real, helpful guide written by an expert
2. Must resonate with the target customer (active travelers who research before booking)
3. Should feel modern and specific, not generic travel blog
4. No empty superlatives (breathtaking, stunning, hidden gem, etc.)
5. Prefer names that sound like they answer "which one?" rather than "what is this?"
6. Favor names that suggest authority and comparison (Guide, Honest, Compare, Pick, Choice)
7. Must work as a domain name (short enough, easy to spell)

OUTPUT FORMAT: Return ONLY a JSON array of objects, no markdown, no explanation:
[
  {{"name": "Madeira Hiking Guide", "slug": "madeira-hiking-guide", "reasoning": "Specify which? approach — sounds authoritative and helpful", "vibe": "trustworthy guide"}},
  ...
]

Generate 12-15 names. Make them genuinely good — names people would actually bookmark."""

    system = """You are a brand naming expert. You generate site names that build immediate consumer trust.
    Your names feel like real guides written by experts, not generic travel blogs.
    You avoid empty superlatives and favor comparison-led, evidence-based naming.
    Output ONLY valid JSON arrays with objects containing: name, slug, reasoning, vibe."""

    payload = {
        "model": MINIMAX_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 2000,
        "temperature": 0.8,
        "stream": False
    }

    url = f"{MINIMAX_BASE_URL}/v1/text/chatcompletion_v2"
    data, err = call_api(url, payload, MINIMAX_API_KEY, timeout=120)
    
    if err:
        # Try OpenAI-compatible endpoint
        url2 = f"{MINIMAX_BASE_URL}/v1/chat/completions"
        data, err = call_api(url2, payload, MINIMAX_API_KEY, timeout=120)
        if err:
            print(f"ERROR: {err}")
            return None
        content = data["choices"][0]["message"]["content"]
    else:
        content = data["choices"][0]["message"]["content"]
    
    # Extract JSON
    match = re.search(r'\[.*\]', content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception as e:
            print(f"JSON parse error: {e}")
            print(f"Raw: {content[:500]}")
            return None
    else:
        # Try parsing entire output as JSON
        try:
            return json.loads(content)
        except:
            print(f"No JSON found in response: {content[:500]}")
            return None


def check_availability(slug):
    """Check DNS availability across TLDs. Returns list of (domain, tld, cost, available)."""
    results = []
    for tld, cost, note in TLDS:
        if tld == "vercel.app":
            # Always available
            results.append((f"{slug}.vercel.app", tld, cost, True, note))
            continue
        
        domain = f"{slug}.{tld}"
        try:
            socket.gethostbyname(domain)
            available = False
        except socket.gaierror:
            available = True
        except OSError:
            available = None  # unknown
        
        results.append((domain, tld, cost, available, note))
        time.sleep(0.05)
    
    return results


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 name_site.py <niche> [destination]")
        sys.exit(1)
    
    if not MINIMAX_API_KEY:
        print("ERROR: MINIMAX_API_KEY not set. Source ~/.hermes/.env first.")
        sys.exit(1)
    
    niche = sys.argv[1].lower()
    destination = sys.argv[2].lower() if len(sys.argv) > 2 else None
    
    print(f"🎯 Naming Agent — {niche.title()}" + (f" in {destination.title()}" if destination else ""))
    print(f"   Using MiniMax M2.7 to generate trust-building names\n")
    
    # Step 1: Generate names
    names = generate_names(niche, destination)
    if not names:
        print("Failed to generate names. Check API key and try again.")
        sys.exit(1)
    
    print(f"Generated {len(names)} candidate names. Checking availability...\n")
    
    # Step 2: Check availability
    results = []
    for item in names:
        slug = item.get("slug", "")
        if not slug:
            continue
        avail = check_availability(slug)
        results.append((item, avail))
    
    # Step 3: Display
    print(f"{'NAME':<45} {'REASONING':<50} {'BEST DOMAIN':<40} {'COST':<10}")
    print(f"{'-'*45} {'-'*50} {'-'*40} {'-'*10}")
    
    best_free = None
    for item, avail_list in results:
        name = item["name"]
        reasoning = item.get("reasoning", "")[:48]
        
        # Find best available option
        available_domains = [(d, t, c, n) for d, t, c, a, n in avail_list if a]
        if not available_domains:
            continue
        
        best = available_domains[0]  # vercel.app (free) is first
        domain_str = f"{best[0]}"
        cost_str = f"${best[2]}/yr" if best[2] > 0 else "FREE"
        
        print(f"{name:<45} {reasoning:<50} {domain_str:<40} {cost_str:<10}")
        
        if not best_free and best[2] == 0:
            best_free = (item, domain_str)
    
    # Step 4: Recommendation
    print(f"\n{'='*70}")
    if best_free:
        print(f"✅ BEST FREE: {best_free[0]['name']} → {best_free[1]}")
    
    # Also suggest the single best paid option
    all_paid = []
    for item, avail_list in results:
        for d, t, c, a, n in avail_list:
            if a and c > 0:
                all_paid.append((item, d, c, t))
    if all_paid:
        best_paid = min(all_paid, key=lambda x: x[2])
        print(f"💎 BEST PAID: {best_paid[0]['name']} → {best_paid[1]} (${best_paid[2]}/yr)")
    
    print(f"\nMiniMax reasoning — {niche.title()} naming strategy:")
    print(f"  Names focus on trust and comparison, not empty superlatives.")
    print(f"  'Guide', 'Honest', 'Compare', 'Choice' — words that build consumer confidence.")


if __name__ == "__main__":
    main()
