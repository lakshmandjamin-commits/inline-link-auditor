#!/usr/bin/env python3
"""
Domain Name Finder — generates and checks available domains for affiliate sites.

Naming strategy (tested for availability in 2026):
  Pattern A: .guide TLD — abundant availability, ~$15/yr, relevant for travel/activity
  Pattern B: .info TLD — ~$8/yr, available for most 3-word combos
  Pattern C: Vercel subdomain — free, immediate, can upgrade later
  Pattern D: .co TLD — ~$12/yr, available for most niches

Usage: python3 domain_finder.py <niche> [destination]
  Example: python3 domain_finder.py surfing costa-rica
  Example: python3 domain_finder.py hiking madeira

Outputs available domains ranked by preference.
"""
import sys, socket, time, re

# Common patterns that pass the "sounds like an affiliate site" test
PATTERNS = [
    # Tier 1: Best (keyword rich + brandable)
    "{niche}{destination}guide",       # surfingcostaricaguide
    "{destination}{niche}guide",       # costaricasurfguide
    "{destination}{niche}hub",         # costaricasurfhub
    
    # Tier 2: Good
    "best{niche}{destination}",        # bestsurfingcostarica
    "{niche}the{destination}",         # surfthecostarica
    "the{niche}{destination}",         # thesurfingcostarica
    
    # Tier 3: OK (longer, less brandable)
    "complete{niche}{destination}",    # completesurfingcostarica
    "{destination}adventure{niche}",   # costaricaadventuresurfing
    "got{niche}{destination}",         # gotsurfingcostarica
]

TLDS = [("com", 12), ("guide", 15), ("info", 8), ("co", 12), ("travel", 14)]
TLDS_FREE = [("vercel.app", 0)]  # always available


def slugify(text):
    """Convert to domain-safe slug."""
    return re.sub(r'[^a-z0-9]', '', text.lower().replace(' ', ''))


def is_available(domain):
    """Quick DNS check — false positives are OK (domain may be parked but not resolving)."""
    try:
        socket.gethostbyname(domain)
        return False  # resolves = taken
    except socket.gaierror:
        return True   # doesn't resolve = likely available
    except OSError:
        return None   # DNS timeout = unknown


def generate_names(niche, destination=None):
    """Generate candidate domain names."""
    niche_slug = slugify(niche)
    dest_slug = slugify(destination) if destination else ""
    
    names = []
    for pattern in PATTERNS:
        name = pattern.format(
            niche=niche_slug,
            destination=dest_slug
        )
        if name not in names:
            names.append(name)
    
    # Also try {niche}-{destination} style (for vercel subdomains)
    if dest_slug:
        names.append(f"{niche_slug}-{dest_slug}")
        names.append(f"{dest_slug}-{niche_slug}")
    
    return names


def check_availability(names):
    """Check all TLDs for each name. Returns sorted results."""
    results = []
    for name in names:
        for tld, cost in TLDS:
            domain = f"{name}.{tld}"
            available = is_available(domain)
            if available:
                # Estimate pattern quality by matching against stripped names
                name_clean = name.replace("-", "")
                results.append({
                    "domain": domain,
                    "tld": tld,
                    "cost": cost,
                    "name": name,
                })
            time.sleep(0.1)
    
    return results


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 domain_finder.py <niche> [destination]")
        sys.exit(1)
    
    niche = sys.argv[1]
    destination = sys.argv[2] if len(sys.argv) > 2 else None
    
    # Generate Vercel slug
    vercel_slug = f"{slugify(niche)}-{slugify(destination)}" if destination else slugify(niche)
    
    print(f"Domain Finder — {niche.title()}" + (f" in {destination.title()}" if destination else ""))
    print(f"{'='*50}\n")
    
    # Free option
    print(f"✅ FREE: {vercel_slug}.vercel.app")
    print(f"   Deploy immediately, zero cost. Add custom domain later.\n")
    
    # Check paid options
    names = generate_names(niche, destination)
    results = check_availability(names)
    
    if not results:
        print("No available domains found with these patterns.")
        print("Try: different TLD, or use Vercel subdomain.")
        sys.exit(0)
    
    # Sort by name length (shorter = better for domain), then cost
    results.sort(key=lambda r: (len(r["name"]), r["cost"]))
    
    # Display
    print("Available domains (ranked):")
    for i, r in enumerate(results[:10], 1):
        cost_label = "$0" if r["cost"] == 0 else f"${r['cost']}/yr"
        print(f"  {i}. {r['domain']}  [{cost_label}]")
    
    print(f"\nTotal available: {len(results)}")
    print(f"\nRecommended: {vercel_slug}.vercel.app + register the best .guide or .info later.")


if __name__ == "__main__":
    main()
