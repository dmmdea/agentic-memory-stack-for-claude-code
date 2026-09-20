"""Unit tests for wiki-index-build.py pure helpers (CM5).

The embed/Qdrant path needs the deployed venv + live services and is covered by
the live idempotence check (build twice -> second run all-unchanged); these tests
pin the parsing contract, which is what the vault schema depends on.

Run: python3 -m pytest scripts/wsl/test_wiki_index.py -q  (no venv deps needed)
"""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "wiki_index_build", Path(__file__).parent / "wiki-index-build.py")
wib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wib)

PAGE = """---
type: entity
tags: [node, hardware]
updated: 2026-06-26
---

# Example Node

Primary workstation node of the ecosystem.

## Overview
More text here.
"""


def test_parse_page_frontmatter_and_body():
    fm, body = wib.parse_page(PAGE)
    assert fm["type"] == "entity"
    assert fm["tags"] == "[node, hardware]"
    assert fm["updated"] == "2026-06-26"
    assert body.startswith("# Example Node")


def test_parse_page_no_frontmatter():
    fm, body = wib.parse_page("# Title\n\nJust a body.\n")
    assert fm == {}
    assert body.startswith("# Title")


def test_first_body_line_skips_headings_and_blanks():
    _, body = wib.parse_page(PAGE)
    assert wib.first_body_line(body) == "Primary workstation node of the ecosystem."


def test_first_body_line_caps_length():
    long = "x" * 500
    assert len(wib.first_body_line(long)) == wib.SUMMARY_CAP


def test_dir_type_mapping():
    assert wib.DIR_TYPE["entities"] == "entity"
    assert wib.DIR_TYPE["syntheses"] == "synthesis"
