#!/usr/bin/env python3
"""
Entity consistency checker — catches voice/identity drift across editorial pages.

Scans for:
  1. Author name variations (Tiago, Tiago F., Tiago Ferreira — should be one form)
  2. Years-of-experience contradictions ("a decade" vs "12 years" vs "15 years")
  3. Pronoun drift (I vs we — mixed on same site)
  4. Inconsistent numeric claims (same stat, different values)

Usage: python3 entity_consistency_check.py [all|site_id] [--fix]
Output: JSON. Exit code 1 if inconsistencies found.
"""
import os, re, sys, json, sqlite3
from collections import Counter

DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Author name forms to detect (site-specific) ---
SITE_AUTHORS = {
    'porto': {
        'canonical': 'Tiago Ferreira',
        'accepted_variants': {'Tiago Ferreira', 'Tiago F.', 'Tiago F', 'Tiago'},
        'years_patterns': [
            r'(?:over |more than |nearly |almost )?(\d+)(?:\+)?\s*years?\s*(?:of\s+)?(?:experience|in\s+(?:wine|port|tourism))',
            r'a\s+decade(?!-old)',
        ],
        'pronoun_check': True,
    },
    'tenerife': {
        'canonical': 'Alejandro Vega',
        'accepted_variants': {'Alejandro Vega', 'Alejandro V.', 'Alejandro V', 'Alejandro', 'Ale'},
        'years_patterns': [
            r'(?:over |more than |nearly |almost )?(\d+)(?:\+)?\s*years?\s*(?:of\s+)?(?:experience|guiding|in\s+Teide)',
            r'a\s+decade(?!-old)',
        ],
        'pronoun_check': True,
    },
    'madeira': {
        'canonical': 'Sofia Almeida',
        'accepted_variants': {'Sofia Almeida', 'Sofia A.', 'Sofia A', 'Sofia'},
        'years_patterns': [
            r'(?:over |more than |nearly |almost )?(\d+)(?:\+)?\s*years?\s*(?:of\s+)?(?:experience|guiding|on\s+Madeira)',
            r'a\s+decade(?!-old)',
        ],
        'pronoun_check': True,
    },
    'lapland': {
        'canonical': 'Mia Ahola',
        'accepted_variants': {'Mia Ahola', 'Mia A.', 'Mia A', 'Mia'},
        'years_patterns': [
            r'(?:over |more than |nearly |almost )?(\d+)(?:\+)?\s*years?\s*(?:of\s+)?(?:experience|in\s+Lapland|testing)',
            r'a\s+decade(?!-old)',
        ],
        'pronoun_check': True,
    },
}

# Map site slugs → short names used in SITE_AUTHORS
SLUG_TO_SHORT = {
    'porto-wine-tours': 'porto',
    'tenerife-outdoor-guide': 'tenerife',
    'madeira-hiking': 'madeira',
    'lapland-adventure-guide': 'lapland',
}

def get_sites(target="all"):
    reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
    if target == "all":
        sites = reg.execute("SELECT site_id, local_path FROM sites WHERE status='active'").fetchall()
    else:
        sites = reg.execute("SELECT site_id, local_path FROM sites WHERE site_id=? AND status='active'", (target,)).fetchall()
    reg.close()
    return [(SLUG_TO_SHORT.get(s[0], s[0]), s[1]) for s in sites]

def get_lang_prefix(filepath):
    """Detect language from path."""
    if '/de/' in filepath.lower() or filepath.endswith('/de'):
        return 'de'
    elif '/es/' in filepath.lower() or filepath.endswith('/es'):
        return 'es'
    return 'en'

