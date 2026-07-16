#!/usr/bin/env python3
"""
prompt_optimizer.py — Generator Prompt Optimizer (Loop 3)

Weekly regression detection for page_generator prompts.
Generates golden briefs, scores them, compares to baseline.
Reports regressions only. Human reviews, no auto-apply.

Usage:
  prompt_optimizer.py --check          # Run all, report regressions
  prompt_optimizer.py --check --verbose  # Report even if passing
  prompt_optimizer.py --baseline       # Store current scores as baseline
"""

import json
import os
import sys
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# Self-contained env loading — cron doesn't source .env
from dotenv import load_dotenv
load_dotenv(Path.home() / ".hermes" / ".env")

# Ensure affiliate-crons/ is on path for 'from scripts.xxx' imports
_s = str(Path(__file__).resolve().parent.parent)
if _s not in sys.path:
    sys.path.insert(0, _s)


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
GOLDEN_PATH = DATA_DIR / "golden_briefs.json"
BASELINE_PATH = DATA_DIR / "prompt_baseline.json"


def load_golden():
    with open(GOLDEN_PATH) as f:
        return json.load(f)


def load_baseline():
    if BASELINE_PATH.exists():
        with open(BASELINE_PATH) as f:
            return json.load(f)
    return {"baselines": {}, "version": 1}


def save_baseline(data):
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(BASELINE_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)


def score_brief(brief):
    """Run a single golden brief through the quality gate. Returns score dict."""
    slug = "porto-sommelier"  # default test site
    lang = brief.get("language", "en")

    script = Path(__file__).resolve().parent / "page_generator.py"

    # Write brief to temp file for page_generator to read
    import tempfile
    tmp_briefs = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump({"briefs": [brief]}, tmp_briefs)
    tmp_briefs.close()

    result = subprocess.run(
        [sys.executable, str(script), slug, "0", "--lang", lang, "--briefs", tmp_briefs.name],
        capture_output=True, text=True, timeout=300
    )

    os.unlink(tmp_briefs.name)

    if result.returncode != 0:
        return {"error": f"generation failed (exit {result.returncode})", 
                "stderr": result.stderr[:200]}

    # Run quality gate on the generated output
    # The generator saves to OUTPUT_DIR/slug/<brief_slug>.html
    from scripts.quality_gate import score_generated_page

    gen_path = Path.home() / ".hermes" / "affiliate-crons" / "generated" / slug / f"{brief['slug']}.html"
    if not gen_path.exists():
        return {"error": f"generated file not found: {gen_path}"}

    with open(gen_path) as f:
        html = f.read()

    score = score_generated_page(html, lang)
    return score


def run_check(verbose=False):
    """Run all golden briefs, compare to baseline, report regressions."""
    golden = load_golden()
    baseline_data = load_baseline()
    baselines = baseline_data.get("baselines", {})

    results = {}
    regressions = []
    new_baselines = {}

    for brief in golden["briefs"]:
        bid = brief["id"]
        print(f"  Checking: {bid} ({brief['language']}) ...", end=" ")

        score = score_brief(brief)

        if "error" in score:
            print(f"ERROR: {score['error']}")
            regressions.append({"id": bid, "error": score["error"]})
            continue

        print(f"score={score['score']}/100")

        results[bid] = score
        new_baselines[bid] = {
            "score": score["score"],
            "checks": score.get("checks", {}),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "language": brief["language"],
        }

        # Compare to baseline
        prev = baselines.get(bid)
        if prev:
            prev_score = prev.get("score", 0)
            delta = score["score"] - prev_score
            if delta < -10:
                regressions.append({
                    "id": bid,
                    "delta": delta,
                    "previous": prev_score,
                    "current": score["score"],
                    "failures": score.get("failures", []),
                })

    # Save new baselines
    baseline_data["baselines"] = new_baselines
    baseline_data["checked_at"] = datetime.now(timezone.utc).isoformat()
    baseline_data["version"] = baseline_data.get("version", 1) + 1
    save_baseline(baseline_data)

    # Report
    print()
    if regressions:
        print("⚠️  REGRESSIONS DETECTED:")
        for r in regressions:
            if "error" in r:
                print(f"  ❌ {r['id']}: {r['error']}")
            else:
                print(f"  📉 {r['id']}: {r['previous']} → {r['current']} (Δ{r['delta']})")
                if r.get("failures"):
                    for f in r["failures"]:
                        print(f"     - {f}")
        print("\n⚠️  Review prompt before next content drip.")
        return 1
    elif verbose:
        print("✅ All golden briefs pass. No regressions.")
        for bid, score in results.items():
            print(f"  {bid}: {score['score']}/100")
    else:
        print("✅ All golden briefs pass. No regressions.")

    return 0


def store_baseline():
    """Store current scores as the new baseline (human-approved)."""
    golden = load_golden()
    new_baselines = {}

    for brief in golden["briefs"]:
        bid = brief["id"]
        print(f"  Generating baseline for: {bid} ...")
        score = score_brief(brief)
        new_baselines[bid] = {
            "score": score.get("score", 0),
            "checks": score.get("checks", {}),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "language": brief["language"],
        }
        print(f"    score={score.get('score', '?')}/100")

    baseline_data = {"baselines": new_baselines, "version": 1,
                     "created_at": datetime.now(timezone.utc).isoformat()}
    save_baseline(baseline_data)
    print(f"\n✅ Baseline stored: {len(new_baselines)} briefs")


if __name__ == "__main__":
    if "--check" in sys.argv:
        verbose = "--verbose" in sys.argv
        sys.exit(run_check(verbose=verbose))
    elif "--baseline" in sys.argv:
        store_baseline()
    else:
        print("Usage: prompt_optimizer.py --check [--verbose] | --baseline")
        sys.exit(1)
