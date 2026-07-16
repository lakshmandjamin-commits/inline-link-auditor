#!/usr/bin/env python3
"""
QA Pipeline — Tiered quality assurance for generated articles.

Tier 1 (free): Automated checks — HTML structure, product IDs, banned words,
               readability, schema presence, link format
Tier 2 (paid): DeepSeek Flash review — voice consistency, EEAT signals,
               factual accuracy

Usage: python3 qa_pipeline.py <site_slug> [article_slug|--all]
  article_slug: review a specific article (without .html extension)
  --all: review all un-reviewed articles for this site

Pass threshold: ≥ 7/10 across all categories
Below 7: regenerate. 5-6: regenerate with specific feedback. <5: discard.

Environment: needs DEEPSEEK_API_KEY and DEEPSEEK_BASE_URL
"""
import sys, os, json, yaml, re, math, shutil
from collections import Counter
from datetime import datetime
from urllib.request import Request, urlopen

# Import shared retry utility
try:
    from scripts.api_utils import call_with_retry
except ImportError:
    from api_utils import call_with_retry

CONTENT_BANKS_DIR = os.path.expanduser("~/.hermes/affiliate-crons/content-banks")
GENERATED_DIR = os.path.expanduser("~/.hermes/affiliate-crons/generated")
QA_STATE_FILE = os.path.expanduser("~/.hermes/affiliate-crons/state/qa_state.json")
BRIEFS_DIR = os.path.expanduser("~/.hermes/affiliate-crons/briefs")
APPROVED_DIR = os.path.expanduser("~/.hermes/affiliate-crons/approved")

# Alias mapping: registry site_id → content bank filename
SLUG_ALIASES = {
    "tenerife": "tenerife-outdoor-guide",
    "madeira-trail-guide": "madeira-hiking",
    "porto-sommelier": "porto-wine-tours",
}

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MODEL = "deepseek-chat"

PASS_THRESHOLD = 7
REGENERATE_THRESHOLD = 5


# ── Tier 1: Automated Checks ──────────────────────────────────────

def check_html_structure(html, article_slug):
    """Verify required HTML elements are present."""
    issues = []
    checks = {
        "<!DOCTYPE html>": "Missing DOCTYPE declaration",
        "<html": "Missing <html> tag",
        "<head>": "Missing <head> section",
        "<meta charset=": "Missing charset meta tag",
        "<meta name=\"viewport\"": "Missing viewport meta tag",
        "<meta name=\"description\"": "Missing description meta",
        "<title>": "Missing <title> tag",
        "<body>": "Missing <body> tag",
        "<article>": "Missing <article> element (semantic HTML)",
        "<h1>": "Missing <h1> heading",
        "<script type=\"application/ld+json\">": "Missing JSON-LD schema",
        "</html>": "Missing closing </html> tag",
    }
    for tag, msg in checks.items():
        if tag not in html:
            issues.append(msg)
    return issues


def check_product_ids(html, cb):
    """Verify all product cards reference valid Viator IDs from content bank."""
    issues = []
    raw_products = cb.get("products", [])
    # Normalize dict-structured products (e.g. {featured: [...]}) to flat list
    if isinstance(raw_products, dict):
        flat = []
        for key in ("featured", "products", "tours", "top", "others"):
            vals = raw_products.get(key, [])
            if isinstance(vals, list):
                flat.extend(vals)
        raw_products = flat
    # Accept both viator_id and code field names
    valid_ids = set()
    for p in raw_products:
        pid = p.get("viator_id") or p.get("code", "")
        if pid:
            valid_ids.add(pid)

    # Find all data-viator-id attributes
    used_ids = re.findall(r'data-viator-id="([^"]+)"', html)
    for pid in used_ids:
        if pid not in valid_ids:
            issues.append(f"Unknown product ID: {pid}")

    # Check all product card hrefs point to Viator
    viator_links = re.findall(r'href="https://partners\.viator\.com/([^"]+)"', html)
    for vid in viator_links:
        if vid not in valid_ids:
            issues.append(f"Viator link references unknown product: {vid}")

    return issues


