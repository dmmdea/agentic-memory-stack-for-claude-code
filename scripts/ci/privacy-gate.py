#!/usr/bin/env python3
"""Privacy gate: fail when the tree, a filename, or a commit message carries a personal
or operator-private identifier -- WITHOUT publishing the list of identifiers it guards.

Two layers, both stdlib-only:

1. Digest layer (needs a key). Every denylisted term is stored as an HMAC-SHA256 digest of
   its normalized form (lowercase, letters and digits only) in privacy-digests.txt. The key
   is never committed: it comes from $PRIVACY_GATE_KEY, else the file named by
   $PRIVACY_GATE_KEY_FILE, else ~/.config/privacy-gate/key. Text is tokenized (alphanumeric
   runs, camelCase and letter/digit pieces, and 2-3 adjacent runs joined, so "two words"
   and "two-words" match a two-word term), each token is HMAC-ed and compared. Without the
   key the digests are opaque: a dictionary attack needs the key, not just this file.
   Known limit: matching is whole-token, so a term glued inside a longer lowercase word
   ("prefixterm") is not seen; camelCase and separator forms are.
2. Shape layer (no key). Patterns that identify a machine or person by SHAPE: home
   directories with a real user name, tailnet FQDNs, private/CGNAT IP literals, MAC
   addresses, full GPU UUIDs, drive-rooted developer paths, non-reserved e-mail domains.
   Placeholders (<user>, example.test, 192.0.2.x, 00:00:5E:00:53:xx ...) pass.

Scope: every tracked file's text, every tracked path, and the message of each non-merge
commit in the range (--range BASE..HEAD, or --base REF for REF..HEAD). Commit AUTHOR
metadata is deliberately not checked (it cannot be changed without rewriting history).

Output never echoes the matched text (CI logs of a public repo are public): only
path:line:column and the category. Exit 0 clean, 1 violation, 2 usage/key error.

Subcommands: `add-term -` (read terms from stdin, append their digests) and `digest -`
(print digests only). Terms are read from stdin so they never enter shell history or a
commit. See docs/systems/privacy-gate.md.
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CONFIG_PATH = HERE / "privacy-gate.json"
DIGESTS_PATH = HERE / "privacy-digests.txt"
EXEMPT_NAMES = {"privacy-digests.txt"}

_RUN = re.compile(r"[A-Za-z0-9]+")
_PIECE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def normalize(term):
    return re.sub(r"[^a-z0-9]", "", term.lower())


def load_key():
    raw = os.environ.get("PRIVACY_GATE_KEY", "").strip()
    if not raw:
        kf = os.environ.get("PRIVACY_GATE_KEY_FILE") or str(Path.home() / ".config" / "privacy-gate" / "key")
        try:
            raw = Path(kf).read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
    return raw.encode("utf-8") if raw else None


def digest(key, normalized):
    return hmac.new(key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def load_digests():
    if not DIGESTS_PATH.exists():
        return set()
    out = set()
    for line in DIGESTS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line.lower())
    return out


def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {"sanctioned": []}


def candidates(text):
    """Normalized tokens of one line of text, each with its start column."""
    runs = [(m.group(0), m.start()) for m in _RUN.finditer(text)]
    for i, (run, col) in enumerate(runs):
        yield run.lower(), col
        pieces = _PIECE.findall(run)
        if len(pieces) > 1:
            for p in pieces:
                yield p.lower(), col
            for n in (2, 3):
                for j in range(len(pieces) - n + 1):
                    yield "".join(pieces[j:j + n]).lower(), col
        for n in (2, 3):
            if i + n <= len(runs):
                yield "".join(r[0].lower() for r in runs[i:i + n]), col


# ---- shape layer -----------------------------------------------------------------

_PLACEHOLDER_USERS = {
    "user", "users", "username", "you", "me", "x", "u", "u1", "someone", "example", "youruser",
    "public", "default", "name", "test", "tester", "foo", "bar", "runner", "first", "tenantx",
    "yourname", "your", "somebody", "alice", "bob", "operator", "admin",
}


def _placeholder_user(name):
    if not name or name[0] in "<$%{_.*[(" + chr(92):
        return True
    return name.lower() in _PLACEHOLDER_USERS


_STOP = r"\s\"'`<>|*:;,)\]}"
_USER_PATHS = [
    re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+([^\\/" + _STOP + r"]+)"),
    re.compile(r"(?<![\w.])/home/([^/" + _STOP + r"]+)"),
    re.compile(r"(?<![\w.])/Users/([^/" + _STOP + r"]+)"),
    re.compile(r"/mnt/[a-z]/Users/([^/" + _STOP + r"]+)"),
]
_TAILNET = re.compile(r"\.tail[0-9a-f]{6}\.ts\.net", re.I)
_IPV4 = re.compile(r"(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\w.]*\w)")
_MAC = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])")
_GPU = re.compile(r"GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_DEVPATH = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]+(?:Dev|repos)[\\/]|/mnt/[a-z]/(?:Dev|repos)\b")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
_RESERVED_TLD = {"test", "invalid", "example", "localhost", "local", "internal"}


def _ip_is_private_literal(m):
    a, b, c, d = (int(x) for x in m.groups())
    if max(a, b, c, d) > 255:
        return False
    if a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31):
        return True
    if a == 100 and 64 <= b <= 127 and not (b == 64 and c < 4):  # the first /22 of the CGNAT block is the doc range
        return True
    return False


def shape_hits(line):
    """Yield (column, category) for shape violations in one line."""
    for rx in _USER_PATHS:
        for m in rx.finditer(line):
            if not _placeholder_user(m.group(1)):
                yield m.start(), "user-home-path"
    for m in _TAILNET.finditer(line):
        yield m.start(), "tailnet-name"
    for m in _IPV4.finditer(line):
        if _ip_is_private_literal(m):
            yield m.start(), "private-ip-literal"
    for m in _MAC.finditer(line):
        v = re.sub(r"[:-]", "", m.group(0)).lower()
        if v.startswith("00005e0053") or len(set(v)) <= 2:
            continue
        yield m.start(), "mac-address"
    for m in _GPU.finditer(line):
        tail = m.group(0)[4:].replace("-", "").lower()
        if len(set(tail)) > 3:
            yield m.start(), "gpu-uuid"
    for m in _DEVPATH.finditer(line):
        yield m.start(), "developer-path"
    for m in _EMAIL.finditer(line):
        domain = m.group(1).lower()
        local = m.group(0).split("@", 1)[0].lower()
        tld = domain.rsplit(".", 1)[-1]
        base = ".".join(domain.split(".")[-2:])
        if tld.isdigit() or tld in _RESERVED_TLD or base in {"example.com", "example.org", "example.net"}:
            continue
        if local.startswith(("noreply", "no-reply")) or local == "git":
            continue
        yield m.start(), "email-address"


# ---- scanning --------------------------------------------------------------------

def _strip_sanctioned(line, sanctioned):
    for s in sanctioned:
        line = line.replace(s, " ")
    return line


def scan_text(label, text, digests, key, sanctioned, out):
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_sanctioned(raw, sanctioned)
        for col, cat in shape_hits(line):
            out.append(f"{label}:{lineno}:{col + 1}: {cat}")
        if key and digests:
            seen = set()
            for tok, col in candidates(line):
                if tok in seen or len(tok) < 3:
                    continue
                seen.add(tok)
                if digest(key, tok) in digests:
                    out.append(f"{label}:{lineno}:{col + 1}: denylisted-term")


def git(args, root):
    return subprocess.run(["git", "-C", str(root)] + args, capture_output=True, text=True,
                          check=True, timeout=120).stdout


def tracked_files(root):
    return [p for p in git(["ls-files", "-z"], root).split("\0") if p]


def run(root, rng, no_tree):
    key = load_key()
    digests = load_digests()
    cfg = load_config()
    sanctioned = cfg.get("sanctioned", [])
    out = []
    notes = []
    if not key:
        notes.append("NOTE: no privacy-gate key found: digest layer skipped, shape layer only")
        if os.environ.get("PRIVACY_GATE_REQUIRE_KEY") == "1":
            print("privacy gate: key required (PRIVACY_GATE_REQUIRE_KEY=1) but none supplied", file=sys.stderr)
            return 2
    elif not digests:
        notes.append("NOTE: digest list is empty: digest layer has nothing to match")
    if not no_tree:
        for rel in tracked_files(root):
            if Path(rel).name in EXEMPT_NAMES:
                continue
            posix = rel.replace("\\", "/")
            # A flagged path component is masked in every label: CI logs of a public repo are
            # public, so the gate must not print the very name it objects to.
            parts, flagged = [], False
            for comp in posix.split("/"):
                hit = []
                scan_text("c", comp, digests, key, sanctioned, hit)
                parts.append("***" if hit else comp)
                flagged = flagged or bool(hit)
            posix = "/".join(parts)
            if flagged:
                out.append(f"path {posix}: flagged path component")
            try:
                data = (Path(root) / rel).read_bytes()
            except OSError:
                continue
            if b"\0" in data[:8192]:
                continue
            scan_text(posix, data.decode("utf-8", "replace"), digests, key, sanctioned, out)
    if rng:
        for sha in git(["rev-list", "--no-merges", rng], root).split():
            msg = git(["log", "-1", "--format=%B", sha], root)
            scan_text(f"commit {sha[:10]} message", msg, digests, key, sanctioned, out)
    for n in notes:
        print(n)
    if out:
        print("\n".join(out))
        print(f"privacy gate FAILED: {len(out)} finding(s); values are not echoed. "
              "Replace them with role names or placeholders (see docs/systems/privacy-gate.md).")
        return 1
    print("privacy gate OK" + ("" if key else " (shape layer only)"))
    return 0


def cmd_terms(mode):
    key = load_key()
    if not key:
        print("privacy gate: no key (set PRIVACY_GATE_KEY or create ~/.config/privacy-gate/key)", file=sys.stderr)
        return 2
    terms = [normalize(t) for t in sys.stdin.read().splitlines()]
    new = sorted({digest(key, t) for t in terms if len(t) >= 3})
    if mode == "digest":
        print("\n".join(new))
        return 0
    have = load_digests()
    add = [d for d in new if d not in have]
    if add:
        with DIGESTS_PATH.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(add) + "\n")
    print(f"added {len(add)} digest(s); {len(new) - len(add)} already present")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", nargs="?", choices=["scan", "add-term", "digest"], default="scan")
    ap.add_argument("stdin_marker", nargs="?", help="use '-' with add-term/digest (terms come from stdin)")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--range", dest="rng", help="commit range whose messages are scanned, e.g. origin/main..HEAD")
    ap.add_argument("--base", help="shorthand for --range BASE..HEAD")
    ap.add_argument("--no-tree", action="store_true", help="scan commit messages only")
    a = ap.parse_args(argv)
    if a.command in ("add-term", "digest"):
        if a.stdin_marker != "-":
            print("terms are read from stdin: pass '-'", file=sys.stderr)
            return 2
        return cmd_terms(a.command)
    rng = a.rng or (f"{a.base}..HEAD" if a.base else None)
    return run(a.root, rng, a.no_tree)


if __name__ == "__main__":
    sys.exit(main())
