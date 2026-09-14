"""v1.23 P2-3: the session banner probes the per-host authority, never a loopback literal —
on a replica loopback is the dormant local store and read "still starting" forever."""
from __future__ import annotations
import os, re, shutil, subprocess
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SH = REPO_ROOT / "claude-config" / "storage-cap-check.sh"


def test_no_loopback_literal_in_server_calls():
    src = SH.read_text(encoding="utf-8")
    hits = [ln for ln in src.splitlines() if "127.0.0.1:18791" in ln and not ln.lstrip().startswith("#")]
    # the ONLY literal allowed is the resolver's last-resort fallback (MEM0_URL unset, no file)
    assert hits == ['  printf \'%s\\n\' "${MEM0_URL:-http://127.0.0.1:18791}"'], hits
    assert src.count("$AMS_URL/") == 3, "every server call must go through the resolved authority"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_ams_authority_url_precedence(tmp_path):
    (tmp_path / ".mem0").mkdir()
    (tmp_path / ".mem0" / "authority-url").write_text("\n# x\n http://brain.invalid:18791/ \n", encoding="utf-8")
    m = re.search(r"ams_authority_url\(\) \{.*?\n\}\n", SH.read_text(encoding="utf-8"), re.S)
    assert m, "ams_authority_url() must be defined in storage-cap-check.sh"
    env = {"HOME": str(tmp_path), "MEM0_URL": "http://env.invalid:18791", "PATH": os.environ["PATH"]}
    out = subprocess.run(["bash", "-c", m.group(0) + "ams_authority_url"], env=env,
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == "http://brain.invalid:18791"
    (tmp_path / ".mem0" / "authority-url").unlink()
    out = subprocess.run(["bash", "-c", m.group(0) + "ams_authority_url"], env=env,
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == "http://env.invalid:18791"
