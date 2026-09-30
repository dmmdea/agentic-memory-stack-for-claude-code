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


# The routes FastAPI itself adds to `FastAPI(title=..., version=...)` when docs_url / redoc_url / openapi_url
# are left at their defaults: the schema, Swagger UI (with its OAuth2 redirect page) and ReDoc.
FRAMEWORK_ROUTES = ["/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"]
FRAMEWORK_SWITCHES = {"docs_url", "redoc_url", "openapi_url", "swagger_ui_oauth2_redirect_url"}


def test_the_docs_name_fastapis_own_routes_as_keyless_while_the_app_leaves_them_on():
    """The AST walk above sees only `@app.<verb>` routes, so it cannot see these four, and they answer
    without a key: the app builds `FastAPI(...)` with none of the switches that turn them off, and no
    middleware or app-level dependency stands in front of them. The auth sentence said every endpoint
    but the five health probes needs the key. It is true only while the framework defaults stay on;
    turn them off in the code and this test asks for the sentence to change with it."""
    src = (REPO_ROOT / "mem0-server" / "app.py").read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "FastAPI"]
    assert len(calls) == 1, "app.py builds exactly one FastAPI app"
    keywords = {kw.arg for kw in calls[0].keywords}
    assert not keywords & FRAMEWORK_SWITCHES, (
        f"app.py now sets {sorted(keywords & FRAMEWORK_SWITCHES)}: the framework routes may be off or moved, so "
        "docs/api-contracts.md and CLAUDE.md must say so")
    assert "dependencies" not in keywords, "an app-level dependency may put the framework routes behind a key"
    assert "add_middleware(" not in src and "@app.middleware" not in src, "a middleware may put the framework routes behind a key"
    text = (REPO_ROOT / "docs" / "api-contracts.md").read_text(encoding="utf-8")
    auth_line = next(ln for ln in text.splitlines() if ln.startswith("Auth:"))
    missing = [r for r in FRAMEWORK_ROUTES if f"`{r}`" not in auth_line]
    assert not missing, f"docs/api-contracts.md's auth sentence does not name FastAPI's own keyless routes {missing}"
    guide = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    line = next(ln for ln in guide.splitlines() if "X-API-Key" in ln and "keyless" in ln)
    assert "/openapi.json" in line and "/docs" in line and "/redoc" in line, line


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