def scan_entity_consistency(site_dir, site_id):
    """Full entity consistency scan for a site. Returns findings dict."""
    config = SITE_AUTHORS.get(site_id)
    if not config:
        print(json.dumps({"status": "skipped", "reason": f"no author config for {site_id}"}))
        return None

    findings = {
        'site_id': site_id,
        'canonical_author': config['canonical'],
        'author_variants_found': Counter(),
        'years_claims': [],       # (file, claim_text, numeric_value)
        'pronoun_counts': {'I': 0, 'we': 0, 'We': 0, 'mixed_pages': []},
        'suspect_claim_contradictions': [],
        'errors': [],
    }

    editorial_files = []
    utility_names = {'about.html', 'contact.html', 'privacy.html', '404.html', 'impressum.html', 'datenschutz.html'}

    for root, dirs, files in os.walk(site_dir):
        dirs[:] = [d for d in dirs if 'backup' not in d and not d.startswith('.')]
        for fn in files:
            if not fn.endswith('.html'):
                continue
            rel = os.path.relpath(os.path.join(root, fn), site_dir)
            # Skip utility pages for narrative checks
            if os.path.basename(fn) in utility_names:
                continue
            editorial_files.append((os.path.join(root, fn), rel))

    for filepath, rel in editorial_files:
        try:
            with open(filepath, 'r') as f:
                content = f.read()
        except Exception as e:
            findings['errors'].append(f"{rel}: read error: {e}")
            continue

        # Extract main content only (skip nav, footer, JSON-LD, meta)
        main_match = re.search(r'<main[^>]*>(.*?)</main>', content, re.DOTALL)
        if not main_match:
            main_match = re.search(r'<body[^>]*>(.*?)</body>', content, re.DOTALL)
        if not main_match:
            findings['errors'].append(f"{rel}: no <main> or <body>")
            continue

        body = main_match.group(1)
        # Strip HTML tags for text analysis
        text = re.sub(r'<script[^>]*>.*?</script>', ' ', body, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()

        # --- 1. Author name variants ---
        # Build regex patterns from accepted variants for detection
        author_regexes = [
            r'Tiago\s+Ferreira', r'Tiago\s+F\.', r'\bTiago\b',
            r'Alejandro\s+Vega', r'Alejandro\s+V\.', r'\bAlejandro\b', r'\bAle\b',
            r'Sofia\s+Almeida', r'Sofia\s+A\.', r'\bSofia\b',
            r'Mia\s+Ahola', r'Mia\s+A\.', r'\bMia\b',
        ]
        for variant_pat in author_regexes:
            for m in re.finditer(variant_pat, text, re.IGNORECASE):
                h_stripped = m.group(0).strip()
                # Skip author names inside email addresses (tiago@domain.com)
                ctx_start = max(0, m.start() - 3)
                ctx_end = min(len(text), m.end() + 3)
                ctx_near = text[ctx_start:ctx_end]
                if '@' in ctx_near:
                    continue
                # Filter: only count if not in accepted variants for this site
                accepted = config.get('accepted_variants', set())
                if h_stripped not in accepted:
                    findings['author_variants_found'][h_stripped] += 1

        # --- 2. Years-of-experience claims ---
        for y_pat in config['years_patterns']:
            for m in re.finditer(y_pat, text, re.IGNORECASE):
                claim = m.group(0)
                # Only count claims about the AUTHOR's experience, not general facts
                # Check surrounding context for first-person or author name signals
                ctx_window = 150
                ctx_start = max(0, m.start() - ctx_window)
                ctx_end = min(len(text), m.end() + ctx_window)
                ctx = text[ctx_start:ctx_end]
                canonical = config.get('canonical', '')
                author_first = canonical.split()[0] if canonical else ''
                
                # Skip if not about the author (no first-person, no author name nearby)
                is_personal = (
                    re.search(r'\bI\b|\bmy\b|\bme\b|\bI\'ve\b', ctx) or
                    (author_first and author_first.lower() in ctx.lower())
                )
                if not is_personal:
                    continue
                
                # Skip if inside HTML heading (poetic section titles, not factual claims)
                pre_text = text[max(0, m.start()-200):m.start()]
                if re.search(r'<h[1-6][^>]*>[^<]*$', pre_text):
                    continue
                    
                if 'decade' in claim.lower():
                    findings['years_claims'].append((rel, claim, 10))
                else:
                    num_match = re.search(r'(\d+)', claim)
                    if num_match:
                        findings['years_claims'].append((rel, claim, int(num_match.group(1))))

        # --- 3. Pronoun consistency ---
        # Count editorial voice pronouns: first-person "I" vs editorial "we"
        # Narrative "we" (we arrived, we walked — group experience) is NOT editorial voice
        i_experiential = re.compile(
            r'\bI\b(?:\s+(?:have|was|am|got|took|went|booked|found|saw|noticed|learned|walked|arrived|spent|joined|asked))',
            re.IGNORECASE
        )
        # Only EDITORIAL "we" — making claims, not describing shared experience.
        # Also excludes footer disclosure boilerplate ("we earn a commission", "we keep the site free")
        we_editorial = re.compile(
            r'\b[Ww]e\b(?:\s+(?:recommend|suggest|think|believe|prefer|avoid|love|like|choose|pick|test|review|rank|select|favor|favour|stand\s+by|vouch\s+for|swear\s+by))'
            r'(?!(?:\s+\w+){0,3}\s+(?:commission|site\s+free|no\s+extra\s+cost|at\s+no))',  # Skip disclosure boilerplate
            re.IGNORECASE
        )
        # Also catch "we use" / "we rate" outside disclosure context
        we_editorial_use = re.compile(
            r'\b[Ww]e\s+use\b'
            r'(?!(?:\s+\w+){0,3}\s+(?:commission|site\s+free))',
            re.IGNORECASE
        )
        i_count = len(i_experiential.findall(text))
        we_count = len(we_editorial.findall(text)) + len(we_editorial_use.findall(text))

        if i_count > 0 or we_count > 0:
            findings['pronoun_counts']['I'] += i_count
            findings['pronoun_counts']['we'] += we_count
            if i_count > 0 and we_count > 0:
                findings['pronoun_counts']['mixed_pages'].append(rel)

    # --- Post-processing: detect contradictions ---
    year_values = [v for _, _, v in findings['years_claims']]
    if len(set(year_values)) > 1 and year_values:
        # Multiple different year claims — flag all
        claims_by_value = {}
        for fname, claim_text, val in findings['years_claims']:
            claims_by_value.setdefault(val, []).append((fname, claim_text))
        if len(claims_by_value) > 1:
            findings['suspect_claim_contradictions'] = [
                {"value": v, "claims": c} for v, c in sorted(claims_by_value.items())
            ]

    return findings


def scan_typos(site_dir):
    """Scan for real editorial typos — repeated words within sentences, excluding
    legitimate grammatical patterns, table data, proper nouns, and product names."""
    findings = []
    utility_names = {'about.html', 'contact.html', 'privacy.html', '404.html', 'impressum.html', 'datenschutz.html'}

    # ONLY true typos: exact consecutive function-word repeats.
    # These are always errors: "the the", "and and", "is is", "to to", etc.
    # Skip: proper nouns (capitalized), content words (legitimate repetition in prose)
    FUNCTION_WORDS = {
        'the', 'The', 'a', 'A', 'an', 'An',
        'and', 'And', 'or', 'Or', 'but', 'But',
        'is', 'Is', 'are', 'Are', 'was', 'Was', 'were', 'Were',
        'in', 'In', 'on', 'On', 'at', 'At', 'to', 'To',
        'of', 'Of', 'for', 'For', 'with', 'With',
        'it', 'It', 'this', 'This', 'that', 'That',
        'be', 'Be', 'been', 'Been', 'has', 'Has', 'have', 'Have',
        'can', 'Can', 'will', 'Will', 'would', 'Would',
        'not', 'Not', 'no', 'No',
        'by', 'By', 'from', 'From',
        'as', 'As', 'so', 'So', 'if', 'If',
        'also', 'Also', 'just', 'Just', 'still', 'Still',
        'very', 'Very', 'too', 'Too', 'much', 'Much',
    }
    CONSECUTIVE_REPEAT = re.compile(
        r'\b([a-zA-Z]{2,})\b\s+\1\b',
        re.IGNORECASE | re.UNICODE
    )

    # German grammatical patterns that are NEVER typos
    GERMAN_FALSE_POSITIVES = {
        'die', 'der', 'das', 'dem', 'den', 'des',
        'sie', 'Sie', 'ihre', 'Ihre', 'ihren', 'Ihren',
        'ein', 'eine', 'einen', 'einem', 'eines',
        'und', 'oder', 'aber', 'wenn', 'weil', 'dass', 'ob',
        'nicht', 'auch', 'noch', 'schon', 'nur', 'mehr',
    }

    # Known proper names, product names, navigational terms — not typos
    KNOWN_PROPER = {
        'Tuk', 'Porto', 'Oporto', 'Madeira', 'Tenerife', 'Lapland',
        'Teide', 'Douro', 'Ribeira', 'Prova', 'Aurora', 'Morgengrauen',
        'Vergleich', 'Sumiller', 'Guía', 'Temporada', 'Jahreszeit',
        'Selbstgeführt', 'Individuell', 'Adventure', 'Season', 'Criteria',
        'Fitnesslevel', 'Dawn', 'Custom', 'Precio', 'Links',
    }

    for root, dirs, files in os.walk(site_dir):
        dirs[:] = [d for d in dirs if 'backup' not in d and not d.startswith('.')]
        for fn in files:
            if not fn.endswith('.html'):
                continue
            if os.path.basename(fn) in utility_names:
                continue

            filepath = os.path.join(root, fn)
            rel = os.path.relpath(filepath, site_dir)

            try:
                with open(filepath, 'r') as f:
                    content = f.read()
            except Exception:
                continue

            # Extract editorial text (skip nav, footer, scripts, style, JSON-LD)
            main_match = re.search(r'<main[^>]*>(.*?)</main>', content, re.DOTALL)
            if not main_match:
                continue

            body = main_match.group(1)
            text = re.sub(r'<script[^>]*>.*?</script>', ' ', body, flags=re.DOTALL)
            text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.DOTALL)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()

            # Exact consecutive function-word repeats only ("the the", "and and")
            for m in CONSECUTIVE_REPEAT.finditer(text):
                word = m.group(1)
                # Only report function words (not content words or proper nouns)
                if word[0].isupper() and word.lower() not in FUNCTION_WORDS:
                    continue  # Skip proper nouns
                if word.lower() not in {w.lower() for w in FUNCTION_WORDS}:
                    continue  # Skip content words (legitimate repetition in prose)
                ctx_start = max(0, m.start() - 30)
                ctx_end = min(len(text), m.end() + 30)
                ctx = text[ctx_start:ctx_end].strip()
                findings.append({
                    "file": rel, "type": "repeated_word", "word": word,
                    "context": f"...{ctx}...",
                })

    return findings


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    do_fix = "--fix" in sys.argv

    if do_fix:
        sys.argv.remove("--fix")

    sites = get_sites(target)
    if not sites:
        print(json.dumps({"error": f"no active sites found for target '{target}'"}))
        sys.exit(1)

    all_entity_issues = []
    all_typo_issues = []
    exit_code = 0

    for site_id, site_path in sites:
        # --- Entity consistency ---
        result = scan_entity_consistency(site_path, site_id)
        if result is None:
            continue

        entity_issues = []
        config = SITE_AUTHORS.get(site_id, {})

        # Check author variants — all should match canonical or a single short form
        variants = result.get('author_variants_found', {})
        if len(variants) > 1:
            most_common = config.get('canonical', '')
            for variant, count in variants.items():
                if most_common and variant != most_common:
                    entity_issues.append({
                        "site": site_id,
                        "type": "author_variant",
                        "canonical": most_common,
                        "found": variant,
                        "count": count,
                    })

        # Check years-of-experience contradictions
        contradictions = result.get('suspect_claim_contradictions', [])
        if contradictions:
            entity_issues.append({
                "site": site_id,
                "type": "years_contradiction",
                "values": [{c['value']: [claim[1] for claim in c['claims']]} for c in contradictions],
                "files": [f for c in contradictions for f, _ in c['claims']],
            })

        # Check pronoun mixing
        pronoun = result.get('pronoun_counts', {})
        mixed = pronoun.get('mixed_pages', [])
        if mixed:
            entity_issues.append({
                "site": site_id,
                "type": "pronoun_mixing",
                "message": f"I and we both used on {len(mixed)} pages",
                "files": mixed,
            })

        if entity_issues:
            all_entity_issues.extend(entity_issues)
            exit_code = 1

        # --- Typo scan ---
        typos = scan_typos(site_path)
        for t in typos:
            t["site"] = site_id
        if typos:
            all_typo_issues.extend(typos)
            exit_code = 1

    # Output
    output = {
        "status": "fail" if exit_code else "pass",
        "entity_consistency": all_entity_issues,
        "typos": all_typo_issues,
    }

    if all_entity_issues:
        output["entity_summary"] = f"{len(all_entity_issues)} entity consistency issues found"
    else:
        output["entity_summary"] = "All entity claims consistent ✅"

    if all_typo_issues:
        output["typo_summary"] = f"{len(all_typo_issues)} typo/spelling issues found"
    else:
        output["typo_summary"] = "No typos found ✅"

    print(json.dumps(output, indent=2, ensure_ascii=False))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
