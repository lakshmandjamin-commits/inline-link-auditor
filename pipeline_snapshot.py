#!/usr/bin/env python3
"""
Pipeline Snapshot — freeze, restore, and diff pipeline state.

Creates immutable snapshots of the content pipeline for DIF cycles.
Detects dangling (un-restored) snapshots on startup.

Usage:
  pipeline_snapshot.py freeze <label>
  pipeline_snapshot.py restore <label>
  pipeline_snapshot.py diff <old_label> <new_label>
  pipeline_snapshot.py status
"""
import sys, os, json, shutil, hashlib
from pathlib import Path
from datetime import datetime

SNAPSHOT_DIR = Path.home() / ".hermes" / "affiliate-crons" / "state" / "pipeline_snapshots"

# Files to snapshot
PIPELINE_FILES = {
    "content_pipeline_skill": Path.home() / ".hermes" / "skills" / "productivity" / "content-pipeline" / "SKILL.md",
    "page_generator": Path.home() / ".hermes" / "affiliate-crons" / "scripts" / "page_generator.py",
    "antiword_scan": Path.home() / ".hermes" / "affiliate-crons" / "scripts" / "antiword_scan.py",
    "gate_config": Path.home() / ".hermes" / "affiliate-crons" / "config" / "gate-config.json",
    "validate_template": Path.home() / ".hermes" / "skills" / "productivity" / "website-build-learnings" / "templates" / "validate.py",
}


def hash_file(path):
    """SHA-256 of file contents."""
    if not path.exists():
        return "FILE_NOT_FOUND"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze(label):
    """Create an immutable snapshot."""
    snap_path = SNAPSHOT_DIR / label
    if snap_path.exists():
        print(f"ERROR: Snapshot '{label}' already exists. Delete it first or use a new label.")
        return 1

    snap_path.mkdir(parents=True, exist_ok=True)
    manifest = {"label": label, "timestamp": datetime.now().isoformat(), "files": {}}

    for name, path in PIPELINE_FILES.items():
        if path.exists():
            dest = snap_path / path.name
            shutil.copy2(str(path), str(dest))
            manifest["files"][name] = {
                "source": str(path),
                "sha256": hash_file(path),
                "size": path.stat().st_size
            }
        else:
            manifest["files"][name] = {
                "source": str(path),
                "sha256": "FILE_MISSING",
                "size": 0
            }

    with open(snap_path / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Snapshot '{label}' frozen — {len(manifest['files'])} files")
    print(f"  {snap_path}")
    return 0


def restore(label):
    """Restore pipeline files from snapshot."""
    snap_path = SNAPSHOT_DIR / label
    if not snap_path.exists():
        print(f"ERROR: Snapshot '{label}' not found at {snap_path}")
        return 1

    manifest_path = snap_path / "manifest.json"
    if not manifest_path.exists():
        print(f"ERROR: No manifest in snapshot '{label}' — corrupted?")
        return 1

    with open(manifest_path) as f:
        manifest = json.load(f)

    restored = 0
    for name, info in manifest["files"].items():
        source_path = Path(info["source"])
        snap_file = snap_path / source_path.name
        if snap_file.exists():
            source_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(snap_file), str(source_path))
            restored += 1
            print(f"  Restored: {source_path.name}")
        else:
            print(f"  SKIPPED: {source_path.name} (not in snapshot)")

    print(f"Snapshot '{label}' restored — {restored} files")
    return 0


def diff_snapshots(old_label, new_label):
    """Show unified diff between two snapshots."""
    old_path = SNAPSHOT_DIR / old_label
    new_path = SNAPSHOT_DIR / new_label

    if not old_path.exists():
        print(f"ERROR: Old snapshot '{old_label}' not found")
        return 1
    if not new_path.exists():
        print(f"ERROR: New snapshot '{new_label}' not found")
        return 1

    import subprocess
    for name in PIPELINE_FILES:
        old_file = old_path / PIPELINE_FILES[name].name
        new_file = new_path / PIPELINE_FILES[name].name
        if old_file.exists() and new_file.exists():
            old_hash = hash_file(old_file)
            new_hash = hash_file(new_file)
            if old_hash != new_hash:
                print(f"\n{'='*60}")
                print(f"  {name}: {old_hash[:12]} → {new_hash[:12]}")
                print(f"{'='*60}")
                r = subprocess.run(
                    ["diff", "-u", str(old_file), str(new_file)],
                    capture_output=True, text=True
                )
                print(r.stdout[:2000])
    return 0


def status():
    """Check for dangling snapshots."""
    if not SNAPSHOT_DIR.exists():
        print("No snapshots directory.")
        return 0

    snapshots = sorted(SNAPSHOT_DIR.iterdir())
    if not snapshots:
        print("No snapshots found.")
        return 0

    dangling = []
    for snap in snapshots:
        if snap.is_dir():
            manifest = snap / "manifest.json"
            if manifest.exists():
                with open(manifest) as f:
                    data = json.load(f)
                ts = data.get("timestamp", "unknown")
                label = data.get("label", snap.name)
                file_count = len(data.get("files", {}))
                print(f"  {label:20s}  {ts[:19]}  {file_count} files")
            else:
                print(f"  {snap.name:20s}  NO MANIFEST — corrupted")
                dangling.append(snap.name)

    if dangling:
        print(f"\n⚠️  {len(dangling)} dangling snapshot(s): {', '.join(dangling)}")
        return 1
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    action = sys.argv[1]
    if action == "freeze" and len(sys.argv) >= 3:
        return freeze(sys.argv[2])
    elif action == "restore" and len(sys.argv) >= 3:
        return restore(sys.argv[2])
    elif action == "diff" and len(sys.argv) >= 4:
        return diff_snapshots(sys.argv[2], sys.argv[3])
    elif action == "status":
        return status()
    else:
        print(__doc__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
