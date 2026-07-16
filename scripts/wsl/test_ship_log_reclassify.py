#!/usr/bin/env python3
"""Tests for ship_log_reclassify --apply (soft-retire + per-record episode).

Run with:
  python3 scripts/wsl/test_ship_log_reclassify.py
  # or: pytest scripts/wsl/test_ship_log_reclassify.py

All HTTP calls are mocked; no live mem0 or Qdrant is touched.
"""
from __future__ import annotations

import json
import sys
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Allow running from any directory
sys.path.insert(0, os.path.dirname(__file__))

# ---------------------------------------------------------------------------
# Helpers — build fake report JSON entries
# ---------------------------------------------------------------------------

def _make_candidate(
    record_id: str = "aaaaaaaa-0000-0000-0000-000000000001",
    text: str = "x" * 900,
    brand: str = "ecosystem",
    conservative: bool = True,
) -> dict:
    return {
        "id": record_id,
        "len": len(text),
        "conservative": conservative,
        "brand": brand,
        "text": text,
        "source": "reextract-v013",
    }


# ---------------------------------------------------------------------------
# Shared mock-response factories
# ---------------------------------------------------------------------------

def _ok_response(status_code: int = 200, json_body: dict | None = None) -> MagicMock:
    """httpx Response-like mock with status_code and .raise_for_status() no-op."""
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body or {}
    r.text = json.dumps(json_body or {})
    r.raise_for_status = MagicMock()
    return r


def _error_response(status_code: int = 500, body: str = "internal error") -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.text = body
    r.raise_for_status = MagicMock()
    return r


def _qdrant_points_response(payload: dict) -> MagicMock:
    """Qdrant POST /collections/memories/points response with one point."""
    return _ok_response(json_body={"result": [{"id": "fake", "payload": payload}]})


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestApplyLive(unittest.TestCase):
    """--apply --live over 1 fake candidate -> 1 episode POST + 1 soft-retire call."""

    def _run_apply_with_one_candidate(self, candidate: dict, *, qdrant_payload: dict | None = None):
        """
        Patch httpx.Client used inside run_apply and run it with one fake candidate
        written to a temp report JSON.

        Returns (result_code, mock_client_instance).
        """
        import tempfile
        import ship_log_reclassify as m

        # Write a temp report JSON
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump([candidate], f)
            tmp_path = Path(f.name)

        # Build a mock httpx.Client context manager
        mock_client = MagicMock()

        def side_effect_post(url, **kwargs):
            if "/points" in url and "ids" in (kwargs.get("json") or {}):
                # Idempotency check: return no points (not yet processed)
                return _ok_response(json_body={"result": []})
            if "/episodes" in url:
                return _ok_response(status_code=201, json_body={"id": 1})
            return _error_response(500, "unexpected POST")

        def side_effect_patch(url, **kwargs):
            if "/metadata" in url:
                return _ok_response(200)
            return _error_response(500, "unexpected PATCH")

        mock_client.post.side_effect = side_effect_post
        mock_client.patch.side_effect = side_effect_patch

        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_client)
        mock_cm.__exit__ = MagicMock(return_value=False)

        with (
            patch.object(m, "_REPORT_JSON_PATH", tmp_path),
            patch.object(m, "_api_key", return_value="test-key"),
            patch("httpx.get", return_value=_ok_response(200)),  # Qdrant /readyz
            patch("httpx.Client", return_value=mock_cm),
        ):
            rc = m.run_apply(dry_run=False)

        tmp_path.unlink(missing_ok=True)
        return rc, mock_client

    def test_apply_live_one_candidate_happy_path(self):
        """--apply --live: 1 candidate -> 1 episode POST + 1 metadata PATCH.

        FIX: PATCH must use actor='backfill-apply-v013' and metadata body
        must be exactly {'retrievable': False} — no extra keys (retired_at,
        reclassified_to_episode) because the trusted actor only allows 'retrievable'.
        """
        candidate = _make_candidate()
        rc, mock_client = self._run_apply_with_one_candidate(candidate)

        self.assertEqual(rc, 0, "run_apply should return 0 on full success")

        # Episode POST called once (to /v1/episodes)
        episode_calls = [
            c for c in mock_client.post.call_args_list
            if "/episodes" in str(c)
        ]
        self.assertEqual(len(episode_calls), 1, "exactly one episode POST expected")

        # Soft-retire PATCH called once (to /v1/memories/<id>/metadata)
        patch_calls = [
            c for c in mock_client.patch.call_args_list
            if "/metadata" in str(c)
        ]
        self.assertEqual(len(patch_calls), 1, "exactly one metadata PATCH expected")

        # Verify actor and metadata body (FIX: actor must be backfill-apply-v013,
        # metadata must contain ONLY retrievable=False — no extra keys).
        patch_body = mock_client.patch.call_args_list[0][1].get("json") or {}
        self.assertEqual(
            patch_body.get("actor"), "backfill-apply-v013",
            "actor must be backfill-apply-v013 (only actor permitted to write retrievable)",
        )
        meta = patch_body.get("metadata", {})
        self.assertFalse(meta.get("retrievable"), "retrievable must be False (soft-retire)")
        # Extra keys (retired_at, reclassified_to_episode) must NOT be present —
        # that actor is permitted only the single 'retrievable' key.
        self.assertNotIn("retired_at", meta, "retired_at must NOT be in PATCH body (actor would 403)")
        self.assertNotIn(
            "reclassified_to_episode", meta,
            "reclassified_to_episode must NOT be in PATCH body (actor would 403)",
        )
        # Exactly one key in metadata
        self.assertEqual(
            set(meta.keys()), {"retrievable"},
            "metadata body must contain exactly {'retrievable'}",
        )


