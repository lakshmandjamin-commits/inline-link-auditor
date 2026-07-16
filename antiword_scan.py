#!/usr/bin/env python3
"""
Unicode-aware anti-word scanner. Replaces macOS grep which cannot match
non-ASCII characters (ä, ö, ü, ß, í, ó, ñ) in regex character classes.

Usage: python3 antiword_scan.py [all|site_id]
Output: JSON-formatted list of findings per page.
Exit code 1 if any anti-words found.
"""
import os, re, sys, json, sqlite3

DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")

# --- Anti-word lists (Unicode-safe) ---

DE_ANTI = re.compile(
    r'(ideale|perfekte|traumhafte|unvergesslich|magisch|einzigartig|'
    r'atemberaubend|paradies|spektakulär|wunderschön|fantastisch|'
    r'märchenhaft|zauberhaft|paradiesisch|unberührt|'
    r'weltklasse|einmalig|idyllisch|hautnah|malerisch|entdecken|'
    r'wundervoll|überwältigend|faszinierend|grandios|immersiv|'
    r'einprägsam)',
    re.IGNORECASE
)

ES_ANTI = re.compile(
    r'(paraíso|mágico|impresionante|increíble|espectacular|maravilloso|'
    r'único|sueño|inolvidable|escondido|tesoro|joya|auténtico|exclusivo)',
    re.IGNORECASE
)

EN_ANTI = re.compile(
    r'\b(breathtaking|stunning|unforgettable|magical|paradise|'
    r'hidden\s*gem|incredible|amazing|unique|'
    r'world.class|seamless|immersive|life.changing|'
    r'unparalleled|elevate\s+your|bucket.list|nestled|boasts|'
    r'renowned|once.in.a.lifetime|pristine)\b',
    re.IGNORECASE
)

def get_sites(target="all"):
    reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
    if target == "all":
        sites = reg.execute("SELECT site_id, local_path FROM sites WHERE status='active'").fetchall()
    else:
        sites = reg.execute("SELECT site_id, local_path FROM sites WHERE site_id=? AND status='active'", (target,)).fetchall()
    reg.close()
    return sites

def get_lang_pattern(filepath):
    """Determine which anti-word list to use based on file path."""
    if '/de/' in filepath or filepath.endswith('/de'):
        return DE_ANTI, "DE"
    elif '/es/' in filepath or filepath.endswith('/es'):
        return ES_ANTI, "ES"
    else:
        return EN_ANTI, "EN"

def scan_file(filepath):
    """Scan a single HTML file for anti-words. Returns list of (word, context)."""
    pattern, lang = get_lang_pattern(filepath)
    results = []
    try:
        with open(filepath, 'r') as f:
            content = f.read()
        lines = content.split('\n')
        in_script = False
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # Track script block boundaries — skip everything inside <script>...</script>
            if '<script' in stripped:
                in_script = True
            if in_script:
                if '</script>' in stripped:
                    in_script = False
                continue
            # Skip meta tags (they contain structured data, not editorial)
            if '<meta' in stripped:
                continue
            matches = pattern.findall(line)
            for m in matches:
                ctx = line.strip()[:120]
                results.append({"line": i, "word": m.lower(), "context": ctx})
    except Exception as e:
        return [{"error": str(e)}]
    return results

def scan_site(site_path):
    """Scan all HTML files in a site directory."""
    findings = {}
    for root, dirs, files in os.walk(site_path):
        dirs[:] = [d for d in dirs if not any(kw in d for kw in ('backup', 'node_modules', '.git'))]
        for fname in files:
            if not fname.endswith('.html'):
                continue
            fp = os.path.join(root, fname)
            rel = fp.replace(site_path, '')
            matches = scan_file(fp)
            if matches:
                findings[rel] = matches
    return findings

def main():
    # --dir mode: scan a directory directly (for build gates, no registry needed)
    if '--dir' in sys.argv:
        idx = sys.argv.index('--dir')
        if idx + 1 >= len(sys.argv):
            print("Usage: antiword_scan.py --dir <path>", file=sys.stderr)
            sys.exit(2)
        site_path = os.path.expanduser(sys.argv[idx + 1])
        if not os.path.isdir(site_path):
            print(f"Directory not found: {site_path}", file=sys.stderr)
            sys.exit(2)
        findings = scan_site(site_path)
        site_hits = sum(len(v) for v in findings.values())
        # Count all HTML files scanned, not just those with hits
        scanned = 0
        for root, dirs, files in os.walk(site_path):
            dirs[:] = [d for d in dirs if not any(kw in d for kw in ('backup', 'node_modules', '.git'))]
            scanned += sum(1 for f in files if f.endswith('.html'))
        if scanned == 0:
            print(f"No HTML files found in {site_path}", file=sys.stderr)
            sys.exit(2)
        if site_hits > 0:
            print(f"❌ {site_hits} hits across {len(findings)} pages")
            worst = sorted(findings.items(), key=lambda x: len(x[1]), reverse=True)[:5]
            for rel, matches in worst:
                words = set(m["word"] for m in matches if "word" in m)
                print(f"   {rel}: {len(matches)} hits [{', '.join(sorted(words)[:6])}]")
            sys.exit(1)
        else:
            print(f"✅ Clean — 0 hits across {scanned} pages")
            sys.exit(0)

    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    sites = get_sites(target)
    
    output = {}
    total_hits = 0
    total_pages = 0
    
    for site_id, site_path in sites:
        if not os.path.isdir(site_path):
            output[site_id] = {"error": "path not found"}
            continue
        
        findings = scan_site(site_path)
        site_hits = sum(len(v) for v in findings.values())
        site_pages = len(findings)
        
        output[site_id] = {
            "total_hits": site_hits,
            "pages_affected": site_pages,
            "findings": {k: v for k, v in sorted(findings.items())}
        }
        
        total_hits += site_hits
        total_pages += site_pages
        
        if site_pages > 0:
            print(f"❌ {site_id}: {site_hits} hits across {site_pages} pages")
            # Show top 5 worst pages
            worst = sorted(findings.items(), key=lambda x: len(x[1]), reverse=True)[:3]
            for rel, matches in worst:
                words = set(m["word"] for m in matches if "word" in m)
                print(f"   {rel}: {len(matches)} hits [{', '.join(sorted(words)[:6])}]")
        else:
            print(f"✅ {site_id}: clean")
    
    print(f"\nFleet total: {total_hits} hits across {total_pages} pages")
    
    # Write full JSON output
    json_path = os.path.expanduser("~/.hermes/affiliate-crons/state/antiword_scan_results.json")
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    with open(json_path, 'w') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    sys.exit(1 if total_hits > 0 else 0)

if __name__ == "__main__":
    main()
