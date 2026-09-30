"""brand_routing - the C3 brand map resolver (Python side).

One brand map (`brands.json`) routes a session or a fact to a brand label. The PowerShell
resolver (scripts/windows/brand-routing.ps1) and the Go judge run the same corpus,
tests/fixtures/brand-routing-cases.jsonl, so the three cannot drift apart.

Map shape (every key optional; a file with none of them routes nothing and everything stays
brand-neutral, exactly as before this module existed):

    {"rules": [{"pattern": "<regex over cwd / transcript path / workspace slug>", "brand": "x"}],
     "shared_brands": ["<label visible to every scope>"],
     "content_rule_workspaces": ["<regex over path/slug whose facts are classified by content>"],
     "content_rules": [{"pattern": "<regex over fact text>", "brand": "x"}]}

Resolution for ONE fact:
  1. the first `rules` entry whose pattern matches the path -> that brand;
  2. else, when the path matches any `content_rule_workspaces` pattern, every `content_rules`
     pattern runs over the fact text: exactly one DISTINCT brand matched -> that brand; zero or
     several -> no brand;
  3. else no brand.

Matching is case-insensitive. Path separators (backslash, slash, space) are one character to the
matcher: the path is normalized so each becomes "-", and the same three literals in a pattern are
normalized the same way, so `projects/client-a` matches `G:\\My Drive\\Projects\\Client-A`, the
Claude Code slug `g--My-Drive-Projects-Client-A` and `/home/u/projects/client-a` alike.

Never raises: a missing or malformed map, an invalid regex or a malformed entry routes nothing.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

_WARNED: set[str] = set()   # one stderr warning per problem per process (a hook loop must not spam)

_PATH_SEP = re.compile(r"[\\/ ]")
# literal separators inside a PATTERN: an escaped backslash pair, "\/", "\ ", or a bare "/" or " "
_PATTERN_SEP = re.compile(r"\\\\|\\/|\\ |[/ ]")


def _warn_once(key: str, msg: str) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    print(f"brand_routing: {msg}", file=sys.stderr, flush=True)


def norm_path(path: Optional[str]) -> str:
    return _PATH_SEP.sub("-", path or "")


_CLASS = re.compile(r"\[[^\]]*\]")


def norm_pattern(pattern: str) -> str:
    out = _PATTERN_SEP.sub("-", pattern)
    # A separator class such as [\\/ -] becomes [----] here; Python warns on that ("possible set
    # difference") and a later release will reject it, so a run of dashes inside a class is one dash.
    return _CLASS.sub(lambda m: re.sub(r"-{2,}", "-", m.group(0)), out)


def _matches(pattern, haystack: str, *, is_path: bool) -> bool:
    if not isinstance(pattern, str) or not pattern:
        return False
    try:
        rx = re.compile(norm_pattern(pattern) if is_path else pattern, re.IGNORECASE)
    except re.error:
        _warn_once("regex:" + pattern, f"ignoring invalid pattern {pattern!r}")
        return False
    return rx.search(haystack) is not None


def _entries(brand_map, key: str) -> list:
    if not isinstance(brand_map, dict):
        return []
    v = brand_map.get(key)
    return v if isinstance(v, list) else []


def resolve(brand_map, path: Optional[str], text: Optional[str] = "") -> Optional[str]:
    """The brand for one path (and, in a content-rule workspace, one fact text), or None."""
    hay = norm_path(path)
    if not hay:
        return None
    for r in _entries(brand_map, "rules"):
        if isinstance(r, dict) and r.get("brand") and _matches(r.get("pattern"), hay, is_path=True):
            return str(r["brand"])
    if in_content_rule_workspace(brand_map, path):
        return resolve_by_content(brand_map, text)
    return None


def in_content_rule_workspace(brand_map, path: Optional[str]) -> bool:
    """True when the path matches a `content_rule_workspaces` pattern (resolution step 2)."""
    hay = norm_path(path)
    return bool(hay) and any(_matches(p, hay, is_path=True) for p in _entries(brand_map, "content_rule_workspaces"))


def content_brands(brand_map, text: Optional[str]) -> set[str]:
    """Every distinct brand whose `content_rules` pattern matches the text (case-insensitive)."""
    return {str(r["brand"]) for r in _entries(brand_map, "content_rules")
            if isinstance(r, dict) and r.get("brand") and _matches(r.get("pattern"), text or "", is_path=False)}


def resolve_by_content(brand_map, text: Optional[str]) -> Optional[str]:
    """The content-rule step on its own, for a fact with no path (a stored record): exactly one
    distinct brand matched -> that brand; none or several -> None."""
    found = content_brands(brand_map, text)
    return next(iter(found)) if len(found) == 1 else None


def _stack_env_value(key: str) -> str:
    """KEY from ~/.mem0/stack.env (KEY=VALUE lines); '' when absent. Self-contained on purpose:
    the mem0 server imports nothing from scripts/wsl, and this module needs nothing but stdlib."""
    try:
        for line in (Path.home() / ".mem0" / "stack.env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip()
    except OSError:
        pass
    return ""


def map_path() -> str:
    """MEM0_BRAND_MAP env > stack.env MEM0_BRAND_MAP > ~/.claude/scripts/brands.json."""
    env = (os.environ.get("MEM0_BRAND_MAP") or "").strip() or _stack_env_value("MEM0_BRAND_MAP")
    return env or str(Path.home() / ".claude" / "scripts" / "brands.json")


def load_brand_map(path: Optional[str] = None) -> dict:
    """The parsed brand map, or {} (brand-neutral) when the file is missing, unreadable, not JSON
    or not an object. A malformed file warns once on stderr; a missing one is the normal
    unconfigured state and stays silent."""
    p = path or map_path()
    try:
        raw = Path(p).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except OSError as e:
        _warn_once("read:" + p, f"cannot read brand map {p}: {e}; routing nothing")
        return {}
    try:
        data = json.loads(raw)
    except ValueError as e:
        _warn_once("json:" + p, f"malformed brand map {p}: {e}; routing nothing")
        return {}
    if not isinstance(data, dict):
        _warn_once("shape:" + p, f"brand map {p} is not a JSON object; routing nothing")
        return {}
    return data


def shared_brands(brand_map=None) -> set[str]:
    """Lower-cased labels visible to every scope: the map's `shared_brands` plus
    MEM0_SHARED_BRANDS (env, else stack.env; comma-separated)."""
    out = {str(b).strip().lower() for b in _entries(brand_map, "shared_brands") if str(b).strip()}
    raw = os.environ.get("MEM0_SHARED_BRANDS")
    if raw is None:
        raw = _stack_env_value("MEM0_SHARED_BRANDS")
    out |= {b.strip().lower() for b in raw.split(",") if b.strip()}
    return out


def routable_brands(brand_map=None) -> set[str]:
    """Every brand the map can produce or treats as shared (lower-cased)."""
    out = shared_brands(brand_map)
    for key in ("rules", "content_rules"):
        out |= {str(r["brand"]).strip().lower() for r in _entries(brand_map, key)
                if isinstance(r, dict) and r.get("brand")}
    return out
