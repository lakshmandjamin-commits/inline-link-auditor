#!/usr/bin/env python3
"""
Multi-Link Viator Affiliate Injector v2.1
FTS5 title-scoped + destination-filtered + token-overlap re-ranked + false-positive filters.
"""
import os, re, sys, sqlite3, shutil, html as html_mod, unicodedata
from collections import Counter
from pathlib import Path

PID = "P00303273"
MCID = "42383"
DB_PATH = os.path.expanduser("~/.hermes/affiliate-crons/db/viator_cli.db")
UTILITY_PAGES = {'about.html', 'contact.html', 'privacy.html'}
STRONG_RE = re.compile(r'<strong>([^<]{3,100})</strong>')

# Price-adjacent proper nouns: "Gaia Cable Car (€9)" without strong tags
PRICE_ADJACENT_RE = re.compile(
    r'(?:€|USD\s*)?\$?\d+(?:\.\d{2})?\s*(?:per|each)?[^.!?\n]{0,80}'
)

INSTRUCTIONAL_PREFIXES = (
    'who this', 'who is', 'what is', 'what this', 'how to', 'how much',
    'is this', 'are these', 'can i', 'do i', 'should i', 'why is',
    'book ', 'skip ', 'take ', 'wear ', 'dont ', 'visit ',
    'pro ', 'pro tip',
    'best ', 'worst ', 'top ', 'not ', 'not all', 'the ', 'this ', 'that ',
    'these ', 'those ', 'order', 'harvest', 'bring',
    'try ', 'combine ', 'with ', 'go ', 'group ',
    'avoid ', 'buy ', 'use ', 'get ', 'make ', 'keep ',
    'choose ', 'pick ', 'start ', 'end ', 'check ', 'look ',
    'ask ', 'tell ', 'remember ', 'forget ', 'double ',
    'morning ', 'afternoon ', 'evening ',
    'gran can', 'not ',
)

SENTENCE_WORDS = {'the', 'a', 'an', 'and', 'in', 'on', 'at', 'to', 'for',
                  'with', 'from', 'your', 'this', 'that', 'these', 'those',
                  'its', 'it', 'is', 'are', 'was', 'were', 'be', 'been',
                  'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
                  'can', 'could', 'should', 'may', 'might', 'must', 'not',
                  'or', 'but', 'if', 'so', 'as', 'by', 'no', 'yes'}

def get_db():
    return sqlite3.connect(DB_PATH)

def slug_from_path(filepath, site_root):
    rel = os.path.relpath(filepath, site_root)
    return rel.replace('/index.html', '').replace('.html', '').replace('/', '-')

def normalize_tokens(text):
    text = html_mod.unescape(text)
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"([a-z0-9])(?:'s|'s)\b", r'\1', text)
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    out = []
    for w in text.split():
        if len(w) > 3 and w.endswith('s') and not w.endswith(('ss', 'us', 'is')):
            w = w[:-1]
        if len(w) > 1 and w not in out:
            out.append(w)
    return out

def detect_destination(site_path):
    rx = re.compile(r'viator\.com[^\x27"\s<>]*?/d(\d+)-', re.I)
    counts = Counter()
    for p in Path(site_path).rglob('*.html'):
        try:
            counts.update(map(int, rx.findall(p.read_text())))
        except (OSError, UnicodeError):
            pass
    return counts.most_common(1)[0][0] if counts else 26879