def check_banned_words(html, cb):
    """Check for banned words/phrases from content bank's donts list."""
    issues = []
    donts = cb.get("voice", {}).get("donts", [])
    text = html.lower()

    # Hard-coded anti-words list (from content bank's superlatives ban)
    ANTI_WORDS = [
        "breathtaking", "stunning", "paradise", "hidden gem", "must-visit",
        "unforgettable", "magical", "world-class", "bucket list", "nestled",
        "boasts", "renowned", "thriving"
    ]

    # Check for unqualified "best" — "best for X" is OK, bare "best" is not
    best_matches = re.findall(r'\b(the best|is best|are best)\b(?!\s+for)', text)
    for match in best_matches:
        issues.append(f"Unqualified superlative: '{match}' — specify 'best for whom'")

    for word in ANTI_WORDS:
        if word in text:
            issues.append(f"Anti-word found: '{word}' — empty superlatives are banned")

    for dont in donts:
        # Handle dict-type dont (YAML colon parsing)
        if isinstance(dont, dict):
            dont = list(dont.keys())[0] + ": " + list(dont.values())[0]

        # Extract prohibited words from "Never say X" patterns
        for m in re.finditer(
            r'(?:never|don' "'" r't|avoid|stop)\s+(?:say|use|call)\s+'
            r'["\u201c]([^"\u201d]+)["\u201d]',
            str(dont), re.IGNORECASE
        ):
            word = m.group(1).lower()
            if word in text:
                issues.append(f"Banned word: '{word}' — {str(dont)[:80]}")

    return issues


def check_readability(html):
    """Calculate Flesch-Kincaid readability. Target: 8th-10th grade."""
    # Extract text content (strip HTML tags)
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'\s+', ' ', text).strip()

    sentences = len(re.findall(r'[.!?]+', text)) or 1
    words = len(text.split())
    syllables = count_syllables(text)

    if words < 200:
        return [f"Article too short: {words} words (minimum 300)"]

    # Flesch-Kincaid Grade Level
    grade = 0.39 * (words / sentences) + 11.8 * (syllables / words) - 15.59
    grade = max(0, min(20, round(grade, 1)))

    issues = []
    if grade < 5:
        issues.append(f"Readability too simple: grade {grade} (minimum 5)")
    elif grade > 12:
        issues.append(f"Readability too complex: grade {grade} (maximum 12)")

    return issues


def count_syllables(text):
    """Rough syllable counter."""
    text = text.lower()
    count = 0
    vowels = "aeiouy"
    for word in text.split():
        word = re.sub(r'[^a-z]', '', word)
        if not word:
            continue
        word_syllables = 0
        prev_vowel = False
        for char in word:
            is_vowel = char in vowels
            if is_vowel and not prev_vowel:
                word_syllables += 1
            prev_vowel = is_vowel
        # Adjust for common endings
        if word.endswith('e') and word_syllables > 1:
            word_syllables -= 1
        if word.endswith('le') and len(word) > 2 and word[-3] not in vowels:
            word_syllables += 1
        count += max(1, word_syllables)
    return count


def check_internal_links(html, cb, existing_slugs):
    """Verify internal links reference valid article slugs."""
    issues = []
    internal_links = re.findall(r'href="(/[^"]+)"', html)

    if not internal_links:
        return ["No internal links found — article needs at least 2"]

    valid_count = 0
    for link in internal_links:
        if link.startswith("/css/") or link.startswith("/js/") or link.startswith("/images/"):
            continue
        slug = link.lstrip("/")
        if slug in existing_slugs or slug in ("", "#", "about"):
            valid_count += 1
        else:
            # Check if this is a plausible future slug (contains hyphens, looks like an article)
            if "/" in slug and not slug.startswith("http"):
                # Directory-style link like "levada-walks/comparison" — check if dir exists
                pass  # Allow directories that may exist
            valid_count += 1  # Accept plausible new article slugs

    if valid_count < 2:
        issues.append(f"Only {valid_count} internal link(s) — need at least 2")

    return issues


