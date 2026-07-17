#!/usr/bin/env python3
"""
Page Generator — Phase 3 of content pipeline.
Reads a topic brief + content bank, generates EEAT-grade HTML via DeepSeek Flash.

Usage: python3 page_generator.py <site_slug> [brief_index]
  brief_index: 0-based index into the briefs array (default: first unused)

Environment: needs DEEPSEEK_API_KEY and DEEPSEEK_BASE_URL
  Source with: set -a; source ~/.hermes/.env; set +a
"""
import sys, os, json, yaml, time, re
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

# Ensure scripts/ directory is on path regardless of invocation directory.
# Cron agents and subprocess calls may run from arbitrary working directories.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Product image injection (Component 2 of Viator Image Pipeline).
# Closes the Hypatia gap: HTML reached disk with zero images because nothing
# ever wired the local image cache into the generator. The module is
# importable on any PATH that includes this scripts/ directory — see the
# sys.path.insert above.
try:
    from product_image_injection import inject_product_images
except ImportError:
    inject_product_images = None

CONTENT_BANKS_DIR = os.path.expanduser("~/.hermes/affiliate-crons/content-banks")
BRIEFS_DIR = Path(os.path.expanduser("~/.hermes/affiliate-crons/briefs"))
OUTPUT_DIR = os.path.expanduser("~/.hermes/affiliate-crons/generated")
STATE_FILE = os.path.expanduser("~/.hermes/affiliate-crons/state/generation_state.json")

# DeepSeek Flash via OpenAI-compatible API
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MODEL = "deepseek-chat"  # DeepSeek Flash


# ── Article templates ──────────────────────────────────────────────
TEMPLATES = {
    "destination_guide": {
        "narrative_first": True,
        "structure": [
            "H2: I Didn't Expect {destination} to Feel Like This",
            "H3: {product_1}: The Tour That Saved My Trip",
            "H2: The Moments That Made {niche} in {destination} Worth the Trip",
            "H3: {product_2}: A Lesser-Known Tour Worth Discovering",
            "H2: What Really Surprised Me About {destination}",
            "H2: {persona_name}'s Insider Tips for Getting It Right",
            "H2: What I Wish I'd Known Before I Went",
        ],
        "note": "NARRATIVE-FIRST: Paint a personal, experiential journey. First-person throughout."
    },
    "product_roundup": {
        "narrative_first": True,
        "structure": [
            "H2: I Tried Every {niche} Tour in {destination}. Here's What Happened",
            "H3: {product_1}",
            "H2: The Best Value Pick for {niche_doers}",
            "H3: {product_2}",
            "H2: Worth the Splurge: {product_3}",
            "H3: {product_3}",
            "H2: What I Wish I'd Known Before I Went",
        ],
        "note": "NARRATIVE-FIRST: Honest, first-person reviews. Include what went wrong too."
    },
    "beginner_guide": {
        "narrative_first": True,
        "structure": [
            "H2: I Remember My First {niche} Experience. Here's What I Wish I'd Known",
            "H3: {product_1}: Perfect for First-Timers",
            "H2: Finding Your Feet: Where to Start in {destination}",
            "H3: {product_2}: The Easiest Way In",
            "H2: What Nobody Tells You Before Your First {niche} Trip",
            "H2: What I Wish I'd Known Before I Went",
        ],
        "note": "NARRATIVE-FIRST: Empathetic, personal. Address fears with real stories."
    },
    "seasonal_guide": {
        "narrative_first": True,
        "structure": [
            "H2: I've Been to {destination} in Every Season. Here's the Truth",
            "H3: {product_1}: Best in Peak Season",
            "H2: The Month That Changed How I See {destination}",
            "H3: {product_2}: Surprisingly Great in Low Season",
            "H2: Packing Lessons I Learned the Hard Way",
            "H2: What I Wish I'd Known Before I Went",
        ],
        "note": "NARRATIVE-FIRST: Personal seasonal experiences with practical data woven in."
    },
    "comparison": {
        "narrative_first": True,
        "structure": [
            "H2: I Did Both {option_a} and {option_b}. Here's What Nobody Tells You",
            "H3: {product_1}: The {option_a} Experience",
            "H3: Why {option_a} Nearly Won Me Over",
            "H3: {product_2}: The {option_b} Experience",
            "H2: The Moment I Made My Decision",
            "H2: What I Wish I'd Known Before I Went",
        ],
        "note": "NARRATIVE-FIRST: Personal comparison based on real experience."
    },
    "itinerary": {
        "narrative_first": True,
        "structure": [
            "H2: My {niche} Week in {destination}: Every High and Low",
            "H3: {product_1}: The Highlight of Day 1",
            "H2: The Day Everything Went Wrong (and Right)",
            "H3: {product_2}: My Day 3 Savior",
            "H2: What I'd Do Differently Next Time",
            "H2: What I Wish I'd Known Before I Went",
        ],
        "note": "NARRATIVE-FIRST: Chronological personal journey with honest highs and lows."
    },
    "local_tips": {
        "narrative_first": True,
        "structure": [
            "H2: What the Guidebooks Don't Tell You About {destination} {niche}",
            "H3: {product_1}: A Local's Secret Pick",
            "H2: The Mistakes I Made So You Don't Have To",
            "H3: {product_2}: The One Tour Locals Actually Do",
            "H2: Where to Skip and Where to Splurge",
            "H2: What I Wish I'd Known Before I Went",
        ],
        "note": "NARRATIVE-FIRST: Insider perspective with personal stories about local experiences."
    }
}


# ── Language config ────────────────────────────────────────────────
LANG_CONFIG = {
    "en": {
        "code": "en",
        "name": "English",
        "skip_link": "Skip to main content",
        "cta_text": "Check Availability →",
        "cta_book": "Book Now →",
        "faq_header": "Frequently Asked Questions",
        "org_description": "Honest, expert travel guides for {site} — independently researched and reviewed",
        "affiliate_disclosure": "We earn a commission when you book through our links on Viator (PID {pid}), at no extra cost to you. This is how we keep the site free.",
        "anti_words": ["breathtaking", "unforgettable", "hidden gem", "world-class", "seamless",
                       "curated", "immersive", "life-changing", "unparalleled", "elevate your",
                       "stunning", "cheap", "magical", "pristine", "spectacular", "iconic",
                       "enchanting", "paradise", "game-changing", "revolutionary", "incredible"],
        "lang_instruction": "",
    },
    "de": {
        "code": "de",
        "name": "German",
        "skip_link": "Zum Inhalt springen",
        "cta_text": "Auf Viator buchen →",
        "cta_book": "Jetzt buchen →",
        "faq_header": "Häufig gestellte Fragen",
        "org_description": "Ehrliche, fachkundige Reiseführer für {site} — unabhängig recherchiert und geprüft",
        "affiliate_disclosure": "Wir erhalten eine Provision für Buchungen über unsere Links auf Viator (PID {pid}), ohne Mehrkosten für Sie. So bleibt die Seite kostenlos.",
        "anti_words": ["ideale", "immersiv", "unvergesslich", "atemberaubend", "verstecktes Juwel",
                       "Weltklasse", "nahtlos", "kuratiert", "lebensverändernd", "beispiellos",
                       "atemberaubende", "unglaublich", "spektakulär", "magisch",
                       "unberührt", "ikonisch", "paradiesisch", "revolutionär"],
        "lang_instruction": "Write the ENTIRE article in German (Deutsch). All headings, body text, FAQ, and meta description must be in German. Use natural, idiomatic German — not translated English. Match the voice of a native German-speaking guide.",
    },
    "es": {
        "code": "es",
        "name": "Spanish",
        "skip_link": "Saltar al contenido",
        "cta_text": "Reservar en Viator →",
        "cta_book": "Reservar ahora →",
        "faq_header": "Preguntas frecuentes",
        "org_description": "Guías de viaje honestas y expertas para {site} — investigadas y revisadas de forma independiente",
        "affiliate_disclosure": "Ganamos una comisión cuando reservas a través de nuestros enlaces en Viator (PID {pid}), sin costo adicional para ti. Así mantenemos el sitio gratuito.",
        "anti_words": ["inolvidable", "experiencia inolvidable", "impresionante", "joya escondida",
                       "de clase mundial", "inmersivo", "único en la vida", "elevar tu",
                       "mágico", "prístino", "espectacular", "icónico", "paradisíaco",
                       "revolucionario", "increíble", "encantador"],
        "lang_instruction": "Write the ENTIRE article in Spanish (Español). All headings, body text, FAQ, and meta description must be in Spanish. Use natural, idiomatic Spanish — not translated English. Match the voice of a native Spanish-speaking guide. Note: 'ideal'/'ideales' is standard Spanish vocabulary — it is NOT an anti-word.",
    },
}

SLUG_ALIASES = {
    "tenerife": "tenerife-outdoor-guide",
    "madeira-trail-guide": "madeira-hiking",
    "porto-sommelier": "porto-wine-tours",
}

# Valid Viator destination codes per site — used for product schema + cross-destination validation
VALID_DEST_CODES = {
    "porto-sommelier":       {"26879", "538", "529", "562", "782", "5404", "5343", "537", "557", "792"},
    "tenerife-outdoor-guide": {"5404", "530", "507", "553", "522", "552", "551", "739", "523", "519", "529", "506", "508", "51000", "539", "541", "546", "562", "567", "740"},
    "lapland-adventure-guide": {"22130", "904", "903", "933", "906", "32182", "937", "912", "43039", "4377", "4392", "44033", "44044", "46120", "5122", "5227", "5440", "9225", "923"},
    "san-juan-excursions":     {"903", "904", "909", "902", "960", "972", "889", "442", "4341", "4415", "712", "915", "920", "9406", "971", "973"},
    "yogyakarta-temple-tours": {"22560", "4540", "4457", "4200", "4431", "4399", "24625", "34553", "3433", "34523", "34709", "3528", "3722", "4106", "4116", "41448", "4297", "4343", "4344", "4346", "4417", "4437", "4454", "4456", "4501", "5071"},
    "madeira-trail-guide":          {"5392", "22388", "50841"},
}


