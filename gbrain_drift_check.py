#!/usr/bin/env python3
"""
GBrain drift detector — checks if fleet-registry.yaml is newer than
the latest gbrain capture. Alerts if drift detected.
"""
import os
import sys
import glob
from datetime import datetime

FLEET_YAML = os.path.expanduser("~/.hermes/skills/devops/affiliate-operations/references/fleet-registry.yaml")

# GBrain captures can land in two paths:
#   - ~/.gbrain/repo/           (pre-migration, legacy)
#   - ~/.hermes/profiles/hanumanhermes/data/.sources/fleet-data/  (current)
GBRAIN_SEARCH_PATHS = [
    os.path.expanduser("~/.gbrain/repo/"),
    os.path.expanduser("~/.hermes/profiles/hanumanhermes/data/.sources/fleet-data/"),
]

def get_latest_capture_mtime():
    """Find the most recent gbrain capture of fleet-registry across all search paths."""
    latest = 0
    for search_dir in GBRAIN_SEARCH_PATHS:
        pattern = os.path.join(search_dir, "affiliate-fleet-registry-*.md")
        for f in glob.glob(pattern):
            mtime = os.path.getmtime(f)
            if mtime > latest:
                latest = mtime
    return latest

def get_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0

def check():
    yaml_mtime = get_mtime(FLEET_YAML)
    capture_mtime = get_latest_capture_mtime()
    
    if yaml_mtime == 0:
        return False, "fleet-registry.yaml not found"
    if capture_mtime == 0:
        return False, "no gbrain fleet-registry capture files found in any search path"
    
    # YAML newer than latest capture = drift (60s grace period for filesystem precision)
    drifted = yaml_mtime > capture_mtime + 60
    
    if drifted:
        yaml_dt = datetime.fromtimestamp(yaml_mtime)
        cap_dt = datetime.fromtimestamp(capture_mtime)
        return False, (
            f"DRIFT: fleet-registry.yaml ({yaml_dt.strftime('%Y-%m-%d %H:%M')}) "
            f"is newer than latest gbrain capture ({cap_dt.strftime('%Y-%m-%d %H:%M')})"
        )
    
    return True, "OK — gbrain in sync with fleet-registry.yaml"

if __name__ == "__main__":
    ok, msg = check()
    if not ok:
        print(msg, file=sys.stderr)
        sys.exit(1)
    # Silent on success