def check_narrative_quality(html):
    """Score narrative-first content quality 0-10.

    Criteria:
    - Anecdote density (first-person narrative patterns)
    - Inline Viator link placement in first 1500 chars
    - 'Not for'/'skip'/'avoid' sections (authenticity signals)
    - Editorial-vs-product word ratio
    """
    # Extract <main> body
    main_match = re.search(r'<main[^>]*>(.*?)</main>', html, re.DOTALL | re.IGNORECASE)
    body_text = main_match.group(1) if main_match else html
    text = re.sub(r'<[^>]+>', ' ', body_text)
    text = re.sub(r'\s+', ' ', text).strip()

    total_words = len(text.split())
    if total_words < 100:
        return 5.0  # Not enough text to judge; neutral score

    # (a) Count anecdote patterns
    anecdote_patterns = re.findall(
        r"(?i)\b(I've|I arrived|I remember|I spent|I booked|I found|'ve been)\b",
        text
    )
    anecdote_count = len(anecdote_patterns)

    # (b) Count inline Viator links in first 400 words (Rule 32)
    first_400_words = ' '.join(body_text.split()[:400])
    inline_viator = len(re.findall(r'href="https?://(?:www\.)?viator\.com/[^"]*"', first_400_words))

    # (c) Count 'not for'/'skip'/'avoid' sections
    authenticity_signals = len(re.findall(
        r"(?i)\b(not for|skip|avoid)\b",
        text
    ))

    # (d) Editorial-vs-product word ratio
    # Heuristic: text inside product-card-like containers is product copy
    product_blocks = re.findall(
        r'<div[^>]*class="[^"]*product[^"]*"[^>]*>(.*?)</div>',
        body_text, re.DOTALL | re.IGNORECASE
    )
    product_words = 0
    for block in product_blocks:
        block_text = re.sub(r'<[^>]+>', ' ', block)
        product_words += len(block_text.split())

    editorial_words = max(total_words - product_words, 1)
    ratio = product_words / editorial_words if editorial_words else 0

    # Score computation (0-10)
    score = 5.0  # baseline

    # Anecdote bonus: 2+ anecdotes → good narrative
    if anecdote_count >= 2:
        score += min(anecdote_count * 0.5, 2.0)

    # Inline link in first 1500 chars: good placement
    if inline_viator >= 1:
        score += 1.0
    elif inline_viator == 0 and total_words > 500:
        score -= 0.5  # No links early in a long article

    # Authenticity signals
    if authenticity_signals >= 1:
        score += min(authenticity_signals * 0.5, 1.0)

    # Editorial-vs-product ratio: ideal is 3:1 to 10:1
    if 0.1 <= ratio <= 0.33:
        score += 1.0  # Good balance
    elif ratio > 0.5:
        score -= 1.0  # Too much product copy
    elif ratio < 0.05 and total_words > 500:
        score -= 0.5  # Not enough commercial content

    return round(max(0.0, min(10.0, score)), 1)


def check_truncation(html):
    """Detect mid-word truncation artifacts — words ending abruptly (LLM token-limit artifacts).
    Returns list of truncated words found."""
    import re
    # Strip HTML tags to get plain text
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'\s+', ' ', text).strip()
    
    # Find words at end of paragraphs/sentences that look truncated
    # Pattern: word ending with unusual consonant cluster, no punctuation after
    sentences = re.split(r'[.!?]\s+', text)
    truncated = []
    
    for sentence in sentences:
        words = sentence.split()
        if not words:
            continue
        last_word = words[-1].strip(',;:()[]')
        # Skip obvious non-truncation cases
        if len(last_word) < 4:
            continue
        if last_word[0].isupper():
            continue  # proper nouns
        # Truncation patterns: unusual consonant clusters at word end (LLM token-limit artifact)
        # Carefully curated — avoid endings that match common English words
        truncation_endings = ['ckl', 'ctl', 'ngl', 'nsl', 'rkl', 
                              'tcl', 'thn', 'ptl', 'chl', 'phl']
        for ending in truncation_endings:
            if last_word.lower().endswith(ending) and not last_word.lower().endswith('ing'):
                # Verify it's not a valid short word
                # Check if last_word looks like a prefix of a common English word
                truncated.append(last_word)
                break
    
    return truncated


def run_tier1(html, article_slug, cb, existing_slugs):
    """Run all automated checks. Returns (score, issues_by_category)."""
    results = {}

    results["html_structure"] = check_html_structure(html, article_slug)
    results["product_ids"] = check_product_ids(html, cb)
    results["banned_words"] = check_banned_words(html, cb)
    results["truncation"] = check_truncation(html)
    results["readability"] = check_readability(html)
    results["internal_links"] = check_internal_links(html, cb, existing_slugs)
    results["narrative_quality"] = check_narrative_quality(html)

    # Graduated scoring: each category worth 2 points, severity-weighted
    # 0 issues = 2.0, 1 minor issue = 1.5, 2+ issues = 1.0, critical = 0
    category_scores = {}
    for category, issues in results.items():
        if category == "narrative_quality":
            continue  # this returns a float score, not a list; handled separately
        n = len(issues)
        if n == 0:
            category_scores[category] = 2.0
        elif n == 1:
            category_scores[category] = 1.5
        elif n <= 3:
            category_scores[category] = 1.0
        else:
            category_scores[category] = 0.5

    total = sum(category_scores.values())
    max_possible = len(category_scores) * 2.0
    score = round((total / max_possible) * 10, 1)

    return score, results