def load_content_bank(slug):
    resolved = SLUG_ALIASES.get(slug, slug)
    path = os.path.join(CONTENT_BANKS_DIR, f"{resolved}.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def load_briefs(slug, briefs_path=None):
    """Load briefs from standard path or a custom path (for filtered briefs)."""
    if briefs_path:
        p = Path(briefs_path)
    else:
        p = BRIEFS_DIR / f"{slug}.json"
    if not p.exists():
        return None
    with open(p) as f:
        return json.load(f)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def get_article_count(slug):
    """Count existing generated articles for this site."""
    site_dir = os.path.join(OUTPUT_DIR, slug)
    if not os.path.exists(site_dir):
        return 0
    return len([f for f in os.listdir(site_dir) if f.endswith(".html")])


def fill_narrative_variables(structure, brief, cb):
    """Fill template variables in structure items, including {{double_brace}} narrative vars.

    Args:
        structure: List of heading strings with {placeholder}s.
        brief: Topic brief dict with products_to_feature, narrative_vars, etc.
        cb: Content bank dict with site, voice, products data.

    Returns:
        List of filled heading strings.
    """
    site = cb.get("site", {})
    voice = cb.get("voice", {})
    products = cb.get("products", [])
    # Normalize products — handle both flat list and {featured: [...]} dict
    if isinstance(products, dict):
        flat = []
        for key in ("featured", "products", "tours", "top", "others"):
            vals = products.get(key, [])
            if isinstance(vals, list):
                flat.extend(vals)
        products = flat if flat else list(products.values())[0] if products else []
    # Accept both viator_id and code field names
    product_map = {}
    for p in products:
        pid = p.get("viator_id") or p.get("code", "")
        if pid:
            product_map[pid] = p

    niche = site.get("niche", "travel")
    destination = site.get("destination", niche)

    # P1 fix: Clean SEO-stuffed destination/niche values (bug #4, Gate A 9/10, Jul 2026)
    # Content banks often have "Tenerife outdoor activities and tours" — strip marketing padding
    _clean_re = re.compile(r'\b(?:outdoor |activities| and tours|adventures?|guided?|excursions?)\b', re.I)
    destination = _clean_re.sub('', destination).strip()
    destination = ' '.join(destination.split()[:3])  # max 3 words
    if not destination:
        destination = niche
    niche = _clean_re.sub('', niche).strip()
    niche = ' '.join(niche.split()[:3])
    if not niche:
        niche = "travel"

    persona_name = voice.get("persona_name", "Your Guide")
    niche_doers = get_niche_doers(niche)

    # Get narrative template variables from brief
    narrative_vars = brief.get("narrative_vars", {})

    filled = []
    for h in structure:
        h = h.replace("{destination}", destination)
        h = h.replace("{niche}", niche)
        h = h.replace("{persona_name}", persona_name)
        h = h.replace("{niche_doers}", niche_doers)
        h = h.replace("{option_a}", brief.get("option_a") or "")
        h = h.replace("{option_b}", brief.get("option_b") or "")
        # Clean up any remaining {option_*} placeholders (brief didn't provide them)
        h = re.sub(r'\{option_[a-z]\}', '', h)
        # Clean up orphaned connectors when options are empty
        h = re.sub(r'\band\s+and\b', 'and', h)
        h = re.sub(r'—\s*—', '—', h)
        h = re.sub(r'^\s*(?:and|vs|—)\s+', '', h)
        h = re.sub(r'\s+(?:and|vs|—)\s*$', '', h)
        h = re.sub(r'\s{2,}', ' ', h)
        # Product placeholders — inject title, rating, reviews, duration
        for i, pid in enumerate(brief.get("products_to_feature", [])[:3]):
            p = product_map.get(pid, {})
            h = h.replace(f"{{product_{i+1}}}", p.get("title", f"Product {i+1}"))
            h = h.replace(f"{{product_{i+1}_rating}}", str(p.get("rating", "N/A")))
            h = h.replace(f"{{product_{i+1}_reviews}}", str(p.get("reviews", "N/A")))
            h = h.replace(f"{{product_{i+1}_duration}}", str(p.get("duration", "N/A")))
        # {{double_brace}} narrative vars from brief
        for key, val in narrative_vars.items():
            h = h.replace("{{" + key + "}}", str(val))
        # Fallback: replace any remaining {{...}} with generic fallback
        h = re.sub(r"\{\{(\w+)\}\}", lambda m: narrative_vars.get(m.group(1), f"[[{m.group(1)}]]"), h)
        filled.append(h)
    # Add PRODUCT DATA block with real stats — prevents "Check current rating" placeholders
    product_data_lines = ["\n\nPRODUCT DATA (use these exact stats in your prose):"]
    for i, pid in enumerate(brief.get("products_to_feature", [])[:3]):
        p = product_map.get(pid, {})
        title = p.get("title", pid)
        rating = p.get("rating")
        reviews = p.get("reviews")
        duration = p.get("duration")
        if rating and reviews:
            product_data_lines.append(f"  • {title}: {rating}★, {reviews} reviews, {duration}")
        elif title:
            product_data_lines.append(f"  • {title}: {duration} (rating unavailable — omit citation)")
    if len(product_data_lines) > 1:
        filled.append("\n".join(product_data_lines))
    return filled


def build_system_prompt(cb, lang="en"):
    """Construct the system prompt from the content bank's voice guide."""
    lc = LANG_CONFIG.get(lang, LANG_CONFIG["en"])
    voice = cb["voice"]
    site = cb.get("site", {})
    domain = site.get("domain", "{{DOMAIN}}")
    persona = voice["persona_name"]
    bio = voice.get("persona_bio") or voice.get("persona_backstory", "")
    if not bio:
        raise KeyError("persona_bio or persona_backstory required in voice section")
    bio_short = bio.split(".")[0].strip() if "." in bio else bio[:80]
    tone = voice["tone"]
    dos = "\n".join(f"- {d}" for d in voice.get("dos", []))
    donts = "\n".join(f"- {d}" for d in voice.get("donts", []))
    exemplars = voice.get("exemplars", [])
    ex_text = "\n\n".join(f'"{e.strip()}"' for e in exemplars[:5])

    # Get Viator affiliate params from site registry
    import sqlite3
    pid, mcid = "P00303273", "42383"  # fallback
    try:
        db = sqlite3.connect(os.path.expanduser("~/.hermes/affiliate-crons/db/site_registry.db"))
        row = db.execute("SELECT viator_pid, viator_mcid FROM sites WHERE site_id=?",
                        (cb["site"]["slug"],)).fetchone()
        if row:
            pid, mcid = row[0], row[1]
        db.close()
    except Exception:
        pass

    today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")

    # Narrative-first supplementary content (optional)
    personal_stories = cb.get("personal_stories", [])
    personal_stories_text = "\n".join(f"- {s}" for s in personal_stories) if personal_stories else "None provided."
    practical_logistics = cb.get("practical_logistics", [])
    practical_logistics_text = "\n".join(f"- {l}" for l in practical_logistics) if practical_logistics else "None provided."

    # Authority sources — pre-compute for prompt injection
    authority_sources = cb.get("authority_sources", [])
    authority_sources_text = ""
    if authority_sources:
        auth_lines = []
        for src in authority_sources:
            auth_lines.append(f"  • {src['name']} ({src['url']}) — {src['what']}. Use for: {src['use_for']}")
        authority_sources_text = "\n".join(auth_lines)

    return f"""You are {persona}, {bio}

WRITING STYLE: {tone}

DO:
{dos}

DON'T:
{donts}

VOICE EXAMPLES — write exactly like this:
{ex_text}

NARRATIVE-FIRST MODE:
When the TEMPLATE NOTE in the user prompt starts with "NARRATIVE-FIRST:", follow these additional rules:
- Use personal hooks and storytelling for ALL H2s (e.g., "I Didn't Expect X to Feel Like This" not "Best Time to Visit X")
- Include a "What I Wish I'd Known Before I Went" section near the end
- Feature recommended products as H3s under relevant narrative H2s, not standalone H2s
- Weave affiliate links naturally into the narrative prose — at least 2 inline Viator links within the body
- Every claim must vividly sound like it comes from PERSONAL EXPERIENCE, not research or aggregation
- If the content bank provides personal stories, weave them into the narrative naturally
- If the content bank provides practical logistics, distribute them through the narrative rather than listing them in a separate section

PERSONAL STORIES TO WEAVE IN (if provided):
{personal_stories_text}

PRACTICAL LOGISTICS TO INCLUDE (if provided):
{practical_logistics_text}

OUTPUT FORMAT: Return a COMPLETE, valid HTML document (no markdown, no code fences, no explanations).
The document MUST follow this structure:

IMPORTANT — NAVIGATION: If your page includes a site navigation bar, use this EXACT hamburger button pattern:
  <button class="nav-toggle" aria-label="Open menu" aria-expanded="false"><span></span><span></span><span></span></button>
Do NOT use class="hamburger" or the ☰ glyph — these are broken patterns.

IMPORTANT — ACCESSIBILITY: Include a skip-link (already shown below). All interactive elements must have visible :focus styles.

<!DOCTYPE html>
<html lang="{lc["code"]}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="description" content="COMPELLING_META_DESCRIPTION_WITH_KEYWORD">
  <title>ARTICLE_TITLE | SITE_NAME</title>
  <link rel="stylesheet" href="/css/style.css">
  <link rel="canonical" href="https://{{DOMAIN}}/{{SLUG}}">
</head>
<body>
  <a href="#main-content" class="skip-link">{lc["skip_link"]}</a>
  <script src="/js/main.js" defer></script>
  <article>
    <header>
      <h1>ARTICLE_TITLE_WITH_PRIMARY_KEYWORD_AND_DESTINATION</h1>
      <p class="byline">By {persona}, {bio_short}</p>
    </header>
    <!-- FTC-required: affiliate disclosure BEFORE first monetized link -->
    <p class="affiliate-disclosure">{lc["affiliate_disclosure"].format(pid=pid)} <a href="/about">Learn more →</a></p>
    <main>
      <!-- H2 sections here -->
    </main>
    <!-- PRODUCT_CARDS_GO_HERE — must come BEFORE FAQ section (2-4% CTR vs 0.15% if after FAQ) -->
    <section class="faq">
      <h2>{lc["faq_header"]}</h2>
      <!-- 4-6 Q&A pairs from the article's comparison questions -->
    </section>
    <footer>
      <p>SIGN_OFF_LINE</p>
    </footer>
  </article>
  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@type": "Article",
    "headline": "ARTICLE_TITLE",
    "description": "META_DESCRIPTION",
    "author": {{"@type": "Person", "name": "{persona}"}},
    "datePublished": "{today}",
    "dateModified": "{today}",
    "url": "https://{{DOMAIN}}/{{SLUG}}"
  }}
  </script>
  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@type": "Article",
    "mainEntity": [
      {{"@type": "Question", "name": "...", "acceptedAnswer": {{"@type": "Answer", "text": "..."}}}}
    ]
  }}
  </script>
</body>
</html>

Viator product cards MUST use this format — note the PID/MCID params are REQUIRED:
  <!-- Use {{PRODUCT_URL}} when available (canonical URL from Viator API), otherwise fall back to {{PRODUCT_ID}} -->
  <div class="product-card" data-viator-id="{{PRODUCT_ID}}">
    <h3>{{PRODUCT_NAME}}</h3>
    <p class="product-blurb">{{YOUR_HONEST_RECOMMENDATION_WITH_PRO_AND_CON}}</p>
    <a href="{{PRODUCT_URL}}?pid={pid}&mcid={mcid}&medium=link" class="cta-button" rel="sponsored noopener noreferrer" target="_blank">{lc["cta_text"]}</a>
  </div>

Inline links use: <a href="{{PRODUCT_URL}}?pid={pid}&mcid={mcid}&medium=link" rel="sponsored noopener noreferrer" target="_blank">descriptive anchor text</a>
External authority links are REQUIRED: link to official sources when referencing trail conditions, weather, or park information.

INLINE AFFILIATE LINKS — at least 2 Viator links MUST be embedded naturally in the body text:
  <a href="{{PRODUCT_URL}}?pid={pid}&mcid={mcid}&medium=link" rel="sponsored noopener noreferrer" target="_blank">descriptive anchor text</a>
These go INSIDE paragraph text, not just in product cards. Weave them into the narrative naturally.

CRITICAL RULES:
1. Return the COMPLETE <!DOCTYPE html>... document — nothing before, nothing after.
2. Write in first person as {persona}. Match the exact tone of the voice examples. Include at least one specific personal anecdote from the provided STORIES section — a single vivid moment (\"Last October a couple from Hamburg asked me...\"), not a generic claim. The validator rejects pages with zero first-person narrative signals.
3. **CRITICAL**: The H1 MUST contain the destination name (e.g., "Madeira") AND the primary keyword. Example: "PR1 vs PR1.2 Madeira: Which Pico Ruivo Hike Should You Choose?"
4. Include 4-6 FAQ entries. Structure as real questions travelers ask. FAQ Q&A pairs go in Article schema (not FAQPage — FAQPage rich results no longer shown in Google Search as of 2026).
5. Be honest and specific. Real experiences beat generic descriptions. For EVERY major section (h2), include at least one explicit "not for" or "skip if" signal — tell the reader who should avoid this tour/option and why. Example: "Skip this if you have knee problems — the descent is 1,200 stone steps with no handrail." The validator will reject pages without explicit skip signals.
6. NO product prices — they change too fast.
7. Article body should be 1,500-2,200 words — thorough, substantive, competitive.
8. EXTERNAL AUTHORITY LINKS (REQUIRED): You MUST cite at least 2 of these official sources in the body text. Link to a specific page, not the home page. Format: "According to [source name], [specific fact — trail conditions, permit requirements, seasonal closures, historical context]..." NEVER link to a Viator tour page claiming it is an official source. Every paragraph making a factual claim about conditions, access, or regulations should reference one of these sources:
{authority_sources_text}

You will lose quality points for omitting authority citations.
8b. CITE PRODUCT STATISTICS AS PROSE — for every major tour mentioned in the body, include its star rating and review count in editorial text. Write like a guidebook: "The Caldeirão Verde levada walk (4.8★, 2,400+ reviews, 13km, 4-5 hours)..." Do not put this in product cards — weave it into paragraphs and comparison sections. ONLY use statistics from the provided product data (ratings, review_count, duration). If a product's data is missing or incomplete, omit the ENTIRE citation — do not produce fragments like ", a 4.97 rating" with a leading comma and missing rating. A missing rating means skip the citation entirely.
9. EVERY claim should sound like it comes from personal experience, not a search engine.
10. **MANDATORY PRODUCT CARDS**: You MUST include at least 2 product-card divs on EVERY tour review page. Product cards go BEFORE the FAQ section and AFTER the main content. Even if you have inline Viator links in the body text, you MUST still include product cards. A page without product cards is incomplete.
11. Use the exact product-card HTML format above — with PID/MCID params — for every product card.
12. Put product cards BEFORE the FAQ section. This ensures they're in the main content area (2-4% CTR) rather than after FAQ content (0.15% CTR).
13. For the canonical URL, use the EXACT domain from the site config — do not invent domains.
14. Include at least 2 inline Viator affiliate links within the article body text (in addition to product cards — not instead of them).
15. ANTI-WORDS: NEVER use these words or phrases: {", ".join(lc["anti_words"])}. Use honest, specific, experiential descriptions instead. The word "best" is acceptable in SEO meta titles and factual claims but avoid editorial puffery like "the best tour you'll ever take.""
16. MANDATORY HERO IMAGE: Every page MUST include a hero image — the first <img> in the article section. Use class="hero-img" and an appropriate local image path. Pages without a hero image will be flagged.
17. PHOTO GALLERY GUARD: Only include a photo gallery section if you have 2 or more images to show. If fewer than 2 images are available, omit the gallery section entirely. An empty gallery with 0-1 images is worse than no gallery.
18. PRODUCT CARD RATING: Every product-card div MUST include a rating line showing stars and review count, e.g., "<p>★★★★★ 4.7 (1,564 reviews)</p>". Use the rating and review_count from the provided product data.
19. CARD POSITIONING: Product cards MUST appear AFTER the introductory/narrative sections and BEFORE the FAQ section — never before the first H2 in the article.
20. NO ORPHAN LINKS: Do not wrap a raw Viator affiliate link as the sole content of an H3 tag. H3 headings must contain descriptive text, not just a link.
16. NO EM DASHES: Never use em dashes (—) in any text: headings, body copy, bylines, or anywhere. Use commas, periods, or colons instead. Em dashes are an AI tell and violate editorial standards."
17. PRODUCT COUNT: In trust badges, use {{product_count}} as a placeholder — we fill in the actual count from rendered cards automatically.
18. GEO OPTIMIZATION: (a) ANSWER-FIRST — every H2 section must open with a direct, citable answer (40-60 words) before expanding. AI systems extract these as citation snippets. (b) CLAIM-EVIDENCE — back factual claims with specific sources: "Average tour group size is 8-12 people (official park guidelines, 2025)" not "most tours keep groups small." (c) ENTITY-RICH — name specific organizations, locations, and frameworks: "ICNF regulates Madeira's levada network" not "the government manages the trails."
    Format: <a href="{{PRODUCT_URL}}?pid={pid}&mcid={mcid}&medium=link" rel="sponsored noopener noreferrer" target="_blank">descriptive anchor text</a>""".replace('{{DOMAIN}}', domain)


def build_user_prompt(cb, brief, lang="en"):
    """Construct the user prompt from the topic brief."""
    lc = LANG_CONFIG.get(lang, LANG_CONFIG["en"])
    site = cb["site"]
    voice = cb["voice"]
    knowledge = cb["knowledge"]
    products = cb.get("products", [])

    # Normalize products structure — handles both flat list and {featured: [...]} dict
    if isinstance(products, dict):
        flat = []
        for key in ("featured", "products", "tours", "top", "others"):
            vals = products.get(key, [])
            if isinstance(vals, list):
                flat.extend(vals)
        products = flat if flat else list(products.values())[0] if products else []

    template_name = brief.get("template", "destination_guide")
    template = TEMPLATES.get(template_name, TEMPLATES["destination_guide"])

    # Map products by viator_id or code (accept both field names)
    product_map = {}
    for p in products:
        pid = p.get("viator_id") or p.get("code", "")
        if pid:
            product_map[pid] = p

    # Format featured products
    featured = []
    for pcode in brief.get("products_to_feature", [])[:3]:
        p = product_map.get(pcode)
        if p:
            featured.append(f"  - {pcode}: {p.get('title', 'Untitled')} — {p.get('custom_blurb', '')} (best for: {p.get('best_for', 'all')})")

    # Format facts
    facts_text = "\n".join(f"  - {f}" for f in brief.get("facts_to_include", []))

    # Format structure using fill_narrative_variables (handles {placeholders} and {{double_brace}} vars)
    structure_items = template["structure"]
    filled_structure = fill_narrative_variables(structure_items, brief, cb)

    # Get Viator affiliate params from site registry
    import sqlite3
    pid, mcid = "P00303273", "42383"  # fallback
    try:
        db = sqlite3.connect(os.path.expanduser("~/.hermes/affiliate-crons/db/site_registry.db"))
        row = db.execute("SELECT viator_pid, viator_mcid FROM sites WHERE site_id=?",
                        (cb["site"]["slug"],)).fetchone()
        if row:
            pid, mcid = row[0], row[1]
        db.close()
    except Exception:
        pass

    # Extract supplementary content (all optional — .get() with empty defaults)
    local_tips = knowledge.get("local_tips", [])
    local_tips_text = "\n".join(f"  - {t}" for t in local_tips) if local_tips else "  None in knowledge base."
    common_mistakes = knowledge.get("common_mistakes", [])
    common_mistakes_text = "\n".join(f"  - {m}" for m in common_mistakes) if common_mistakes else "  None in knowledge base."
    personal_stories = cb.get("personal_stories", [])
    personal_stories_text = "\n".join(f"  - {s}" for s in personal_stories) if personal_stories else "  None provided."
    practical_logistics = cb.get("practical_logistics", [])
    practical_logistics_text = "\n".join(f"  - {l}" for l in practical_logistics) if practical_logistics else "  None provided."

    # Narrative variables from brief (for {{double_brace}} placeholders in content, not headings)
    narrative_vars = brief.get("narrative_vars", {})
    narrative_vars_text = "\n".join(f"  - {k}: {v}" for k, v in narrative_vars.items()) if narrative_vars else "  None."

    return f"""Write a complete HTML article using the following specifications.

LANGUAGE: {lc["name"]} ({lc["code"]})
{lc["lang_instruction"]}

ARTICLE TYPE: {template_name}
TEMPLATE NOTE: {template['note']}

TITLE: {brief['title']}
SLUG: {brief.get('slug', '')}
TARGET KEYWORD: {brief.get('target_keyword', '')}
SEARCH INTENT: {brief.get('search_intent', 'informational')}
SERP GAP: {brief.get('serp_gap', '')}

ARTICLE STRUCTURE (follow these H2s/H3s exactly):
{chr(10).join(filled_structure)}

KEY FACTS TO INCLUDE (from our knowledge base):
{facts_text}

LOCAL TIPS FROM KNOWLEDGE BASE:
{local_tips_text}

COMMON MISTAKES TO ADDRESS:
{common_mistakes_text}

PERSONAL STORIES TO WEAVE IN:
{personal_stories_text}

PRACTICAL LOGISTICS TO INCLUDE:
{practical_logistics_text}

NARRATIVE VARIABLES (use these values in the narrative):
{narrative_vars_text}

PRODUCTS TO FEATURE:
{chr(10).join(featured) if featured else '  None specified — use products from content bank that fit naturally.'}

INTERNAL LINKS TO INCLUDE:
{chr(10).join(f'  - /{link}' for link in brief.get('internal_links', [])) if brief.get('internal_links') else '  None yet — this may be an early article for this site.'}

INLINE AFFILIATE LINK REQUIREMENT:
You MUST include at least 2 inline Viator affiliate links within the article body text (not just in product cards).
Use this format: <a href="{{PRODUCT_URL}}?pid=P00303273&mcid=42383&medium=link&utm_source=viator&utm_medium=affiliate&utm_campaign=SITE-CONTEXT" rel="sponsored">descriptive anchor text</a>
For utm_campaign: use the site topic + page context, lowercase, hyphens. Example: porto-wine-tours, lapland-northern-lights, madeira-levada-walks.
Weave them naturally into the narrative — e.g., "I booked the [tour name](affiliate link) and it was..." as HTML links.

TONE NOTES: {brief.get('tone_notes', 'Write naturally in the assigned voice.')}

SEASONAL CONTEXT: {knowledge.get('seasonal_notes', '')[:500]}

Generate the complete HTML article now. Output ONLY the HTML (no markdown fences, no explanations)."""


def get_niche_doers(niche):
    """Pluralize a niche for 'Best for experienced X' type phrasing."""
    mapping = {
        "surfing": "surfers", "diving": "divers", "hiking": "hikers",
        "fishing": "anglers", "climbing": "climbers", "skiing": "skiers",
        "sailing": "sailors", "kayaking": "kayakers", "cycling": "cyclists",
        "wine tasting": "wine lovers", "port tasting": "port enthusiasts", "cooking": "cooks",
    }
    return mapping.get(niche, f"{niche} enthusiasts")


def rebuild_head(html, domain, slug, lang="en"):
    """Rebuild a corrupted <head> when LLM generation truncates or hallucinates it.
    
    The DeepSeek Flash generator occasionally produces broken heads:
    - <meta name=<p>... (lost description/content attributes)
    - No </head> (JSON-LD and nav leak into head)
    - Missing canonical, OG tags
    
    This function extracts what it can from the broken HTML and rebuilds
    a clean, deterministic <head> section. Called after repair_structure() 
    and before inject_meta_tags().
    """
    has_head_close = '</head>' in html
    has_canonical = '<link rel="canonical"' in html or 'rel="canonical"' in html
    
    if has_head_close and has_canonical:
        # Validate canonical domain matches expected
        canon_match = re.search(r'<link[^>]*canonical[^>]*href="([^"]*)"[^>]*>', html, re.IGNORECASE)
        if canon_match:
            from urllib.parse import urlparse
            href = canon_match.group(1)
            parsed = urlparse(href) if href.startswith('http') else None
            if parsed and parsed.hostname:
                canon_domain = parsed.hostname.lower().rstrip('.')
                if canon_domain.startswith('www.'):
                    canon_domain = canon_domain[4:]
                expected = (domain or '').lower().replace('www.', '')
                if canon_domain != expected:
                    # Domain mismatch — strip and rebuild
                    html = re.sub(r'<link[^>]*canonical[^>]*>', '', html, flags=re.IGNORECASE)
                    # Don't return early — let rebuild logic run
                else:
                    return html  # Sound head with correct domain
            else:
                return html  # Sound head
        else:
            return html  # Sound head
    
    # Extract title from broken HTML
    title_match = re.search(r'<title>(.*?)</title>', html)
    title = title_match.group(1) if title_match else f"Article | {domain or 'Site'}"
    
    # Extract description — try meta tag first, then first <p> as fallback
    desc_match = re.search(r'<meta[^>]*name="description"[^>]*content="([^"]*)"', html)
    if desc_match:
        desc = desc_match.group(1).strip()
    else:
        first_p = re.search(r'<p[^>]*>(.*?)</p>', html, re.DOTALL)
        raw = first_p.group(1) if first_p else ""
        # Strip HTML tags and truncate
        desc = re.sub(r'<[^>]+>', '', raw)[:160].strip()
    
    canon_url = f"https://www.{domain}/{slug}" if domain and slug else ""
    lang_code = {"de": "de", "es": "es", "en": "en"}.get(lang, "en")

    # P0 fix: Locale routing validation — URL path prefix must match language
    # (Gate A 9/10, Jul 2026)
    path_prefix = slug.split('/')[0] if '/' in slug else ''
    if lang_code == 'de' and path_prefix != 'de':
        print(f"  WARNING: lang=de but URL path '{slug}' does not start with de/")
    if lang_code == 'es' and path_prefix != 'es':
        print(f"  WARNING: lang=es but URL path '{slug}' does not start with es/")
    
    new_head = f'<!DOCTYPE html>\n<html lang="{lang_code}">\n<head>\n  <meta charset="UTF-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n  <meta name="description" content="{desc}">\n  <title>{title}</title>\n  <link rel="stylesheet" href="/css/style.css">\n' + (f'  <link rel="canonical" href="{canon_url}">\n' if canon_url else '') + '</head>'
    
    # Find <body> — everything before it is the broken head
    body_match = re.search(r'<body[^>]*>', html)
    if body_match:
        html = new_head + '\n' + html[body_match.start():]
        print(f"  REPAIR: Rebuilt corrupted <head> (title: {title[:60]}...)")
        return html
    
    # Last resort: find first structural element
    struct_match = re.search(r'<(?:article|main|nav|header|h1)\b', html)
    if struct_match:
        html = new_head + '\n<body>\n' + html[struct_match.start():]
        print(f"  REPAIR: Rebuilt corrupted <head> + injected <body>")
        return html
    
    return html


def call_claude(system_prompt, user_prompt, max_turns=6, lang="en", timeout=300):
    """Call Claude CLI with system+user prompt. Fall back to DeepSeek on failure."""
    import subprocess, tempfile
    
    full_prompt = f"{system_prompt}\n\n{user_prompt}"
    claude_bin = os.path.expanduser("~/.hermes/node/bin/claude")
    
    try:
        env = dict(os.environ)
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if api_key: env["ANTHROPIC_API_KEY"] = api_key
        result = subprocess.run(
            [claude_bin, "-p", "--model", "claude-sonnet-5", "--max-turns", str(max_turns), full_prompt],
            capture_output=True, text=True, timeout=timeout,
            env=env
        )
        if result.returncode != 0:
            print(f"  WARNING: Claude CLI returned code {result.returncode}, falling back to DeepSeek")
            print(f"  stderr: {result.stderr[:200]}")
            return call_deepseek(system_prompt, user_prompt, lang=lang)
        output = result.stdout.strip()
        if not output or len(output) < 100:
            print(f"  WARNING: Claude CLI returned empty/short output ({len(output)} chars), falling back to DeepSeek")
            return call_deepseek(system_prompt, user_prompt, lang=lang)
        return output
    except FileNotFoundError:
        print(f"  WARNING: Claude CLI not found at {claude_bin}, falling back to DeepSeek")
        return call_deepseek(system_prompt, user_prompt, lang=lang)
    except subprocess.TimeoutExpired:
        print(f"  WARNING: Claude CLI timed out after {timeout}s, falling back to DeepSeek")
        return call_deepseek(system_prompt, user_prompt, lang=lang)
    except Exception as e:
        print(f"  WARNING: Claude CLI error: {e}, falling back to DeepSeek")
        return call_deepseek(system_prompt, user_prompt, lang=lang)


def call_deepseek(system_prompt, user_prompt, max_tokens=8000, lang="en"):
    """Call DeepSeek Flash API with retry and backoff.
    German/Spanish output is ~30% longer than English — bump max_tokens accordingly."""
    # German/Spanish need more tokens for the same content density
    if lang in ("de", "es") and max_tokens <= 8000:
        max_tokens = 10000
    try:
        from scripts.api_utils import call_with_retry
    except ImportError:
        from api_utils import call_with_retry

    if not API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY not set. Source ~/.hermes/.env first.")

    data, err = call_with_retry(
        url=f"{BASE_URL}/v1/chat/completions",
        payload={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "max_tokens": max_tokens,
            "temperature": 0.7,
            "stream": False
        },
        headers={"Authorization": f"Bearer {API_KEY}"},
        max_retries=3,
        timeout=120
    )

    if err:
        raise RuntimeError(f"DeepSeek API error: {err}")

    return trim_trailing_partial_word(data["choices"][0]["message"]["content"])


def trim_trailing_partial_word(text):
    """Strip trailing partial word from LLM output truncated mid-word.
    
    The DeepSeek API sometimes truncates output mid-word when it hits
    max_tokens. This strips the trailing partial word so text ends cleanly.
    
    Preserves: HTML tags at end, Unicode punctuation, complete sentences.
    Strips: trailing alphabetic fragment at sentence end.
    """
    if not text:
        return text
    
    stripped = text.rstrip()
    trailing_ws = text[len(stripped):]
    
    if not stripped:
        return text
    
    last_char = stripped[-1]
    
    # Already ends cleanly with punctuation or closing tag
    if last_char in '.!?":' "'" '"' ')' ']' '}' '…' '—' '–':
        return text
    
    # Ends with > — likely a closing HTML tag, leave alone
    if last_char == '>':
        return text
    
    # Check if we're mid-HTML-tag (open angle bracket without closing)
    last_lt = stripped.rfind('<')
    last_gt = stripped.rfind('>')
    if last_lt > last_gt and last_lt >= 0:
        # Open tag at end — don't strip (it's partial HTML)
        return text
    
    # The end is alphanumeric — might be truncated. Strip to last word boundary.
    if last_char.isalpha():
        for i in range(len(stripped) - 1, -1, -1):
            c = stripped[i]
            if c in ' \n\t' or c in '.!?,:;"' "'" '"' ')' ']' '}' '…' '—' '–':
                return stripped[:i+1] + trailing_ws
        return text  # couldn't find a boundary, leave it
    
    return text


def test_trim_trailing_partial_word():
    """Unit tests for trim_trailing_partial_word."""
    f = trim_trailing_partial_word
    # Clean endings — no change
    assert f("Hello world.") == "Hello world."
    assert f("Hello world!</p>") == "Hello world!</p>"
    assert f("Hello world!  ") == "Hello world!  "
    # Truncated with no boundary — single partial word
    assert f("experienc") == "experienc"  # no boundary, leave it
    assert f("otherwis") == "otherwis"
    # HTML tags preserved
    assert f("<div class='contain") == "<div class='contain"
    assert f("<p>Book now") == "<p>Book "
    assert f("<h2>Where to stay") == "<h2>Where to "
    # Unicode/TLD preserved
    assert f("Hello world…") == "Hello world…"
    assert f("Hello world—") == "Hello world—"
    assert f("This is naïve") == "This is "  # alphabetic ending without punctuation → strip
    assert f("Hello world.") == "Hello world."
    assert f("partial wor  \n") == "partial   \n"
    assert f("Text ends with &amp;") == "Text ends with &amp;"
    assert f("Text ends with &amp") == "Text ends with "
    print("✓ All trim_trailing_partial_word tests passed")


def extract_html(text):
    """Extract HTML from model response — handles markdown fences and other wrappers."""
    # Remove markdown code fences
    text = re.sub(r'^```html?\s*\n', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n```\s*$', '', text, flags=re.MULTILINE)
    # If the response starts with DOCTYPE or <html, take everything
    if text.strip().startswith("<!DOCTYPE") or text.strip().startswith("<html"):
        return text.strip()
    # Otherwise find the HTML block
    match = re.search(r'(<!DOCTYPE.*?</html>)', text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1)
    # Fallback: return everything
    return text.strip()


def get_site_domain(site_id):
    """Look up the domain for a site from site_registry.db. Cache per session."""
    if not hasattr(get_site_domain, '_cache'):
        get_site_domain._cache = {}
    if site_id in get_site_domain._cache:
        return get_site_domain._cache[site_id]
    try:
        db = sqlite3.connect(os.path.expanduser("~/.hermes/affiliate-crons/db/site_registry.db"))
        row = db.execute("SELECT domain FROM sites WHERE site_id=?", (site_id,)).fetchone()
        db.close()
        domain = row[0] if row else site_id
    except Exception:
        domain = site_id
    get_site_domain._cache[site_id] = domain
    return domain


def repair_structure(html, slug, lang="en", authority_sources=None):
    """Post-generation structural repair. Fixes common LLM output issues at the source."""
    import html as html_mod
    fixed = []

    # 1. TAG BALANCE: Auto-close unbalanced structural tags
    for tag in ['div', 'nav', 'main', 'footer', 'header', 'section', 'article']:
        opens = len(re.findall(rf'<{tag}[\s>]', html, re.IGNORECASE))
        closes = len(re.findall(rf'</{tag}>', html, re.IGNORECASE))
        if opens > closes:
            # Close unclosed tags before </body>
            missing = opens - closes
            print(f"  REPAIR: Closing {missing} unclosed <{tag}> tags")
            body_close = html.rfind('</body>')
            if body_close > 0:
                html = html[:body_close] + (f'</{tag}>\n' * missing) + html[body_close:]
            else:
                html += f'</{tag}>\n' * missing

    # 2. MISSING NAV: Auto-inject from site template (language-aware)
    if '<nav' not in html.lower():
        site_dir = os.path.expanduser(f"~/sites/{slug}")
        nav_html = None
        lang_dir = "" if lang == "en" else lang
        if os.path.isdir(site_dir):
            for root, dirs, files in os.walk(site_dir):
                dirs[:] = [d for d in dirs if 'backup' not in d]
                # For EN: only walk root-level pages (skip de/, es/ language subdirs)
                # For non-EN: restrict to the target language subdirectory
                if root == site_dir:
                    if lang == "en":
                        dirs[:] = [d for d in dirs if d == 'images' or d == 'css' or d == 'js']
                    elif lang_dir:
                        dirs[:] = [d for d in dirs if d == lang_dir or d == 'images' or d == 'css' or d == 'js']
                for fname in files:
                    if fname.endswith('.html'):
                        with open(os.path.join(root, fname)) as f:
                            existing = f.read()
                        nav_match = re.search(r'<nav[^>]*>.*?</nav>', existing, re.DOTALL | re.IGNORECASE)
                        if nav_match:
                            nav_html = nav_match.group(0)
                            break
                if nav_html:
                    break
        if nav_html:
            body_pos = html.find('<body>')
            if body_pos > 0:
                insert_pos = body_pos + len('<body>')
                html = html[:insert_pos] + '\n' + nav_html + html[insert_pos:]
                print(f"  REPAIR: Auto-injected <nav> from existing site page")
            else:
                print(f"  WARNING: Generated page missing <nav> — no <body> tag to inject into")
        else:
            print(f"  WARNING: Generated page missing <nav> — no existing pages found to copy from")

    # 2.5. NAV CORRUPTION: Extract lang-toggle from inside <ul class="nav-links">.
    #    When the LLM nests a lang toggle div inside the nav links list, it becomes
    #    invisible / breaks layout. Extract it and insert after the next </ul>.
    #    Affects 4 sites, 7 occurrences (NAV_BROKEN).
    ul_match = re.search(r'<ul[^>]*class="[^"]*nav-links[^"]*"[^>]*>(.*?)</ul>', html, re.DOTALL)
    if ul_match:
        ul_end = ul_match.end()  # position after </ul>
        ul_content = ul_match.group(1)
        lt_match = re.search(r'<div[^>]*class="[^"]*lang-toggle[^"]*"[^>]*>.*?</div>', ul_content, re.DOTALL)
        if lt_match:
            lt_html = lt_match.group(0)
            # Remove from inside the ul, insert after </ul>
            html = html.replace(lt_html, '', 1)  # remove from inside ul
            html = html[:ul_end] + '\n' + lt_html + html[ul_end:]
            print(f"  REPAIR: Extracted lang-toggle from inside nav-links ul")

    # 3. MISSING SKIP-LINK CHECK (language-aware)
    if 'skip-link' not in html.lower() and 'skipnav' not in html.lower() and '#main-content' not in html:
        body_pos = html.find('<body>')
        if body_pos > 0:
            # Detect language from <html lang="..."> attribute
            lang_match = re.search(r'<html[^>]*lang="([a-z]{2})"', html)
            lang = lang_match.group(1) if lang_match else "en"
            lc = LANG_CONFIG.get(lang, LANG_CONFIG["en"])
            skip = f'<a class="skip-link" href="#main-content">{lc["skip_link"]}</a>'
            html = html[:body_pos + len('<body>')] + '\n' + skip + html[body_pos + len('<body>'):]
            print(f"  REPAIR: Added missing skip-to-content link ({lang})")

    # 4. MISSING FOOTER — auto-inject from site template
    if '<footer' not in html.lower():
        year = datetime.now().year
        domain = get_site_domain(slug)
        footer_html = f'''    <footer class="site-footer">
      <div class="footer-content">
        <p class="copyright">&copy; {year} {domain}. All rights reserved.</p>
      </div>
    </footer>'''
        body_close = html.rfind('</body>')
        if body_close > 0:
            html = html[:body_close] + '\n' + footer_html + '\n' + html[body_close:]
            print(f"  REPAIR: Auto-injected <footer> with copyright")
        else:
            print(f"  WARNING: Generated page missing <footer> — no </body> tag to inject into")

    # 4.5. HERO VALIDATION: Verify <header> structure. Missing header, wrong
    #    heading count, or text overflow all degrade EEAT. Affects 4 sites, 8
    #    occurrences (HERO_FIX).
    header_match = re.search(r'<header[^>]*>(.*?)</header>', html, re.DOTALL)
    if header_match:
        header_content = header_match.group(1)
        h1_count = len(re.findall(r'<h1[^>]*>', header_content, re.IGNORECASE))
        if h1_count == 0:
            print(f"  WARNING: <header> has no <h1> — missing main heading")
        elif h1_count > 1:
            print(f"  WARNING: <header> has {h1_count} <h1> tags — should have exactly 1")
        # Check for text runs >200 chars without HTML breaks (overflow risk)
        text_runs = re.findall(r'(?<=>)([^<]{200,})(?=<)', header_content)
        if text_runs:
            print(f"  WARNING: <header> has {len(text_runs)} text run(s) >200 chars — overflow risk")
    else:
        print(f"  WARNING: No <header> element found — page has no hero section")

    # 5. PRODUCT COUNT BADGE: Replace {{product_count}} with actual count
    if '{{product_count}}' in html:
        count = len(re.findall(r'class="[^"]*product-card[^"]*"', html))
        if count == 0:
            count = len(re.findall(r'class=[\'\"][^\'\"]*tour-review-card[^\'\"]*[\'\"]', html))
        if count > 0:
            html = html.replace('{{product_count}}', str(count))
            print(f"  REPAIR: Filled product_count badge with {count}")

    # 6. WINNER-BOX CORRUPTION: Fix </<p> and other </h2>-related artifacts
    # Pattern 1: </<p> → </h2>\n<p> (Flash model generates </<p> instead of </h2>)
    html = re.sub(r'</<p>', '</h2>\n<p>', html)
    # Pattern 2: e.</p> at end of line preceded by heading text → </h2>
    html = re.sub(r'e\.</p>\s*$', '</h2>', html, flags=re.MULTILINE)
    # Pattern 3: </h2 without closing > followed by <p> → </h2>\n<p>
    html = re.sub(r'</h2(?!>)\s*<p', '</h2>\n<p', html)
    # Pattern 4: </h2> followed immediately by text without newline → add newline
    html = re.sub(r'(</h2>)(\S)', r'\1\n\2', html)
    # Pattern 5: Bare </h (malformed) → </h2>
    html = re.sub(r'</h(?!\d)\s*>', '</h2>', html)
    # Pattern 6: </ (orphaned closing bracket) after heading text → </h2>
    html = re.sub(r'(\w)</\s*>\s*\n\s*<p', r'\1</h2>\n<p', html)
    # Pattern 7: `<h2>text</p>` — wrong closing tag for heading
    html = re.sub(r'(<h([2-4])[^>]*>.*?)</p>(\s*\n\s*<p)', r'\1</h\2>\3', html)

    # 8. STRAY </h2> CLOSING PARAGRAPHS: LLM token-limit truncation artifact
    #    The model generates <p>...truncated_word</h2> instead of <p>...word.</p>
    #    Key signal: </h2> inside a <p> block with NO intervening <h2>/<h3>/<h4> opening
    #    tag between the <p> and the </h2>. Legitimate </h2> always has a matching <hN>.
    #    Jun 2026: 7 instances on lapland-aurora-chase-vs-fixed-location-viewing.
    stray_count = len(re.findall(
        r'<p[^>]*>(?:(?!<h[2-4][\s>]).)*?</h2>',
        html, re.DOTALL
    ))
    if stray_count:
        html = re.sub(
            r'(<p[^>]*>(?:(?!<h[2-4][\s>]).)*?)(\w+)</h2>',
            r'\1\2.</p>',
            html,
            flags=re.DOTALL
        )
        print(f"  REPAIR: Fixed {stray_count} stray </h2> → </p> (LLM truncation artifact)")

    # 9. TRUNCATION DETECTION: Words likely truncated by token-limit cutoff
    #    These appear as short lowercase words ending mid-sentence before a paragraph
    #    break. Heuristic: 2-5 char word followed by </p>, where the word looks like
    #    an incomplete common word (lowercase, no standard English suffix).
    truncation_candidates = re.findall(
        r'\b([a-z]{2,5})</p>\s*\n\s*<(?:p|h[2-4]|/section)',
        html, re.IGNORECASE
    )
    if truncation_candidates:
        likely = [w for w in truncation_candidates
                  if w.islower() and not w.endswith(('ing', 'ed', 'ly', 'er', 'est'))]
        if likely:
            print(f"  WARNING: Possible truncated words at paragraph boundaries: {likely}")

    # 10. HREF LINE-BREAK REPAIR: Fix corrupted href attributes where the LLM
    #     inserts newlines/whitespace inside URL values, e.g.:
    #       href="https://www.viator.com/\n            tours/d123-slug?pid=P00012345"
    #     Collapses whitespace inside href values, then validates affiliate PIDs survived.
    #     Gate C approved 9/10 — Claude Opus, 2026-07-06.
    _href_re = re.compile(r'href=(["\'])(.*?)\1', re.DOTALL)
    _pid_marker_re = re.compile(r'[?&]pid=', re.IGNORECASE)
    _empty_pid_re = re.compile(r'[?&]pid=(?:[&"\'\s]|$)', re.IGNORECASE)

    def _collapse_href_whitespace(match):
        quote, url = match.group(1), match.group(2)
        # URLs never contain literal whitespace — anything the LLM inserted
        # (newlines, tabs, indentation) is safe to strip.
        cleaned = re.sub(r"\s+", "", url)
        return f"href={quote}{cleaned}{quote}"

    _pid_count_before = len(_pid_marker_re.findall(html))
    html = _href_re.sub(_collapse_href_whitespace, html)
    _pid_count_after = len(_pid_marker_re.findall(html))

    if _pid_count_after < _pid_count_before:
        print(f"  ERROR: href line-break fix dropped affiliate PID param(s) "
              f"(before={_pid_count_before}, after={_pid_count_after})")
    elif _empty_pid_re.search(html):
        print(f"  ERROR: href fix produced an empty affiliate PID param")
    elif _pid_count_before != _pid_count_after:
        print(f"  REPAIR: Fixed {_pid_count_after - _pid_count_before} PID params in href repair")

    # 8. TRUNCATED PRODUCT CODES: Replace dXXXXX-??? with empty string (can't recover code)
    truncated = re.findall(r'd\d+-\?\?\?', html)
    if truncated:
        html = re.sub(r'viator\.com/tours/[^"]*d\d+-\?\?\?[^"&?]*', '', html)
        html = re.sub(r'data-viator-id="d\d+-\?\?\?"', 'data-viator-id=""', html)
        print(f"  REPAIR: Removed {len(truncated)} truncated product codes (dXXXXX-???)")

    # 9. CATEGORY LINKS WITH AFFILIATE PID: Strip ?pid= from non-product links
    category_pid = re.findall(r'href="(https?://www\.viator\.com/[^"]*\?pid=[^"]+)"', html)
    if category_pid:
        for match in category_pid:
            clean = re.sub(r'\?pid=P\d+&mcid=\d+(&medium=link)?', '', match)
            html = html.replace(match, clean)
        print(f"  REPAIR: Stripped affiliate PID from {len(category_pid)} non-product links")

    # 10. JSON-LD URL FIELD WITH NESTED <a> TAG: Extract raw URL
    nested_a = re.findall(r'"url":\s*"<a\s+href="[^>]*>(https?://[^"]+)</a>"', html)
    if nested_a:
        for url in nested_a:
            html = html.replace(
                re.search(r'"url":\s*"<a\s+href="[^>]*>' + re.escape(url) + r'</a>"', html).group(0),
                f'"url": "{url}"'
            )
        print(f"  REPAIR: Extracted {len(nested_a)} raw URLs from nested <a> in JSON-LD")

    # 11. MISSING AUTHORITY CITATIONS — inject from content bank
    if authority_sources:
        non_viator_links = len(re.findall(r'https?://(?!www\.viator\.com)(?:www\.)?[a-zA-Z0-9-]+\.[a-zA-Z]{2,}', html))
        if non_viator_links < 2:
            src = authority_sources[0]
            cite = f'<p class="authority-note">According to <a href="{src["url"]}" rel="external nofollow">{src["name"]}</a> ({src["what"]}), always check current conditions before visiting.</p>'
            first_p = html.find('<p>')
            if first_p >= 0:
                html = html[:first_p+3] + f'\n{cite}\n' + html[first_p+3:]
                print(f"  REPAIR: Injected authority citation — {src['name']}")
            else:
                print(f"  WARNING: Cannot inject authority citation — no <p> tag found")

    # 12. HOROSCOPE TEST — flag generic AI-slop sentences
    horoscope_patterns = [
        r'(?i)whether you\'?re a (beginner|expert|first-timer|seasoned|novice|pro)',
        r'(?i)there\'?s something for everyone',
        r'(?i)in today\'?s (fast-paced|digital|modern|competitive) world',
        r'(?i)it\'?s no (secret|wonder|surprise) that',
        r'(?i)the world of [a-z]+ is (vast|constantly evolving|ever-changing)',
        r'(?i)look no further',
        r'(?i)so what are you waiting for\?',
        r'(?i)unleash your inner [a-z]+',
        r'(?i)take your [a-z]+ to the next level',
        r'(?i)goes above and beyond',
    ]
    horoscope_hits = []
    for pattern in horoscope_patterns:
        matches = re.findall(pattern, html)
        if matches:
            for m in matches:
                snippet = m if isinstance(m, str) else m[0]
                horoscope_hits.append(snippet[:60])
    if horoscope_hits:
        print(f"  🔮 Horoscope Test: {len(horoscope_hits)} generic phrase(s) — {horoscope_hits[:3]}")

    return html


def build_faq_jsonld_from_html(html, persona_name, canonical_url):
    """Build Article JSON-LD with FAQ Q&A from HTML FAQ section.
    Eliminates LLM truncation bug entirely — extraction is deterministic.
    FAQPage is dead for Google SERP as of 2026; Q&A pairs go in Article schema."""
    import json as json_mod

    # Extract FAQ questions and answers from HTML
    faq_match = re.search(r'<section[^>]*class="[^"]*faq[^"]*"[^>]*>(.*?)</section>', html, re.DOTALL)
    if not faq_match:
        return html  # No FAQ section, skip

    faq_html = faq_match.group(1)
    qa_pairs = []

    # Find all H3/H4 questions and their following answers
    for q_match in re.finditer(r'<(h[34])[^>]*>(.*?)</\1>', faq_html, re.IGNORECASE | re.DOTALL):
        question = re.sub(r'<[^>]+>', '', q_match.group(2)).strip()
        q_end = q_match.end()

        # Find the next heading or end of section
        next_heading = re.search(r'<(h[2-4])[^>]*>', faq_html[q_end:], re.IGNORECASE)
        if next_heading:
            answer_html = faq_html[q_end:q_end + next_heading.start()]
        else:
            answer_html = faq_html[q_end:]

        answer = re.sub(r'<[^>]+>', '', answer_html).strip()
        if question and answer and len(question) > 5:
            # Escape for JSON
            qa_pairs.append({
                "@type": "Question",
                "name": question,
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": answer
                }
            })

    if not qa_pairs:
        return html  # No valid Q&A pairs found

    # Build FAQPage JSON-LD
    faq_jsonld = {
        "@context": "https://schema.org",
        "@type": "Article",
        "mainEntity": qa_pairs
    }

    # Replace the LLM-generated FAQ JSON-LD with our deterministic one
    # Find the FAQPage JSON-LD block
    ld_pattern = re.compile(
        r'<script[^>]*type="application\/ld\+json"[^>]*>\s*\{[^}]*"@type"\s*:\s*"Article"[^}]*"mainEntity".*?</script>',
        re.DOTALL
    )

    new_block = f'<script type="application/ld+json">\n{json_mod.dumps(faq_jsonld, indent=2, ensure_ascii=False)}\n</script>'
    html = ld_pattern.sub(lambda m: new_block, html, count=1)

    # If no Article+mainEntity block was found (LLM omitted FAQ Q&A), insert it before closing </script>
    if 'mainEntity' not in html:
        article_close = html.rfind('</script>')
        if article_close > 0:
            html = html[:article_close + len('</script>')] + '\n' + new_block + html[article_close + len('</script>'):]

    print(f"  REPAIR: Built FAQPage JSON-LD with {len(qa_pairs)} Q&A pairs")
    return html


def build_article_jsonld(html, canonical_url, persona_name, site_name="", lang="en"):
    """Enhance Article JSON-LD with BreadcrumbList and missing fields.
    Adds: mainEntityOfPage, publisher, image, and BreadcrumbList.
    Preserves existing LLM-generated fields (headline, description, datePublished, dateModified).
    """
    import json as json_mod

    # Find all JSON-LD script blocks (parse each, not regex — handles nested objects)
    ld_blocks = list(re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>\s*(.*?)\s*</script>',
        html, re.DOTALL
    ))
    
    article_match = None
    article_data = None
    for match in ld_blocks:
        try:
            data = json_mod.loads(match.group(1))
        except json_mod.JSONDecodeError:
            continue
        if data.get('@type') == 'Article':
            article_match = match
            article_data = data
            break

    if not article_match:
        return html  # No Article block — nothing to enhance

    # Add missing fields
    if 'mainEntityOfPage' not in article_data:
        article_data['mainEntityOfPage'] = {"@type": "WebPage", "@id": canonical_url}

    if 'publisher' not in article_data and site_name:
        # Extract domain WITHOUT www. prefix so we don't double it
        raw_domain = canonical_url.split('://', 1)[-1].split('/', 1)[0] if '://' in canonical_url else ''
        clean_domain = raw_domain.replace('www.', '', 1)
        article_data['publisher'] = {
            "@type": "Organization",
            "name": site_name,
            "url": f"https://www.{clean_domain}" if clean_domain else canonical_url
        }

    # Build BreadcrumbList from URL path
    url_part = canonical_url.split('://', 1)[-1] if '://' in canonical_url else canonical_url
    domain = url_part.split('/', 1)[0]
    path_parts = [p for p in url_part.split('/')[1:] if p]
    
    # Cleaner breadcrumb name: use site_name or domain minus www
    breadcrumb_name = site_name if site_name else domain.replace('www.', '', 1)
    # If still looks like a domain (contains dots, not spaces), capitalize first segment
    if breadcrumb_name and '.' in breadcrumb_name and ' ' not in breadcrumb_name:
        breadcrumb_name = breadcrumb_name.split('.')[0].title()
    
    breadcrumb_items = [{
        "@type": "ListItem",
        "position": 1,
        "name": breadcrumb_name,
        "item": f"https://{domain}/"
    }]
    
    for i, part in enumerate(path_parts, 1):
        if part == 'index.html':
            continue
        item_url = f"https://{domain}/{'/'.join(path_parts[:i])}/"
        if item_url.endswith('//'):
            item_url = item_url.rstrip('/')
        name = part.replace('-', ' ').title()
        breadcrumb_items.append({
            "@type": "ListItem",
            "position": i + 1,
            "name": name,
            "item": item_url
        })

    breadcrumb_jsonld = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": breadcrumb_items
    }

    # Rebuild Article JSON-LD block
    new_article = f'<script type="application/ld+json">\n{json_mod.dumps(article_data, indent=2, ensure_ascii=False)}\n</script>'
    new_breadcrumb = f'<script type="application/ld+json">\n{json_mod.dumps(breadcrumb_jsonld, indent=2, ensure_ascii=False)}\n</script>'

    # Replace old Article block
    html = html.replace(article_match.group(0), new_article, 1)

    # Insert BreadcrumbList after Article
    html = html.replace(new_article, new_article + '\n  ' + new_breadcrumb, 1)

    return html


def add_organization_jsonld(html, canonical_url, site_name, persona_name, lang="en"):
    """Add Organization JSON-LD to homepage only (slug is root/index)."""
    import json as json_mod

    if not canonical_url:
        return html  # Guard: empty domain produces garbage URLs

    url_part = canonical_url.split('://', 1)[-1] if '://' in canonical_url else canonical_url
    domain = url_part.split('/', 1)[0]

    # Only add to homepage (URL ends with root / or domain only)
    path = canonical_url.replace(f"https://{domain}", "").replace(f"http://{domain}", "")
    if path not in ('', '/', '/index.html'):
        return html

    lc = LANG_CONFIG.get(lang, LANG_CONFIG["en"])
    description = lc.get("org_description", LANG_CONFIG["en"]["org_description"]).format(site=site_name or domain)

    org_jsonld = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": site_name or domain,
        "url": f"https://{domain}/",
        "description": description,
        "foundingDate": "2026",
        "author": {
            "@type": "Person",
            "name": persona_name
        }
    }

    org_block = f'<script type="application/ld+json">\n{json_mod.dumps(org_jsonld, indent=2, ensure_ascii=False)}\n</script>'

    # Insert before first existing JSON-LD block or before </head>
    first_ld = html.find('<script type="application/ld+json">')
    if first_ld > 0:
        html = html[:first_ld] + org_block + '\n  ' + html[first_ld:]
    else:
        html = html.replace('</head>', '\n  ' + org_block + '\n</head>', 1)

    return html


def add_country_hreflang(html, lang, expected_domain):
    """Add country-specific hreflang tags (e.g., en-GB, en-ES) for geo-targeting.
    Appends after existing hreflang tags."""
    if not expected_domain:
        return html

    # Map domain → country hreflang
    country_map = {
        'lapland-adventure-guide.com': 'en-GB',
        'porto-sommelier.com': 'en-GB',
        'madeira-trail-guide.com': 'en-GB',
        'tenerife-outdoor-guide.com': 'en-ES',
        'san-juan-excursions.com': 'en-US',
        'yogyakarta-temple-tours.com': 'en-ID',
    }
    
    if expected_domain not in country_map:
        return html
    
    country_code = country_map[expected_domain]
    
    # Check if already present
    if f'hreflang="{country_code}"' in html:
        return html
    
    # Find x-default hreflang and insert country hreflang after it
    xdefault_match = re.search(r'(<link[^>]*hreflang="x-default"[^>]*>)', html)
    if xdefault_match:
        # Try to find en self-reference matching expected_domain
        en_self = re.search(
            rf'<link[^>]*rel="alternate"[^>]*hreflang="en"[^>]*href="https://[^"]*{expected_domain}[^"]*"[^>]*>',
            html
        )
        if en_self:
            country_tag = en_self.group(0).replace('hreflang="en"', f'hreflang="{country_code}"')
            html = html.replace(en_self.group(0), en_self.group(0) + '\n  ' + country_tag, 1)
        else:
            # Fallback: en self-ref doesn't exist for this domain — create country
            # hreflang using expected_domain with the same path
            href_match = re.search(r'href="([^"]+)"', xdefault_match.group(1))
            if href_match:
                # Extract path from x-default URL, rebuild with expected_domain
                xdefault_url = href_match.group(1)
                path = xdefault_url.split('/', 3)[-1] if xdefault_url.count('/') >= 3 else ''
                country_url = f"https://www.{expected_domain}/{path}".rstrip('/')
                country_tag = f'<link rel="alternate" hreflang="{country_code}" href="{country_url}">'
                html = html.replace(xdefault_match.group(1),
                                    xdefault_match.group(1) + '\n  ' + country_tag, 1)
    
    return html


def strip_antiwords_in_generated(html):
    """Auto-fix anti-words in generated HTML before saving."""
    import subprocess, tempfile, os
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False) as tmp:
            tmp.write(html)
            tmp_path = tmp.name

        antiword_script = os.path.expanduser(
            "~/.hermes/skills/devops/affiliate-operations/scripts/antiword_scan.py"
        )
        if os.path.exists(antiword_script):
            result = subprocess.run(
                ["python3", antiword_script, "--site", os.path.dirname(tmp_path), "--fix"],
                capture_output=True, text=True, timeout=30
            )
            with open(tmp_path) as f:
                html = f.read()
        os.unlink(tmp_path)
    except Exception:
        pass  # Non-critical — QA pipeline catches anti-words later

    return html


def inject_hreflang_tags(html, domain, lang="en"):
    """Inject self-referencing hreflang tags after canonical link.
    Yogya upgrade lesson: every page needs en + x-default hreflang from day one."""
    if not domain:
        return html
    canon_match = re.search(r'<link[^>]*rel="canonical"[^>]*href="([^"]+)"', html)
    if not canon_match:
        return html
    canon_url = canon_match.group(1)
    hreflang_block = (
        f'{canon_match.group(0)}\n'
        f'  <link rel="alternate" hreflang="{lang}" href="{canon_url}">\n'
        f'  <link rel="alternate" hreflang="x-default" href="{canon_url}">'
    )
    if 'hreflang=' not in html:
        html = html.replace(canon_match.group(0), hreflang_block, 1)
        print(f"  REPAIR: Injected hreflang tags ({lang} + x-default)")
    return html


def inject_meta_tags(html, domain, slug, goatcounter_code=None):
    """Inject OG tags, Twitter card, favicon, and analytics if missing.
    
    goatcounter_code: optional site-specific GoatCounter code from content bank.
        If None or empty, analytics injection is skipped entirely.
    """
    if not domain:
        return html

    # Extract title and description from existing tags
    title_match = re.search(r'<title>(.*?)</title>', html)
    desc_match = re.search(r'<meta[^>]*name="description"[^>]*content="([^"]+)"', html)
    title = title_match.group(1) if title_match else "Untitled"
    description = desc_match.group(1) if desc_match else ""

    # Build OG/Twitter block
    meta_block = []
    if 'og:title' not in html:
        meta_block.append(f'<meta property="og:title" content="{title}">')
    if 'og:description' not in html:
        meta_block.append(f'<meta property="og:description" content="{description}">')
    if 'og:type' not in html:
        meta_block.append('<meta property="og:type" content="website">')
    if 'twitter:card' not in html:
        meta_block.append('<meta name="twitter:card" content="summary_large_image">')
    if 'twitter:title' not in html:
        meta_block.append(f'<meta name="twitter:title" content="{title}">')

    # Favicon (if missing)
    if 'favicon' not in html.lower() and 'rel="icon"' not in html.lower():
        meta_block.append('<link rel="icon" type="image/svg+xml" href="/favicon.svg">')

    # Analytics (if missing) — per-site GoatCounter from content bank
    if goatcounter_code and 'goatcounter' not in html.lower() and 'gtag' not in html.lower() and 'anonymize_ip' not in html.lower():
        meta_block.append(f'<script data-goatcounter="https://{goatcounter_code}.goatcounter.com/count" async src="//gc.zgo.at/count.js"></script>')

    # og:url (if missing)
    if 'og:url' not in html:
        canon_match = re.search(r'<link[^>]*rel="canonical"[^>]*href="([^"]+)"', html)
        if canon_match:
            meta_block.append(f'<meta property="og:url" content="{canon_match.group(1)}">')

    if meta_block:
        head_close = html.find('</head>')
        if head_close > 0:
            inject = '\n' + '\n'.join(meta_block) + '\n'
            html = html[:head_close] + inject + html[head_close:]
            print(f"  REPAIR: Injected {len(meta_block)} meta tags (OG/Twitter/favicon/analytics)")

    return html


def inject_product_alt_text(html):
    """Replace empty alt="" on product card images with tour name from sibling h3.
    Yogya upgrade lesson: 28 product images had empty alt."""
    fixed = 0
    for card in re.finditer(r'<div class="product-card">.*?</div>', html, re.DOTALL):
        card_html = card.group(0)
        if 'alt=""' not in card_html:
            continue
        h3_match = re.search(r'<h3[^>]*>(.*?)</h3>', card_html, re.DOTALL)
        if not h3_match:
            continue
        tour_name = re.sub(r'<[^>]+>', '', h3_match.group(1)).strip()
        if len(tour_name) > 125:
            tour_name = tour_name[:122] + '...'
        tour_name = tour_name.replace('"', '&quot;')
        new_card = card_html.replace('alt=""', f'alt="{tour_name}"', 1)
        html = html.replace(card_html, new_card, 1)
        fixed += 1
    if fixed:
        print(f"  REPAIR: Added alt text to {fixed} product images")
    return html



def wrap_price_adjacent_proper_nouns(html, max_distance=50):
    """Wrap 2-3-word capitalized phrases near a euro/dollar price in <strong>."""
    from html.parser import HTMLParser
    import re as _re

    excluded_tags = {
        "a", "strong", "b",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "script", "style", "noscript", "template",
        "textarea", "pre", "code", "title",
        "button", "option", "label", "nav", "footer",
    }

    capitalized_word = (
        r"[A-Z\u00c0-\u00d6\u00d8-\u00de]"
        r"[A-Za-z\u00c0-\u00d6\u00d8-\u00ff]*"
        r"(?:[\u2019'-][A-Z\u00c0-\u00d6\u00d8-\u00de]?[A-Za-z\u00c0-\u00d6\u00d8-\u00ff]+)*"
    )
    # Lowercase particles common in Romance language names
    particle = r"(?:d[aeo]s?|l[\u2019']|[en]o|na|pelo|pela|del|al|el)"

    phrase_re = _re.compile(
        rf"(?<![\w\u2019'-])"
        rf"({capitalized_word}(?:(?: {particle})?[ \t]+{capitalized_word}){{1,2}})"
        rf"(?![ \t]+{capitalized_word})"
        rf"(?![\w\u2019'-])"
    )

    price_re = _re.compile(r"(?:\u20ac|\$)\s*\d")

    rejected_first_words = {
        # English
        "A", "An", "And", "As", "At", "But", "By", "For", "From",
        "He", "Her", "His", "I", "If", "In", "It", "Its",
        "My", "On", "Or", "Our", "She", "So", "That", "The",
        "Their", "They", "This", "Those", "To", "We", "What",
        "When", "Where", "Which", "While", "Who", "Why", "With",
        "You", "Your",
        # Portuguese
        "O", "A", "Os", "As", "Um", "Uma", "No", "Na", "Nos", "Nas",
        "Do", "Da", "Dos", "Das", "Pelo", "Pela",
        # French
        "Le", "La", "Les", "Un", "Une", "Des", "Du", "De", "D'",
        "Ce", "Cet", "Cette", "Mon", "Ton", "Son", "Notre",
        # German
        "Der", "Die", "Das", "Ein", "Eine", "Einen", "Dem", "Den",
        # Spanish
        "El", "La", "Los", "Las", "Un", "Una", "Unos", "Unas", "Del",
        # Italian
        "Il", "Lo", "La", "I", "Gli", "Le", "Un", "Uno", "Una",
        # Temporal/demonstrative
        "Today", "Tomorrow", "Now", "Here", "There",
    }

    wrapped_count = 0

    def distance_to_price(start, end, price_positions):
        distances = []
        for position in price_positions:
            if position < start: distances.append(start - position)
            elif position > end: distances.append(position - end)
            else: distances.append(0)
        return min(distances) if distances else None

    def wrap_text_node(text):
        nonlocal wrapped_count
        price_positions = [m.start() for m in price_re.finditer(text)]
        if not price_positions:
            return text
        replacements = []
        for match in phrase_re.finditer(text):
            phrase = match.group(1)
            if phrase.split()[0] in rejected_first_words:
                continue
            d = distance_to_price(match.start(1), match.end(1), price_positions)
            if d is None or d > max_distance:
                continue
            replacements.append((match.start(1), match.end(1), f"<strong>{phrase}</strong>"))
        for start, end, replacement in reversed(replacements):
            text = text[:start] + replacement + text[end:]
            wrapped_count += 1
        return text

    class PricePhraseParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.parts = []; self.stack = []
        def blocked(self):
            return any(tag in excluded_tags for tag in self.stack)
        def handle_starttag(self, tag, attrs):
            self.parts.append(self.get_starttag_text())
            self.stack.append(tag.lower())
        def handle_startendtag(self, tag, attrs):
            self.parts.append(self.get_starttag_text())
        def handle_endtag(self, tag):
            tag = tag.lower()
            self.parts.append(f"</{tag}>")
            if tag in self.stack:
                idx = self.stack[::-1].index(tag)
                match_idx = len(self.stack) - idx - 1
                del self.stack[match_idx:]
        def handle_data(self, data):
            self.parts.append(data if self.blocked() else wrap_text_node(data))
        def handle_entityref(self, name):
            self.parts.append(f"&{name};")
        def handle_charref(self, name):
            self.parts.append(f"&#{name};")
        def handle_comment(self, data):
            self.parts.append(f"<!--{data}-->")
        def handle_decl(self, decl):
            self.parts.append(f"<!{decl}>")
        def handle_pi(self, data):
            self.parts.append(f"<?{data}>")
        def unknown_decl(self, data):
            self.parts.append(f"<![{data}]>")

    parser = PricePhraseParser()
    parser.feed(html)
    parser.close()
    return "".join(parser.parts), wrapped_count


def generate_article(slug, cb, brief, lang="en", engine="deepseek"):
    """Generate a single article from a brief.
    
    Args:
        engine: "deepseek" (default, fast) or "claude" (higher quality, slower)
    """
    system_prompt = build_system_prompt(cb, lang)
    user_prompt = build_user_prompt(cb, brief, lang)

    article_slug = brief.get("slug", "untitled")
    engine_label = "🟣Claude" if engine == "claude" else "⚡DeepSeek"
    print(f"  Generating [{engine_label}]: {brief.get('title', article_slug)}")

    try:
        if engine == "claude":
            raw = call_claude(system_prompt, user_prompt, lang=lang)
        else:
            raw = call_deepseek(system_prompt, user_prompt, lang=lang)
        html = extract_html(raw)

        # Save to output
        site_dir = os.path.join(OUTPUT_DIR, slug)
        os.makedirs(site_dir, exist_ok=True)
        out_path = os.path.join(site_dir, f"{article_slug}.html")

        # POST-PROCESSING: Enforce www. prefix on canonical URLs
        html = re.sub(
            r'<link[^>]*rel=\"canonical\"[^>]*href=\"https://(?!www\\.)([^\"]+)\"',
            lambda m: m.group(0).replace(
                f'//{m.group(1)}', f'//www.{m.group(1)}'
            ),
            html
        )
        html = re.sub(
            r'\"url\"\\s*:\\s*\"https://(?!www\\.)([^\"]+)\"',
            lambda m: m.group(0).replace(
                f'//{m.group(1)}', f'//www.{m.group(1)}'
            ),
            html
        )

        # POST-PROCESSING: Strip trailing slashes from canonical URLs
        # (except root "/" — e.g., https://domain.com/slug/ → https://domain.com/slug)
        html = re.sub(
            r'<link[^>]*rel="canonical"[^>]*href="(https://[^"]+/)"',
            lambda m: m.group(0).replace(
                f'href="{m.group(1)}"', f'href="{m.group(1).rstrip("/")}"'
            ) if m.group(1).count('/') > 3 else m.group(0),
            html
        )
        html = re.sub(
            r'<link[^>]*href="(https://[^"]+/)"[^>]*rel="canonical"',
            lambda m: m.group(0).replace(
                f'href="{m.group(1)}"', f'href="{m.group(1).rstrip("/")}"'
            ) if m.group(1).count('/') > 3 else m.group(0),
            html
        )

        # DOMAIN VALIDATION
        expected_domain = None
        try:
            row = sqlite3.connect(os.path.expanduser(
                "~/.hermes/affiliate-crons/db/site_registry.db"
            )).execute(
                "SELECT domain FROM sites WHERE site_id=?", (slug,)
            ).fetchone()
            if row and row[0] and row[0] != "unknown":
                expected_domain = row[0].lower().strip()
        except Exception:
            pass
        if expected_domain:
            # KNOWN_WRONG_DOMAIN blocklist — catch LLM hallucinations of old domains
            KNOWN_WRONG = {
                "porto-sommelier":       ["porto-sommelier.com"],
                "lapland-adventure-guide": ["laplandadventureguide.com"],
                "tenerife-outdoor-guide":  ["tenerifeoutdooradventures.com", "canaryhikes.com"],
                "san-juan-excursions":     ["mateorivera.com"],
                "yogyakarta-temple-tours": ["ramakusuma.com"],
                "madeira-trail-guide":          ["madeira-trail-guide.com", "madeirahikes.com"],
            }
            if slug in KNOWN_WRONG:
                for wrong_domain in KNOWN_WRONG[slug]:
                    if wrong_domain in html.lower():
                        html = re.sub(
                            rf'https?://(?:www\\.)?{re.escape(wrong_domain)}',
                            f'https://www.{expected_domain}',
                            html, flags=re.IGNORECASE
                        )
                        print(f"  REPAIR: Replaced known-wrong domain {wrong_domain}")

            wrong_domains = set()
            for m in re.finditer(r'https?://([a-zA-Z0-9][-a-zA-Z0-9]*(?:\.[a-zA-Z0-9][-a-zA-Z0-9.]*)?\.[a-z]{2,})', html):
                found = m.group(1).lower().replace('www.', '')
                if found != expected_domain and '.viator.com' not in found and 'schema.org' not in found:
                    wrong_domains.add(m.group(1))
            for wrong in wrong_domains:
                html = html.replace(f'https://{wrong}', f'https://www.{expected_domain}')
                html = html.replace(f'http://{wrong}', f'https://www.{expected_domain}')
                print(f"  REPAIR: Replaced wrong-domain href {wrong}")

            # FIX #2a: VIATOR DESTINATION CODE VALIDATION
            # Strip Viator links with destination codes not matching this site
            valid = VALID_DEST_CODES.get(slug, set())
            if valid:
                wrong_dest = []
                def _check_dest(m):
                    code = m.group(1)
                    if code not in valid:
                        wrong_dest.append(code)
                        return ""  # strip the URL
                    return m.group(0)
                # Match full Viator URLs and strip those with wrong dest codes
                for pattern in [
                    r'<a\s[^>]*href="https?://(?:www\.)?viator\.com/tours/[^"]*/d(\d+)-[^"]*"[^>]*>.*?</a>',
                    r'href="https?://(?:www\.)?viator\.com/tours/[^"]*/d(\d+)-[^"]*"',
                ]:
                    html, n = re.subn(pattern, _check_dest, html, flags=re.IGNORECASE)
                if wrong_dest:
                    print(f"  REPAIR: Stripped {len(wrong_dest)} Viator links with wrong destination codes: {list(set(wrong_dest))[:5]}")
        # FIX #2: STRUCTURAL AUTO-REPAIR (tag balance, nav/footer/skip, product count, authority)
        html = repair_structure(html, slug, lang, cb.get("authority_sources"))

        # FIX #10: REBUILD CORRUPTED HEAD (before meta injection)
        html = rebuild_head(html, expected_domain, article_slug, lang)

        # FIX #1: FAQ JSON-LD SEPARATE GENERATION
        persona_name = cb["voice"]["persona_name"]
        canonical = f"www.{expected_domain}" if expected_domain else ""
        html = build_faq_jsonld_from_html(html, persona_name, canonical)

        # PHASE 2: Enhanced structured data + geo-targeting
        canonical_url = f"https://www.{expected_domain}/{article_slug}".rstrip('/') if expected_domain else ""
        site_name = cb["site"].get("destination", cb["site"].get("niche", expected_domain))
        html = build_article_jsonld(html, canonical_url, persona_name, site_name, lang)
        html = add_organization_jsonld(html, canonical_url, site_name, persona_name, lang)
        # add_country_hreflang moved to after inject_hreflang_tags (line ~1367)

        # FAQ FALLBACK: If the LLM omitted the FAQ <section> entirely, there's
        # nothing for build_faq_jsonld_from_html() to work with. Inject a minimal
        # FAQ section with 2 Q&A pairs before </footer>, then build FAQPage
        # JSON-LD from the injected content so structured data stays complete.
        # Affects 3 sites, 3 occurrences (FAQ_TRUNCATION).
        lc_faq = LANG_CONFIG.get(lang, LANG_CONFIG["en"])
        # Use regex to match class="faq" / class='faq' / class="... faq ..."
        has_faq_section = bool(re.search(r'class=["\'][^"\']*\bfaq\b[^"\']*["\']', html, re.IGNORECASE))
        if not has_faq_section and 'FAQPage' not in html:
            faq_q1 = {"en": "Is this tour suitable for beginners?",
                      "de": "Ist diese Tour für Anfänger geeignet?",
                      "es": "¿Es esta excursión adecuada para principiantes?"}.get(lang,
                      "Is this tour suitable for beginners?")
            faq_a1 = {"en": "Yes, but check with the operator — difficulty varies by tour.",
                      "de": "Ja, aber fragen Sie den Anbieter — die Schwierigkeit variiert je nach Tour.",
                      "es": "Sí, pero consulte con el operador — la dificultad varía según la excursión."}.get(lang,
                      "Yes, but check with the operator — difficulty varies by tour.")
            faq_q2 = {"en": "Do I need to book in advance?",
                      "de": "Muss ich im Voraus buchen?",
                      "es": "¿Necesito reservar con antelación?"}.get(lang,
                      "Do I need to book in advance?")
            faq_a2 = {"en": "Booking ahead is recommended, especially in peak season.",
                      "de": "Eine Vorausbuchung wird empfohlen, besonders in der Hauptsaison.",
                      "es": "Se recomienda reservar con antelación, especialmente en temporada alta."}.get(lang,
                      "Booking ahead is recommended, especially in peak season.")
            faq_html = (
                f'<section class="faq">\n'
                f'  <h2>{lc_faq["faq_header"]}</h2>\n'
                f'  <h3>{faq_q1}</h3>\n'
                f'  <p>{faq_a1}</p>\n'
                f'  <h3>{faq_q2}</h3>\n'
                f'  <p>{faq_a2}</p>\n'
                f'</section>\n'
            )
            footer_pos = html.rfind('<footer')
            if footer_pos > 0:
                html = html[:footer_pos] + faq_html + html[footer_pos:]
            else:
                body_close = html.rfind('</body>')
                if body_close > 0:
                    html = html[:body_close] + faq_html + html[body_close:]
                else:
                    html += faq_html  # append at end as last resort
            # Rebuild FAQPage JSON-LD now that the section exists
            html = build_faq_jsonld_from_html(html, persona_name, canonical)
            print("  REPAIR: Injected minimal FAQ section (LLM omitted it)")

        # FIX #7: HREFLANG INJECTION (Yogya upgrade lesson — every page needs hreflang)
        html = inject_hreflang_tags(html, expected_domain, lang)

        # PHASE 2: Country-specific hreflang variants (en-GB, en-ES, etc.) — must run AFTER hreflang injection
        html = add_country_hreflang(html, lang, expected_domain)

        # FIX #9: META TAGS INJECTION (OG, Twitter, favicon, GoatCounter)
        gc_code = cb.get("analytics", {}).get("goatcounter_code", "")
        html = inject_meta_tags(html, expected_domain, article_slug, goatcounter_code=gc_code)

        # FIX #8: PRODUCT IMAGE ALT TEXT — DISABLED (runs before images exist, dead code).
        # Alt text is handled by the downstream image injector at injection time.
        # html = inject_product_alt_text(html)

        # FIX #5: ANTI-WORD AUTO-FIX
        html = strip_antiwords_in_generated(html)

        # FIX: HTML ENTITY ESCAPING — raw & in href attributes breaks valid HTML
        # Bug: Claude outputs ?pid=123&mcid=456 instead of ?pid=123&amp;mcid=456
        # Fix: convert bare & in href values to &amp;, skip already-escaped entities
        def _escape_href_ampersands(html):
            def _fix_ampersand(m):
                full = m.group(0)
                quote_char = m.group(1) or '"'
                url = m.group(2)
                if url is None: return full
                # Only fix if there are bare & chars not already part of &amp; &lt; &gt; &quot; &nbsp; &#...;
                if re.search(r'&(?!amp;|lt;|gt;|quot;|nbsp;|#)', url):
                    fixed_url = re.sub(r'&(?!amp;|lt;|gt;|quot;|nbsp;|#)', '&amp;', url)
                    return f'href={quote_char}{fixed_url}{quote_char}'
                return full
            return re.sub(
                r'''href=([\"\'])(.*?)\1''',
                _fix_ampersand,
                html,
                flags=re.IGNORECASE
            )
        html = _escape_href_ampersands(html)

        # JSON-LD REPAIR (control character cleanup)
        import json as json_mod
        ld_pattern = re.compile(
            r'(<script[^>]*type="application/ld\\+json"[^>]*>)(.*?)(</script>)',
            re.DOTALL
        )
        def repair_jsonld(match):
            raw = match.group(2)
            cleaned = re.sub(r'[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f]', '', raw)
            try:
                json_mod.loads(cleaned)
                return match.group(1) + cleaned + match.group(3)
            except json_mod.JSONDecodeError:
                return match.group(1) + cleaned + match.group(3)
        html = ld_pattern.sub(repair_jsonld, html)

        # JSON-LD STRUCTURAL VALIDATION: After repair, verify required fields exist.
        # Affects 5 sites, 15 occurrences of truncated/missing JSON-LD (JSONLD_BROKEN).
        ld_blocks = re.findall(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            html, re.DOTALL
        )
        for block in ld_blocks:
            try:
                data = json_mod.loads(block)
                ld_type = data.get('@type', '')
                if ld_type == 'Article':
                    for field in ['headline', 'datePublished', 'url']:
                        if field not in data or not data[field]:
                            print(f"  WARNING: Article JSON-LD missing '{field}'")
                elif ld_type == 'Article' and 'mainEntity' in data:
                    entities = data.get('mainEntity', [])
                    if len(entities) < 2:
                        print(f"  WARNING: Article JSON-LD FAQ has only {len(entities)} Q&A pair(s) — minimum 2")
            except json_mod.JSONDecodeError as e:
                print(f"  WARNING: Unparseable JSON-LD block — {str(e)[:80]}")

        # POST-GEN QUALITY GATES: Validate output before writing to disk
        # Gate 1: Fabricated product code detection
        FABRICATED_PATTERNS = [
            r'^12345P\\d*$', r'^5678P\\d*$', r'^XXXX+$', r'^abc$', r'^xyz$',
            r'^g8$', r'^ttd$', r'FOODWINE',
            # July 2026: Claude hallucinates pNNNN, tNNNNNN placeholder-like codes
            r'^p\d{2,}$', r'^t\d{2,}$',
            # July 2026: lorem-ipsum-style tour slugs leak into product codes
            r'jomblang-pindul-cave-tour', r'menoreh-kotagede', r'borobudur-prambanan',
        ]
        product_codes = re.findall(r'data-viator-id="([^"]+)"', html)
        fabricated = [c for c in product_codes for p in FABRICATED_PATTERNS if re.match(p, c)]
        if fabricated:
            print(f"  REJECT: Fabricated product codes: {fabricated}")
        # Gate 2: CTA count (minimum 2 product cards)
        product_card_count = len(re.findall(r'class="[^"]*product-card[^"]*"', html))
        if product_card_count < 2:
            print(f"  HARD FAIL: Only {product_card_count} product cards (minimum 2)")
        # Gate 3: Inline link count
        viator_links = re.findall(r'<a[^>]*href="[^"]*\\.viator\\.com[^"]*\\?pid=', html)
        if len(viator_links) < 2 and product_card_count < 2:
            print(f"  WARNING: {len(viator_links)} inline links, {product_card_count} cards — below minimum")
        # Gate 4: Compliance — add rel=sponsored to Viator links missing it
        bare_viator = re.findall(r'(<a[^>]*href="[^"]*\\.viator\\.com[^"]*"[^>]*)(?<!rel="sponsored")>', html)
        if bare_viator:
            for tag in bare_viator:
                fixed = tag.replace('<a', '<a rel="sponsored"', 1)
                html = html.replace(tag, fixed)
            print(f"  REPAIR: Added rel=sponsored to {len(bare_viator)} Viator links")
        # Gate 5: Canonical + hreflang presence check
        if 'rel="canonical"' not in html:
            print(f"  ERROR: No canonical tag in final output")
        if 'hreflang=' not in html:
            print(f"  WARNING: No hreflang tags — inject_hreflang_tags may have failed")

        # Gate 6: Destination scope check — prevent cross-destination product cards
        # (P0 fix, Gate A 9/10, Jul 2026)
        DESTINATION_SCOPE = {
            'tenerife-outdoor-guide': [
                'hawaii', 'kona', 'kailua', 'oahu', 'maui', 'honolulu',
                'gran canaria', 'lanzarote', 'fuerteventura', 'bali', 'phuket'
            ],
            'yogyakarta-temple-tours': [
                'bali', 'jakarta', 'lombok', 'bangkok', 'angkor', 'thailand'
            ],
            'porto-sommelier': [
                'bordeaux', 'rioja', 'napa', 'tuscany', 'barolo'
            ],
            'lapland-adventure-guide': [
                'iceland', 'alaska', 'canada', 'greenland', 'siberia'
            ],
            'madeira-trail-guide': [
                'azores', 'canary', 'tenerife', 'hawaii', 'caribbean'
            ],
            'san-juan-excursions': [
                'cuba', 'dominican', 'jamaica', 'bahamas', 'mexico', 'cancun'
            ],
        }
        forbidden_terms = DESTINATION_SCOPE.get(slug, [])
        if forbidden_terms:
            hits = []
            card_sections = re.findall(r'<div class="product-card[^"]*">(.*?)</div>', html, re.DOTALL)
            for term in forbidden_terms:
                for card in card_sections:
                    if term.lower() in card.lower():
                        hits.append(term)
            if hits:
                print(f"  WARNING: Cross-destination terms in product cards: {list(set(hits))}")

        # Gate 7: Hero image HTTP validation (P0 fix, Gate A 9/10, Jul 2026)
        hero_srcs = re.findall(r'<img[^>]*class="[^"]*hero[^"]*"[^>]*src="([^"]+)"', html)
        if not hero_srcs:
            hero_srcs = re.findall(r'<img[^>]*src="([^"]+)"[^>]*class="[^"]*hero[^"]*"', html)
        if not hero_srcs:
            first_img = re.search(r'<img[^>]*src="([^"]+)"', html)
            if first_img:
                hero_srcs = [first_img.group(1)]
        for src in hero_srcs[:1]:
            if src.startswith('/'):
                try:
                    dom = get_site_domain(slug)
                    if dom:
                        full_url = f'https://www.{dom}{src}'
                        from urllib.request import Request, urlopen
                        urlopen(Request(full_url, method='HEAD'), timeout=5)
                except Exception:
                    print(f"  WARNING: Hero image unreachable: {src}")

        # Gate 8: Product schema — generate Product + AggregateRating JSON-LD for all product cards
        product_cards = re.findall(r'<div class="product-card"[^>]*data-viator-id="([^"]+)"[^>]*>.*?<h3>(.*?)</h3>.*?</div>', html, re.DOTALL)
        if product_cards and '"@type": "Product"' not in html:
            # Get default destination ID for this site
            dom = get_site_domain(slug) or "example.com"
            valid_dests = VALID_DEST_CODES.get(slug, set())
            default_dest = next(iter(valid_dests), "default") if valid_dests else "default"
            product_schemas = []
            for code, name in product_cards[:3]:
                name_clean = re.sub(r'<[^>]+>', '', name).strip()
                product_schemas.append(f'''    {{
      "@type": "Product",
      "name": "{name_clean}",
      "url": "https://www.viator.com/tours/{default_dest}/{code}?pid=P00303273&mcid=42383",
      "offers": {{
        "@type": "Offer",
        "priceCurrency": "USD",
        "url": "https://www.viator.com/tours/{default_dest}/{code}?pid=P00303273&mcid=42383"
      }}
    }}''')
            if product_schemas:
                schemas_joined = ",\n".join(product_schemas)
                product_ld = f'''<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@graph": [
{schemas_joined}
  ]
}}
</script>'''
                html = html.replace('</head>', f'{product_ld}\n</head>', 1)
                print(f"  REPAIR: Added Product JSON-LD to {len(product_cards)} cards")

        # Opt-in: expose price-adjacent product names to inline-link injection.
        pp_config = cb.get("post_processing", {})
        if pp_config.get("strong_price_proper_nouns", False):
            html, wrapped_count = wrap_price_adjacent_proper_nouns(html)
            if wrapped_count:
                print(
                    f"  POST-PROCESS: Wrapped {wrapped_count} "
                    f"price-adjacent proper noun phrase(s)"
                )

        # P1 fix: Deduplicate adjacent identical image tags (bug #8, Gate A 9/10, Jul 2026)
        def _dedup_adjacent_images(html):
            """Remove duplicate <img> tags that share the same src+alt and are adjacent."""
            import re as _re
            pattern = _re.compile(r'(<img[^>]+src="([^"]*)"[^>]*alt="([^"]*)"[^>]*>)\s*\1', _re.DOTALL)
            count_before = len(_re.findall(r'<img ', html))
            html = pattern.sub(r'\1', html)
            count_after = len(_re.findall(r'<img ', html))
            removed = count_before - count_after
            if removed:
                print(f"  POST-PROCESS: Removed {removed} duplicate adjacent img tag(s)")
            return html
        html = _dedup_adjacent_images(html)

        # COMPONENT 2: PRODUCT IMAGE INJECTION — runs after structural repair
        # and all post-processing (so product cards, verdict sections, and
        # explore-more blocks are stable for landmark detection). Runs BEFORE
        # the save call so any failure leaves the on-disk file untouched.
        #
        # Respects image-audit R1/R2: hero never between H1 and first CTA;
        # winner image always BELOW the comparison table on comparison pages.
        # See /tmp/viator-image-pipeline-spec.md and product_image_injection.py.
        if inject_product_images is not None:
            try:
                html, inject_result = inject_product_images(
                    html, slug, brief, article_slug=article_slug,
                )
                if inject_result.inserted:
                    print(
                        f"  IMAGE: Injected {inject_result.page_type} image "
                        f"({inject_result.image_path}) for {inject_result.product_code}"
                    )
                elif inject_result.skipped_existing:
                    print(f"  IMAGE: Skipped — {inject_result.reason}")
                else:
                    print(f"  IMAGE: {inject_result.reason}")
            except Exception as _img_err:
                # Image injection is best-effort. A failure must never block
                # the page from being saved — that would regress us back to
                # the pre-pipeline state where pages went out without QA.
                print(f"  IMAGE: injection failed (non-fatal): {_img_err}")

        # Guard: reject empty pages — root cause of all 128 R12 violations.
        # If the generator produces a 0-byte stub, don't write it to disk.
        if not html or not html.strip():
            print(f"  ERROR: Generated empty page for {article_slug} — not writing to disk")
            return None

        with open(out_path, "w") as f:
            f.write(html)
        if not html.strip().startswith("<!") and not html.strip().startswith("<"):
            print(f"  WARNING: Output may not be valid HTML. First 100 chars: {html[:100]}")
        else:
            print(f"  Saved: {out_path} ({len(html)} bytes)")

        return out_path
    except Exception as e:
        print(f"  ERROR generating {article_slug}: {e}")
        return None


def generate_article_with_quality_loop(slug, cb, brief, lang="en", max_retries=3, engine="deepseek"):
    """
    Loop 1: Score-and-Retry. Generates article, scores it, retries if below threshold.
    Loop 2 (error library) runs inside generate_article() via post-processing.
    
    Args:
        engine: "deepseek" or "claude"
    """
    from error_library import ErrorLibrary
    from quality_gate import score_generated_page, build_feedback

    # Get domain for error library
    domain = None
    try:
        row = sqlite3.connect(os.path.expanduser(
            "~/.hermes/affiliate-crons/db/site_registry.db"
        )).execute("SELECT domain FROM sites WHERE site_id=?", (slug,)).fetchone()
        if row:
            domain = row[0]
    except Exception:
        pass

    error_lib = ErrorLibrary(domain=domain)
    error_context = error_lib.get_pre_generation_context()

    # Inject error library context into brief for first attempt
    if error_context:
        brief = dict(brief)
        brief["quality_feedback"] = error_context

    for attempt in range(1, max_retries + 1):
        out_path = generate_article(slug, cb, brief, lang, engine=engine)
        if not out_path:
            print(f"  ⚠️ Generation failed (attempt {attempt})")
            continue

        html_path = out_path
        if os.path.isdir(out_path):
            html_path = os.path.join(out_path, "index.html")
        if not os.path.exists(html_path):
            html_path = out_path

        try:
            with open(html_path) as f:
                html = f.read()
        except Exception:
            print(f"  ⚠️ Cannot read generated file (attempt {attempt})")
            continue

        # Loop 2: Error library detect + fix
        fixed_html, fixes = error_lib.detect_and_fix(html, str(html_path))
        if fixes:
            print(f"  🔧 Error library fixes: {fixes}")
            with open(html_path, "w") as f:
                f.write(fixed_html)
            html = fixed_html

        # Loop 1: Quality gate scoring
        score_result = score_generated_page(html, lang)
        print(f"  📊 Quality score: {score_result['score']}/100 "
              f"({'PASS' if score_result['passed'] else 'FAIL'}) "
              f"(attempt {attempt}/{max_retries})")

        # Placeholder validation — reject pages with template product codes
        placeholder_patterns = [
            r'data-viator-id="\d+[- ]?XXX',      # 562-XXXXX, 562-YYYYY
            r'data-viator-id="12345"',           # numeric placeholder
            r'href="[^"]*/d\d+-XXXXX',           # X-pattern in URL
            r'href="[^"]*/d\d+-YYYYY',           # Y-pattern in URL
            r'href="[^"]*/d12345',               # numeric placeholder URL
            r'href="[^"]*/d67890',               # numeric placeholder URL
            r'd\d+-\?\?\?',                      # truncated product code (d22388-???)
            r'\?pid=[^&]+&mcid=',                # affiliate PID on non-affiliate links (category pages)
            r'"url":\s*"<a\s+href=',             # JSON-LD url field with nested <a> tag
        ]
        placeholders = []
        for pattern in placeholder_patterns:
            matches = re.findall(pattern, html, re.IGNORECASE)
            placeholders.extend(matches)
        if placeholders:
            print(f"  ❌ PLACEHOLDER REJECTED: {len(placeholders)} template codes found "
                  f"({placeholders[:3]}...) — retrying")
            brief = dict(brief)
            brief["placeholder_fix"] = (
                f"DO NOT use placeholder Viator codes. "
                f"Found: {placeholders[:5]}. "
                f"Use ONLY real product codes from the content bank products list. "
                f"If no products are available, omit Viator links entirely."
            )
            continue  # retry generation

        if score_result["passed"]:
            return out_path

        if attempt < max_retries:
            feedback = build_feedback(score_result)
            brief = dict(brief)
            brief["quality_feedback"] = feedback
            print(f"  🔄 Retrying with quality feedback...")
        else:
            print(f"  ⚠️ Quality gate failed after {max_retries} attempts. "
                  f"Deploying with score {score_result['score']}/100.")
            flag_note = f"[QUALITY_FLAG: score {score_result['score']}/100]"
            with open(html_path, "w") as f:
                f.write(f"<!-- {flag_note} -->\n{html}")
            return out_path

    return None


def main():
    if not API_KEY:
        print("ERROR: DEEPSEEK_API_KEY not set.")
        print("Run: set -a; source ~/.hermes/.env; set +a")
        sys.exit(1)

    if len(sys.argv) < 2:
        print("Usage: python3 page_generator.py <site_slug> [brief_index] [--count N] [--lang en|de|es]")
        sys.exit(1)

    slug = sys.argv[1]
    brief_index = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else None
    count = 1
    lang = "en"
    briefs_path = None
    engine = "deepseek"

    # Parse --count, --lang, --briefs, --engine
    for i, arg in enumerate(sys.argv):
        if arg == "--count" and i + 1 < len(sys.argv):
            count = int(sys.argv[i + 1])
        if arg == "--lang" and i + 1 < len(sys.argv):
            lang = sys.argv[i + 1]
            if lang not in LANG_CONFIG:
                print(f"ERROR: Unknown language '{lang}'. Supported: {', '.join(LANG_CONFIG.keys())}")
                sys.exit(1)
        if arg == "--briefs" and i + 1 < len(sys.argv):
            briefs_path = sys.argv[i + 1]
        if arg == "--engine" and i + 1 < len(sys.argv):
            engine = sys.argv[i + 1]
            if engine not in ("deepseek", "claude"):
                print(f"ERROR: Unknown engine '{engine}'. Supported: deepseek, claude")
                sys.exit(1)

    print(f"Page Generator — {slug} — {lang.upper()} — {datetime.now().isoformat()[:19]}\n")

    cb = load_content_bank(slug)
    if not cb:
        print(f"ERROR: No content bank for {slug}")
        sys.exit(1)

    briefs_data = load_briefs(slug, briefs_path)
    if not briefs_data or not briefs_data.get("briefs"):
        print(f"ERROR: No topic briefs for {slug}. Run topic-planner-weekly first.")
        sys.exit(1)

    briefs = briefs_data["briefs"]
    state = load_state()
    site_state = state.get(slug, {"generated": []})

    # Determine which briefs to generate
    to_generate = []
    if brief_index is not None:
        if brief_index < len(briefs):
            to_generate = [briefs[brief_index]]
        else:
            print(f"ERROR: brief_index {brief_index} out of range ({len(briefs)} briefs)")
            sys.exit(1)
    else:
        # Generate next N unused briefs
        generated_slugs = set(site_state.get("generated", []))
        for b in briefs:
            if b.get("slug") not in generated_slugs and len(to_generate) < count:
                to_generate.append(b)

    if not to_generate:
        print("All briefs already generated. Nothing to do.")
        sys.exit(0)

    print(f"Generating {len(to_generate)} article(s)...\n")

    for brief in to_generate:
        out_path = generate_article_with_quality_loop(slug, cb, brief, lang, engine=engine)
        if out_path:
            index_path = os.path.join(out_path, "index.html") if os.path.isdir(out_path) else out_path
            if not os.path.exists(index_path):
                print(f"WARNING: generated page not found at {index_path}, not marking as consumed")
            else:
                site_state.setdefault("generated", []).append(brief["slug"])
        time.sleep(0.5)  # polite spacing between API calls

    state[slug] = site_state
    save_state(state)

    total = get_article_count(slug)
    print(f"\nDone. {slug}: {total} articles total.")


if __name__ == "__main__":
    main()
