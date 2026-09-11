#!/usr/bin/env python3
"""dream-consolidate.py — the nightly consolidator on the native authority (spec §4, register P1-3).

Port of scripts/windows/dream-consolidate.ps1 (4-phase pattern orient -> gather -> consolidate ->
prune, from grandamenium/dream-skill MIT; phase 3.5 autonomous canonical promotion behind the 4C
gate; phase 5 retrieval-drift canary). The PowerShell original keeps running on the WSL brain until
the Phase 5 gate retires it.

What differs on the authority, and why:
- GATHER READS THE STORE, NOT PC TRANSCRIPTS. The PCs' transcripts never reach the authority
  (centralised transcript extraction was cut in review, spec §2/§12): each PC's L1a extractor is
  the transcript reader and its facts land here as evidence. The gather window is therefore the
  last 36 h of evidence (source-tagged) plus the recent episodes, with any transcripts that DO
  exist locally appended exactly as the PS did (a box may run its own Claude Code sessions).
- ONE JUDGE, ONE LOCK. Every Codex call goes through codex_shim_client.judge on the native
  transport; the file lock inside it is the single-flight mutex. This process also holds
  ~/.mem0/maintenance/dream.lock for the whole cycle so a chain re-entry cannot overlap.
- QUOTA GATE (spec §4): before the first judge call the newest Codex plan-window probe is read
  (codex-usage-report.py --probe writes it; the dream step runs that first) and the cycle is
  skipped, receipted and NOT throttle-marked when the window has less than the 25 % reserve.
- NO CATCH-UP SCRIPT: ams-nightly.timer is Persistent= and the boot guard covers a missed night.
- NOTHING UNDER /tmp: the drift snapshots and every receipt live under ~/.mem0/maintenance/dream
  (a symlink into the dataset; the root disk carries no AMS state).

Exit codes: 0 for every completed OR deliberately skipped cycle (the receipt note says which);
4 when the authority is unreachable (that is a failed step, not a quiet night).
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # deployed flat: ~/apps/mem0-scripts
import ams_env  # noqa: E402
import autopromote_lib as ap  # noqa: E402
import codex_usage  # noqa: E402

# The server package (codex_shim_client, redact) lives beside the scripts in the repo and under
# ~/apps/mem0-server when deployed (contradiction-sweep.py's layout-aware bridge).
for _cand in (Path(__file__).resolve().parents[2] / "mem0-server", Path.home() / "apps" / "mem0-server"):
    if _cand.is_dir():
        sys.path.insert(0, str(_cand))
        break
try:
    import codex_shim_client as _csc
except Exception:  # noqa: BLE001 — the dream must still import for a dry run without the server package
    _csc = None
try:
    from redact import redact_secrets as _redact
except Exception:  # noqa: BLE001
    def _redact(text):  # type: ignore[misc]
        return text

import httpx  # noqa: E402

COMPONENT = "dream"
THROTTLE_S = 82800            # 23 h, NOT 24 h (2026-07-24): the stamp lands ~1 min after 03:00
EVIDENCE_WINDOW_H = 36
DEDUP_LOCK_MAX_MIN = 30
TRANSCRIPT_FILES = 10
TRANSCRIPT_TURNS = 48
TRANSCRIPT_CHARS = 6000
GATHER_TIMEOUT_S = 180
SYNTH_TIMEOUT_S = 240
MORNING_ROTATE_BYTES = 131072
MORNING_KEEP_SECTIONS = 20


def log(msg: str) -> None:
    ams_env.log(COMPONENT, msg)


def _now(now) -> dt.datetime:
    if isinstance(now, dt.datetime):
        return now if now.tzinfo else now.replace(tzinfo=dt.timezone.utc)
    if isinstance(now, str) and now:
        d = dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc)


def _parse_ts(s) -> dt.datetime | None:
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def _clip(s, n: int) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[:n]


# ---------------------------------------------------------------------------------------------
# mem0 client (the HTTP surface the PS helpers used, one place)
# ---------------------------------------------------------------------------------------------
class Mem0Client:
    def __init__(self, url: str, key: str, user_id: str, http: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.key = key
        self.user_id = user_id
        self.http = http or httpx.Client(timeout=30.0)
        self.h = {"X-API-Key": key, "Content-Type": "application/json"}

    def _get(self, path: str, timeout: float = 5.0):
        r = self.http.get(f"{self.url}{path}", headers=self.h, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def health(self) -> bool:
        try:
            return bool(self._get("/health").get("ok"))
        except Exception:  # noqa: BLE001
            return False

    def health_deep(self) -> dict | None:
        try:
            return self._get("/health/deep", timeout=30.0)
        except Exception:  # noqa: BLE001
            return None

    def evidence(self, limit: int = 100) -> list[dict]:
        d = self._get(f"/v1/memories?user_id={self.user_id}&limit={limit}", timeout=30.0)
        return list(d.get("results", d) if isinstance(d, dict) else d) or []

    def search_canonical(self) -> list[dict]:
        # FIX 6 + A4a: filter-only fetch so the COMPLETE canonical set comes back; the server
        # requires a scope key in filters (user_id) or it 500s.
        body = {"query": "", "filters": {"tier": "canonical", "user_id": self.user_id}, "limit": 1000}
        r = self.http.post(f"{self.url}/v1/memories/search", headers=self.h, json=body, timeout=10.0)
        r.raise_for_status()
        d = r.json()
        return list(d.get("results", d) if isinstance(d, dict) else d) or []

    def goals(self, status: str, limit: int) -> list[dict]:
        return list(self._get(f"/v1/goals?status={status}&limit={limit}") or [])

    def open_questions(self, status: str, limit: int) -> list[dict]:
        return list(self._get(f"/v1/open_questions?status={status}&limit={limit}") or [])

    def episodes(self, recent: int) -> list[dict]:
        return list(self._get(f"/v1/episodes?recent={recent}") or [])

    def add(self, text: str, metadata: dict):
        body = {"messages": text, "user_id": self.user_id, "infer": False, "metadata": metadata}
        r = self.http.post(f"{self.url}/v1/memories", headers=self.h, json=body, timeout=15.0)
        r.raise_for_status()
        d = r.json()
        try:
            return d["results"][0]["id"]
        except (KeyError, IndexError, TypeError):
            return True

    def patch_metadata(self, mid: str, metadata: dict, actor: str, reason: str) -> bool:
        body = {"metadata": metadata, "actor": actor, "reason": reason}
        r = self.http.patch(f"{self.url}/v1/memories/{mid}/metadata", headers=self.h, json=body, timeout=5.0)
        r.raise_for_status()
        return True

    def read_memory_md(self) -> str:
        try:
            return (Path.home() / ".mem0" / "MEMORY.md").read_text(encoding="utf-8")
        except OSError:
            return ""


# ---------------------------------------------------------------------------------------------
# pure helpers (ported from memory-common.ps1)
# ---------------------------------------------------------------------------------------------
def extract_json(text: str | None, expected_key: str):
    """Extract-JsonFromText: fence strip -> bare top-level array (wrapped under the key; the
    2026-09-07 empty-list case) -> whole-object parse -> regex fallback. None when nothing fits."""
    if not text or not text.strip():
        return None
    cleaned = re.sub(r"```(?:json)?\s*", "", text, flags=re.S)
    cleaned = re.sub(r"```\s*", "", cleaned)
    trimmed = cleaned.strip()
    if trimmed.startswith("["):
        try:
            arr = json.loads(trimmed)
            if isinstance(arr, list):
                return {expected_key: arr}
        except ValueError:
            pass
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict) and expected_key in obj:
            return obj
    except ValueError:
        pass
    m = re.search(r'\{[^{}]*"' + re.escape(expected_key) + r'"\s*:\s*\[[^\]]*\][^{}]*\}', cleaned, flags=re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            return None
    return None


def recent_transcript_turns(path: Path, max_turns: int = TRANSCRIPT_TURNS, max_chars: int = TRANSCRIPT_CHARS) -> str | None:
    """Get-RecentTranscriptTurns: bounded tail read (512 KB), oversized records skipped (the
    24.6 MB single-line transcript that pegged a core for 11 h), secrets redacted, newest-bounded."""
    tail_bytes, max_record = 524288, 262144
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            seeked = size > tail_bytes
            fh.seek(-tail_bytes if seeked else 0, os.SEEK_END)
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    if not raw.strip():
        return None
    records = raw.split("\n")
    if seeked and len(records) > 1:
        records = records[1:]
    turns = []
    for line in records[-max_turns:]:
        line = line.rstrip("\r")
        if not line.strip() or len(line) > max_record:
            continue
        try:
            obj = json.loads(line)
            msg = obj.get("message") or {}
            role, content = msg.get("role"), msg.get("content")
            if not role or not content:
                continue
            text = content if isinstance(content, str) else "\n".join(
                c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
            if text:
                turns.append(f"[{role}] {text}")
        except (ValueError, AttributeError):
            continue
    if not turns:
        return None
    joined = _redact("\n\n".join(turns)) or ""
    return joined[-max_chars:] if len(joined) > max_chars else joined


def gather_transcripts(transcripts_dir: str, now: dt.datetime) -> str:
    """Last 36 h of local *.jsonl transcripts (10 newest files), as the PS did. Empty when the
    directory does not exist — on the authority that is the normal case."""
    if not transcripts_dir:
        return ""
    root = Path(transcripts_dir)
    if not root.is_dir():
        return ""
    cutoff = now.timestamp() - EVIDENCE_WINDOW_H * 3600
    files = [p for p in root.rglob("*.jsonl") if p.is_file() and p.stat().st_mtime > cutoff]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for f in files[:TRANSCRIPT_FILES]:
        turns = recent_transcript_turns(f)
        if turns:
            mtime = dt.datetime.fromtimestamp(f.stat().st_mtime, dt.timezone.utc).isoformat()
            out.append(f"=== transcript: {f.name} (mtime={mtime}) ===\n{turns}\n")
    return "\n".join(out)


def evidence_window(evidence: list[dict], now: dt.datetime, hours: int = EVIDENCE_WINDOW_H, cap: int = 60) -> list[dict]:
    """Evidence records created inside the window (undated records are kept: absence of a stamp
    is not evidence of age), newest first, capped."""
    cutoff = now - dt.timedelta(hours=hours)
    keep = []
    for e in evidence:
        ts = _parse_ts(e.get("created_at") or (e.get("metadata") or {}).get("created_at"))
        if ts is None or ts >= cutoff:
            keep.append(e)
    keep.sort(key=lambda e: str(e.get("created_at") or ""), reverse=True)
    return keep[:cap]


def _mem_text(e: dict) -> str:
    return str(e.get("memory") or e.get("data") or e.get("text") or "")


def _tier(e: dict):
    return (e.get("metadata") or {}).get("tier")


def _lines(items) -> str:
    return "\n".join(items)


# ---------------------------------------------------------------------------------------------
# prompts (verbatim from the PS, with the authority's gather input named)
# ---------------------------------------------------------------------------------------------
def gather_prompt(memorymd, insights, episodes, open_goals, blocked_goals, open_questions, corpus) -> str:
    return f"""You are reading the last 36h of captured evidence and conversation transcripts to identify HIGH-SIGNAL events for memory consolidation. Output STRICT JSON:
{{"signals":[{{"kind":"correction|decision|surprise|contradiction","text":"...","source_transcript":"...","priority":1-5}}]}}