class TestApplyLiveEpisodeFailure(unittest.TestCase):
    """Episode POST failure -> no retire call."""

    def test_episode_failure_does_not_retire(self):
        import tempfile
        import ship_log_reclassify as m

        candidate = _make_candidate()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump([candidate], f)
            tmp_path = Path(f.name)

        mock_client = MagicMock()

        def side_effect_post(url, **kwargs):
            if "/points" in url and "ids" in (kwargs.get("json") or {}):
                return _ok_response(json_body={"result": []})  # not yet processed
            if "/episodes" in url:
                return _error_response(500, "episode service down")
            return _error_response(500, "unexpected")

        mock_client.post.side_effect = side_effect_post

        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_client)
        mock_cm.__exit__ = MagicMock(return_value=False)

        with (
            patch.object(m, "_REPORT_JSON_PATH", tmp_path),
            patch.object(m, "_api_key", return_value="test-key"),
            patch("httpx.get", return_value=_ok_response(200)),
            patch("httpx.Client", return_value=mock_cm),
        ):
            rc = m.run_apply(dry_run=False)

        tmp_path.unlink(missing_ok=True)

        # Should exit non-zero (errors > 0)
        self.assertNotEqual(rc, 0, "should return non-zero when episode POST fails")
        # PATCH must NOT have been called
        patch_calls = [
            c for c in mock_client.patch.call_args_list
            if "/metadata" in str(c)
        ]
        self.assertEqual(len(patch_calls), 0, "must NOT retire when episode POST failed")


class TestApplyLiveIdempotency(unittest.TestCase):
    """Re-run on an already-processed record -> skip (no episode POST, no retire)."""

    def test_already_processed_is_skipped(self):
        import tempfile
        import ship_log_reclassify as m

        candidate = _make_candidate()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump([candidate], f)
            tmp_path = Path(f.name)

        mock_client = MagicMock()

        def side_effect_post(url, **kwargs):
            if "/points" in url and "ids" in (kwargs.get("json") or {}):
                # Simulate already-retired record (retrievable=False)
                return _ok_response(json_body={
                    "result": [{"id": candidate["id"], "payload": {"retrievable": False, "retired_at": "2026-01-01T00:00:00+00:00"}}]
                })
            return _error_response(500, "unexpected POST — should not reach episodes")

        mock_client.post.side_effect = side_effect_post

        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_client)
        mock_cm.__exit__ = MagicMock(return_value=False)

        with (
            patch.object(m, "_REPORT_JSON_PATH", tmp_path),
            patch.object(m, "_api_key", return_value="test-key"),
            patch("httpx.get", return_value=_ok_response(200)),
            patch("httpx.Client", return_value=mock_cm),
        ):
            rc = m.run_apply(dry_run=False)

        tmp_path.unlink(missing_ok=True)

        self.assertEqual(rc, 0, "re-run on already-processed record should return 0")
        # No episode POST
        episode_calls = [c for c in mock_client.post.call_args_list if "/episodes" in str(c)]
        self.assertEqual(len(episode_calls), 0, "no episode POST on already-processed record")
        # No retire PATCH
        self.assertEqual(mock_client.patch.call_count, 0, "no PATCH on already-processed record")

    def test_already_processed_toplevel_retrievable_false_no_retired_at(self):
        """Idempotency: bare top-level retrievable=False (no retired_at, no metadata wrapper).

        The live PATCH (actor=backfill-apply-v013) writes only {retrievable: False} to
        the Qdrant payload top level via set_payload shallow-merge — it does NOT add
        retired_at or any nested metadata key.  _is_already_processed must recognise
        this exact payload shape as already-processed and skip, so a --apply re-run
        cannot create duplicate episodes for already-soft-retired records.
        """
        import tempfile
        import ship_log_reclassify as m

        candidate = _make_candidate()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump([candidate], f)
            tmp_path = Path(f.name)

        mock_client = MagicMock()

        def side_effect_post(url, **kwargs):
            if "/points" in url and "ids" in (kwargs.get("json") or {}):
                # Live shape: top-level retrievable=False, no retired_at, no metadata key
                return _ok_response(json_body={
                    "result": [{"id": candidate["id"], "payload": {"retrievable": False}}]
                })
            return _error_response(500, "unexpected POST — should not reach episodes")

        mock_client.post.side_effect = side_effect_post

        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_client)
        mock_cm.__exit__ = MagicMock(return_value=False)

        with (
            patch.object(m, "_REPORT_JSON_PATH", tmp_path),
            patch.object(m, "_api_key", return_value="test-key"),
            patch("httpx.get", return_value=_ok_response(200)),
            patch("httpx.Client", return_value=mock_cm),
        ):
            rc = m.run_apply(dry_run=False)

        tmp_path.unlink(missing_ok=True)

        self.assertEqual(rc, 0, "re-run on bare-retrievable=False record should return 0")
        # No episode POST — record is already retired
        episode_calls = [c for c in mock_client.post.call_args_list if "/episodes" in str(c)]
        self.assertEqual(len(episode_calls), 0, "no episode POST when payload has bare retrievable=False")
        # No retire PATCH
        self.assertEqual(mock_client.patch.call_count, 0, "no PATCH when payload has bare retrievable=False")


