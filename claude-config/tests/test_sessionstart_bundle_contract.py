"""The SessionStart enrichment call stamps the hook contract version.

/health/deep hook_contract.missing counts calls that carry no `hook_contract_version`. The unstamped
secondary callers (this one, the dream's searches) dominated the counter, so a real hook regression
could not show in it. Stamp '20.0' (the batched /v1/context/bundle wire contract)."""
import importlib.util
import json
from pathlib import Path

_MOD = Path(__file__).resolve().parents[1] / "sessionstart_bundle.py"
_spec = importlib.util.spec_from_file_location("sessionstart_bundle_contract", _MOD)
ssb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ssb)


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return b'{"memories": []}'


def test_bundle_request_body_carries_the_hook_contract_version(monkeypatch):
    seen = []

    def urlopen(req, timeout=None):
        seen.append(json.loads(req.data))
        return _Resp()
    monkeypatch.setattr(ssb.urllib.request, "urlopen", urlopen)
    ssb.fetch_bundle("http://authority.invalid", "k", "boot query", "brand-x", "init-y")
    assert seen[0]["hook_contract_version"] == "20.0"
    assert seen[0]["session_id"] == "sessionstart-enrich" and seen[0]["checkpoint"] is False, "the rest of the payload is unchanged"


def test_stamped_version_is_one_the_server_knows():
    """Extend hook_contract.py's KNOWN set in the same change that bumps a wire contract; a stamp the
    server does not know is counted as 'unknown', which is worse than 'missing'."""
    src = (Path(__file__).resolve().parents[2] / "mem0-server" / "hook_contract.py").read_text(encoding="utf-8")
    assert f'"{ssb.BUNDLE_HOOK_CONTRACT_VERSION}"' in src.split("KNOWN_HOOK_CONTRACT_VERSIONS", 1)[1].splitlines()[0]
