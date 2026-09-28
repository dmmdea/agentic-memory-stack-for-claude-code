#!/usr/bin/env python3
"""Proof that scripts/ci/privacy-gate.py catches what it claims to, using a made-up term and
a throwaway key and repo (no real term, no real key). Run: python scripts/ci/test_privacy_gate.py"""
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("privacy_gate", Path(__file__).with_name("privacy-gate.py"))
pg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pg)

TERM = "Zebrafoxtrot"          # invented for this test; matches nothing real
KEY = "unit-test-key-not-a-secret"


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t.test",
                    "-c", "commit.gpgsign=false"] + list(args), check=True, capture_output=True)


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q")
        (self.root / "ok.txt").write_text("nothing personal here\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "seed")
        self.digests = self.root / "digests.txt"
        self.digests.write_text(pg.digest(KEY.encode(), pg.normalize(TERM)) + "\n", encoding="utf-8")
        pg.DIGESTS_PATH = self.digests
        pg.CONFIG_PATH = self.root / "none.json"
        os.environ["PRIVACY_GATE_KEY"] = KEY
        os.environ.pop("PRIVACY_GATE_REQUIRE_KEY", None)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("PRIVACY_GATE_KEY", None)

    def scan(self, rng=None):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = pg.run(self.root, rng, False)
        return rc, buf.getvalue()

    def commit(self, name, text, msg="change"):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", msg)

    def test_clean_tree_passes(self):
        self.assertEqual(self.scan()[0], 0)

    def test_planted_term_in_content_fails_and_is_not_echoed(self):
        self.commit("a.md", f"the box called {TERM} is idle\n")
        rc, out = self.scan()
        self.assertEqual(rc, 1)
        self.assertIn("a.md:1", out)
        self.assertNotIn(TERM.lower(), out.lower())

    def test_camelcase_and_separator_forms(self):
        self.commit("a.go", "func zebraFoxtrotDevices() {}\nvar x = \"zebra-foxtrot\"\n")
        rc, out = self.scan()
        self.assertEqual(rc, 1)
        self.assertEqual(out.count("denylisted-term"), 2)

    def test_filename(self):
        self.commit("docs/zebrafoxtrot-notes.md", "harmless\n")
        rc, out = self.scan()
        self.assertEqual(rc, 1)
        self.assertIn("path docs/***: flagged path component", out)
        self.assertNotIn("zebrafoxtrot", out.lower())

    def test_commit_message_in_range(self):
        base = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.commit("b.txt", "fine\n", msg=f"move work off {TERM}")
        rc, out = self.scan(f"{base}..HEAD")
        self.assertEqual(rc, 1)
        self.assertIn("message", out)
        self.assertNotIn(TERM.lower(), out.lower())

    def test_without_key_only_shapes_run(self):
        os.environ.pop("PRIVACY_GATE_KEY", None)
        os.environ["PRIVACY_GATE_KEY_FILE"] = str(self.root / "missing-key")
        try:
            self.commit("a.md", f"{TERM}\n")
            self.assertEqual(self.scan()[0], 0)
            # built at runtime so this file passes its own gate
            self.commit("c.md", "see C:\\Users\\" + "realperson\\x and 192.168." + "1.20\n")
            rc, out = self.scan()
            self.assertEqual(rc, 1)
            self.assertIn("user-home-path", out)
            self.assertIn("private-ip-literal", out)
            os.environ["PRIVACY_GATE_REQUIRE_KEY"] = "1"
            self.assertEqual(self.scan()[0], 2)
        finally:
            os.environ.pop("PRIVACY_GATE_KEY_FILE", None)

    def test_placeholders_pass_shapes(self):
        self.commit("p.md", "C:\\Users\\<user>\\x /home/$USER/y 192.0.2.10 00:00:5E:00:53:01 a@example.test\n")
        self.assertEqual(self.scan()[0], 0)

    def test_sanctioned_url_is_stripped(self):
        (self.root / "cfg.json").write_text('{"sanctioned": ["github.com/x/keep"]}', encoding="utf-8")
        pg.CONFIG_PATH = self.root / "cfg.json"
        self.commit("u.md", f"https://github.com/x/keep/{TERM}\n")
        self.assertEqual(self.scan()[0], 1)          # only the URL part is sanctioned
        self.commit("u.md", "https://github.com/x/keep\n")
        self.assertEqual(self.scan()[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
