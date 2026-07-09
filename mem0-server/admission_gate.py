"""v0.18 Phase C: Admission Gate Phase-1. v0.19 Phase I: Phase-2 dimensions.

Server-side retrieval admission policy. Phase-1 enforces three dimensions:
- scope: brand isolation only (fail-closed since v0.19 M4: a brandless request
  scope admits ONLY null-brand records unless the caller explicitly opts in via
  scope["allow_cross_brand"]; user_id/workspace/project are NOT enforced here —
  user_id is filtered upstream by Qdrant, workspace/project enforcement is
  still deferred)
- tier: tier allowlist by query_class
- recency: age cap by query_class

Phase-2 (v0.19 Phase I) adds:
- supersession-aware filtering (I.1): records stamped superseded_by=<mid> are
  rejected in durable/operational; the new 'history' query_class (forensic=True)
  admits them for forensic queries.
- task-relevance floor (I.2): operational-class results whose rerank_score
  falls below MEM0_RELEVANCE_FLOOR_OPERATIONAL are rejected; fail-open when the
  score is absent; disabled by default (see _relevance_floor_from_env).
- contradiction filtering (I.3): records the offline contradiction sweep
  stamped with contradicts_canonical=<mid> are rejected in durable/operational;
  the history class (forensic=True) admits them.

Decision object:
- admit: True/False
- reason: human-readable string explaining the decision

Rejected results are logged to ~/.mem0/admission-rejected.jsonl for audit."""
from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)


@dataclass
class AdmissionDecision:
    admit: bool
    reason: str


# MEM-8 (2026-07-03): retrieval-starvation observability. The gate silently
# eating results was invisible short of grepping admission-rejected.jsonl —
# the exact failure mode behind branded-recall starvation (A2 measured 37.5%
# recall before the v1.0 Phase B fix). In-memory counters per reason FAMILY
# (the prefix before ':' — the suffix carries per-record ids/brands and would
# explode cardinality), reset when the UTC day rolls; /health/deep surfaces
# the snapshot as checks.admission_rejections_today. In-process only — a
# restart zeroes it; admission-rejected.jsonl stays the durable audit record.
admission_rejection_stats: dict = {"date": None, "total": 0, "reasons": {}}


def _count_rejection(reason: str) -> None:
    """Bump the per-family daily counter (lazy day-roll reset)."""
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    if admission_rejection_stats["date"] != today:
        admission_rejection_stats["date"] = today
        admission_rejection_stats["total"] = 0
        admission_rejection_stats["reasons"] = {}
    family = (reason or "unknown").split(":", 1)[0]
    admission_rejection_stats["total"] += 1
    admission_rejection_stats["reasons"][family] = (
        admission_rejection_stats["reasons"].get(family, 0) + 1)


def admission_rejections_today(top_n: int = 8) -> dict:
    """Snapshot for /health/deep: {date, total, reasons} with the top_n reason
    families by count. Zero-I/O; informational — never flips health ok."""
    reasons = dict(sorted(
        admission_rejection_stats["reasons"].items(), key=lambda kv: -kv[1])[:top_n])
    return {"date": admission_rejection_stats["date"],
            "total": admission_rejection_stats["total"],
            "reasons": reasons}


