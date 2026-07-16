#!/usr/bin/env python3
"""
Pipeline Benchmark — metric capture with N-run averaging.

Regenerates Golden Eval pages against the current pipeline, captures
quantitative metrics with variance bands. Used by pipeline_dif.py
for controlled baseline vs. candidate comparison.

Usage: pipeline_benchmark.py [--runs N] [--output FILE]
"""
import sys, os, json, subprocess, time, hashlib
from pathlib import Path
from datetime import datetime

SCRIPTS_DIR = Path.home() / ".hermes" / "affiliate-crons" / "scripts"
CONFIG_DIR = Path.home() / ".hermes" / "affiliate-crons" / "config"
STATE_DIR = Path.home() / ".hermes" / "affiliate-crons" / "state"
GENERATED_DIR = Path.home() / ".hermes" / "affiliate-crons" / "generated"
ENV_FILE = Path.home() / ".hermes" / ".env"
EVAL_CONFIG = CONFIG_DIR / "golden-eval-pages.json"


def source_env():
    """Source .env for API keys, unset GIT env vars."""
    env = os.environ.copy()
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    val = val.strip().strip('"').strip("'")
                    env[key.strip()] = val
    for v in ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"]:
        env.pop(v, None)
    return env


def run_script(script_name, args, env, timeout=300):
    """Run a script, return (exit_code, stdout)."""
    cmd = [sys.executable, str(SCRIPTS_DIR / script_name)] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           env=env, timeout=timeout, cwd=str(SCRIPTS_DIR))
        return r.returncode, r.stdout + "\n" + r.stderr
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
    except Exception as e:
        return -1, str(e)


def count_anti_words(html):
    """Count anti-word hits using antiword_scan.py CLI."""
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
        f.write(html)
        tmp_path = f.name
    try:
        rc, out = run_script("antiword_scan.py", [tmp_path], source_env(), timeout=30)
        # Parse count from output: "X anti-word hit(s)"
        import re
        m = re.search(r'(\d+)\s+anti-word\s+hit', out, re.IGNORECASE)
        if m:
            return int(m.group(1))
        m = re.search(r'(\d+)\s+hit', out, re.IGNORECASE)
        if m:
            return int(m.group(1))
        return 0
    finally:
        os.unlink(tmp_path)


def count_viator_links(html):
    """Count viator.com hrefs in the page."""
    import re
    return len(re.findall(r'href="[^"]*viator\.com[^"]*"', html, re.IGNORECASE))