def fts_search(db, phrase, destination_id):
    words = normalize_tokens(phrase)
    if len(words) < 2:
        return []
    query_set = set(words)
    fts_query = 'title : (' + ' OR '.join(words) + ')'
    try:
        rows = db.execute(
            """SELECT p.product_code, p.title, p.product_url, rank
               FROM products_fts f JOIN products p ON f.rowid = p.rowid
               WHERE products_fts MATCH ?
                 AND p.destination_id = ?
                 AND p.active = 1 AND p.is_available = 1
                 AND p.review_count >= 1
               ORDER BY rank LIMIT 50""",
            (fts_query, destination_id)
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    ranked = []
    for code, title, url, rank in rows:
        title_tokens = set(normalize_tokens(title))
        matched = len(query_set & title_tokens)
        if matched < 3:
            continue
        query_coverage = matched / len(query_set)
        title_precision = matched / len(title_tokens) if title_tokens else 0
        ranked.append((-matched, -query_coverage, -title_precision, rank,
                       code, title, url, matched, len(query_set)))
    ranked.sort(key=lambda x: x[:4])
    return [(r[4], r[5], r[6], r[7], r[8]) for r in ranked]

PRICE_TAGGED_RE = re.compile(
    r'^(.+?)\s*\((?:€|\$)[\d,.]+(?:[\u20ac\$]\d[\d,.]*)?\s*(?:[–-]\s*(?:€|\$)[\d,.]+)?(?:,?\s*\d+\.?\d*[\u2605\u2B50]*\s*(?:⭐|stars?)?|,?\s*\d+\s*(?:hours?|hrs?|min(?:utes?)?)\s*)\)\s*$'
)

def match_priced_callout(db, phrase, destination_id):
    """Handle 'Tour Name ($XX, X.X⭐)' patterns — extract tour name, exact match."""
    m = PRICE_TAGGED_RE.match(phrase.strip())
    if not m:
        return None
    tour_name = m.group(1).strip()
    if len(tour_name.split()) < 2:
        return None
    # OR-based FTS5 search on just the tour name tokens  
    clean_tokens = normalize_tokens(tour_name)
    if len(clean_tokens) < 2:
        return None
    fts_query = 'title : (' + ' OR '.join(clean_tokens) + ')'
    try:
        rows = db.execute(
            """SELECT p.product_code, p.title, p.product_url, rank
               FROM products_fts f JOIN products p ON f.rowid = p.rowid
               WHERE products_fts MATCH ?
                 AND p.destination_id = ?
                 AND p.active = 1 AND p.is_available = 1
                 AND p.review_count >= 1
               ORDER BY rank LIMIT 5""",
            (fts_query, destination_id)
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None
    # Re-rank: require ≥3 token overlap for price-tagged callouts
    query_tokens = set(clean_tokens)
    best = None
    for code, title, url, rank in rows:
        title_tokens = set(normalize_tokens(title))
        matched = len(query_tokens & title_tokens)
        if matched >= 3 and (best is None or rank > best[3]):
            best = (code, title, url, rank, matched)
    if not best:
        return None
    return best[0], best[1], best[2], 999, 999

def looks_like_sentence(phrase):
    words = phrase.lower().split()
    if len(words) < 3:
        return False
    func_count = sum(1 for w in words if w in SENTENCE_WORDS)
    return func_count / len(words) > 0.4

def extract_candidates(html_content):
    candidates = []
    seen = set()
    for m in STRONG_RE.finditer(html_content):
        phrase = m.group(1).strip()
        clean = html_mod.unescape(phrase).strip()
        words = clean.split()
        if len(words) < 2 or len(words) > 8:
            continue
        if not any(c.isupper() for c in clean):
            continue
        # Skip phrases that end with colon (section headers, category labels)
        if clean.rstrip().endswith(':'):
            continue
        lower = clean.lower().replace("'", "").replace("'", "")
        if lower in seen:
            continue
        if any(lower.startswith(p) for p in INSTRUCTIONAL_PREFIXES):
            continue
        if looks_like_sentence(clean):
            continue
        # Require at least one proper noun (capitalized word not at position 0 when many words)
        has_proper = False
        for i, w in enumerate(words):
            if w and w[0].isupper() and not (i == 0 and len(words) > 3):
                has_proper = True
                break
        if not has_proper:
            continue
        seen.add(lower)
        candidates.append((phrase, clean, m.start(1), m.end(1)))
    return candidates

def is_inside_heading(html, pos):
    before = html[:pos]
    for i in range(1, 7):
        if before.rfind(f'<h{i}') > before.rfind(f'</h{i}>'):
            return True
    return False

def inject_link_at(html, pos, end_pos, code, title, url, page_slug, matched_codes):
    if matched_codes.get(code, 0) >= 2:
        return html, False
    base_url = url.split('?')[0] if '?' in url else url
    aff_url = (f"{base_url}?pid={PID}&mcid={MCID}&medium=link"
               f"&utm_source=viator&utm_medium=affiliate"
               f"&utm_campaign={page_slug}&utm_content={code}")
    phrase = html[pos:end_pos]
    link = f'<a href="{aff_url}" rel="sponsored">{phrase}</a>'
    new_html = html[:pos] + link + html[end_pos:]
    matched_codes[code] = matched_codes.get(code, 0) + 1
    return new_html, True

def process_page(filepath, site_root, db, destination_id, fix=False):
    with open(filepath) as f:
        html = f.read()
    if '<main' not in html:
        return {'status': 'no_main', 'links': 0}
    page_slug = slug_from_path(filepath, site_root)
    candidates = extract_candidates(html)
    candidates.sort(key=lambda x: x[2], reverse=True)
    matched_codes = {}  # code -> count, max 2 per page
    links_added = 0
    matches_found = []
    for phrase, clean, pos, end_pos in candidates:
        before = html[:pos]
        if before.rfind('<a ') > before.rfind('</a>'):
            continue
        if is_inside_heading(html, pos):
            continue
        # First: try exact match for price-tagged callouts like "Tour Name ($XX, X.X⭐)"
        is_priced = PRICE_TAGGED_RE.match(clean.strip())
        if is_priced:
            price_result = match_priced_callout(db, clean, destination_id)
            if price_result:
                code, title, url, matched, total = price_result
                matches_found.append((clean, title, f"{matched}/{total}", 'LINK'))
                if fix:
                    new_html, added = inject_link_at(html, pos, end_pos, code, title, url, page_slug, matched_codes)
                    if added:
                        html = new_html
                        links_added += 1
            # Whether matched or not, skip FTS5 fallback for priced callouts
            continue
        # Fall back to FTS5 search
        results = fts_search(db, clean, destination_id)
        if not results:
            continue
        code, title, url, matched, total = results[0]
        matches_found.append((clean, title, f"{matched}/{total}", 'LINK'))
        if fix:
            new_html, added = inject_link_at(html, pos, end_pos, code, title, url, page_slug, matched_codes)
            if added:
                html = new_html
                links_added += 1
    if fix and links_added > 0:
        backup = filepath + '.pre-inline-link-backup'
        if not os.path.exists(backup):
            shutil.copy2(filepath, backup)
        with open(filepath, 'w') as f:
            f.write(html)
    return {'status': 'ok', 'links': links_added, 'candidates': len(candidates),
            'matches': matches_found}

def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    site_path = os.path.abspath(sys.argv[1])
    fix = '--fix' in sys.argv
    limit = None
    for i, arg in enumerate(sys.argv):
        if arg == '--limit' and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])
    db = get_db()

    # Single-file mode: process one HTML file directly
    if os.path.isfile(site_path):
        # Walk up to find site root (directory with .git or multiple .html files)
        site_root = os.path.dirname(site_path)
        while site_root != '/' and not os.path.isdir(os.path.join(site_root, '.git')):
            parent = os.path.dirname(site_root)
            if parent == site_root: break
            site_root = parent
        dest_id = detect_destination(site_root)
        print(f"Destination: d{dest_id}  Mode: {'FIX' if fix else 'DRY-RUN'}")
        result = process_page(site_path, site_root, db, dest_id, fix=fix)
        if result['matches']:
            for phrase, title, tokens, status in result['matches']:
                icon = 'LINK' if status == 'LINK' else 'SKIP'
                print(f"  {icon}: '{phrase[:55]}' -> '{title[:55]}' ({tokens})")
            print(f"\nLinks injected: {result['links']}")
        else:
            print("No matches")
        return

    dest_id = detect_destination(site_path)
    print(f"Destination: d{dest_id}  Mode: {'FIX' if fix else 'DRY-RUN'}")
    if limit: print(f"Limit: {limit} pages")
    stats = {'scanned': 0, 'fixed': 0, 'total_links': 0, 'skipped': 0}
    for root, dirs, files in os.walk(site_path):
        dirs[:] = [d for d in dirs if 'backup' not in d and not d.startswith('.')
                    and d not in ('css', 'images', '__pycache__', 'js', 'fonts')]
        for fn in sorted(files):
            if not fn.endswith('.html'): continue
            if limit and stats['scanned'] >= limit: break
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, site_path)
            if os.path.basename(fp) in UTILITY_PAGES: stats['skipped'] += 1; continue
            stats['scanned'] += 1
            result = process_page(fp, site_path, db, dest_id, fix=fix)
            if result['matches']:
                stats['fixed'] += 1
                if fix: stats['total_links'] += result['links']
                print(f"\nPAGE: {rel}")
                for phrase, title, tokens, status in result['matches']:
                    icon = 'LINK' if status == 'LINK' else 'SKIP'
                    print(f"  {icon}: '{phrase[:55]}' -> '{title[:55]}' ({tokens})")
        if limit and stats['scanned'] >= limit: break
    print(f"\n{'='*50}")
    print(f"Scanned:{stats['scanned']}  Fixed:{stats['fixed']}  Links:{stats['total_links']}  Skipped:{stats['skipped']}")
    db.close()

if __name__ == '__main__':
    main()