Prioritize signals that REDUCE UNCERTAINTY or CONTRADICT existing memory (Information Gain principle):
- CORRECTIONS: user said "actually X" / "no, Y" / "wait" / "stop" / "that's wrong" - these are the strongest signals.
- DECISIONS: locked-in choices ("ok let's do X", "go with Y", "approved")
- SURPRISES: outcomes the user didn't expect (positive or negative)
- CONTRADICTIONS: claims that conflict with the existing memory index below

Existing memory index (do NOT restate; only flag what contradicts it):
{memorymd}

Existing insights (do NOT restate; only update if newer evidence supersedes):
{insights}

Recent sessions (episodic context — use to detect goal continuity and contradictions across sessions; the Information Gain principle prefers SURPRISES vs these established goals):
{episodes}

Active goals (top OPEN by priority):
{open_goals}

Currently BLOCKED goals (sources of friction):
{blocked_goals}

Open frontier questions (Epistemic Reachability — what we know we don't know):
{open_questions}

PRIORITY: surprises in the evidence that RESOLVE an open question are HIGHEST signal. Treat them as the strongest source of insight.

VALUE IMPROVEMENT priority: surprises that ADVANCE the OPEN goals or UNBLOCK the BLOCKED goals are HIGHEST signal. Corrections that CONTRADICT an existing goal's premise are also HIGHEST signal. Treat goal continuity across sessions as Information Gain when the current session moves a goal forward or reveals a block.

Rules:
- Max 8 signals. Drop low-priority ones first.
- Each signal: <=30 words, self-contained, declarative.
- priority 5 = corrects an existing canonical/insight; priority 1 = generic confirmation.
- If nothing surprising/correcting/deciding happened: {{"signals":[]}}.

Recent evidence and transcripts:
{corpus}
"""


def consolidate_prompt(signal_bullets: str, evidence_bullets: str) -> str:
    return f"""You are a memory consolidator. Synthesize 1-3 INSIGHTS that emerge from BOTH the surprise/correction signals below AND the recent evidence. Output STRICT JSON:
{{"insights":[{{"text":"...","source_signal_indexes":[0,2],"source_memory_ids":["..."],"confidence":0.7}}]}}

Rules:
- Each insight: <=40 words, declarative, durable.
- An insight must integrate >=1 signal AND >=1 evidence id (lineage matters).
- Resolve contradictions: newer evidence + corrections WIN over older statements.
- If nothing crosses the bar of "more than the sum of its parts": {{"insights":[]}}.

Surprise/correction signals (priority-weighted):
{signal_bullets}

Recent evidence (use ids in source_memory_ids):
{evidence_bullets}
"""


def promote_prompt(evidence_bullets: str, canonical_facts: list[str]) -> str:
    canon = _lines(f"- {c}" for c in canonical_facts[:30])
    return f"""You are a precision-first memory curator. Review the evidence memories below and nominate ONLY those that meet ALL of the following strict criteria for promotion to canonical (ground-truth) tier:

PROMOTE ONLY IF the memory is:
1. EVERGREEN — will still be true in a year (no time-bound or session-specific content)
2. DECLARATIVE — a fact, operator preference, locked decision, or environment invariant (never a task, status update, ship log, or action item)
3. GROUND-TRUTH — operator-stated or a verifiable environment fact (not speculation or inference)
4. CROSS-SESSION — relevant beyond the current session or project
5. HIGH-CONFIDENCE — you are certain it belongs in the authority tier

NEVER nominate: tasks, ship logs, status updates, transient debugging notes, brand/voice content, speculation, anything imperative (rules/orders), anything already in the canonical set, or anything where you are unsure.

PRECISION OVER RECALL: when in doubt, omit. A missed nomination is cheap (it will be re-reviewed tomorrow); a wrong authority-write is not.

Return STRICT JSON only — no prose, no markdown:
[{{"memory_id":"<id>","reason":"<why this is canonical: <=30 words>","confidence":<0.0-1.0>}}]

If nothing meets the bar, return: []

Evidence memories to evaluate:
{evidence_bullets}

Existing canonical facts (do NOT re-nominate anything that duplicates these):
{canon}
"""


# ---------------------------------------------------------------------------------------------
# collaborators with real side effects (injectable)
# ---------------------------------------------------------------------------------------------
def _run_deployed(script: str, env: dict | None = None) -> tuple[int, str]:
    """Run a sibling maintenance script with THIS interpreter: the deployed copy under
    ~/apps/mem0-scripts when it exists (never the dev tree at 3 am), else the repo sibling."""
    deployed = Path.home() / "apps" / "mem0-scripts" / script
    target = deployed if deployed.exists() else Path(__file__).resolve().parent / script
    try:
        cp = subprocess.run([sys.executable, str(target)], capture_output=True, text=True, timeout=1800,
                            env=dict(os.environ, **(env or {})))
        return cp.returncode, (cp.stdout + cp.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _canonize(memory_id: str, reason: str) -> tuple[int, str]:
    """mem0-canonize.sh --actor dream-autopromote (the deployed copy); list argv, no shell quoting.
    Success is judged by the OUTPUT (a tier=canonical body), never the exit code alone."""
    deployed = Path.home() / "apps" / "mem0-scripts" / "mem0-canonize.sh"
    script = deployed if deployed.exists() else Path(__file__).resolve().parent / "mem0-canonize.sh"
    try:
        cp = subprocess.run(["bash", str(script), "--actor", "dream-autopromote", memory_id, reason],
                            capture_output=True, text=True, timeout=120, env=dict(os.environ))
        return cp.returncode, (cp.stdout + cp.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _real_eval_runner(eval_root: str):
    """Runs retrieval_drift.py subcommands with cwd = its own directory so the argv stays short
    and the harness' sibling imports resolve; env carries the authority URL/key/tenant."""
    cwd = Path(eval_root) / "eval" / "retrieval-drift"

    def run(cmd: list[str]) -> tuple[int, str]:
        env = dict(os.environ, MEM0_URL=ams_env.mem0_url(), MEM0_KEY=ams_env.api_key(),
                   MEM0_DEFAULT_USER_ID=ams_env.user_id())
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=str(cwd), env=env)
            return cp.returncode, (cp.stdout + cp.stderr).strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            return 1, str(e)
    return run