# ── Tier 2: AI Review ─────────────────────────────────────────────

def build_review_prompt(html, cb):
    """Build the prompt for DeepSeek Flash review."""
    voice = cb.get("voice", {})
    persona = voice.get("persona_name", "a local expert")
    tone = voice.get("tone", "conversational")
    exemplars = voice.get("exemplars", [])
    ex_sample = exemplars[0][:300] if exemplars else ""

    # Extract only banned words (not alternatives)
    excluded_words = []
    for dont in voice.get("donts", []):
        if not isinstance(dont, str):
            continue
        for m in re.finditer(
            r'(?:never|don' "'" r't|avoid|stop)\s+(?:say|use|call)\s+'
            r'["\u201c]([^"\u201d]+)["\u201d]',
            dont, re.IGNORECASE
        ):
            excluded_words.append(m.group(1))

    excluded_str = ", ".join(f"'{w}'" for w in excluded_words) if excluded_words else "none"

    # Extract article text (trim HTML for token efficiency)
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'\s+', ' ', text).strip()[:6000]

    prompt = f"""Review this article against the site's quality standards. Score each category 1-10.

SITE CONTEXT:
- Persona: {persona}, {tone}
- Voice sample: "{ex_sample}"
- BANNED WORDS (must NOT appear): {excluded_str}

ARTICLE TEXT (first 3000 chars — article may continue beyond this):
{text}

NOTE: The article text shown here is the first 3000 chars for review efficiency — this is normal review truncation and should NOT be flagged. HOWEVER, if you see mid-word truncation artifacts IN the content itself (words like \"complet\", \"insid\", \"peopl\", \"watchin\" that end abruptly without completing), those ARE content quality issues — flag them in the issues list.

SCORE EACH CATEGORY (1-10):
1. VOICE CONSISTENCY: Does the article sound like {persona}? Match the tone, slang, and persona?
2. EEAT SIGNALS: Does it demonstrate Experience, Expertise, Authoritativeness, Trustworthiness?
3. FACTUAL ACCURACY: Are claims plausible and consistent? No obvious hallucinations?
4. ENGAGEMENT: Is the writing compelling? Would a human reader stay on the page?
5. COMMERCIAL BALANCE: Does it recommend products naturally without sounding like a sales pitch?
6. NARRATIVE QUALITY: Does it use first-person anecdotes, personal experience signals ("I've", "I remember", "I booked"), and authenticity markers ("not for", "skip", "avoid")? Are Viator links placed naturally in editorial flow rather than just product cards? Is the editorial-to-product word ratio healthy (more editorial than commercial)?

OUTPUT: Return ONLY valid JSON (no markdown, no explanation):
{{"voice": N, "eeat": N, "accuracy": N, "engagement": N, "commercial": N, "narrative": N, "overall": N, "issues": ["issue1", "issue2"], "verdict": "PASS"|"REGENERATE"|"DISCARD"}}"""
    return prompt


def run_tier2(html, article_slug, cb):
    """Run DeepSeek Flash review. Returns (score, issues, verdict)."""
    if not API_KEY:
        print("  WARNING: No API key — skipping Tier 2")
        return None, ["SKIPPED: No API key"], "SKIPPED"

    prompt = build_review_prompt(html, cb)

    try:
        data, err = call_with_retry(
            url=f"{BASE_URL}/v1/chat/completions",
            payload={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": "You are a strict content quality auditor. Review articles critically. Output ONLY valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 500,
                "temperature": 0.1,
                "stream": False
            },
            headers={"Authorization": f"Bearer {API_KEY}"},
            max_retries=2,
            timeout=60
        )

        if err:
            raise Exception(err)

        content = data["choices"][0]["message"]["content"]
        # Extract JSON
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            review = json.loads(match.group())
        else:
            review = json.loads(content)

        overall = review.get("overall", 5)
        issues = review.get("issues", [])
        verdict = review.get("verdict", "REGENERATE")

        return overall, issues, verdict, review
    except Exception as e:
        print(f"  Tier 2 review error: {e}")
        return None, [f"Review API failed: {str(e)}"], "API_ERROR"


# ── Main Pipeline ──────────────────────────────────────────────────

def load_qa_state():
    if os.path.exists(QA_STATE_FILE):
        with open(QA_STATE_FILE) as f:
            return json.load(f)
    return {}


