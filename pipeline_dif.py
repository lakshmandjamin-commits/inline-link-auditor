#!/usr/bin/env python3
"""
Pipeline DIF Orchestrator — Differential Improvement Flow for content pipeline.

Orchestrates the full DIF cycle: freeze → benchmark → Gate A (hypothesis) →
apply → benchmark → Gate B (regression) → keep/revert → Gate C (iterate/stop).

Usage: pipeline_dif.py --hypothesis <diff_file_or_description>

The hypothesis is a path to a unified diff file, or a text description of
the proposed change. Gate A validates it before application.
"""
import sys, os, json, subprocess, shutil, time
from pathlib import Path
from datetime import datetime

SCRIPTS_DIR = Path.home() / ".hermes" / "affiliate-crons" / "scripts"
STATE_DIR = Path.home() / ".hermes" / "affiliate-crons" / "state"
DIF_STATE = STATE_DIR / "pipeline_dif"
SNAPSHOT_DIR = STATE_DIR / "pipeline_snapshots"
CLAUDE_CLI = Path.home() / ".hermes" / "node" / "bin" / "claude"


def run(cmd, timeout=120, **kwargs):
    """Run a command, return (exit_code, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kwargs)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"TIMEOUT after {timeout}s"
    except Exception as e:
        return -1, "", str(e)


def run_py(script, *args, timeout=300):
    """Run a Python script from SCRIPTS_DIR."""
    return run([sys.executable, str(SCRIPTS_DIR / script)] + list(args), timeout=timeout)


def gate_review(gate_name, brief_path, max_turns=3, timeout=300):
    """Run Claude Opus gate review. Returns (passed, reasoning)."""
    if not CLAUDE_CLI.exists():
        print(f"  ⚠️ Claude CLI not found at {CLAUDE_CLI} — skipping gate")
        return True, "GATE SKIPPED — Claude CLI unavailable"

    home = os.environ.get("HOME", f"/Users/{os.environ.get('USER', 'saraswati')}")
    cmd = f"{CLAUDE_CLI} -p --model opus --max-turns {max_turns} < {brief_path}"
    rc, out, err = run(
        ["bash", "-c", cmd],
        timeout=timeout,
        env={**os.environ, "HOME": home}
    )

    print(f"  {gate_name}: {out[:500] if out else err[:500]}")
    if "APPROVED" in (out + err).upper():
        return True, out[:2000]
    return False, out[:2000] if out else err[:2000]


def load_metrics(metrics_path):
    """Load benchmark metrics JSON."""
    with open(metrics_path) as f:
        return json.load(f)


def compare_metrics(baseline, candidate):
    """
    Compare baseline vs candidate metrics.
    Returns: "improved", "regressed", or "inconclusive"
    """
    b_agg = baseline["aggregate"]
    c_agg = candidate["aggregate"]

    improved = False
    regressed = False
    findings = []

    # Compare key metrics — anti-words is the primary quality signal
    b_aw = b_agg.get("mean_anti_words", 0)
    c_aw = c_agg.get("mean_anti_words", 0)
    if c_aw < b_aw:
        improved = True
        findings.append(f"anti-words: {b_aw}→{c_aw} (↓{b_aw - c_aw:.1f})")

    # Check for regressions
    for metric in ["mean_viator_links", "mean_editorial_words", "mean_jsonld_score", "mean_structural_score"]:
        b_val = b_agg.get(metric, 0)
        c_val = c_agg.get(metric, 0)
        if b_val > 0 and c_val < b_val * 0.85:  # >15% regression
            regressed = True
            findings.append(f"REGRESSION: {metric}: {b_val}→{c_val}")

    # Blocker checks
    if c_agg.get("mean_viator_links", 0) == 0:
        regressed = True
        findings.append("BLOCKER: zero Viator links in candidate")
    if c_agg.get("mean_editorial_words", 0) < 800:
        regressed = True
        findings.append("BLOCKER: editorial words <800")
    if c_agg.get("mean_jsonld_score", 0) == 0:
        regressed = True
        findings.append("BLOCKER: zero JSON-LD score")

    if regressed:
        return "regressed", findings
    if improved:
        return "improved", findings
    return "inconclusive", findings


def main():
    hypothesis = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--hypothesis" and i + 1 < len(args):
            hypothesis = args[i + 1]
            i += 2
        else:
            i += 1

    if not hypothesis:
        print("ERROR: --hypothesis required (path to diff file or change description)")
        print("Usage: pipeline_dif.py --hypothesis <diff_file_or_description>")
        return 1

    run_id = datetime.now().strftime("%Y%m%dT%H%M%SZ")
    run_dir = DIF_STATE / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"Pipeline DIF — {run_id}")
    print(f"Hypothesis: {hypothesis}")
    print(f"Run dir: {run_dir}")
    print()

    # ── Phase 0: Sanity check for dangling snapshots ──
    print("## Phase 0 — Sanity")
    rc, out, err = run_py("pipeline_snapshot.py", "status")
    print(out)
    if rc != 0:
        print("⚠️ Dangling snapshots detected — running idempotent restore...")
        # Auto-restore any dangling snapshots
        if SNAPSHOT_DIR.exists():
            for snap in SNAPSHOT_DIR.iterdir():
                if snap.is_dir() and (snap / "manifest.json").exists():
                    print(f"  Restoring {snap.name}...")
                    run_py("pipeline_snapshot.py", "restore", snap.name)
    print()

    # ── Phase 1: Freeze baseline ──
    print("## Phase 1 — Freeze Baseline")
    rc, out, err = run_py("pipeline_snapshot.py", "freeze", "baseline")
    print(out)
    if rc != 0:
        print("❌ Freeze failed")
        return 1
    print()

    # ── Phase 2: Benchmark baseline ──
    print("## Phase 2 — Benchmark Baseline (3 runs)")
    baseline_path = run_dir / "benchmark_baseline.json"
    rc, out, err = run_py("pipeline_benchmark.py", "--runs", "3", "--output", str(baseline_path), timeout=600)
    print(out)
    if rc != 0:
        print("❌ Baseline benchmark failed")
        return 1
    baseline_metrics = load_metrics(baseline_path)
    print()

    # ── Phase 3: Gate A — Hypothesis validation ──
    print("## Phase 3 — Gate A (Hypothesis Validation)")
    gate_a_brief = run_dir / "gate_a_brief.txt"
    with open(gate_a_brief, "w") as f:
        f.write(f"""Gate A — Pipeline DIF Hypothesis Validation

