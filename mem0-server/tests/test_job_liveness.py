"""Headless pins for job_liveness.py + drift_state.py (W3 alarm mouths).

Pure parts (epoch-content parsing, age computation, mojibake-tolerant section
counting) pin directly; the collectors pin against tmp_path fixtures and must
NEVER raise (missing files -> nulls plus an error note, not exceptions). The
mojibake dash in the section fixture is assembled from chr() codepoints on
purpose -- this source file stays pure ASCII while the fixture reproduces the
CP437/CP1252 boundary the real morning summary crosses.
"""
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from job_liveness import (  # noqa: E402
    age_hours,
    count_lines,
    count_recent_sections,
    get_role,
    job_liveness_health,
    normalize_host,
    parse_epoch_content,
    read_stack_env,
)
from drift_state import (  # noqa: E402
    drift_state_health,
    parse_ts_epoch,
    shape_drift_state,
)

# UTF-8 em-dash bytes (E2 80 94) decoded through cp1252 -> the classic
# three-glyph mojibake the pre-W3 PS 5.1 writer left in section headers.
MOJI_DASH = chr(0x00E2) + chr(0x20AC) + chr(0x201D)
# U+FEFF, the BOM Windows PowerShell 5.1 prefixes to `Set-Content -Encoding UTF8`
# output. Spelled with chr() for the same reason as MOJI_DASH above: this source
# file stays pure ASCII while the fixture reproduces the real byte sequence.
BOM = chr(0xFEFF)

JL_KEYS = {"role", "last_dream_age_h", "prune_age_h", "gather_age_h",
           "backup_manifest_age_h", "dedup_report_age_h",
           "morning_summary_age_h", "morning_summary_sections_48h",
           # W4 session-scoped receipts (additive -- the cross-track contract
           # grows by ADDING keys; renaming one breaks capabilities.py and the
           # PowerShell readers together).
           "l1a_attempt_age_h", "l1a_success_age_h",
           "sessionstart_banner_age_h",
           "mcp_shim_receipt_age_h", "mcp_shim_host_match",
           "mcp_shim_stack_version",
           "brand_scope_age_h", "brand_scope_misscoped",
           "outbox_depth", "outbox_replayed_age_h", "outbox_drain_log_age_h",
           # W6 D5: job-queue aggregates from the jobs-heartbeat.json mirror
           # (same-commit with job_liveness.py — this set is strict-equality).
           "jobs_heartbeat_age_h", "jobs_queued", "jobs_running",
           "jobs_failed_24h", "jobs_reaped_24h",
           "jobs_oldest_running_age_h", "jobs_oldest_queued_age_h"}
DS_KEYS = {"state_present", "last_compare_ts", "age_hours",
           "before_retrievable", "n_total", "hwm", "hwm_seeded",
           "consecutive_below_hwm", "consecutive_snapshot_failures",
           "alarm", "missing", "compat_fallback"}


# ----------------------------------------------------------------------
# job_liveness: pure parts
# ----------------------------------------------------------------------

def test_parse_epoch_content():
    assert parse_epoch_content("1723000000\n") == 1723000000.0
    assert parse_epoch_content(" 1723000000 ") == 1723000000.0
    assert parse_epoch_content("") is None
    assert parse_epoch_content("not-an-epoch") is None
    assert parse_epoch_content(None) is None


def test_age_hours():
    assert age_hours(0, 7200) == 2.0
    assert age_hours(3600, 3600) == 0.0
    assert age_hours(None, 12345) is None


def test_count_recent_sections_mojibake_tolerant():
    now = dt.datetime(2026, 8, 7, 12, 0)
    text = (
        "## Heartbeat -- 2026-08-07 03:00\n- line\n"
        "## Autonomous promotions " + MOJI_DASH + " 2026-08-06 09:00 (review)\n"
        "body 2026-08-07 03:00 not a header\n"
        "## Old section " + MOJI_DASH + " 2026-08-01 03:00\n"
        "## No timestamp header\n"
    )
    # ASCII-dash and mojibake-dash headers count identically (digits-only
    # matching); the 6-day-old one is outside 48h; the timestampless one and
    # the non-header body line never count.
    assert count_recent_sections(text, now) == 2


def test_count_recent_sections_empty_and_none():
    now = dt.datetime(2026, 8, 7, 12, 0)
    assert count_recent_sections("", now) == 0
    assert count_recent_sections(None, now) == 0