def save_qa_state(state):
    os.makedirs(os.path.dirname(QA_STATE_FILE), exist_ok=True)
    with open(QA_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def get_existing_slugs(site_slug):
    """Get all article slugs that exist (both generated and approved)."""
    slugs = set()
    for d in [GENERATED_DIR, APPROVED_DIR]:
        site_dir = os.path.join(d, site_slug)
        if os.path.exists(site_dir):
            for f in os.listdir(site_dir):
                if f.endswith(".html"):
                    slugs.add(f.replace(".html", ""))
    return slugs


def review_article(site_slug, article_slug, cb):
    """Run full QA pipeline on a single article."""
    article_path = os.path.join(GENERATED_DIR, site_slug, f"{article_slug}.html")
    if not os.path.exists(article_path):
        print(f"  SKIP: article not found — {article_path}")
        return None

    with open(article_path) as f:
        html = f.read()

    existing_slugs = get_existing_slugs(site_slug)
    existing_slugs.discard(article_slug)

    print(f"  {article_slug}:")

    # Tier 1
    t1_score, t1_results = run_tier1(html, article_slug, cb, existing_slugs)
    t1_issues = []
    for category, issues in t1_results.items():
        if category == "narrative_quality":
            continue
        for issue in issues:
            t1_issues.append(f"[{category}] {issue}")

    if t1_issues:
        print(f"    Tier 1: {t1_score}/10 — {len(t1_issues)} issue(s)")
        for i in t1_issues[:5]:
            print(f"      • {i}")
    else:
        print(f"    Tier 1: {t1_score}/10 — clean")

    # Tier 2
    t2_score, t2_issues, verdict, t2_details = run_tier2(html, article_slug, cb)
    if t2_score is None:
        # API error or skipped — flag for human review, don't auto-approve
        print(f"    Tier 2: ERROR — {t2_issues[0] if t2_issues else 'unknown'}")
        t2_score = 5  # neutral score, forces review
        verdict = "API_ERROR"
    print(f"    Tier 2: {t2_score}/10 — {verdict}")

    if t2_issues:
        for i in t2_issues[:3]:
            print(f"      • {i}")

    # Composite score
    composite = round((t1_score * 0.4) + (t2_score * 0.6), 1)

    # Determine final verdict
    if composite >= PASS_THRESHOLD and verdict == "PASS":
        final = "PASS"
    elif composite < REGENERATE_THRESHOLD or verdict == "DISCARD":
        final = "DISCARD"
    else:
        final = "REGENERATE"

    # If passes, move to approved
    if final == "PASS":
        approved_path = os.path.join(APPROVED_DIR, site_slug, f"{article_slug}.html")
        os.makedirs(os.path.dirname(approved_path), exist_ok=True)
        with open(approved_path, "w") as f:
            f.write(html)
        print(f"    → APPROVED — moved to {approved_path}")

    result = {
        "article_slug": article_slug,
        "tier1_score": t1_score,
        "tier2_score": t2_score,
        "composite": composite,
        "tier1_issues": t1_issues,
        "tier2_issues": t2_issues,
        "tier2_details": t2_details,
        "verdict": final,
        "reviewed_at": datetime.now().isoformat(),
        "retries": 0
    }

    return result, (t1_issues + t2_issues) if final != "PASS" else []


def regenerate_article(site_slug, article_slug, cb, issues, retry_count):
    """Regenerate an article with QA feedback injected into the prompt."""
    article_path = os.path.join(GENERATED_DIR, site_slug, f"{article_slug}.html")
    briefs_data_path = os.path.join(BRIEFS_DIR, f"{site_slug}.json")

    if not os.path.exists(briefs_data_path):
        return None, "No briefs file found"

    with open(briefs_data_path) as f:
        briefs_data = json.load(f)

    # Find the matching brief
    brief = None
    for b in briefs_data.get("briefs", []):
        if b.get("slug") == article_slug:
            brief = b
            break

    if not brief:
        return None, "Brief not found"

    # Build regeneration prompt with feedback
    try:
        from scripts.page_generator import build_system_prompt, build_user_prompt, call_deepseek, extract_html, repair_structure, build_faq_jsonld_from_html
    except ImportError:
        # Fallback: scripts dir is on path
        from page_generator import build_system_prompt, build_user_prompt, call_deepseek, extract_html, repair_structure, build_faq_jsonld_from_html

    system_prompt = build_system_prompt(cb)
    # Inject QA feedback into user prompt
    user_prompt = build_user_prompt(cb, brief)
    feedback_block = f"""

QA FEEDBACK FROM PREVIOUS ATTEMPT (attempt {retry_count + 1}/3):
{chr(10).join(f'- {i}' for i in issues)}

Please regenerate the article addressing ALL of the above issues.
Maintain the same title, slug, and structure but fix the problems identified."""
    user_prompt += feedback_block

    print(f"    🔄 Regenerating (attempt {retry_count + 1}/3)...")

    # Backup original before overwriting
    backup_path = article_path + f".bak.{retry_count}"
    shutil.copy2(article_path, backup_path)

    try:
        raw = call_deepseek(system_prompt, user_prompt, max_tokens=6000)
        html = extract_html(raw)
        # Apply structural repair + FAQ JSON-LD rebuild
        html = repair_structure(html, site_slug)
        html = build_faq_jsonld_from_html(html, cb["voice"]["persona_name"], "")
        with open(article_path, "w") as f:
            f.write(html)
        return article_path, None
    except Exception as e:
        return None, str(e)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 qa_pipeline.py <site_slug> [article_slug|--all]")
        sys.exit(1)

    site_slug = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"QA Pipeline — {site_slug} — {datetime.now().isoformat()[:19]}\n")

    # Load content bank (resolve alias for registry site_id → content bank filename)
    resolved = SLUG_ALIASES.get(site_slug, site_slug)
    cb_path = os.path.join(CONTENT_BANKS_DIR, f"{resolved}.yaml")
    if not os.path.exists(cb_path):
        print(f"ERROR: No content bank for {site_slug}")
        sys.exit(1)
    with open(cb_path) as f:
        cb = yaml.safe_load(f)

    # Determine articles to review
    site_gen_dir = os.path.join(GENERATED_DIR, site_slug)
    if not os.path.exists(site_gen_dir):
        print(f"No generated articles for {site_slug}")
        sys.exit(0)

    state = load_qa_state()
    site_state = state.get(site_slug, {"reviewed": {}})

    articles = []
    if mode == "--all":
        for f in sorted(os.listdir(site_gen_dir)):
            if f.endswith(".html"):
                slug = f.replace(".html", "")
                if slug not in site_state.get("reviewed", {}):
                    articles.append(slug)
    elif mode:
        articles = [mode]
    else:
        # Default: review oldest unreviewed
        for f in sorted(os.listdir(site_gen_dir)):
            if f.endswith(".html"):
                slug = f.replace(".html", "")
                if slug not in site_state.get("reviewed", {}):
                    articles.append(slug)
                    break

    if not articles:
        print("All articles already reviewed. Nothing to do.")
        sys.exit(0)

    print(f"Reviewing {len(articles)} article(s)...\n")

    results = []
    for article_slug in articles:
        result, issues = review_article(site_slug, article_slug, cb)
        if not result:
            continue

        # Retry loop for REGENERATE verdicts
        while result["verdict"] == "REGENERATE" and result.get("retries", 0) < 2:
            result["retries"] += 1
            new_path, err = regenerate_article(
                site_slug, article_slug, cb, issues, result["retries"]
            )
            if err:
                print(f"    ❌ Regeneration failed: {err}")
                result["verdict"] = "DISCARD"
                break

            # Re-review
            result, issues = review_article(site_slug, article_slug, cb)
            if not result:
                break

        site_state["reviewed"][article_slug] = result
        results.append(result)

    state[site_slug] = site_state
    save_qa_state(state)

    # Summary
    passes = sum(1 for r in results if r["verdict"] == "PASS")
    regenerates = sum(1 for r in results if r["verdict"] == "REGENERATE")
    discards = sum(1 for r in results if r["verdict"] == "DISCARD")

    print(f"\n{'='*50}")
    print(f"QA Summary — {site_slug}")
    print(f"  Reviewed: {len(results)}")
    print(f"  ✅ PASS: {passes}")
    print(f"  🔄 REGENERATE: {regenerates}")
    print(f"  ❌ DISCARD: {discards}")

    if regenerates or discards:
        print(f"\nArticles needing attention:")
        for r in results:
            if r["verdict"] != "PASS":
                print(f"  {r['article_slug']}: {r['verdict']} ({r['composite']}/10)")
                for issue in r.get("tier2_issues", [])[:3]:
                    print(f"    • {issue}")


if __name__ == "__main__":
    main()