Proposed change: {hypothesis}

Baseline metrics (aggregate): {json.dumps(baseline_metrics.get('aggregate', {}), indent=2)}

Questions:
1. Is this a general failure pattern or page-specific?
2. Does the hypothesis address a real gap in the content pipeline?
3. Could this change regress other pages?
4. Is the expected improvement measurable?

Do NOT read any files. Reply APPROVED or NOT_APPROVED with reasoning.
""")
    passed, reasoning = gate_review("Gate A", gate_a_brief, max_turns=3)
    if not passed:
        print("❌ Gate A NOT_APPROVED — aborting DIF cycle")
        with open(run_dir / "outcome.json", "w") as f:
            json.dump({"run_id": run_id, "decision": "revert", "reason": "Gate A NOT_APPROVED",
                       "gate_a_reasoning": reasoning}, f, indent=2)
        return 1
    print()

    # ── Phase 4: Apply hypothesis ──
    print("## Phase 4 — Apply Hypothesis")
    if hypothesis.endswith(".diff") or hypothesis.endswith(".patch"):
        # Apply a diff file
        rc, out, err = run(["git", "apply", hypothesis],
                          cwd=str(SCRIPTS_DIR), timeout=30)
        if rc != 0:
            print(f"❌ git apply failed: {err[:300]}")
            print("❌ ABORTING — cannot apply hypothesis as diff")
            # Save the diff for inspection
            shutil.copy(hypothesis, run_dir / "failed_diff.patch")
            with open(run_dir / "outcome.json", "w") as f:
                json.dump({"run_id": run_id, "decision": "revert",
                           "reason": f"git apply failed: {err[:300]}"}, f, indent=2)
            return 1
        shutil.copy(hypothesis, run_dir / "diff.patch")
    else:
        # Text description — save for manual application
        diff_path = run_dir / "hypothesis.txt"
        with open(diff_path, "w") as f:
            f.write(hypothesis)
        print(f"Hypothesis saved: {diff_path}")
        print("⚠️ Manual application required — hypothesis is a description, not a diff")
        print("   Apply the change manually, then press Enter to continue...")
        try:
            input()
        except EOFError:
            print("⚠️ No interactive input available — assuming manual changes applied")
    print()

    # ── Phase 5: Benchmark candidate ──
    print("## Phase 5 — Benchmark Candidate (3 runs)")
    candidate_path = run_dir / "benchmark_candidate.json"
    rc, out, err = run_py("pipeline_benchmark.py", "--runs", "3", "--output", str(candidate_path), timeout=600)
    print(out)
    if rc != 0:
        print("❌ Candidate benchmark failed")
        # Revert on failure
        run_py("pipeline_snapshot.py", "restore", "baseline")
        return 1
    candidate_metrics = load_metrics(candidate_path)
    print()

    # ── Phase 6: Gate B — Regression check ──
    print("## Phase 6 — Gate B (Regression Check)")
    verdict, findings = compare_metrics(baseline_metrics, candidate_metrics)
    for f_item in findings:
        print(f"  {f_item}")

    gate_b_brief = run_dir / "gate_b_brief.txt"
    with open(gate_b_brief, "w") as f:
        f.write(f"""Gate B — Pipeline DIF Regression Check