# ----------------------------------------------------------------------
# job_liveness: collector (tmp_path fixtures; never raises)
# ----------------------------------------------------------------------

def test_collector_missing_everything_nulls_not_raises(tmp_path):
    out = job_liveness_health(mem0_dir=tmp_path / "mem0",
                              win_home=tmp_path / "winhome",
                              now_s=1_700_000_000,
                              environ={}, stack_env={})
    assert set(out) == JL_KEYS | {"error"}
    assert out["role"] is None
    for k in JL_KEYS - {"role"}:
        assert out[k] is None, k
    assert "missing" in out["error"] or "none found" in out["error"]


def test_collector_reads_jobs_heartbeat_mirror(tmp_path):
    """W6 D5 + review fix 4a: the mirror-reading branch had NO functional
    coverage — all 7 keys initialize to None, so the strict-equality JL_KEYS
    pin stayed green through any parse regression (renamed field, broken ts).
    This fixture makes the branch real."""
    now = 1_700_000_000
    mem0 = tmp_path / "mem0"
    mem0.mkdir(parents=True)
    (mem0 / "jobs-heartbeat.json").write_text(json.dumps({
        "ts": dt.datetime.fromtimestamp(now - 3600, dt.timezone.utc).isoformat(),
        "queued": 2, "running": 1, "failed_24h": 3, "reaped_24h": 1,
        "oldest_running_age_s": 7200, "oldest_queued_age_s": 10800,
    }), encoding="utf-8")
    out = job_liveness_health(mem0_dir=mem0, win_home=tmp_path / "winhome",
                              now_s=now, environ={}, stack_env={})
    assert out["jobs_heartbeat_age_h"] == 1.0
    assert out["jobs_queued"] == 2 and out["jobs_running"] == 1
    assert out["jobs_failed_24h"] == 3 and out["jobs_reaped_24h"] == 1
    assert out["jobs_oldest_running_age_h"] == 2.0
    assert out["jobs_oldest_queued_age_h"] == 3.0


def test_collector_jobs_mirror_absent_is_silent_not_an_error(tmp_path):
    """No mirror = no adopter has run on this box — silent nulls, never an
    error note (a fresh box must not read as broken)."""
    mem0 = tmp_path / "mem0"
    mem0.mkdir(parents=True)
    out = job_liveness_health(mem0_dir=mem0, win_home=tmp_path / "winhome",
                              now_s=1_700_000_000, environ={}, stack_env={})
    assert out["jobs_heartbeat_age_h"] is None
    assert "jobs_heartbeat" not in (out.get("error") or "")


def test_collector_jobs_mirror_corrupt_is_fail_soft(tmp_path):
    mem0 = tmp_path / "mem0"
    mem0.mkdir(parents=True)
    (mem0 / "jobs-heartbeat.json").write_text("{not json", encoding="utf-8")
    out = job_liveness_health(mem0_dir=mem0, win_home=tmp_path / "winhome",
                              now_s=1_700_000_000, environ={}, stack_env={})
    assert set(out) == JL_KEYS | {"error"}
    assert out["jobs_heartbeat_age_h"] is None
    assert "jobs_heartbeat" in out["error"]


def test_collector_dream_age_from_stamp_content_not_mtime(tmp_path):
    epoch = 1_700_000_000
    state = tmp_path / "winhome" / ".claude" / "state"
    state.mkdir(parents=True)
    marker = state / "last-dream"
    marker.write_text(str(epoch), encoding="utf-8")
    # Skew the mtime far away from the content: the age must come from the
    # VALUE (the throttle's own record), never the filesystem timestamp.
    os.utime(marker, (epoch - 999_999, epoch - 999_999))
    out = job_liveness_health(mem0_dir=tmp_path / "mem0",
                              win_home=tmp_path / "winhome",
                              now_s=epoch + 2 * 3600,
                              environ={}, stack_env={})
    assert out["last_dream_age_h"] == 2.0