def count_editorial_words(html):
    """Count words in editorial content (exclude scripts, styles, meta, JSON-LD)."""
    import re
    body = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
    body = re.sub(r'<style[^>]*>.*?</style>', '', body, flags=re.DOTALL)
    body = re.sub(r'<meta[^>]*>', '', body)
    body = re.sub(r'<link[^>]*>', '', body)
    body = re.sub(r'<head>.*?</head>', '', body, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', body)
    return len(text.split())


def score_jsonld(html):
    """Score JSON-LD completeness (0-100)."""
    import re
    score = 0
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
    if not blocks:
        return 0
    for block in blocks:
        try:
            data = json.loads(block)
            if isinstance(data, list):
                data = data[0] if data else {}
            at_type = str(data.get("@type", ""))
            if "WebPage" in at_type or "Article" in at_type:
                score += 30
                if data.get("headline") or data.get("name"):
                    score += 10
                if data.get("description"):
                    score += 10
            if "FAQPage" in at_type:
                score += 30
                if data.get("mainEntity"):
                    score += 20
            if "Product" in at_type:
                score += 30
                if data.get("name") and data.get("offers"):
                    score += 20
        except (json.JSONDecodeError, AttributeError):
            pass
    return min(score, 100)


def score_structural(html):
    """Score structural elements (0-5): nav, author-block, footer, OG title, OG description."""
    score = 0
    if '<nav' in html or '<div class="nav' in html:
        score += 1
    if 'author-block' in html or 'class="author' in html:
        score += 1
    if '<footer' in html:
        score += 1
    if 'property="og:title"' in html:
        score += 1
    if 'property="og:description"' in html:
        score += 1
    return score


def benchmark_page(site_slug, slug, env, runs=3):
    """Regenerate a page N times, capture metrics with variance."""
    metrics = {
        "anti_words": [], "viator_links": [], "editorial_words": [],
        "jsonld_score": [], "structural_score": [], "latency_seconds": [],
        "tokens": []
    }

    for i in range(runs):
        t0 = time.time()
        rc, out = run_script("page_generator.py",
                             [site_slug, "--briefs", filtered_path,
                              "--count", "1", "--temp", "0"],
                             env, timeout=300)
        latency = time.time() - t0

        # Estimate tokens from output (DeepSeek reports usage in stderr)
        tokens = 0
        import re as _re2
        tm = _re2.search(r'usage.*?total_tokens[:\s]+(\d+)', out, _re2.IGNORECASE)
        if tm:
            tokens = int(tm.group(1))

        # Find generated file
        gen_dir = GENERATED_DIR / site_slug
        html = ""
        if gen_dir.exists():
            for f in sorted(gen_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if f.suffix == ".html":
                    html = f.read_text()
                    break

        if not html:
            # Try direct site path
            import sqlite3
            db = Path.home() / ".hermes" / "affiliate-crons" / "db" / "site_registry.db"
            conn = sqlite3.connect(str(db))
            row = conn.execute(
                "SELECT local_path FROM sites WHERE site_id=?", (site_slug,)
            ).fetchone()
            conn.close()
            if row:
                site_dir = Path(row[0])
                page_path = site_dir / f"{slug}.html"
                if page_path.exists():
                    html = page_path.read_text()

        if html:
            metrics["anti_words"].append(count_anti_words(html))
            metrics["viator_links"].append(count_viator_links(html))
            metrics["editorial_words"].append(count_editorial_words(html))
            metrics["jsonld_score"].append(score_jsonld(html))
            metrics["structural_score"].append(score_structural(html))
            metrics["latency_seconds"].append(latency)
            metrics["tokens"].append(tokens)
        else:
            # Page generation failed
            metrics["anti_words"].append(None)
            metrics["viator_links"].append(0)
            metrics["editorial_words"].append(0)
            metrics["jsonld_score"].append(0)
            metrics["structural_score"].append(0)
            metrics["latency_seconds"].append(latency)
            metrics["tokens"].append(0)

        time.sleep(0.5)

    # Compute stats
    def stats(vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            return {"mean": 0, "std": 0, "min": 0, "max": 0, "n": 0}
        mean = sum(vals) / len(vals)
        variance = sum((v - mean) ** 2 for v in vals) / len(vals)
        return {
            "mean": round(mean, 2),
            "std": round(variance ** 0.5, 2),
            "min": min(vals),
            "max": max(vals),
            "n": len(vals)
        }

    return {k: stats(metrics[k]) for k in metrics}


def main():
    runs = 3
    output_file = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--runs" and i + 1 < len(args):
            runs = int(args[i + 1])
            i += 2
        elif args[i] == "--output" and i + 1 < len(args):
            output_file = args[i + 1]
            i += 2
        else:
            i += 1

    env = source_env()

    # Load eval config
    if not EVAL_CONFIG.exists():
        print(f"ERROR: {EVAL_CONFIG} not found")
        sys.exit(1)

    with open(EVAL_CONFIG) as f:
        eval_config = json.load(f)

    # Hash the config for pinning
    config_hash = hashlib.sha256(
        json.dumps(eval_config, sort_keys=True).encode()
    ).hexdigest()[:16]

    pages = eval_config.get("pages", [])
    if not pages:
        print("ERROR: No pages in golden-eval-pages.json")
        sys.exit(1)

    results = {
        "eval_config_hash": config_hash,
        "runs": runs,
        "timestamp": datetime.now().isoformat(),
        "pages": {},
        "aggregate": {}
    }

    print(f"Pipeline Benchmark — {runs} runs × {len(pages)} pages")
    print()

    for page in pages:
        site = page["site"]
        slug = page["slug"]
        print(f"  {site}/{slug} ...", end=" ", flush=True)
        page_metrics = benchmark_page(site, slug, env, runs)
        results["pages"][f"{site}/{slug}"] = page_metrics
        aw = page_metrics["anti_words"]["mean"]
        vl = page_metrics["viator_links"]["mean"]
        ew = page_metrics["editorial_words"]["mean"]
        print(f"anti:{aw:.1f} links:{vl:.0f} words:{ew:.0f}")

    # Compute aggregate
    all_metrics = ["anti_words", "viator_links", "editorial_words",
                   "jsonld_score", "structural_score", "latency_seconds", "tokens"]
    aggregate = {}
    for metric in all_metrics:
        means = [results["pages"][p][metric]["mean"]
                 for p in results["pages"]]
        mean_val = sum(means) / len(means) if means else 0
        aggregate[f"mean_{metric}"] = round(mean_val, 2)
    results["aggregate"] = aggregate

    # Output
    output = json.dumps(results, indent=2)
    if output_file:
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w") as f:
            f.write(output)
        print(f"\nSaved: {output_file}")
    else:
        print(output)


if __name__ == "__main__":
    main()