Comparison verdict: {verdict}
Findings: {', '.join(findings)}

Baseline: {json.dumps(baseline_metrics.get('aggregate', {}), indent=2)}
Candidate: {json.dumps(candidate_metrics.get('aggregate', {}), indent=2)}

Decision rule:
- If REGRESSED → REVERT mandatory
- If IMPROVED → KEEP (if no blocker)
- If INCONCLUSIVE → default KEEP

Do NOT read any files. Reply APPROVED (keep) or NOT_APPROVED (revert) with reasoning.
""")
    passed, reasoning = gate_review("Gate B", gate_b_brief, max_turns=3)
    print()

    # ── Phase 7: Keep/Revert ──
    print("## Phase 7 — Keep/Revert Decision")
    decision = "keep" if (passed and verdict != "regressed") else "revert"
    reason = f"Gate B: {'APPROVED' if passed else 'NOT_APPROVED'}, verdict: {verdict}"

    if decision == "revert":
        print(f"❌ REVERTING — {reason}")
        run_py("pipeline_snapshot.py", "restore", "baseline")
    else:
        print(f"✅ KEEPING — {reason}")
        # Remove existing candidate snapshot if present (from prior cycle)
        candidate_snap = SNAPSHOT_DIR / "candidate"
        if candidate_snap.exists():
            shutil.rmtree(candidate_snap)
        # Freeze candidate as the new baseline for next cycle
        run_py("pipeline_snapshot.py", "freeze", "candidate")
    print()

    # ── Phase 8: Gate C — Iterate or stop ──
    print("## Phase 8 — Gate C (Iterate/Stop)")
    gate_c_brief = run_dir / "gate_c_brief.txt"
    with open(gate_c_brief, "w") as f:
        f.write(f"""Gate C — Pipeline DIF Iterate-or-Stop

Decision: {decision}
Reason: {reason}
Baseline aggregate: {json.dumps(baseline_metrics.get('aggregate', {}), indent=2)}
Candidate aggregate: {json.dumps(candidate_metrics.get('aggregate', {}), indent=2)}

Stop criteria to evaluate:
1. Diminishing diffs: 2+ cycles with only cosmetic changes
2. Quality plateau: quality fails to improve >2 points across 2 cycles
3. Repeated pattern: same failure category recurs
4. No generalizable failures remain
5. Cost floor reached

Should we iterate again or stop? Reply ITERATE or STOP with reasoning.
Do NOT read any files.
""")
    passed, reasoning = gate_review("Gate C", gate_c_brief, max_turns=3)

    stop_signal = None
    if "STOP" in (reasoning or "").upper():
        stop_signal = "gate_c_stop"

    # Write outcome record
    outcome = {
        "run_id": run_id,
        "hypothesis": hypothesis,
        "baseline_metrics": baseline_metrics.get("aggregate", {}),
        "candidate_metrics": candidate_metrics.get("aggregate", {}),
        "verdict": verdict,
        "findings": findings,
        "decision": decision,
        "reason": reason,
        "stop_signal": stop_signal,
        "gate_c_reasoning": reasoning[:2000] if reasoning else "",
        "timestamp": datetime.now().isoformat()
    }
    with open(run_dir / "outcome.json", "w") as f:
        json.dump(outcome, f, indent=2)

    print(f"\nOutcome: {run_dir / 'outcome.json'}")
    print(f"Decision: {decision.upper()}")
    if stop_signal:
        print(f"Stop: {stop_signal}")
    else:
        print("Next: Ready for another DIF cycle")

    return 0 if decision == "keep" else 1


if __name__ == "__main__":
    sys.exit(main())
