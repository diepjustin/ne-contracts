"""extract_text reads the payload build_site.py wrote, and says so when there is none.

manifest.json is written by build_site.py and read by two functions here. A
checkout that has never built - a fresh clone, or data/ copied across from
elsewhere - has none, and both raised a bare FileNotFoundError that says nothing
about what to run. The OCR wrapper runs build_site.py first for exactly this
reason; anyone running extract_text.py by hand did not know to.
"""

import json
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import extract_text  # noqa: E402


def test_a_checkout_that_never_built_says_to_run_build_site(monkeypatch, tmp_path):
    monkeypatch.setattr(extract_text, "ROOT", str(tmp_path))
    with pytest.raises(SystemExit) as stopped:
        extract_text.load_targets()
    assert "build_site.py" in str(stopped.value) and "manifest.json" in str(stopped.value)


def test_the_other_reader_says_it_too(monkeypatch, tmp_path):
    monkeypatch.setattr(extract_text, "ROOT", str(tmp_path))
    with pytest.raises(SystemExit) as stopped:
        extract_text.payload_dir()
    assert "build_site.py" in str(stopped.value)


def test_the_payload_dir_follows_the_manifest(monkeypatch, tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"buildId": "x", "dir": "d/x"}), encoding="utf-8")
    monkeypatch.setattr(extract_text, "ROOT", str(tmp_path))
    assert extract_text.payload_dir() == os.path.join(str(tmp_path), "d/x")