@dataclass
class AdmissionPolicy:
    allowed_tiers: tuple[str, ...]
    max_age_days: Optional[int]
    # v0.19 Phase I.1: forensic=True disables the supersession check (and the
    # I.3 contradiction check) — set only by the 'history' query_class so
    # superseded/contradicted records stay reachable for forensic queries.
    forensic: bool = False
    # v0.19 Phase I.2: task-relevance floor. Applies ONLY to the operational
    # class and ONLY when the result carries a rerank_score (fail-open
    # otherwise). None = disabled. Populated from the
    # MEM0_RELEVANCE_FLOOR_OPERATIONAL env knob by default_policy_for_class.
    relevance_floor: Optional[float] = None
    # v1.0 Phase 4 / R5: brand-coherence floor. A pragmatic tightening of
    # cross-brand admission "beyond cosine" (MemGate 2606.06054 motivates learned
    # admissibility; this is the score-floor approximation). For a BRANDED result
    # (res_brand truthy) admitted under a brand-scoped request, reject it when its
    # retrieval score is below this floor — catches a near-but-wrong-domain
    # branded match that the flat cosine boundary would have surfaced. None =
    # disabled (default); fail-open when the result carries no score. The
    # fail-CLOSED brand-mismatch / brandless rules below are unchanged — this only
    # ADDS a within-brand weak-match cut. Populated from MEM0_BRAND_COHERENCE_THRESHOLD.
    brand_coherence_floor: Optional[float] = None

    def evaluate(self, result: dict, scope: dict, query_class: str) -> AdmissionDecision:
        meta = result.get("metadata") or {}
        # 1. Tier check
        tier = meta.get("tier")
        if tier and tier not in self.allowed_tiers:
            return AdmissionDecision(False, f"tier_disallowed:{tier}")
        # 1b. Supersession check (v0.19 Phase I.1) — a record stamped
        # superseded_by=<newer_mid> (same convention the cascade-delete chain
        # in app.py walks) is rejected so the newer record surfaces instead.
        # forensic policies (history class) skip this: superseded records are
        # exactly what a forensic query wants back. Null/absent -> falsy ->
        # admitted (legacy data carries no supersession pointer).
        if not self.forensic:
            superseded_by = meta.get("superseded_by")
            if superseded_by:
                return AdmissionDecision(False, f"superseded_by:{superseded_by}")
            # 1c. Contradiction check (v0.19 Phase I.3) — the offline
            # contradiction sweep (scripts/wsl/contradiction-sweep.py, trusted
            # actor contradiction-sweep-v019) stamps candidates that contradict a
            # canonical-tier memory with contradicts_canonical=<canonical_mid>. The
            # gate only reads the stamp — no LLM runs at retrieval time.
            # contradiction_checked_at alone (the sweep's NO verdict) never rejects.
            # v0.29.4: ONLY contradicts_canonical (the AUTHORITATIVE Codex verdict) is
            # enforced here. contradicts_canonical_pending (a weak LOCAL-judge advisory
            # verdict) is DELIBERATELY NOT read — a local verdict must never hide a live
            # record (model-routing rule: no local judgment on the retrieval path). A
            # Codex re-judge promotes pending -> contradicts_canonical before it enforces.
            contradicts = meta.get("contradicts_canonical")
            if contradicts:
                return AdmissionDecision(False, f"contradicts_canonical:{contradicts}")
        # 2. Brand match (v0.19 M4+M14: fail-closed + case-insensitive)
        # v0.20 Phase F (M14): brands are stripped BEFORE the falsiness checks —
        # a whitespace-only brand ('  ') now normalizes to the legacy-empty
        # convention on this layer exactly like '' (the client layer mirrors it
        # via [string]::IsNullOrWhiteSpace in user-prompt-lib.ps1).
        req_brand = str(scope.get("brand") or "").strip()
        res_brand = str(meta.get("brand") or "").strip()
        if req_brand and res_brand and req_brand.lower() != res_brand.lower():
            # M14: case-insensitive compare aligns with the client layer
            # (PowerShell -eq); empty/whitespace brands are falsy -> legacy, admitted.
            return AdmissionDecision(False, f"brand_mismatch:{res_brand}_vs_{req_brand}")
        # v0.20 L3: allow_cross_brand is strict-parsed — only bool True or a
        # recognized true-string ('1'/'true'/'yes', case-insensitive) opts in.
        # Bare Python truthiness previously let a hand-rolled REST client
        # sending the STRING "false" or "0" silently enable cross-brand.
        _acb = scope.get("allow_cross_brand")
        allow_cross = _acb is True or (
            isinstance(_acb, str) and _acb.strip().lower() in ("1", "true", "yes")
        )
        if not req_brand and res_brand and not allow_cross:
            # v0.19 M4 fail-closed: a request with NO brand scope admits only
            # brand-neutral (null-brand) records. Brand-scoped records require a
            # matching scope["brand"], or the explicit, audited opt-in
            # filters.allow_cross_brand=true. Before v0.19 this path was
            # fail-open (brandless searches returned every brand's records).
            return AdmissionDecision(False, f"brand_scope_required:{res_brand}")
        # 2b. v1.0 R5: brand-coherence floor — a BRANDED result that survived the
        # fail-closed match above is still cut if its retrieval score is below the
        # configured floor (a weak near-but-wrong-domain branded match). Disabled by
        # default (None); fail-open when the result has no score. Never relaxes the
        # fail-closed rules above; only tightens within-brand admission.
        if self.brand_coherence_floor is not None and res_brand:
            cscore = result.get("score")
            if cscore is not None:
                try:
                    if float(cscore) < self.brand_coherence_floor:
                        return AdmissionDecision(
                            False, f"brand_coherence:{cscore}_below_{self.brand_coherence_floor}")
                except (ValueError, TypeError):
                    pass  # unparseable score -> fail-open, same as relevance_floor
        # 3. Recency for operational/recency-sensitive classes
        if query_class == "operational" and self.max_age_days is not None:
            created = meta.get("created_at") or result.get("created_at")
            if created:
                try:
                    c_dt = _dt.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                    age = (_dt.datetime.now(_dt.timezone.utc) - c_dt).total_seconds() / 86400
                    if age > self.max_age_days:
                        return AdmissionDecision(False, f"recency:{int(age)}d_exceeds_{self.max_age_days}d_in_class_{query_class}")
                except (ValueError, TypeError):
                    pass
        # 4. Task-relevance floor (v0.19 Phase I.2) — operational class only,
        # and only when the reranker actually scored this result. rerank_score
        # is a raw bge-reranker-v2-m3 cross-encoder logit attached at the top
        # level of the result dict by reranker.rerank(); absent score (rerank
        # off, reranker down, defensive passthrough) fails open. A legitimate
        # score of 0.0 is still compared (explicit None check, mirrors the
        # v0.18 MED-4 convention in app.py).
        if query_class == "operational" and self.relevance_floor is not None:
            rscore = result.get("rerank_score")
            if rscore is not None:
                try:
                    if float(rscore) < self.relevance_floor:
                        return AdmissionDecision(
                            False, f"relevance_floor:{rscore}_below_{self.relevance_floor}")
                except (ValueError, TypeError):
                    pass  # unparseable score -> fail-open, same as recency
        return AdmissionDecision(True, "admitted")


