"""v1.0 Phase 7A (recon defect B1): the installer's MEM0_MODULES list MUST cover
the FULL local-import closure of the mem0-server (not just app.py's bare imports).

Why this exists: `config.py:build_embedder` does a LAZY `from egemma_embedder
import EmbeddingGemmaEmbedder`, and app.py calls build_embedder() at startup
(the v0.22 EmbeddingGemma embedder path). The InstallerParity parity test only
scanned app.py BARE top-level imports, so egemma_embedder.py slipped out of
MEM0_MODULES -> a truly fresh install never copied it -> mem0.service crash-loops
on ModuleNotFoundError. This test walks app.py + config.py (and transitively any
local module they pull in, top-level OR lazy/indented) and asserts every required
module is listed, so a future lazy import can't regress the installer silently.
"""
import re
import pathlib

SRV = pathlib.Path(__file__).resolve().parents[1]            # mem0-server/
INSTALLER = SRV.parents[0] / "install" / "1-wsl-services.sh"  # repo/install/1-wsl-services.sh


def _local_modules() -> set[str]:
    """Stems of every production .py module in mem0-server/ (tests excluded — glob is non-recursive)."""
    return {p.stem for p in SRV.glob("*.py")}


def _imported_closure() -> set[str]:
    """Local modules imported (top-level OR lazy/indented) by app.py + config.py, transitively."""
    local = _local_modules()
    seen: set[str] = set()
    stack = ["app", "config"]
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        f = SRV / f"{mod}.py"
        if not f.exists():
            continue
        seen.add(mod)
        text = f.read_text(encoding="utf-8")
        # match `from X import ...` / `import X` anywhere (incl. indented lazy imports)
        for m in re.finditer(r'^\s*(?:from|import)\s+([a-zA-Z_]\w*)', text, re.M):
            name = m.group(1)
            if name in local and name not in seen:
                stack.append(name)
    # the two entrypoints themselves are deployed; include them in the requirement
    return seen


def _listed_modules() -> set[str]:
    m = re.search(r'MEM0_MODULES="([^"]+)"', INSTALLER.read_text(encoding="utf-8"))
    assert m, "could not find MEM0_MODULES=\"...\" in install/1-wsl-services.sh"
    return {tok.replace(".py", "") for tok in m.group(1).split()}


def test_mem0_modules_covers_import_closure():
    listed = _listed_modules()
    closure = _imported_closure()
    missing = closure - listed
    assert not missing, (
        "installer MEM0_MODULES misses required modules (fresh install crash-loops "
        f"mem0.service on ModuleNotFoundError): {sorted(missing)}"
    )


def test_egemma_embedder_is_listed():
    # explicit regression pin for the v1.0 P7A defect B1
    assert "egemma_embedder" in _listed_modules(), (
        "egemma_embedder.py must be in MEM0_MODULES — config.py lazily imports it for the "
        "production EmbeddingGemma embedder; omitting it crash-loops a fresh install"
    )