def test_collector_full_fixture_all_fields(tmp_path):
    now_s = 1_700_000_000
    mem0_dir = tmp_path / "mem0"
    (mem0_dir / "backups").mkdir(parents=True)
    old = mem0_dir / "backups" / "manifest-old.json"
    new = mem0_dir / "backups" / "manifest-new.json"
    for p, age_h in ((old, 50), (new, 12)):
        p.write_text("{}", encoding="utf-8")
        os.utime(p, (now_s - age_h * 3600, now_s - age_h * 3600))
    dedup = mem0_dir / "dedup-report.jsonl"
    dedup.write_text("{}\n", encoding="utf-8")
    os.utime(dedup, (now_s - 6 * 3600, now_s - 6 * 3600))

    win_home = tmp_path / "winhome"
    state = win_home / ".claude" / "state"
    dream = state / "dream"
    dream.mkdir(parents=True)
    (state / "last-dream").write_text(str(now_s - 10 * 3600), encoding="utf-8")
    for name, age_h in (("prune.json", 10), ("gather.json", 10)):
        f = dream / name
        f.write_text("{}", encoding="utf-8")
        os.utime(f, (now_s - age_h * 3600, now_s - age_h * 3600))
    now_dt = dt.datetime.fromtimestamp(now_s)
    fresh = (now_dt - dt.timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")
    stale = (now_dt - dt.timedelta(hours=90)).strftime("%Y-%m-%d %H:%M")
    ms = dream / "morning-summary.md"
    ms.write_text("## Heartbeat -- " + fresh + "\n- ok\n"
                  "## Older " + MOJI_DASH + " " + stale + "\n",
                  encoding="utf-8")
    os.utime(ms, (now_s - 9 * 3600, now_s - 9 * 3600))

    # --- W4 session-scoped receipts, so the no-error pin below stays honest ---
    (state / "last-l1a-attempt").write_text(str(now_s - 900), encoding="utf-8")
    (state / "last-l1a").write_text(str(now_s - 2 * 3600), encoding="utf-8")
    (mem0_dir / "last-sessionstart-banner").write_text(str(now_s - 3600),
                                                       encoding="utf-8")
    (mem0_dir / "mcp-shim-receipt.json").write_text(json.dumps(
        {"ts": now_s - 1800, "host": "Box-A.local", "stack_version": "1.18.0",
         "authority": "http://127.0.0.1:18791"}), encoding="utf-8")
    (mem0_dir / "brand-scope-status.json").write_text(json.dumps(
        {"ts": "2026-08-07T03:00:00+00:00", "n_canonical": 40,
         "n_misscoped": 0, "misscoped": []}), encoding="utf-8")
    (mem0_dir / "outbox.jsonl").write_text('{"op":"add"}\n\n{"op":"add"}\n',
                                           encoding="utf-8")
    # 2026-09-01: depth must count a retryable-stopped drain's leftover queue file
    # too — counting only outbox.jsonl reported None while a stranded update sat
    # in .replaying for 15 hours and the offline-outbox row read "unknown".
    (mem0_dir / "outbox.replaying.jsonl").write_text('{"op":"update"}\n',
                                                     encoding="utf-8")
    for name, age_h in (("outbox.replayed.jsonl", 5), ("outbox-drain.log", 4)):
        f = mem0_dir / name
        f.write_text("x\n", encoding="utf-8")
        os.utime(f, (now_s - age_h * 3600, now_s - age_h * 3600))

    out = job_liveness_health(mem0_dir=mem0_dir, win_home=win_home,
                              now_s=now_s, environ={},
                              stack_env={"MEM0_ROLE": "brain"},
                              hostname="box-a")
    assert out["role"] == "brain"
    assert out["backup_manifest_age_h"] == 12.0   # newest manifest, not oldest
    assert out["dedup_report_age_h"] == 6.0
    assert out["last_dream_age_h"] == 10.0
    assert out["prune_age_h"] == 10.0
    assert out["gather_age_h"] == 10.0
    assert out["morning_summary_age_h"] == 9.0
    assert out["morning_summary_sections_48h"] == 1
    assert out["l1a_attempt_age_h"] == 0.25
    assert out["l1a_success_age_h"] == 2.0
    assert out["sessionstart_banner_age_h"] == 1.0
    assert out["mcp_shim_receipt_age_h"] == 0.5
    assert out["mcp_shim_host_match"] is True     # 'Box-A.local' ~ 'box-a'
    assert out["mcp_shim_stack_version"] == "1.18.0"
    assert out["brand_scope_misscoped"] == 0
    assert out["brand_scope_age_h"] is not None
    assert out["outbox_depth"] == 3               # outbox (2, blanks excluded) + stranded .replaying (1)
    assert out["outbox_replayed_age_h"] == 5.0
    assert out["outbox_drain_log_age_h"] == 4.0
    assert set(out) == JL_KEYS  # everything present -> no error key


def test_get_role_env_beats_stack_env_and_normalizes(tmp_path):
    # role_file injected as absent so the LIVE box's ~/.mem0/role receipt
    # cannot leak into a headless assertion (the fallback exists on purpose —
    # diff-review fix 3 — and gets its own pin below).
    absent = tmp_path / "role"
    assert get_role(environ={"MEM0_ROLE": " Brain "},
                    stack_env={"MEM0_ROLE": "replica"}, role_file=absent) == "brain"
    assert get_role(environ={}, stack_env={"MEM0_ROLE": "replica"},
                    role_file=absent) == "replica"
    assert get_role(environ={}, stack_env={}, role_file=absent) is None


def test_get_role_falls_back_to_role_receipt(tmp_path):
    # Diff-review fix 3: install/2-windows-config.ps1 writes the authoritative
    # role to ~/.mem0/role; env and stack.env silence must inherit it — a brain
    # default on a replica convicts its by-design-absent jobs as dead_required.
    rf = tmp_path / "role"
    rf.write_text(" Replica \n", encoding="utf-8")
    assert get_role(environ={}, stack_env={}, role_file=rf) == "replica"
    # env still wins over the receipt
    assert get_role(environ={"MEM0_ROLE": "brain"}, stack_env={},
                    role_file=rf) == "brain"


# ----------------------------------------------------------------------
# W4: session-scoped receipts
# ----------------------------------------------------------------------

def test_epoch_stamps_survive_the_powershell_51_bom():
    """Review F13. The Claude Code hooks run under Windows PowerShell 5.1, whose
    `Set-Content -Encoding UTF8` writes a BOM. read_text(encoding='utf-8') keeps
    it as U+FEFF and str.strip() does NOT remove it -- so without explicit
    stripping the very stamps the two-signal rule depends on would parse as None
    and read as 'no signal' forever."""
    assert parse_epoch_content(BOM + "1723000000") == 1723000000.0
    assert parse_epoch_content(BOM + "1723000000\r\n") == 1723000000.0
    assert parse_epoch_content(" " + BOM + " 1723000000 ") == 1723000000.0


def test_l1a_two_signal_stamps_are_read_from_content(tmp_path):
    now_s = 1_700_000_000
    state = tmp_path / "winhome" / ".claude" / "state"
    state.mkdir(parents=True)
    # BOM-prefixed, exactly as PS 5.1 writes them.
    (state / "last-l1a-attempt").write_text(BOM + str(now_s - 600),
                                            encoding="utf-8")
    (state / "last-l1a").write_text(BOM + str(now_s - 7200),
                                    encoding="utf-8")
    # Skew the mtimes: the age must come from the CONTENT, like last-dream.
    for f in (state / "last-l1a-attempt", state / "last-l1a"):
        os.utime(f, (now_s - 999_999, now_s - 999_999))
    out = job_liveness_health(mem0_dir=tmp_path / "mem0",
                              win_home=tmp_path / "winhome", now_s=now_s,
                              environ={}, stack_env={})
    assert out["l1a_attempt_age_h"] == 0.17
    assert out["l1a_success_age_h"] == 2.0


def test_shim_receipt_host_mismatch_is_reported(tmp_path):
    now_s = 1_700_000_000
    mem0_dir = tmp_path / "mem0"
    mem0_dir.mkdir(parents=True)
    (mem0_dir / "mcp-shim-receipt.json").write_text(json.dumps(
        {"ts": now_s - 3600, "host": "OTHER-BOX", "stack_version": "1.0.0"}),
        encoding="utf-8")
    out = job_liveness_health(mem0_dir=mem0_dir, win_home=tmp_path / "nowin",
                              now_s=now_s, environ={}, stack_env={},
                              hostname="this-box")
    # The age is still reported (diagnostics), but the host flag is what the
    # verdict keys on -- ~/.mem0 travels in stack backups.
    assert out["mcp_shim_receipt_age_h"] == 1.0
    assert out["mcp_shim_host_match"] is False
    assert out["mcp_shim_stack_version"] == "1.0.0"


def test_malformed_receipts_are_notes_not_exceptions(tmp_path):
    now_s = 1_700_000_000
    mem0_dir = tmp_path / "mem0"
    mem0_dir.mkdir(parents=True)
    (mem0_dir / "mcp-shim-receipt.json").write_text("{not json",
                                                    encoding="utf-8")
    (mem0_dir / "brand-scope-status.json").write_text("[1, 2, 3]",
                                                      encoding="utf-8")
    (mem0_dir / "last-sessionstart-banner").write_text("not-an-epoch",
                                                       encoding="utf-8")
    out = job_liveness_health(mem0_dir=mem0_dir, win_home=tmp_path / "nowin",
                              now_s=now_s, environ={}, stack_env={})
    assert out["mcp_shim_receipt_age_h"] is None
    assert out["brand_scope_misscoped"] is None
    assert out["sessionstart_banner_age_h"] is None
    assert "error" in out           # named, not raised


def test_normalize_host_strips_domain_and_case():
    assert normalize_host("BOX-A.tail1234.ts.net") == "box-a"
    assert normalize_host(" Box-A ") == "box-a"
    assert normalize_host("") is None
    assert normalize_host(None) is None


def test_count_lines_ignores_blanks_and_absent(tmp_path):
    p = tmp_path / "outbox.jsonl"
    assert count_lines(p) is None            # absent != empty
    p.write_text('{"a":1}\n\n   \n{"b":2}\n', encoding="utf-8")
    assert count_lines(p) == 2


def test_read_stack_env(tmp_path):
    p = tmp_path / "stack.env"
    p.write_text("# comment\nMEM0_ROLE=brain\nMEM0_BIND = 127.0.0.1\n\nnoequals\n",
                 encoding="utf-8")
    env = read_stack_env(p)
    assert env == {"MEM0_ROLE": "brain", "MEM0_BIND": "127.0.0.1"}
    assert read_stack_env(tmp_path / "absent.env") == {}


# ----------------------------------------------------------------------
# drift_state
# ----------------------------------------------------------------------

def test_drift_absent_file_state_present_false(tmp_path):
    out = drift_state_health(path=tmp_path / "no-such-state.json")
    assert set(out) == DS_KEYS  # no error key: absence is a fact, not a fault
    assert out["state_present"] is False
    for k in DS_KEYS - {"state_present"}:
        assert out[k] is None, k


def test_drift_malformed_file_error_field(tmp_path):
    p = tmp_path / "retrieval-drift-state.json"
    p.write_text("{not json", encoding="utf-8")
    out = drift_state_health(path=p)
    assert out["state_present"] is True
    assert "error" in out and out["error"]


def test_drift_non_object_json_error_field():
    out = shape_drift_state([1, 2, 3])
    assert out["state_present"] is True
    assert "list" in out["error"]


def test_drift_present_passthrough_shape(tmp_path):
    raw = {"last_compare_ts": 1_000_000, "before_retrievable": 24,
           "n_total": 25, "hwm": 25, "hwm_seeded": True,
           "consecutive_below_hwm": 1, "consecutive_snapshot_failures": 0,
           "alarm": True, "missing": ["fact-7"], "compat_fallback": False}
    p = tmp_path / "retrieval-drift-state.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    out = drift_state_health(path=p, now_s=1_003_600)
    assert set(out) == DS_KEYS
    assert out["state_present"] is True
    assert out["age_hours"] == 1.0
    for k in ("before_retrievable", "n_total", "hwm", "hwm_seeded",
              "consecutive_below_hwm", "consecutive_snapshot_failures",
              "alarm", "missing", "compat_fallback"):
        assert out[k] == raw[k], k


def test_drift_compat_fallback_marker(tmp_path):
    # Legacy guard predating --state writes only {compat_fallback, ts}:
    # surfaces as state_present with null compare fields.
    p = tmp_path / "retrieval-drift-state.json"
    p.write_text(json.dumps({"compat_fallback": True, "ts": "x"}),
                 encoding="utf-8")
    out = drift_state_health(path=p, now_s=0)
    assert out["state_present"] is True
    assert out["compat_fallback"] is True
    assert out["last_compare_ts"] is None and out["age_hours"] is None


def test_parse_ts_epoch_formats():
    assert parse_ts_epoch(1_000_000) == 1_000_000.0
    assert parse_ts_epoch("1000000") == 1_000_000.0
    # PowerShell 'o' format: 7 fractional digits + Z must parse.
    got = parse_ts_epoch("2026-08-07T03:00:00.1234567Z")
    assert isinstance(got, float)
    assert parse_ts_epoch("not a time") is None
    assert parse_ts_epoch("") is None
    assert parse_ts_epoch(None) is None