class TestApplyDryRunGuard(unittest.TestCase):
    """Bare --apply (no --dry-run, no --live) should exit 2."""

    def test_bare_apply_exits_2(self):
        import ship_log_reclassify as m
        with patch("sys.argv", ["ship_log_reclassify.py", "--apply"]):
            rc = m.main()
        self.assertEqual(rc, 2, "bare --apply must exit 2 (mutation guard)")


class TestApplyDryRunZeroWrites(unittest.TestCase):
    """--apply --dry-run makes NO writes."""

    def test_dry_run_zero_writes(self):
        import tempfile
        import ship_log_reclassify as m

        candidates = [_make_candidate(record_id=f"aaaaaaaa-0000-0000-0000-{i:012d}") for i in range(3)]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(candidates, f)
            tmp_path = Path(f.name)

        mock_client = MagicMock()

        def side_effect_post(url, **kwargs):
            # Idempotency check — all not yet processed
            if "/points" in url and "ids" in (kwargs.get("json") or {}):
                return _ok_response(json_body={"result": []})
            return _error_response(500, "must not call episodes in dry-run")

        mock_client.post.side_effect = side_effect_post

        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_client)
        mock_cm.__exit__ = MagicMock(return_value=False)

        with (
            patch.object(m, "_REPORT_JSON_PATH", tmp_path),
            patch("httpx.Client", return_value=mock_cm),
        ):
            rc = m.run_apply(dry_run=True)

        tmp_path.unlink(missing_ok=True)

        self.assertEqual(rc, 0)
        # No episode POSTs, no metadata PATCHes
        episode_calls = [c for c in mock_client.post.call_args_list if "/episodes" in str(c)]
        self.assertEqual(len(episode_calls), 0, "dry-run must NOT call episode POST")
        self.assertEqual(mock_client.patch.call_count, 0, "dry-run must NOT call metadata PATCH")


# ---------------------------------------------------------------------------
# --retire-only tests
# ---------------------------------------------------------------------------

