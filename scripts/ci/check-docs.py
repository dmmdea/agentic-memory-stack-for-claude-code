#!/usr/bin/env python3
"""Docs gate: (1) every relative Markdown link in docs/, AGENTS.md, ARCHITECTURE.md,
README.md, CLAUDE.md resolves to a real file; (2) no operator-specific value (operator
handles, machine names, brand names, LAN IPs, operator-local paths) appears in any of
those same scanned files -- the repo's own canonical GitHub URL is sanctioned and is
stripped out before the line is scanned, so it cannot mask an adjacent violation;
(3) every top-level ADR in docs/architecture/decisions/ (subdirectories are not
scanned) has valid frontmatter (status/date; superseded_by iff Superseded).
Exit 1 on any violation."""
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
LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)\s]*)?\)")
PII = re.compile(r"(?i)\b(aorus|qube|dmmdea|dmmde|daniel|readypep|eclipton|dojolife|danmar|peptidos|pepclick|intranet-pds)\b"
                 r"|10\.0\.0\.\d+|D:\\Dev\\dmmdea|D:\\repos|/mnt/d/(Dev|repos)")
SANCTIONED = ("github.com/dmmdea/agentic-memory-stack-for-claude-code",)
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
    for line_no, line in enumerate(text.splitlines(), 1):
        scanned = line
        for s in SANCTIONED:
            scanned = scanned.replace(s, "")
        if PII.search(scanned):
            errors.append(f"{f.relative_to(ROOT)}:{line_no}: operator-specific value: {line.strip()[:80]}")

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

print("\n".join(errors) if errors else f"docs gate OK ({len(DOC_FILES)} files)")
sys.exit(1 if errors else 0)