# ---------------------------------------------------------------------------------------------
# the cycle
# ---------------------------------------------------------------------------------------------
class Dream:
    def __init__(self, args, mem0, judge, qdrant_http, eval_runner, now, probe=None):
        self.args = args
        self.mem0 = mem0
        self.judge = judge
        self.qdrant_http = qdrant_http
        self.eval_runner = eval_runner
        self.now = _now(now)
        self.probe = probe
        self.dry = bool(args.dry_run)
        self.state = ams_env.state_dir() / "dream"
        self.state.mkdir(parents=True, exist_ok=True)
        self.morning = ams_env.state_dir() / "morning-summary.md"
        self.eval_root = ams_env.eval_root()
        self._drift_state_supported = None
        self.tokens = {"gather": 0, "consolidate": 0, "gate": 0}
        self.ms = {"gather": 0, "consolidate": 0}
        self.posted = 0
        self.promoted = 0

    # -- receipts -------------------------------------------------------------------------
    def save_phase(self, phase: str, payload: dict) -> None:
        (self.state / f"{phase}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    def _judge_call(self, component: str, prompt: str, model: str, effort: str, timeout_s: int, extra: dict | None = None) -> dict:
        t0 = time.monotonic()
        out = self.judge(prompt, effort=effort, timeout_s=timeout_s, model=model)
        ms = int(out.get("duration_ms") or (time.monotonic() - t0) * 1000)
        if out.get("ok"):
            ams_env.write_usage(component, tokens_used=out.get("tokens_used", 0), duration_ms=ms, status="ok",
                                model_requested=model, effort_requested=effort, outcome="ok", **(extra or {}))
        else:
            et = out.get("error_type", "")
            outcome = "timeout" if et == "client_timeout" else "exit_nonzero"
            ams_env.write_usage(component, duration_ms=ms, status="error", model_requested=model,
                                effort_requested=effort, outcome=outcome)
        out["duration_ms"] = ms
        return out

    # -- phase 5 helpers ------------------------------------------------------------------
    def _drift_script(self) -> Path | None:
        if self.dry or not self.eval_root:
            return None
        s = Path(self.eval_root) / "eval" / "retrieval-drift" / "retrieval_drift.py"
        return s if s.exists() else None

    def _drift_state_support(self) -> bool:
        if self._drift_state_supported is None:
            self._drift_state_supported = False
            try:
                rc, out = self.eval_runner([sys.executable, "retrieval_drift.py", "compare", "-h"])
                self._drift_state_supported = "--state" in out
            except Exception:  # noqa: BLE001
                pass
        return self._drift_state_supported

    def _drift_state_path(self) -> Path:
        return Path.home() / ".mem0" / "retrieval-drift-state.json"

    def add_drift_record(self, kind: str, detail: str) -> None:
        p = Path.home() / ".mem0" / "consolidation-drift.jsonl"
        rec = {"ts": self.now.isoformat(), "schema_version": "drift-v1", "kind": kind, "detail": detail}
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def drift_heartbeat(self, event: str) -> None:
        try:
            if self._drift_state_support():
                self.eval_runner([sys.executable, "retrieval_drift.py", "heartbeat", "--state", str(self._drift_state_path()), "--event", event])
                if event == "snapshot-failure" and self._drift_state_path().exists():
                    st = json.loads(self._drift_state_path().read_text(encoding="utf-8"))
                    if int(st.get("consecutive_snapshot_failures", 0)) >= 2:
                        self.add_drift_record("guard-dead", f"drift guard has not compared for {st['consecutive_snapshot_failures']} consecutive attempts (snapshot failures) -- the canary is not protecting consolidations")
                        log("  !! DRIFT GUARD DEAD -- >=2 consecutive snapshot failures; flagged to consolidation-drift.jsonl")
            elif not self._drift_state_path().exists():
                self._drift_state_path().write_text(json.dumps({"compat_fallback": True, "ts": self.now.isoformat()}) + "\n", encoding="utf-8")
                log("  drift guard running in compat-fallback (deployed retrieval_drift.py predates --state)")
        except Exception as e:  # noqa: BLE001
            log(f"  drift heartbeat ({event}) failed (non-fatal): {e}")

    def drift_snapshot(self, phase: str) -> str | None:
        if self._drift_script() is None:
            return None
        out = self.state / f"drift-{phase}.json"
        try:
            out.unlink(missing_ok=True)   # a FAILED snapshot must not leave an earlier night's file behind
            rc, text = self.eval_runner([sys.executable, "retrieval_drift.py", "snapshot", "--out", str(out)])
            log(f"  drift {phase} snapshot (exit={rc}): {text}")
            if rc != 0 or not out.exists():
                log(f"  drift {phase} snapshot FAILED (exit={rc}) -- skipping drift compare this cycle (no false alarm)")
                self.drift_heartbeat("snapshot-failure")
                return None
            return str(out)
        except Exception as e:  # noqa: BLE001
            log(f"  drift {phase} snapshot FAILED (non-fatal): {e}")
            self.drift_heartbeat("snapshot-failure")
            return None

    # -- morning summary ------------------------------------------------------------------
    def _append_morning(self, section: str) -> None:
        self.morning.parent.mkdir(parents=True, exist_ok=True)
        with open(self.morning, "a", encoding="utf-8") as fh:
            fh.write(section)

    def _rotate_morning(self) -> None:
        try:
            if self.morning.exists() and self.morning.stat().st_size > MORNING_ROTATE_BYTES:
                parts = re.split(r"(?m)^(?=## )", self.morning.read_text(encoding="utf-8"))
                if len(parts) > MORNING_KEEP_SECTIONS + 1:
                    old, new = "".join(parts[:-MORNING_KEEP_SECTIONS]), "".join(parts[-MORNING_KEEP_SECTIONS:])
                    with open(self.morning.with_name("morning-summary.archive.md"), "a", encoding="utf-8") as fh:
                        fh.write(old)
                    self.morning.write_text(new, encoding="utf-8")
                    log(f"  morning-summary rotated ({len(parts) - MORNING_KEEP_SECTIONS - 1} old section(s) archived)")
        except OSError as e:
            log(f"  morning-summary rotation failed (non-fatal): {e}")

    # -- the run ----------------------------------------------------------------------------
    def run(self) -> dict:
        args = self.args
        if not self.dry and not args.force and not ams_env.throttle_ok("dream", THROTTLE_S):
            log("skipping: nightly throttle (23h) not yet elapsed")
            return {"phase": "throttle", "posted": 0, "promoted": 0, "note": "skipping: nightly throttle (23h) not yet elapsed"}

        # ---- quota gate (spec §4): read the newest window probe; never judge below the reserve
        if not self.dry:
            window = codex_usage.last_window()
            if window is None and self.probe is not None:
                try:
                    window = self.probe(ams_env.codex_home())
                except Exception as e:  # noqa: BLE001
                    window = {"used_percent": None, "note": f"probe failed ({e})"}
            gate = codex_usage.quota_gate(window or {"used_percent": None, "note": "no window probe"})
            if not gate["allow"]:
                log(f"skipping: codex quota gate — {gate['reason']}")
                ams_env.write_usage(COMPONENT, status="ok", outcome="skipped_quota")
                return {"phase": "quota", "posted": 0, "promoted": 0, "note": f"skipping: codex quota gate — {gate['reason']}"}
            log(f"  quota gate: {gate['reason']}")

        # ---- phase 1: orient
        log("=== phase 1: orient ===")
        memorymd = self.mem0.read_memory_md()
        all_ev = list(self.mem0.evidence(200))
        insights = _lines(f"- [{e.get('id')}] {_clip(_mem_text(e), 180)}" for e in all_ev if _tier(e) == "insight")
        episodes = self.mem0.episodes(7)
        ep_lines = _lines(f"- [{_clip(e.get('ended_at') or '?', 10)}] {e.get('brand') or 'unknown'}: {_clip(e.get('goal_text') or e.get('summary_text') or '', 130)}" for e in episodes)
        open_goals = _lines(f"- [{g.get('brand') or 'unknown'}] [P{g.get('priority') or 3}] {g.get('title')}" for g in self.mem0.goals("open", 5))
        blocked = _lines(f"- [{g.get('brand') or 'unknown'}] {g.get('title')}" for g in self.mem0.goals("blocked", 3))
        oqs = _lines(f"- [{q.get('brand') or 'cross-brand'}] {_clip(q.get('question_text') or '', 120)}" for q in self.mem0.open_questions("open", 5))
        self.save_phase("orient", {"memorymd_chars": len(memorymd), "existing_insights_count": len(insights.splitlines()),
                                   "recent_episodes_count": len(episodes), "open_goals_count": len(open_goals.splitlines()),
                                   "blocked_goals_count": len(blocked.splitlines()), "open_questions_count": len(oqs.splitlines()),
                                   "timestamp": self.now.isoformat()})
        if not self.mem0.health():
            log("  mem0 unreachable, aborting")
            return {"phase": "orient", "posted": 0, "promoted": 0, "note": "mem0 unreachable, aborting", "unreachable": True}

        # ---- phase 2: gather (store-fed on the authority; local transcripts appended when present)
        log("=== phase 2: gather (surprise-weighted) ===")
        window_ev = evidence_window(all_ev, self.now)
        ev_lines = _lines(f"- [{e.get('id')}] [{(e.get('metadata') or {}).get('source') or 'unsourced'}] {_clip(_mem_text(e), 180)}" for e in window_ev)
        tdir = args.transcripts_dir
        if tdir is None:
            default = Path.home() / ".claude" / "projects"
            tdir = str(default) if default.is_dir() else ""
        transcripts = gather_transcripts(tdir, self.now)
        corpus = ""
        if ev_lines:
            corpus += f"=== evidence captured in the last {EVIDENCE_WINDOW_H}h ({len(window_ev)}) ===\n{ev_lines}\n\n"
        if transcripts:
            corpus += transcripts
        if not corpus.strip():
            log("  no recent evidence or transcripts, skipping gather")
            return {"phase": "gather", "posted": 0, "promoted": 0, "note": "no recent evidence or transcripts, skipping gather"}

        g = self._judge_call("dream-gather", gather_prompt(memorymd, insights, ep_lines, open_goals, blocked, oqs, corpus),
                             ams_env.MODEL_CLASSIFY, "medium", GATHER_TIMEOUT_S)
        self.ms["gather"], self.tokens["gather"] = g["duration_ms"], int(g.get("tokens_used") or 0)
        if not g.get("ok"):
            log(f"  gather codex failed: {g.get('error_type')} {g.get('error', '')}")
            return {"phase": "gather", "posted": 0, "promoted": 0, "note": f"gather codex failed: {g.get('error_type')}"}
        parsed = extract_json(g.get("response", ""), "signals")
        signals = list(parsed.get("signals") or []) if parsed else []
        self.save_phase("gather", {"signals": signals, "codex_ms": self.ms["gather"], "tokens": self.tokens["gather"], "dry_run": self.dry})
        log(f"  gathered {len(signals)} signals (codex {self.ms['gather']}ms, {self.tokens['gather']} tokens)")
        if not signals:
            log("  no signals; nothing to consolidate")
            if not self.dry:
                ams_env.mark_throttle("dream")   # a REAL no-op night marks the throttle; a dry run never does
            return {"phase": "gather", "posted": 0, "promoted": 0, "note": "no signals; nothing to consolidate"}

        # ---- dedup mutex: skip WITHOUT marking (AMS-06: a marked skip cost two nights)
        dl = Path.home() / ".mem0" / "dedup.lock"
        if dl.exists():
            age_min = (time.time() - dl.stat().st_mtime) / 60
            if age_min < DEDUP_LOCK_MAX_MIN:
                note = f"semantic-dedup mutex held (lock {int(age_min)}min old), skipping consolidate phase; throttle NOT marked"
                log(f"  {note}")
                return {"phase": "consolidate", "posted": 0, "promoted": 0, "note": note}

        drift_before = self.drift_snapshot("before")

        # ---- phase 3: consolidate
        log("=== phase 3: consolidate ===")
        signal_bullets = _lines(f"- [{s.get('kind')} p{s.get('priority')}] {s.get('text')}" for s in signals)
        evidence = [e for e in all_ev if _tier(e) in ("evidence", None)][:30]
        evidence_bullets = _lines(f"- [{e.get('id')}] {_clip(_mem_text(e), 180)}" for e in evidence)
        evidence_ids = [str(e.get("id")) for e in evidence]
        c = self._judge_call("dream-consolidate", consolidate_prompt(signal_bullets, evidence_bullets),
                             ams_env.MODEL_SYNTHESIS, ams_env.EFFORT_SYNTHESIS, SYNTH_TIMEOUT_S)
        self.ms["consolidate"], self.tokens["consolidate"] = c["duration_ms"], int(c.get("tokens_used") or 0)
        if not c.get("ok"):
            log(f"  consolidate codex failed: {c.get('error_type')} {c.get('error', '')}")
            return {"phase": "consolidate", "posted": 0, "promoted": 0, "note": f"consolidate codex failed: {c.get('error_type')}"}
        parsed = extract_json(c.get("response", ""), "insights")
        if parsed is None:
            log("  consolidate: malformed JSON from codex; skipping throttle mark")
            log(f"  preview: {_clip(c.get('response', ''), 300)}")
            return {"phase": "consolidate", "posted": 0, "promoted": 0, "note": "consolidate: malformed JSON from codex; throttle NOT marked"}
        ins_list = list(parsed.get("insights") or [])
        self.save_phase("consolidate", {"insights": ins_list, "codex_ms": self.ms["consolidate"], "tokens": self.tokens["consolidate"]})

        if ins_list and not self.dry:
            for ins in ins_list:
                text = str(ins.get("text") or "").strip()
                if not text:
                    continue
                lineage = []
                for raw in ins.get("source_memory_ids") or []:
                    rid = str(raw or "")
                    if not rid:
                        continue
                    matched = [i for i in evidence_ids if i == rid or i.startswith(rid)]
                    if len(matched) == 1:
                        lineage.append(matched[0])
                    elif len(matched) > 1:
                        log(f"  lineage: prefix {rid} matched {len(matched)} evidence ids (ambiguous, dropping)")
                    else:
                        log(f"  lineage: id {rid} matched no evidence in this window (dropping)")
                if not lineage:
                    lineage = evidence_ids[:5]
                try:
                    conf = float(ins.get("confidence")) if ins.get("confidence") is not None else None
                except (TypeError, ValueError):
                    conf = None
                try:
                    ok = self.mem0.add(text, {"tier": "insight", "category": "insight", "confidence": conf,
                                              "source_memory_ids": lineage, "window_evidence_count": len(evidence),
                                              "window_signal_count": len(signals), "consolidated_at": self.now.isoformat(),
                                              "dream_phase": "consolidate", "source": "dream-consolidator"})
                except Exception as e:  # noqa: BLE001
                    log(f"  insight post failed (non-fatal): {e}")
                    ok = False
                if ok:
                    self.posted += 1
                    for mid in lineage:
                        try:
                            self.mem0.patch_metadata(mid, {"touched_by_dream": self.now.isoformat()}, "dream-consolidator",
                                                     "cited as source_memory_id by insight")
                        except Exception as e:  # noqa: BLE001
                            log(f"  PATCH touched_by_dream failed for {mid}: {e}")
        log(f"  consolidated {len(ins_list)} insights, posted {self.posted} (codex {self.ms['consolidate']}ms, {self.tokens['consolidate']} tokens)")

        # ---- phase 3.5: autonomous canonical promotion
        log("=== phase 3.5: autonomous canonical promotion ===")
        candidates = [e for e in all_ev if _tier(e) not in ("canonical", "insight") and _mem_text(e).strip()]
        canonical_facts, canonical_norm = [], []
        try:
            canonical_facts = [_mem_text(e) for e in self.mem0.search_canonical()]
            canonical_norm = [" ".join(t.split()).lower().strip() for t in canonical_facts]
        except Exception as e:  # noqa: BLE001
            log(f"  autopromote: canonical fetch failed (non-fatal): {e}")
        bullets = _lines(f"- [{e.get('id')}] {_clip(_mem_text(e), 160)}" for e in candidates[:50])
        codex_json, codex_failed, promote_ms = None, False, None
        if not candidates:
            log("  autopromote: no evidence candidates, skipping Codex")
            ams_env.write_usage("dream-promote", status="ok", outcome="skipped_no_candidates",
                                model_requested=ams_env.MODEL_SYNTHESIS, effort_requested=ams_env.EFFORT_SYNTHESIS)
        else:
            p = self._judge_call("dream-promote", promote_prompt(bullets, canonical_facts), ams_env.MODEL_SYNTHESIS,
                                 ams_env.EFFORT_SYNTHESIS, SYNTH_TIMEOUT_S)
            promote_ms = p["duration_ms"]
            if p.get("ok") and str(p.get("response", "")).strip():
                codex_json = p.get("response")
            else:
                codex_failed = True
                log(f"  autopromote: Codex call failed (non-fatal): {p.get('error_type', 'empty')}")
        decision = ap.autopromote_decision(codex_json, codex_failed, candidates, canonical_norm, dry_run=self.dry)
        for line in decision["logs"]:
            log(f"  {line}")
        surviving, deduped, over_cap = decision["surviving"], decision["deduped"], decision["over_cap"]
        if not self.dry:
            self.save_phase("promote", {"candidates": len(candidates), "nominated": len(surviving) + len(deduped) + len(over_cap),
                                        "surviving": len(surviving), "deduped": len(deduped), "over_cap": len(over_cap),
                                        "codex_ms": promote_ms, "dry_run": False, "timestamp": self.now.isoformat()})
        else:
            log(f"  autopromote: DryRun=true -- phase state not written (candidates={len(candidates)} surviving={len(surviving)} deduped={len(deduped)} over_cap={len(over_cap)})")

        gate_mode = (os.environ.get("MEM0_PROMOTION_GATE_MODE") or ams_env.stack_env().get("MEM0_PROMOTION_GATE_MODE") or "shadow").strip().lower()
        failed = blocked_n = 0
        summary = []
        by_id = {str(e.get("id")): e for e in candidates}
        for nom in surviving:
            rec = by_id.get(str(nom.get("memory_id")))
            text = _mem_text(rec) if rec else ""
            short, reason = _clip(text, 120), str(nom.get("reason") or "")
            log(f"  autopromote: nominee id={nom.get('memory_id')} confidence={nom.get('confidence')} reason={reason}")
            gate_blocked = False
            if gate_mode != "off" and text.strip():
                gate_errored, gate_promote = False, False
                try:
                    v = ap.promotion_gate_verdict(str(nom.get("memory_id")), text, rec, judge=self.judge, http=self.qdrant_http)
                    self._gate_log(v, gate_mode)
                    gate_promote = bool(v["gate"]["promote"])
                    self.tokens["gate"] += int(v.get("codexTokens") or 0)
                    log(f"  autopromote: GATE [{gate_mode}] id={nom.get('memory_id')} -> {'PROMOTE' if gate_promote else 'BLOCK'} class={v['gate']['gate_class']} src={v['sourceClass']} N={v['corroborationCount']} contradicts={v['contradicts']} :: {v['gate']['reason']}")
                except Exception as e:  # noqa: BLE001 — the gate must never crash the consolidator
                    log(f"  autopromote: GATE error id={nom.get('memory_id')} (non-fatal): {e}")
                    gate_errored = True
                gate_blocked = ap.resolve_gate_blocked(gate_mode, gate_promote, gate_errored)
            if self.dry:
                summary.append(f"- [DRY-RUN, not promoted] {short} [reason: {reason}]")
                continue
            if gate_blocked:
                blocked_n += 1
                summary.append(f"- [GATE-BLOCKED, not promoted] {short} [reason: {reason}]")
                log(f"  autopromote: GATE ENFORCE blocked id={nom.get('memory_id')} — not promoted to canonical")
                continue
            rc, out = _canonize(str(nom.get("memory_id")), reason)
            if re.search(r"tier.{1,8}canonical", out):
                self.promoted += 1
                summary.append(f"- {short} [reason: {reason}]")
                log(f"  autopromote: promoted id={nom.get('memory_id')} confidence={nom.get('confidence')} transport=autonomous")
            else:
                failed += 1
                log(f"  autopromote: promotion failed (exit={rc}) id={nom.get('memory_id')}: {_clip(out or '(no output)', 200)}")
                summary.append(f"- [FAILED] {short} [reason: {reason}]")
            log(f"  autopromote: audit id={nom.get('memory_id')} reason={reason} confidence={nom.get('confidence')} exit={rc} transport=autonomous")
        if not self.dry:
            stamp = self.now.strftime("%Y-%m-%d %H:%M")
            body = _lines(summary) if summary else "- (none promoted this cycle)"
            self._append_morning(f"\n## Autonomous canonical promotions -- {stamp} (review/demote as needed)\n{body}\n")
        log(f"  autopromote done: promoted={self.promoted} failed={failed} gate_blocked={blocked_n} deduped={len(deduped)} over_cap={len(over_cap)} gate_codex_tokens={self.tokens['gate']} (DryRun={self.dry}, {promote_ms if promote_ms is not None else 'skipped'})")

        # ---- phase 4: prune & index (deployed builder; the throttle marks only after a good build)
        log("=== phase 4: prune & index ===")
        index_exit = 0
        if not self.dry:
            index_exit, out = _run_deployed("memory-index-build.py")
            log(f"  {out}")
            if index_exit != 0:
                log(f"  index build failed (exit={index_exit}); throttle NOT marked")
                return {"phase": "prune", "posted": self.posted, "promoted": self.promoted, "note": f"index build failed (exit={index_exit}); throttle NOT marked"}
            ams_env.mark_throttle("dream")
            ams_env.mark_throttle("index-refresh")   # the decoupled refresh step need not rebuild what this just built
            self.save_phase("prune", {"posted_insights": self.posted, "index_rebuilt": True, "index_exit_code": 0,
                                      "skipped_reason": None, "timestamp": self.now.isoformat()})
            rc, out = _run_deployed("brand-scope-audit.py")
            log(f"  brand-scope audit (exit={rc}): {out}")
        else:
            log("  prune receipt not written (DryRun) -- prune.json is the consolidation-completed receipt")

        # ---- phase 5: drift AFTER + compare (only when a BEFORE was taken)
        if drift_before:
            try:
                drift_after = self.drift_snapshot("after")
                if drift_after:
                    cmd = [sys.executable, "retrieval_drift.py", "compare", drift_before, drift_after]
                    if self._drift_state_support():
                        cmd += ["--state", str(self._drift_state_path())]
                    rc, out = self.eval_runner(cmd)
                    log(f"  drift compare (exit={rc}): {out}")
                    if rc == 2:
                        self.add_drift_record("drift", out)
                        log("  !! RETRIEVAL DRIFT ALARM -- a canary became unretrievable (within-run) or the cross-run floor/high-water legs tripped; flagged to consolidation-drift.jsonl")
            except Exception as e:  # noqa: BLE001
                log(f"  drift compare FAILED (non-fatal): {e}")

        # ---- heartbeat section (rotation first)
        if not self.dry:
            try:
                self._rotate_morning()
                hb = []
                deep = self.mem0.health_deep() if hasattr(self.mem0, "health_deep") else None
                if deep is None:
                    hb.append("- /health/deep unreachable from the dream")
                else:
                    hb.append(f"- /health/deep ok: {deep.get('ok')}")
                    cap = (deep.get("checks") or {}).get("capabilities") or {}
                    if cap:
                        dead, unk = list(cap.get("dead_required") or []), list(cap.get("unknown") or [])
                        if dead:
                            hb.append(f"- DEAD required capabilities: {', '.join(map(str, dead))}")
                        hb.append(f"- capabilities: {len(dead)} dead-required, {len(unk)} unknown")
                    rd = (deep.get("checks") or {}).get("retrieval_drift") or {}
                    if rd.get("alarm"):
                        hb.append(f"- DRIFT ALARM standing (before={rd.get('before_retrievable')}/{rd.get('n_total')}, hwm={rd.get('hwm')})")
                    if int(rd.get("consecutive_snapshot_failures") or 0) >= 2:
                        hb.append("- DRIFT GUARD DEAD (>=2 consecutive snapshot failures)")
                if len(hb) <= 1 and deep is not None:
                    hb = ["- healthy; no standing alarms visible to the dream"]
                self._append_morning(f"\n## Heartbeat -- {self.now.strftime('%Y-%m-%d %H:%M')}\n{_lines(hb)}\n")
            except Exception as e:  # noqa: BLE001
                log(f"  heartbeat section failed (non-fatal): {e}")

        ams_env.write_usage(COMPONENT, tokens_used=self.tokens["gather"] + self.tokens["consolidate"] + self.tokens["gate"],
                            duration_ms=self.ms["gather"] + self.ms["consolidate"], status="ok", items_posted=self.posted, outcome="ok")
        log(f"=== dream cycle done (DryRun={self.dry}) ===")
        return {"phase": "done", "posted": self.posted, "promoted": self.promoted, "note": "dream cycle done"}

    def _gate_log(self, v: dict, mode: str) -> None:
        """One pg-v1 line per gated nominee (the calibration log the enforce flip is read from)."""
        try:
            rec = {"ts": self.now.isoformat(), "schema_version": "pg-v1", "mode": mode, "dry_run": self.dry,
                   "memory_id": v.get("memoryId"), "candidate_preview": v.get("candidatePreview"), "source": v.get("source"),
                   "source_class": v.get("sourceClass"), "sibling_count": v.get("siblingCount"),
                   "sibling_threshold": v.get("siblingThreshold"), "was_reobserved": v.get("wasReObserved"),
                   "corroboration_count": v.get("corroborationCount"), "near_canonical_count": v.get("nearCanonicalCount"),
                   "contradicts": v.get("contradicts"), "contradiction_parsed": v.get("contradictionParsed"),
                   "gate_promote": v["gate"]["promote"], "gate_class": v["gate"]["gate_class"], "gate_reason": v["gate"]["reason"],
                   "codex_ms": v.get("codexMs"), "codex_tokens": v.get("codexTokens")}
            p = Path.home() / ".mem0" / "promotion-gate.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            log(f"  autopromote: GATE log append failed (non-fatal): {e}")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="nightly dream consolidator (native authority)")
    p.add_argument("--dry-run", action="store_true", help="no promotions, no writes, no throttle mark")
    p.add_argument("--force", action="store_true", help="bypass ONLY the 23h throttle (the judge lock still applies)")
    p.add_argument("--transcripts-dir", default=None, help="local transcripts to append to the gather corpus (default: ~/.claude/projects when present)")
    return p.parse_args(argv)


def run(args, *, mem0, judge, qdrant_http, eval_runner, now=None, probe=None) -> dict:
    """The cycle with injected collaborators. Holds ~/.mem0/maintenance/dream.lock for its duration
    (a chain re-entry or a hand run overlapping the nightly is a quiet, receipted skip)."""
    lock_path = ams_env.state_dir() / "dream.lock"
    lock = open(lock_path, "w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("skipping: dream lock held by another cycle")
            return {"phase": "lock", "posted": 0, "promoted": 0, "note": "skipping: dream lock held by another cycle"}
        return Dream(args, mem0, judge, qdrant_http, eval_runner, now, probe=probe).run()
    finally:
        lock.close()


def main(argv=None, **injected) -> None:
    args = parse_args(argv)
    mem0 = injected.get("mem0") or Mem0Client(ams_env.mem0_url(), ams_env.api_key(), ams_env.user_id())
    judge = injected.get("judge")
    if judge is None:
        if _csc is None:
            sys.exit("FAIL: codex_shim_client not importable (deploy the server package beside the scripts)")
        judge = _csc.judge
    qdrant_http = injected.get("qdrant_http") or httpx.Client(timeout=10.0)
    eval_runner = injected.get("eval_runner") or _real_eval_runner(ams_env.eval_root())
    probe = injected.get("probe", codex_usage.probe_window)
    out = run(args, mem0=mem0, judge=judge, qdrant_http=qdrant_http, eval_runner=eval_runner, probe=probe)
    print(f"dream: {out['note']} (posted={out['posted']} promoted={out['promoted']})", flush=True)
    if out.get("unreachable"):
        sys.exit(4)
    sys.exit(0)


if __name__ == "__main__":
    main()
