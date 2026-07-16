#!/usr/bin/env python3
"""
quality_gate.py — Content Quality Gate (Loop 1: Score-and-Retry)

Scores generated HTML against 4 criteria. Used by page_generator.py's
generate_with_quality_loop() to decide: deploy or retry.

Runs AFTER error_library.py (Loop 2) has applied deterministic fixes.
Runs AFTER strip_antiwords.

Scoring:
  product_cards ≥ 2    → 30 points  (0 cards = 0, 1 = 15, 2+ = 30)
  anti_words = 0       → 30 points  (each hit = -10, floor 0)
  no template vars     → 20 points  (any {TEMPLATE_VAR} = 0)
  wordcount > 1500     → 20 points  (<800 = 0, 800-1500 = 10, >1500 = 20)
  TOTAL = 100 (all clean)

Threshold: score ≥ 80 to pass (must have product cards AND no template vars)
"""

import re
import sys
from pathlib import Path

# Ensure affiliate-crons/ is on path so 'from scripts.antiword_scan' resolves
# when quality_gate is imported from a different working directory
_affiliate_dir = str(Path(__file__).resolve().parent.parent)
if _affiliate_dir not in sys.path:
    sys.path.insert(0, _affiliate_dir)


# Template variable patterns that should never appear in output
TEMPLATE_VAR_PATTERNS = [
    r'\{DOMAIN\}',
    r'\{\{DOMAIN\}\}',
    r'\{PRODUCT_URL\}',
    r'\{\{PRODUCT_URL\}\}',
    r'\{PRODUCT_ID\}',
    r'\{\{PRODUCT_ID\}\}',
    r'\{PRODUCT_NAME\}',
    r'\{\{PRODUCT_NAME\}\}',
]


def score_generated_page(html, lang="en"):
    """Score generated HTML. Returns {score, passed, checks, failures}."""
    checks = {}
    failures = []

    # 1. Product cards (30 points)
    product_cards = len(re.findall(r'class="product-card"', html))
    if product_cards >= 2:
        checks["product_cards"] = {"score": 30, "detail": f"{product_cards} cards"}
    elif product_cards == 1:
        checks["product_cards"] = {"score": 15, "detail": f"{product_cards} card (need ≥2)"}
        failures.append(f"only {product_cards} product card (need ≥2)")
    else:
        checks["product_cards"] = {"score": 0, "detail": "0 cards"}
        failures.append("0 product cards (need ≥2)")

    # 2. Anti-words (30 points) — count remaining after strip
    from scripts.antiword_scan import EN_ANTI, DE_ANTI, ES_ANTI

    anti_patterns = {"en": EN_ANTI, "de": DE_ANTI, "es": ES_ANTI}
    anti_re = anti_patterns.get(lang, EN_ANTI)
    anti_hits = len(anti_re.findall(html))
    anti_score = max(0, 30 - (anti_hits * 10))
    checks["anti_words"] = {"score": anti_score, "detail": f"{anti_hits} hits"}
    if anti_hits > 0:
        failures.append(f"{anti_hits} anti-word(s) found")

    # 3. Template variables (20 points)
    template_vars_found = []
    for pattern in TEMPLATE_VAR_PATTERNS:
        matches = re.findall(pattern, html)
        template_vars_found.extend(matches)

    if template_vars_found:
        checks["template_vars"] = {"score": 0, "detail": f"found: {template_vars_found}"}
        failures.append(f"unrendered template vars: {template_vars_found}")
    else:
        checks["template_vars"] = {"score": 20, "detail": "clean"}

    # 4. Editorial depth / wordcount (20 points)
    # Strip HTML tags, count words in body
    body_match = re.search(r'<body[^>]*>(.*?)</body>', html, re.DOTALL)
    body_text = body_match.group(1) if body_match else html
    # Remove scripts, styles, HTML tags
    body_text = re.sub(r'<script[^>]*>.*?</script>', '', body_text, flags=re.DOTALL)
    body_text = re.sub(r'<style[^>]*>.*?</style>', '', body_text, flags=re.DOTALL)
    body_text = re.sub(r'<[^>]+>', ' ', body_text)
    body_text = re.sub(r'\s+', ' ', body_text).strip()
    word_count = len(body_text.split())

    if word_count > 1500:
        checks["wordcount"] = {"score": 20, "detail": f"{word_count} words"}
    elif word_count >= 800:
        checks["wordcount"] = {"score": 10, "detail": f"{word_count} words (need >1500)"}
        failures.append(f"wordcount {word_count} < 1500")
    else:
        checks["wordcount"] = {"score": 0, "detail": f"{word_count} words (thin)"}
        failures.append(f"wordcount {word_count} < 800 (thin)")

    # Total
    total_score = sum(c["score"] for c in checks.values())
    passed = total_score >= 80

    return {
        "score": total_score,
        "passed": passed,
        "checks": checks,
        "failures": failures,
    }


def build_feedback(score_result):
    """Build feedback string for retry prompt."""
    if score_result["passed"]:
        return ""

    failures = score_result["failures"]
    checks_detail = []
    for name, check in score_result["checks"].items():
        if check["score"] < check.get("max", 30):  # default max if not specified
            checks_detail.append(f"  - {name}: {check['detail']}")

    feedback = (
        "Previous generation attempt FAILED quality checks. "
        "You MUST fix these issues in your response:\n"
        + "\n".join(f"  • {f}" for f in failures)
        + "\n\nCurrent scores:\n"
        + "\n".join(checks_detail)
        + f"\n\nTotal score: {score_result['score']}/100 (need ≥80 to pass). "
          "Regenerate the complete page now with all fixes applied."
    )
    return feedback


# For backwards compatibility with existing imports in page_generator.py
def strip_antiwords_in_generated(html):
    """Pass-through — anti-word stripping is handled separately."""
    return html


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: quality_gate.py <html_file> [--lang de|es]")
        sys.exit(1)

    filepath = sys.argv[1]
    lang = "en"
    if "--lang" in sys.argv:
        idx = sys.argv.index("--lang")
        lang = sys.argv[idx + 1]

    with open(filepath) as f:
        html = f.read()

    result = score_generated_page(html, lang)
    print(f"Score: {result['score']}/100 → {'PASS' if result['passed'] else 'FAIL'}")
    for name, check in result["checks"].items():
        print(f"  {name}: {check['score']}pts — {check['detail']}")
    if result["failures"]:
        print(f"\nFailures: {result['failures']}")
        print(f"\nFeedback for retry:\n{build_feedback(result)}")