def _relevance_floor_from_env() -> Optional[float]:
    """v0.19 Phase I.2: parse MEM0_RELEVANCE_FLOOR_OPERATIONAL.

    Disabled-by-default: absent, empty, '0'/'0.0' sentinel, an unparseable
    value, or a non-finite value (inf/nan) all return None (no floor). The knob
    exists for tuning, never for surprise rejections. (v0.20 Phase F L10:
    float() happily parses 'inf' — a reject-everything floor — and 'nan',
    whose comparisons are always False — a never-fires floor; both now WARN
    and disable.)

    Observed live rerank_score distribution (2026-06-12, bge-reranker-v2-m3 raw
    logits, 4 varied queries x up to 8 results against the production corpus):
      strong on-topic hits:        +0.69 .. +4.24
      weak/tangential matches:     -1.21 .. -4.89
      off-topic vector matches:    -6.77 .. -8.02
      fully irrelevant query hits: -10.23 .. -11.01  (observed minimum)
    A conservative enabling value is -15.0 — well below the observed minimum,
    so it rejects NOTHING in today's distribution and only catches future
    catastrophically-irrelevant scores. Raw logits vary widely per query, so
    the floor ships disabled until tuned against more live data."""
    raw = os.environ.get("MEM0_RELEVANCE_FLOOR_OPERATIONAL", "").strip()
    if not raw:
        return None
    try:
        val = float(raw)
    except ValueError:
        log.warning("MEM0_RELEVANCE_FLOOR_OPERATIONAL=%r is not a float; relevance floor disabled", raw)
        return None
    if val == 0.0:
        return None  # 0 sentinel = disabled
    if not math.isfinite(val):
        log.warning("MEM0_RELEVANCE_FLOOR_OPERATIONAL=%r is non-finite; relevance floor disabled", raw)
        return None
    return val