class TestRetireOnly(unittest.TestCase):
    """--retire-only: retire NOT-yet-retired conservative candidates; skip retired ones."""

    def _run_retire_only_with_candidates(
        self,
        candidates: list[dict],
        *,
        qdrant_payloads: dict | None = None,
        dry_run: bool = False,
    ):
        """
        Run run_retire_only with a fake report JSON and mocked HTTP.

        qdrant_payloads: dict mapping record_id -> payload dict to return from
        Qdrant /points lookup.  Records not in the dict return no points (not retired).

        Returns (rc, mock_client).
        """
        import tempfile
        import ship_log_reclassify as m

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(candidates, f)
            tmp_path = Path(f.name)

        qdrant_payloads = qdrant_payloads or {}
        mock_client = MagicMock()

        def side_effect_post(url, **kwargs):
            # Qdrant /points idempotency/state check
            if "/points" in url and "ids" in (kwargs.get("json") or {}):
                ids = (kwargs.get("json") or {}).get("ids", [])
                rid = ids[0] if ids else None
                payload = qdrant_payloads.get(rid)
                if payload is not None:
                    return _ok_response(json_body={"result": [{"id": rid, "payload": payload}]})
                return _ok_response(json_body={"result": []})
            return _error_response(500, "unexpected POST — retire-only must NOT call episodes")

        def side_effect_patch(url, **kwargs):
            if "/metadata" in url:
                return _ok_response(200)
            return _error_response(500, "unexpected PATCH")

        mock_client.post.side_effect = side_effect_post
        mock_client.patch.side_effect = side_effect_patch

        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_client)
        mock_cm.__exit__ = MagicMock(return_value=False)

        with (
            patch.object(m, "_REPORT_JSON_PATH", tmp_path),
            patch.object(m, "_api_key", return_value="test-key"),
            patch("httpx.Client", return_value=mock_cm),
        ):
            rc = m.run_retire_only(dry_run=dry_run)

        tmp_path.unlink(missing_ok=True)
        return rc, mock_client

    def test_retire_only_not_retired_candidate_gets_one_patch(self):
        """--retire-only: a not-retired conservative candidate -> exactly 1 PATCH, 0 episode POSTs.

        The PATCH must use actor='backfill-apply-v013' and body metadata == {'retrievable': False}.
        """
        candidate = _make_candidate()
        rc, mock_client = self._run_retire_only_with_candidates([candidate])

        self.assertEqual(rc, 0, "retire-only should return 0 on success")

        # Zero episode POSTs (retire-only must not create episodes)
        episode_calls = [c for c in mock_client.post.call_args_list if "/episodes" in str(c)]
        self.assertEqual(len(episode_calls), 0, "--retire-only must NOT call episode POST")

        # Exactly one PATCH (the soft-retire)
        patch_calls = [c for c in mock_client.patch.call_args_list if "/metadata" in str(c)]
        self.assertEqual(len(patch_calls), 1, "exactly one metadata PATCH expected")

        # Verify actor and metadata body
        patch_body = mock_client.patch.call_args_list[0][1].get("json") or {}
        self.assertEqual(
            patch_body.get("actor"), "backfill-apply-v013",
            "actor must be backfill-apply-v013",
        )
        meta = patch_body.get("metadata", {})
        self.assertEqual(
            set(meta.keys()), {"retrievable"},
            "metadata must contain exactly {'retrievable'}",
        )
        self.assertFalse(meta["retrievable"], "retrievable must be False")

    def test_retire_only_already_retired_is_skipped(self):
        """--retire-only: an already-retired candidate -> 0 PATCH calls, 0 episode POSTs."""
        candidate = _make_candidate()
        # Simulate Qdrant returning retrievable=False (already retired)
        qdrant_payloads = {candidate["id"]: {"retrievable": False, "retired_at": "2026-01-01T00:00:00+00:00"}}
        rc, mock_client = self._run_retire_only_with_candidates(
            [candidate], qdrant_payloads=qdrant_payloads
        )

        self.assertEqual(rc, 0, "retire-only should return 0 when all already retired")

        # No episode POSTs
        episode_calls = [c for c in mock_client.post.call_args_list if "/episodes" in str(c)]
        self.assertEqual(len(episode_calls), 0, "no episode POST on already-retired record")

        # No PATCH (already retired -> skip)
        patch_calls = [c for c in mock_client.patch.call_args_list if "/metadata" in str(c)]
        self.assertEqual(len(patch_calls), 0, "no PATCH when record already retired")

    def test_retire_only_dry_run_zero_writes(self):
        """--retire-only --dry-run: zero PATCH calls, zero episode POSTs."""
        candidates = [
            _make_candidate(record_id=f"aaaaaaaa-0000-0000-0000-{i:012d}") for i in range(3)
        ]
        rc, mock_client = self._run_retire_only_with_candidates(candidates, dry_run=True)

        self.assertEqual(rc, 0, "dry-run should return 0")
        episode_calls = [c for c in mock_client.post.call_args_list if "/episodes" in str(c)]
        self.assertEqual(len(episode_calls), 0, "dry-run must NOT call episode POST")
        self.assertEqual(mock_client.patch.call_count, 0, "dry-run must NOT call metadata PATCH")

    def test_retire_only_bare_exits_2(self):
        """Bare --retire-only (no --dry-run, no --live) should exit 2."""
        import ship_log_reclassify as m
        with patch("sys.argv", ["ship_log_reclassify.py", "--retire-only"]):
            rc = m.main()
        self.assertEqual(rc, 2, "bare --retire-only must exit 2 (mutation guard)")


# ---------------------------------------------------------------------------
# Standalone runner (no pytest required)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestApplyLive,
        TestApplyLiveEpisodeFailure,
        TestApplyLiveIdempotency,
        TestApplyDryRunGuard,
        TestApplyDryRunZeroWrites,
        TestRetireOnly,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
