"""
site_registry — SiteConfig dataclass + sites.yaml loader.

The YAML loader tries PyYAML first and falls back to a small inline parser
that handles the limited syntax our sites.yaml uses (key:value, lists,
nested maps, ``#`` comments). Keeping this in its own module means the
orchestrator doesn't carry ~100 lines of parsing code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SiteConfig:
    slug: str
    path: Path
    domain: str = ""
    fleet: str = ""
    images_dir: Path | None = None
    languages: list[str] = field(default_factory=list)
    paused: bool = False


# ── Tiny YAML loader (handles the limited syntax our sites.yaml uses) ───────
# We don't want to require PyYAML at runtime. The sites.yaml we generate is
# pure YAML 1.1 — keys, scalars, lists, nested maps. This parser handles that.


def _coerce(val: str) -> Any:
    """Coerce a scalar string to int/float/bool/str."""
    if val == "" or val is None:
        return val
    if val.lower() in ("true", "yes"):
        return True
    if val.lower() in ("false", "no"):
        return False
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [_coerce(v.strip()) for v in inner.split(",")]
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def _parse_yaml_minimal(text: str) -> dict:
    """
    Minimal YAML loader for sites.yaml. Supports:
      - key: value
      - key:\\n  nested-key: value
      - key:\\n    - item\\n    - item
      - comments starting with '#'
    Raises ValueError on unsupported constructs so the caller can fall back
    to PyYAML if installed.
    """
    lines = []
    for raw in text.splitlines():
        # strip trailing comments but not '#' inside quoted strings
        # (our file has no quoted scalars so a simple split is safe)
        hash_pos = raw.find("#")
        if hash_pos >= 0 and not raw[:hash_pos].rstrip().endswith('"'):
            raw = raw[:hash_pos]
        stripped = raw.rstrip()
        if not stripped.strip():
            continue
        lines.append(stripped)

    root: dict = {}
    # stack of (indent, container)
    stack: list[tuple[int, Any]] = [(-1, root)]

    def peek_next_is_list(idx: int) -> bool:
        """True if the next non-empty line starts with '- ' at greater indent."""
        for nxt in lines[idx + 1:]:
            nxt_stripped = nxt.strip()
            if not nxt_stripped:
                continue
            nxt_indent = len(nxt) - len(nxt.lstrip(" "))
            return nxt_stripped.startswith("- ") and nxt_indent > indent
        return False

    for idx, line in enumerate(lines):
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()
        # pop containers that don't contain this line
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent_indent, parent = stack[-1]

        if content.startswith("- "):
            # list item
            item_val = content[2:].strip()
            if not isinstance(parent, list):
                raise ValueError(f"unexpected list item: {line!r}")
            if ":" in item_val and not item_val.startswith('"'):
                # inline mapping start: "- key: value"
                key, _, val = item_val.partition(":")
                val = val.strip()
                new_map: dict = {}
                new_map[key.strip()] = _coerce(val)
                parent.append(new_map)
                # push map at indent+2 so nested keys hang off it
                stack.append((indent + 2, new_map))
            else:
                parent.append(_coerce(item_val))
        elif ":" in content:
            key, _, val = content.partition(":")
            key = key.strip()
            val = val.strip()
            if val == "":
                if not isinstance(parent, dict):
                    raise ValueError(f"unexpected map under non-dict: {line!r}")
                # Decide map vs list by peeking ahead
                if peek_next_is_list(idx):
                    new_container: list = []
                    parent[key] = new_container
                    stack.append((indent, new_container))
                else:
                    new_container = {}
                    parent[key] = new_container
                    stack.append((indent, new_container))
            else:
                if not isinstance(parent, dict):
                    raise ValueError(f"unexpected map key: {line!r}")
                parent[key] = _coerce(val)

    return root


def load_sites_yaml(path: Path) -> dict:
    """Load sites.yaml. Tries PyYAML first, falls back to the minimal parser."""
    text = path.read_text()
    try:
        import yaml  # type: ignore[import-not-found]
        return yaml.safe_load(text) or {}
    except ImportError:
        return _parse_yaml_minimal(text)


def load_site_registry(
    yaml_path: Path,
    site_filter: str | None = None,
    fleet_filter: str | None = None,
) -> dict[str, SiteConfig]:
    """
    Load sites.yaml and return a {slug: SiteConfig} dict. Optionally filter
    by site slug or fleet name. Filter is an exact match (no glob).
    """
    if not yaml_path.exists():
        raise FileNotFoundError(
            f"sites.yaml not found at {yaml_path}. "
            "Create it (see ~/.hermes/affiliate-crons/config/sites.yaml) "
            "or pass --sites-yaml to point elsewhere."
        )
    raw = load_sites_yaml(yaml_path)
    sites_raw = raw.get("sites", {}) or {}
    out: dict[str, SiteConfig] = {}
    for slug, cfg in sites_raw.items():
        if site_filter and slug != site_filter:
            continue
        if fleet_filter and cfg.get("fleet") != fleet_filter:
            continue
        if cfg.get("paused"):
            continue
        path = Path(os.path.expanduser(cfg["path"]))
        images_dir = cfg.get("images_dir")
        out[slug] = SiteConfig(
            slug=slug,
            path=path,
            domain=cfg.get("domain", ""),
            fleet=cfg.get("fleet", ""),
            images_dir=Path(os.path.expanduser(images_dir)) if images_dir else (path / "images"),
            languages=list(cfg.get("languages", []) or []),
            paused=bool(cfg.get("paused", False)),
        )
    return out