def _brand_coherence_floor_from_env() -> Optional[float]:
    """v1.0 R5: parse MEM0_BRAND_COHERENCE_THRESHOLD (the within-brand weak-match
    cut). Disabled-by-default: absent / empty / '0' / unparseable / non-finite ->
    None (no floor). Mirrors _relevance_floor_from_env's safety so a bad value can
    never cause surprise rejections. Operates on the result's retrieval `score`."""
    raw = os.environ.get("MEM0_BRAND_COHERENCE_THRESHOLD", "").strip()
    if not raw:
        return None
    try:
        val = float(raw)
    except ValueError:
        log.warning("MEM0_BRAND_COHERENCE_THRESHOLD=%r is not a float; brand-coherence floor disabled", raw)
        return None
    if val == 0.0:
        return None
    if not math.isfinite(val):
        log.warning("MEM0_BRAND_COHERENCE_THRESHOLD=%r is non-finite; brand-coherence floor disabled", raw)
        return None
    return val


def default_policy_for_class(query_class: str) -> AdmissionPolicy:
    """v0.18 default policy mapping by query_class.

    durable: stable+evidence+insight, no recency cap (knowledge facts age well;
        insight is consolidator-distilled durable knowledge — before the v0.18
        fix-pass NO class admitted it, making the tier unreachable read-side)
    operational: stable+evidence+insight, 180d max age (operational notes go
        stale; insight added in v0.19 M2-residual — consolidator insights are
        durable knowledge and the recency cap still applies)
    canonical: stable+canonical, no recency cap (explicit canonical query)
    history: durable allowlist + canonical, no recency cap, forensic=True — the
        v0.19 Phase I.1 escape hatch that admits superseded (and, with I.3,
        contradiction-stamped) records for forensic/audit queries.
        v0.20 Phase F (M13): canonical joined the history allowlist — before
        that, a superseded/contradiction-stamped CANONICAL record was
        unreachable in EVERY class (durable/operational reject the tier,
        canonical class rejects the stamp, history rejected the tier), so the
        documented forensic escape hatch silently excluded exactly the records
        whose history matters most. No trust-boundary change: the same API key
        already reads canonical via query_class="canonical" (the gate is not an
        authorization layer — see the admission-gate.md threat model).
    """
    qc = (query_class or "durable").lower()
    # v1.0 R5: the brand-coherence floor applies to the everyday retrieval classes
    # (durable/operational/canonical), NOT history (forensic queries want weak
    # branded matches back too). Default None -> no behavior change.
    _bcf = _brand_coherence_floor_from_env()
    if qc == "operational":
        return AdmissionPolicy(allowed_tiers=("stable", "evidence", "insight"), max_age_days=180,
                               relevance_floor=_relevance_floor_from_env(), brand_coherence_floor=_bcf)
    if qc == "canonical":
        return AdmissionPolicy(allowed_tiers=("stable", "canonical"), max_age_days=None,
                               brand_coherence_floor=_bcf)
    if qc == "history":
        return AdmissionPolicy(allowed_tiers=("stable", "evidence", "insight", "canonical"),
                               max_age_days=None, forensic=True)
    return AdmissionPolicy(allowed_tiers=("stable", "evidence", "insight"), max_age_days=None,
                           brand_coherence_floor=_bcf)


