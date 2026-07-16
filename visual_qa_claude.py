#!/usr/bin/env python3
"""
Visual QA via Claude CLI — score 5 criteria, return fixes or PASS.
Usage: python3 visual_qa_claude.py <url> [--threshold 20]
Gate: 20/25 passes. 25/25 is perfect.

Requires: Claude CLI at ~/.hermes/node/bin/claude
          ANTHROPIC_API_KEY in environment
"""
import subprocess, sys, os, json, re
from datetime import datetime

def run_claude_vision(url):
    """Run Claude CLI with browser_vision prompt, return scores + fixes."""
    prompt = f"""You are a visual QA reviewer. Analyze this URL: {url}

Use browser_navigate({url}) then browser_vision to inspect the page.

Score 1-5 on each of these criteria:
1. Text readability — is prose constrained to ~72ch? Is line-height comfortable? Dark-on-light contrast?
2. Spacing — consistent section rhythm? No crowding? Cards have breathing room?
3. Image sizing — hero loads? No broken images? Gallery images uniform? Aspect ratios correct?
4. Mobile layout — does it collapse gracefully? Nav becomes hamburger? Text wraps without overflow?
5. Visual hierarchy — heading scale clear? H1 > H2 > H3? CTA buttons stand out? Affiliate disclosure present?

Return EXACTLY this JSON format (no other text):
{{"scores":{{"readability":X,"spacing":X,"images":X,"mobile":X,"hierarchy":X}},"total":Y,"verdict":"PASS" or "FIX","fixes":["fix1","fix2"]}}

Terse. No explanations outside the JSON."""
    
    claude = os.path.expanduser("~/.hermes/node/bin/claude")
    result = subprocess.run(
        [claude, "-p", "--max-turns", "6", prompt],
        capture_output=True, text=True, timeout=180,
        env={**os.environ}
    )
    
    output = result.stdout.strip()
    
    # Extract JSON from output (Claude may add text around it)
    json_match = re.search(r'\{.*"scores".*\}', output, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
    
    # Fallback: parse manually
    scores = {}
    for dim in ["readability", "spacing", "images", "mobile", "hierarchy"]:
        m = re.search(rf'"{dim}":\s*(\d)', output)
        if m:
            scores[dim] = int(m.group(1))
    
    total = sum(scores.values())
    verdict = "PASS" if total >= 20 else "FIX"
    
    fixes = re.findall(r'"fixes":\s*\[(.*?)\]', output, re.DOTALL)
    fix_list = []
    if fixes:
        fix_list = [f.strip('"').strip() for f in fixes[0].split('","') if f.strip()]
    
    return {"scores": scores, "total": total, "verdict": verdict, "fixes": fix_list}

def main():
    if len(sys.argv) < 2:
        print("Usage: visual_qa_claude.py <url> [--threshold 20]")
        sys.exit(1)
    
    url = sys.argv[1]
    threshold = 20
    for arg in sys.argv[2:]:
        if arg.startswith("--threshold="):
            threshold = int(arg.split("=")[1])
    
    print(f"🔍 Visual QA: {url}")
    try:
        result = run_claude_vision(url)
    except subprocess.TimeoutExpired:
        print("❌ TIMEOUT: Claude CLI exceeded 180s")
        sys.exit(1)
    except FileNotFoundError:
        print("❌ Claude CLI not found at ~/.hermes/node/bin/claude")
        sys.exit(1)
    
    scores = result.get("scores", {})
    total = result.get("total", 0)
    verdict = result.get("verdict", "FIX")
    fixes = result.get("fixes", [])
    
    print(f"\n📊 Scores ({total}/25):")
    for dim, score in scores.items():
        bar = "▓" * score + "░" * (5 - score)
        print(f"  {dim:15s} [{bar}] {score}/5")
    
    icon = "✅" if total >= threshold else "❌"
    print(f"\n{icon} {verdict} (threshold: {threshold}/25)")
    
    if fixes:
        print(f"\n🔧 Fixes needed ({len(fixes)}):")
        for fix in fixes:
            print(f"  • {fix}")
    
    if total < threshold:
        sys.exit(1)

if __name__ == "__main__":
    main()
