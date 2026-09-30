"""brand-scope-audit.py (all tiers: untagged brand mentions, unroutable brands) and
brand-backfill.py (a dry-run report you review, then --apply of exactly the reviewed rows).
A fake store stands in for Qdrant and the mem0 API; HOME lives under tmp_path, so nothing
touches the operator's ~/.mem0 and no live authority is ever called."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "wsl"
sys.path.insert(0, str(SCRIPTS))
import brand_routing  # noqa: E402
from _home_isolation import apply_home  # noqa: E402


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


BRAND_MAP = {
    "rules": [{"pattern": "projects/clienta", "brand": "brand-a"}, {"pattern": "shared-ws", "brand": "shared-a"}],
    "shared_brands": ["shared-a"],
    "content_rules": [{"pattern": "alpha-store", "brand": "brand-a"}, {"pattern": "beta-shop", "brand": "brand-b"}],
}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    apply_home(monkeypatch, tmp_path)
    (tmp_path / ".mem0").mkdir()
    for v in ("MEM0_URL", "MEM0_KEY", "MEM0_API_KEY_FILE", "MEM0_BRAND_MAP", "MEM0_SHARED_BRANDS"):
        monkeypatch.delenv(v, raising=False)
    brand_routing._WARNED.clear()
    return tmp_path


def _pt(pid, text, brand=None, tier="evidence", **extra):
    pl = {"data": text, "tier": tier, "updated_at": "2026-09-01T00:00:00+00:00"}
    if brand:
        pl["brand"] = brand
    pl.update(extra)
    return {"id": pid, "payload": pl}


CORPUS = [
    _pt("p1", "The alpha-store catalog lists twelve products"),                       # untagged, one brand
    _pt("p2", "beta-shop checkout uses a flat rate"),                                   # untagged, other brand
    _pt("p3", "alpha-store and beta-shop share one supplier"),                          # untagged, ambiguous
    _pt("p4", "The supplier invoices monthly"),                                         # untagged, no mention
    _pt("p5", "alpha-store prices are in a spreadsheet", brand="brand-a"),              # already tagged
    _pt("p6", "an alpha-store insight", tier="insight"),                                # untagged insight
    _pt("p7", "canonical alpha-store rule", tier="canonical"),                          # untagged canonical
    _pt("p8", "old alpha-store fact", retired_at="2026-08-01T00:00:00+00:00"),          # retired: ignored
    _pt("p9", "tagged with an unmapped brand", brand="brand-z"),                        # unroutable brand
    _pt("p10", "tagged shared", brand="shared-a"),                                      # shared label: routable
]


class FakeStore:
    def __init__(self, pts):
        self.pts = {p["id"]: json.loads(json.dumps(p)) for p in pts}
        self.patched = []
        self.fail_ids = set()

    def points(self):
        return [json.loads(json.dumps(p)) for p in self.pts.values()]

    def get(self, pid):
        p = self.pts.get(pid)
        return json.loads(json.dumps(p)) if p else None

    def patch(self, pid, metadata, actor, reason):
        if pid in self.fail_ids:
            raise RuntimeError("HTTP 500")
        self.patched.append((pid, metadata, actor, reason))
        self.pts[pid]["payload"].update(metadata)
        return True


# ---- brand_routing.content_brands / resolve_by_content -------------------------------------------------

def test_content_brands_and_resolve_by_content():
    assert brand_routing.content_brands(BRAND_MAP, "the alpha-store catalog") == {"brand-a"}
    assert brand_routing.content_brands(BRAND_MAP, "alpha-store beta-shop") == {"brand-a", "brand-b"}
    assert brand_routing.content_brands(BRAND_MAP, "nothing") == set()
    assert brand_routing.content_brands({}, "alpha-store") == set()
    assert brand_routing.resolve_by_content(BRAND_MAP, "ALPHA-STORE stock") == "brand-a"
    assert brand_routing.resolve_by_content(BRAND_MAP, "alpha-store beta-shop") is None
    assert brand_routing.resolve_by_content(BRAND_MAP, "nothing") is None


# ---- brand-scope-audit.py ---------------------------------------------------------------------------------

def test_audit_counts_untagged_brand_mentions_across_all_tiers():
    audit = _load("brand_scope_audit", "brand-scope-audit.py")
    r = audit.find_untagged_mentions(CORPUS, BRAND_MAP)
    # p1 p2 p3 p6 p7 mention a brand and carry none; p5 is tagged, p4 mentions nothing, p8 is retired
    assert r["n"] == 5
    assert r["by_brand"] == {"brand-a": 3, "brand-b": 1}
    assert r["ambiguous"] == 1
    assert set(r["sample_ids"]) == {"p1", "p2", "p3", "p6", "p7"}
    assert r["n_live"] == 9, "10 points, one retired"


def test_audit_untagged_metric_is_zero_without_content_rules():
    audit = _load("brand_scope_audit", "brand-scope-audit.py")
    r = audit.find_untagged_mentions(CORPUS, {"rules": BRAND_MAP["rules"]})
    assert r["n"] == 0 and r["by_brand"] == {}


def test_audit_reports_brands_on_records_that_have_no_route():
    audit = _load("brand_scope_audit", "brand-scope-audit.py")
    assert audit.find_unroutable_brands(CORPUS, BRAND_MAP) == {"brand-z": 1}, \
        "brand-a routes via a rule, shared-a is a shared label, brand-z has neither"
    # with no map at all every brand on a record is unroutable (nothing can produce it)
    assert audit.find_unroutable_brands(CORPUS, {}) == {"brand-a": 1, "brand-z": 1, "shared-a": 1}


def test_audit_main_keeps_its_exit_code_and_adds_metrics_to_the_status_file(home, monkeypatch, capsys):
    audit = _load("brand_scope_audit", "brand-scope-audit.py")
    bm = home / "brands.json"
    bm.write_text(json.dumps(BRAND_MAP), encoding="utf-8")
    monkeypatch.setenv("MEM0_BRAND_MAP", str(bm))
    monkeypatch.setattr(audit, "scroll_points", lambda: CORPUS)
    # p7 is a canonical record with no brand and no project: project-less canonical is neutral (legacy rule)
    rc = audit.main()
    out = capsys.readouterr().out
    assert rc == 0, "untagged mentions and unroutable brands are reported, never an exit-2 failure"
    assert "5 untagged brand mention" in out and "brand-z" in out
    st = json.loads((home / ".mem0" / "brand-scope-status.json").read_text())
    assert st["n_canonical"] == 1 and st["n_misscoped"] == 0, "the canonical-tier fields keep their meaning"
    assert st["untagged_brand_mentions"] == 5 and st["untagged_by_brand"] == {"brand-a": 3, "brand-b": 1}
    assert st["unroutable_brands"] == {"brand-z": 1} and st["n_points"] == 9


def test_audit_main_still_exits_2_for_a_misscoped_canonical(home, monkeypatch):
    audit = _load("brand_scope_audit", "brand-scope-audit.py")
    monkeypatch.setattr(audit, "scroll_points", lambda: [_pt("c1", "a client rule", tier="canonical", project="clienta")])
    assert audit.main() == 2


# ---- brand-backfill.py ---------------------------------------------------------------------------------------

def _dry(bf, store, out, bm=BRAND_MAP):
    return bf.run_dry(store, bm, str(out))


def _rows(path):
    return [json.loads(ln) for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_dry_run_writes_one_row_per_fact_and_changes_nothing(tmp_path):
    bf = _load("brand_backfill", "brand-backfill.py")
    store = FakeStore(CORPUS)
    out = tmp_path / "report.jsonl"
    assert _dry(bf, store, out) == 0
    rows = _rows(out)
    assert sorted(r["id"] for r in rows) == ["p1", "p2", "p6", "p7"], \
        "only untagged, live records that route to exactly one brand get a row"
    by = {r["id"]: r for r in rows}
    assert by["p1"]["current"] is None and by["p1"]["proposed"] == "brand-a" and by["p1"]["rule"] == "content"
    assert by["p2"]["proposed"] == "brand-b"
    assert by["p1"]["text_head"].startswith("The alpha-store catalog")
    assert by["p7"]["tier"] == "canonical" and by["p6"]["tier"] == "insight"
    assert all(r["fp"] for r in rows)
    assert store.patched == [], "a dry run never writes"


def test_dry_run_prefers_a_path_rule_over_content(tmp_path):
    bf = _load("brand_backfill", "brand-backfill.py")
    store = FakeStore([_pt("w1", "beta-shop checkout", workspace="g--My-Drive-Projects-ClientA")])
    out = tmp_path / "report.jsonl"
    assert _dry(bf, store, out) == 0
    (row,) = _rows(out)
    assert row["proposed"] == "brand-a" and row["rule"] == "path"


CONTRACT_MAP = {
    "rules": [{"pattern": "projects/clienta", "brand": "brand-a"}, {"pattern": "shopclick", "brand": "brand-a"}],
    "content_rule_workspaces": ["projects/mixed"],
    "content_rules": [{"pattern": "alpha-store", "brand": "brand-a"}, {"pattern": "beta-shop", "brand": "brand-b"}],
}


def test_dry_run_gives_no_content_brand_to_a_path_outside_the_content_workspaces(tmp_path):
    """C3 step 3: a record whose path routes nowhere, outside the content-rule workspaces, stays
    brand-neutral. Content rules run only in a content-rule workspace or for a record with no path
    (s12: the first live run content-tagged records from unrelated workspaces)."""
    bf = _load("brand_backfill", "brand-backfill.py")
    store = FakeStore([
        _pt("o1", "beta-shop checkout uses a flat rate", workspace="g--My-Drive-Projects-Other"),
        _pt("m1", "beta-shop checkout uses a flat rate", workspace="g--My-Drive-Projects-Mixed"),
        _pt("n1", "beta-shop checkout uses a flat rate"),
    ])
    out = tmp_path / "report.jsonl"
    assert _dry(bf, store, out, CONTRACT_MAP) == 0
    by = {r["id"]: r for r in _rows(out)}
    assert "o1" not in by, "a non-routing path outside the content workspaces gets no content brand"
    assert by["m1"]["proposed"] == "brand-b" and by["m1"]["rule"] == "content"
    assert by["n1"]["proposed"] == "brand-b" and by["n1"]["rule"] == "content"


def test_dry_run_tries_the_project_when_the_workspace_does_not_route(tmp_path):
    """The workspace used to shadow the project: a record from a non-routing workspace whose
    project matches a rule fell through to the content rules and could get the wrong brand."""
    bf = _load("brand_backfill", "brand-backfill.py")
    store = FakeStore([_pt("q1", "beta-shop checkout", workspace="umbrella", project="shopclick-platform")])
    out = tmp_path / "report.jsonl"
    assert _dry(bf, store, out, CONTRACT_MAP) == 0
    (row,) = _rows(out)
    assert row["proposed"] == "brand-a" and row["rule"] == "path"


def test_text_head_is_one_short_line(tmp_path):
    bf = _load("brand_backfill", "brand-backfill.py")
    store = FakeStore([_pt("l1", "alpha-store\n" + "x" * 400)])
    out = tmp_path / "report.jsonl"
    _dry(bf, store, out)
    (row,) = _rows(out)
    assert "\n" not in row["text_head"] and len(row["text_head"]) <= 80


def test_apply_patches_exactly_the_reviewed_rows(tmp_path):
    bf = _load("brand_backfill", "brand-backfill.py")
    store = FakeStore(CORPUS)
    out = tmp_path / "report.jsonl"
    _dry(bf, store, out)
    rows = _rows(out)
    reviewed = [r for r in rows if r["id"] != "p2"]            # the reviewer deleted p2
    for r in reviewed:
        if r["id"] == "p6":
            r["proposed"] = None                                # and blanked p6
    for r in reviewed:
        if r["id"] == "p1":
            r["proposed"] = "brand-b"                           # and changed p1's brand
    out.write_text("\n".join(json.dumps(r) for r in reviewed) + "\n", encoding="utf-8")
    assert bf.run_apply(store, BRAND_MAP, str(out)) == 0
    assert [(p[0], p[1]) for p in store.patched] == [("p1", {"brand": "brand-b"})]
    assert store.patched[0][2] == "brand-backfill" and store.patched[0][3]
    assert store.pts["p2"]["payload"].get("brand") is None, "a row the reviewer removed is never applied"


def test_apply_refuses_a_row_whose_record_changed_since_the_report(tmp_path, capsys):
    bf = _load("brand_backfill", "brand-backfill.py")
    store = FakeStore([_pt("p1", "alpha-store stock"), _pt("p2", "beta-shop stock")])
    out = tmp_path / "report.jsonl"
    _dry(bf, store, out)
    store.pts["p1"]["payload"]["data"] = "alpha-store stock, edited after the report"
    store.pts["p2"]["payload"]["updated_at"] = "2026-09-20T00:00:00+00:00"
    assert bf.run_apply(store, BRAND_MAP, str(out)) == 0
    assert store.patched == []
    text = capsys.readouterr().out
    assert text.count("changed since the report") == 2


def test_apply_refuses_a_record_someone_tagged_meanwhile(tmp_path):
    bf = _load("brand_backfill", "brand-backfill.py")
    store = FakeStore([_pt("p1", "alpha-store stock")])
    out = tmp_path / "report.jsonl"
    _dry(bf, store, out)
    store.pts["p1"]["payload"]["brand"] = "brand-b"
    bf.run_apply(store, BRAND_MAP, str(out))
    assert store.patched == []


def test_apply_never_patches_canonical_or_insight_and_prints_the_hmac_command(tmp_path, capsys):
    bf = _load("brand_backfill", "brand-backfill.py")
    store = FakeStore([_pt("k1", "alpha-store rule", tier="canonical"), _pt("i1", "alpha-store insight", tier="insight"),
                       _pt("e1", "alpha-store fact")])
    out = tmp_path / "report.jsonl"
    _dry(bf, store, out)
    assert bf.run_apply(store, BRAND_MAP, str(out)) == 0
    assert [p[0] for p in store.patched] == ["e1"]
    text = capsys.readouterr().out
    assert "mem0-canonize.sh --action patch_metadata k1" in text and "i1" in text
    assert "--metadata-json '{\"brand\": \"brand-a\"}'" in text


def test_apply_on_a_native_authority_prints_the_signer_that_loads_the_key(tmp_path, home, capsys):
    """A native authority holds the canonical key only inside a unit that loads its credential;
    mem0-canonize.sh run from a shell there finds no key, ams-canonize.sh starts that unit."""
    (home / ".mem0" / "stack.env").write_text("MEM0_HOST_KIND=native\n", encoding="utf-8")
    bf = _load("brand_backfill", "brand-backfill.py")
    store = FakeStore([_pt("k1", "alpha-store rule", tier="canonical")])
    out = tmp_path / "report.jsonl"
    _dry(bf, store, out)
    assert bf.run_apply(store, BRAND_MAP, str(out)) == 0
    text = capsys.readouterr().out
    assert "bash ~/apps/mem0-scripts/ams-canonize.sh --action patch_metadata k1" in text
    assert "mem0-canonize.sh" not in text


def test_apply_refuses_a_brand_the_map_cannot_route(tmp_path, capsys):
    bf = _load("brand_backfill", "brand-backfill.py")
    store = FakeStore([_pt("p1", "alpha-store stock")])
    out = tmp_path / "report.jsonl"
    _dry(bf, store, out)
    rows = _rows(out)
    rows[0]["proposed"] = "brand-typo"
    out.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    bf.run_apply(store, BRAND_MAP, str(out))
    assert store.patched == []
    assert "brand-typo" in capsys.readouterr().out


def test_apply_reports_a_failed_patch_and_keeps_going(tmp_path):
    bf = _load("brand_backfill", "brand-backfill.py")
    store = FakeStore([_pt("p1", "alpha-store one"), _pt("p2", "alpha-store two")])
    store.fail_ids = {"p1"}
    out = tmp_path / "report.jsonl"
    _dry(bf, store, out)
    assert bf.run_apply(store, BRAND_MAP, str(out)) == 1, "a failed PATCH makes the run exit non-zero"
    assert [p[0] for p in store.patched] == ["p2"]


def test_apply_is_idempotent(tmp_path):
    bf = _load("brand_backfill", "brand-backfill.py")
    store = FakeStore([_pt("p1", "alpha-store one")])
    out = tmp_path / "report.jsonl"
    _dry(bf, store, out)
    bf.run_apply(store, BRAND_MAP, str(out))
    bf.run_apply(store, BRAND_MAP, str(out))
    assert len(store.patched) == 1, "the second run finds the record already changed and touches nothing"


def test_cli_requires_out_for_dry_run_and_from_for_apply(tmp_path):
    bf = _load("brand_backfill", "brand-backfill.py")
    with pytest.raises(SystemExit):
        bf.main(["--dry-run"], store=FakeStore([]))
    with pytest.raises(SystemExit):
        bf.main(["--apply"], store=FakeStore([]))
    with pytest.raises(SystemExit):
        bf.main([], store=FakeStore([]))
    with pytest.raises(SystemExit):
        bf.main(["--dry-run", "--apply", "--out", "x", "--from", "y"], store=FakeStore([]))


def test_cli_without_a_brand_map_refuses_rather_than_proposing_nothing(tmp_path, capsys):
    bf = _load("brand_backfill", "brand-backfill.py")
    rc = bf.main(["--dry-run", "--out", str(tmp_path / "r.jsonl")], store=FakeStore(CORPUS))
    assert rc == 3 and "no brand map" in capsys.readouterr().out


def test_cli_dry_run_then_apply_end_to_end(tmp_path, home, monkeypatch):
    bf = _load("brand_backfill", "brand-backfill.py")
    bm = home / "brands.json"
    bm.write_text(json.dumps(BRAND_MAP), encoding="utf-8")
    monkeypatch.setenv("MEM0_BRAND_MAP", str(bm))
    store = FakeStore([_pt("p1", "alpha-store one")])
    out = tmp_path / "r.jsonl"
    assert bf.main(["--dry-run", "--out", str(out)], store=store) == 0
    assert bf.main(["--apply", "--from", str(out)], store=store) == 0
    assert store.pts["p1"]["payload"]["brand"] == "brand-a"
