"""Two lists the docs keep drifting from, pinned to the code they describe.

  - the nightly chain: `systemd/ams-step-*.service` is the list of steps. store-judge was added in
    1.26.0 and the ordered lists in the docs kept reading 16 steps for the 17 that ran;
  - the keyless endpoints: `docs/api-contracts.md` said only /health and /health/deep skip the key,
    while /health/maintenance, /health/morning-summary and /health/embedder answered without one.

The chain lists must name every step and state the count; the auth sentence must name exactly the
routes in app.py that never call auth().
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHAIN_DOCS = ["docs/systems/installer-and-deploy.md", "docs/operations.md", "ARCHITECTURE.md"]


def _chain_steps():
    return sorted(p.name[len("ams-step-"):-len(".service")] for p in (REPO_ROOT / "systemd").glob("ams-step-*.service"))


def _chain_list(line):
    """The ordered list on a line: from the first `dream` to the last `rtcwake` (first and last step)."""
    a, b = line.find("`dream`"), line.rfind("`rtcwake`")
    return line[a:b + len("`rtcwake`")] if 0 <= a < b else ""


@pytest.mark.parametrize("rel", CHAIN_DOCS)
def test_the_doc_lists_every_chain_step_and_says_how_many(rel):
    steps = _chain_steps()
    n = len(steps)
    assert n >= 15, steps   # the units are found; a glob that matched nothing must not pass
    lines = [ln for ln in (REPO_ROOT / rel).read_text(encoding="utf-8").splitlines() if re.search(rf"\b{n} steps\b", ln)]
    assert lines, f"{rel} must state the chain's step count ({n}: one systemd/ams-step-*.service each)"
    listed = "\n".join(_chain_list(ln) for ln in lines)
    assert listed.strip(), f"{rel}: no line that states '{n} steps' carries the ordered list, `dream` to `rtcwake`"
    missing = [s for s in steps if f"`{s}`" not in listed]
    assert not missing, f"{rel}: the ordered chain list (`dream` to `rtcwake`) does not name {missing}"


def _routes():
    """(METHOD, path, calls_auth) for every route decorated on `app` in app.py."""
    tree = ast.parse((REPO_ROOT / "mem0-server" / "app.py").read_text(encoding="utf-8"))
    out = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "app"):
                calls_auth = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "auth"
                                 for n in ast.walk(node))
                out.append((dec.func.attr.upper(), dec.args[0].value, calls_auth))
    return out


def test_api_contracts_names_exactly_the_keyless_endpoints():
    routes = _routes()
    assert len(routes) >= 30, routes   # the parser found the server's routes
    keyless = sorted((m, p) for m, p, a in routes if not a)
    assert keyless, "the server has keyless health probes"
    assert all(p.startswith("/health") for _, p in keyless), f"a non-health route answers without a key: {keyless}"
    text = (REPO_ROOT / "docs" / "api-contracts.md").read_text(encoding="utf-8")
    auth_line = next(ln for ln in text.splitlines() if ln.startswith("Auth:"))
    named = sorted(re.findall(r"`((?:GET|POST|PUT|PATCH|DELETE) /[^`]*)`", auth_line))
    assert named == sorted(f"{m} {p}" for m, p in keyless), (named, keyless)
    headings = "\n".join(ln for ln in text.splitlines() if ln.startswith("### "))
    for m, p in keyless:
        assert f"{m} {p}" in headings, f"api-contracts.md has no section for {m} {p}"
