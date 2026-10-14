"""One crashed worker must not make a checkpoint unreadable.

ocr_pilot.main appends {"crash": ...} and generate_ai_summaries.main appends
{"error": "worker crashed: ..."} when a worker raises, and neither record has a
token. The loaders then did store[rec["tok"]] and raised KeyError, so every
later run - including --status - died until the file was edited by hand. It has
not happened yet (the live files hold no such line), but the OCR script's own
comments record the out-of-memory kill that would cause it.

The torn-final-line repair next to it is pinned here too, and so is the line it
must not cross: corruption in the middle of the file still raises, because the
entries after it cannot be trusted.
"""

import json
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import generate_ai_summaries  # noqa: E402
import ocr_pilot  # noqa: E402

OK = {"tok": "t1", "doc": "D1", "pages": 2, "engines": {"vision": {"text": "hello"}}}
GOOD_AI = {"tok": "t1", "doc": "D1", "model": "m", "seconds": 1.0, "unclear": False, "summary": "A contract."}


@pytest.fixture(params=["ocr_pilot", "generate_ai_summaries"])
def loader(request, monkeypatch, tmp_path):
    """(module's load function, append function, path, a good record, a crash record)."""
    path = tmp_path / "checkpoint.jsonl"
    if request.param == "ocr_pilot":
        monkeypatch.setattr(ocr_pilot, "OUT_JSONL", str(path))
        return ocr_pilot.load_pilot_checkpoint, ocr_pilot.append_pilot_checkpoint, path, OK, {"crash": "worker died"}
    monkeypatch.setattr(generate_ai_summaries, "OUT_JSONL", str(path))
    return (generate_ai_summaries.load_checkpoint, generate_ai_summaries.append_checkpoint, path,
            GOOD_AI, {"error": "worker crashed: boom"})


def test_a_crash_record_does_not_make_the_checkpoint_unreadable(loader, capsys):
    load, append, path, good, crash = loader
    append([good, crash])

    assert load() == {"t1": good}
    assert "has no token" in capsys.readouterr().out


def test_a_crash_record_in_the_middle_is_skipped_and_the_rest_still_load(loader):
    load, append, path, good, crash = loader
    second = dict(good, tok="t2")
    append([good])
    append([crash])
    append([second])

    assert set(load()) == {"t1", "t2"}


def test_a_keyed_error_record_still_counts_as_done(loader):
    """Unchanged on purpose: a record that has its token is kept, error or not.
    Whether to retry it is a separate decision (an opt-in --retry-errors)."""
    load, append, path, good, _crash = loader
    keyed = {"tok": "t9", "doc": "D9", "error": "timed out"}
    append([good, keyed])
    assert set(load()) == {"t1", "t9"}


def test_a_torn_final_line_is_cut_and_the_others_survive(loader, capsys):
    load, append, path, good, _crash = loader
    append([good])
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"tok": "t2", "do')              # killed mid-write
    size_before = path.stat().st_size

    assert set(load()) == {"t1"}
    assert path.stat().st_size < size_before, "the stump was truncated away"
    assert "ends mid-write" in capsys.readouterr().out


def test_corruption_in_the_middle_still_raises(loader):
    """Skipping it would trust the entries after a line nobody can vouch for."""
    load, append, path, good, _crash = loader
    append([good])
    with open(path, "a", encoding="utf-8") as f:
        f.write("{not json}\n")
    append([dict(good, tok="t3")])

    with pytest.raises(json.JSONDecodeError):
        load()


def test_a_missing_checkpoint_is_empty(loader):
    load, _append, path, _good, _crash = loader
    assert not path.exists()
    assert load() == {}
