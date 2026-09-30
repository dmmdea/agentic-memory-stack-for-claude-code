#!/usr/bin/env python3
"""Docs gate: (1) every relative Markdown link in docs/, AGENTS.md, ARCHITECTURE.md,
README.md, CLAUDE.md resolves to a real file; (2) every top-level ADR in
docs/architecture/decisions/ (subdirectories are not scanned) has valid frontmatter
(status/date; superseded_by iff Superseded); (3) no tracked file except CHANGELOG.md
recommends a resident model or CPU inference (see FORBIDDEN_GUIDANCE). Exit 1 on any
violation.

Personal/operator-specific values are NOT checked here: scripts/ci/privacy-gate.py owns that
check for the whole tree, filenames and commit messages, and keeps its term list as keyed
digests rather than plaintext."""
import re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _tracked_markdown():
    """Markdown files git actually tracks, or None if git is unavailable.

    2026-07-25: this gate used to rglob the working tree, so a local scratch note under docs/
    -- something that will never be committed -- made it exit 1 and blocked unrelated work. CI
    runs on a clean checkout where tracked == present, so restricting to tracked files leaves CI
    behaviour identical while making the local run mean what it claims: "what is in the repo".
    Falls back to the filesystem walk if git is not usable (e.g. an exported tarball).
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z", "--", "*.md"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
    except Exception:
        return None
    return [ROOT / p for p in out.split("\0") if p]


_tracked = _tracked_markdown()
if _tracked is None:
    DOC_FILES = [p for p in (ROOT / "docs").rglob("*.md")] + [
        ROOT / n for n in ("AGENTS.md", "ARCHITECTURE.md", "README.md", "CLAUDE.md") if (ROOT / n).exists()
    ]
else:
    _top = {"AGENTS.md", "ARCHITECTURE.md", "README.md", "CLAUDE.md"}
    DOC_FILES = [
        p for p in _tracked
        if (p.is_relative_to(ROOT / "docs") or p.name in _top and p.parent == ROOT) and p.exists()
    ]
# Guidance that must never come back: an idle model that never unloads, a "persistent"
# always-resident group, and CPU-only inference. Every model unloads at ttl 300 and runs on
# the GPU; RAM is overflow only. CHANGELOG.md is the one exemption (it records history).
# The patterns are written so their own source text does not match them.
FORBIDDEN_GUIDANCE = [
    (re.compile(r"\bttl:[ \t]*0(?![0-9.])"), "a zero ttl (the model never unloads; use ttl 300)"),
    (re.compile(r"always[_]loaded"), "an always-loaded (resident) group (use a non-exclusive support group, ttl 300)"),
    (re.compile(r"--n-gpu-layers[ \t=]+0(?![0-9])"), "zero GPU layers (CPU inference; put the layers on the GPU)"),
]
GUIDANCE_EXEMPT = {"CHANGELOG.md"}
MAX_SCAN_BYTES = 5_000_000


def _tracked_files():
    """Every file git tracks (repo-relative posix paths), or a filesystem walk without git."""
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
        return [p for p in out.split("\0") if p]
    except Exception:
        return [
            p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(ROOT).parts
        ]


LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)\s]*)?\)")
ADR_STATUS = {"Proposed", "Accepted", "Superseded", "Deprecated", "Rejected"}
errors = []

for f in DOC_FILES:
    text = f.read_text(encoding="utf-8")
    for m in LINK.finditer(text):
        target = m.group(1)
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if not (f.parent / target).resolve().is_file():
            errors.append(f"{f.relative_to(ROOT)}: broken link -> {target}")

for adr in (ROOT / "docs" / "architecture" / "decisions").glob("*.md"):
    if adr.name == "README.md":
        continue
    text = adr.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        errors.append(f"{adr.name}: missing frontmatter"); continue
    fm = dict(re.findall(r"^(\w+):\s*\"?([^\"\n]+)\"?$", m.group(1), re.M))
    if fm.get("status") not in ADR_STATUS:
        errors.append(f"{adr.name}: bad status {fm.get('status')!r}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fm.get("date", "")):
        errors.append(f"{adr.name}: bad date {fm.get('date')!r}")
    if (fm.get("status") == "Superseded") != ("superseded_by" in fm):
        errors.append(f"{adr.name}: superseded_by must be present iff status=Superseded")

for rel in _tracked_files():
    if rel in GUIDANCE_EXEMPT:
        continue
    path = ROOT / rel
    try:
        if not path.is_file() or path.stat().st_size > MAX_SCAN_BYTES:
            continue
        raw = path.read_bytes()
    except OSError:
        continue
    if b"\0" in raw:
        continue
    for lineno, line in enumerate(raw.decode("utf-8", errors="ignore").splitlines(), 1):
        for rx, what in FORBIDDEN_GUIDANCE:
            if rx.search(line):
                errors.append(f"{rel}:{lineno}: forbidden guidance: {what}")

print("\n".join(errors) if errors else f"docs gate OK ({len(DOC_FILES)} files)")
sys.exit(1 if errors else 0)