def log_rejected(memory_id: str, reason: str, layer: str, target_path: Optional[Path] = None) -> None:
    """Append a rejection event to ~/.mem0/admission-rejected.jsonl.

    v0.19 M6/M11: the audit log is advisory — a write failure must NEVER break
    retrieval (matches the app.py retrieval logger and the PS client's "never
    let audit failure break the hook" intent). Any OSError is downgraded to a
    python-logging WARN and the caller proceeds.
    v0.19 L5: rotated at 10MB with the same .1-.5 scheme as retrieval-log.jsonl
    (app.py) — the admission log previously grew unbounded."""
    if target_path is None:
        target_path = Path.home() / ".mem0" / "admission-rejected.jsonl"
    entry = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "memory_id": memory_id,
        "reason": reason,
        "layer": layer,
        "schema_version": "v18",
    }
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        # Rotate at 10MB — move to .1 through .5, drop .5 if it exists
        # (mirrors the retrieval-log rotation in app.py).
        # v0.20 Phase E (L11): unlink the DST (.5) before renaming .4 into it —
        # the old code unlinked SRC (.4) at i==5, so .4 vanished each cycle and
        # .5 never existed. Windows-rename-safe by induction: dst is always
        # vacated before each rename.
        if target_path.exists() and target_path.stat().st_size > 10 * 1024 * 1024:
            for i in range(5, 0, -1):
                src = target_path.with_suffix(f".jsonl.{i - 1}") if i > 1 else target_path
                dst = target_path.with_suffix(f".jsonl.{i}")
                if i == 5:
                    dst.unlink(missing_ok=True)
                if src.exists():
                    src.rename(dst)
        with target_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except (OSError, NotImplementedError):
        log.warning("admission rejection logging failed (non-fatal)", exc_info=True)
        return
    # chmod 600 (closes v0.17 SEC MED on missing log permissions)
    try:
        os.chmod(target_path, 0o600)
    except (OSError, NotImplementedError):
        pass


def apply_admission(results: Iterable[dict], scope: dict, query_class: str, layer: str = "server-search",
                    stats_out: Optional[dict] = None) -> list[dict]:
    """Apply default policy to a result list. Admitted results returned;
    rejected ones logged. Tier and recency checks apply regardless of scope.
    Brand (v0.19 M4, fail-closed): with scope['brand'] set, mismatched-brand
    records are rejected; with scope['brand'] absent, ONLY null-brand records
    are admitted unless scope['allow_cross_brand'] is True or a recognized
    true-string ('1'/'true'/'yes', case-insensitive — v0.20 L3 strict parse).

    v0.19 L4/L8: query_class is normalized ONCE here (strip+lower) so the
    policy lookup and evaluate's recency branch can never disagree —
    'Operational' previously selected the 180d policy but silently skipped
    the recency rejection (raw-string compare in evaluate).
    v0.19 M11: log_rejected is guarded — an unwritable audit file logs a WARN
    and the search continues; it must never turn a rejecting search into a 500.
    MEM-8 (2026-07-03): every rejection bumps the in-memory daily counters
    (_count_rejection, surfaced at /health/deep). `stats_out`, when passed,
    receives per-CALL counts — today just rejected_brand_scoped (the
    brandless fail-closed hides that starve a shim search invisibly; app.py
    echoes it on the search response so the MCP shim can hint 'pass brand=')."""
    qc = (query_class or "durable").strip().lower() or "durable"
    policy = default_policy_for_class(qc)
    admitted = []
    for r in results:
        d = policy.evaluate(r, scope, qc)
        if d.admit:
            admitted.append(r)
        else:
            _count_rejection(d.reason)
            if stats_out is not None and d.reason.startswith("brand_scope_required"):
                stats_out["rejected_brand_scoped"] = stats_out.get("rejected_brand_scoped", 0) + 1
            try:
                log_rejected(memory_id=r.get("id") or "unknown", reason=d.reason, layer=layer)
            except Exception:
                log.warning("admission rejection logging failed (non-fatal)", exc_info=True)
    return admitted
