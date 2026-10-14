"""build_site.load_ai_summaries: what an AI-summary asset may contain, and what stops the build.

The asset is cut from ai_summaries.jsonl, which a launchd job appends to, so it
can be taken mid-write: a torn last line is expected, as it is for every other
log in the pipeline. Until now the loader did json.loads on every line, so a
blank line or a torn tail raised a bare JSONDecodeError and stopped the nightly
site build. A summary from a model this build does not list failed even later,
in verify_ai_summaries, with "text and source disagree" - a message that names
neither the model nor AI_MODELS. None of it was tested; there was no test of
this loader at all.

These pin the tolerance and its limit: a torn final line is ignored, corruption
with more lines after it is not, and a model nobody listed stops the build with
a message that says which model and what to do.
"""

import json
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import build_site  # noqa: E402
import ne_format  # noqa: E402

MODEL = build_site.AI_MODELS[1]
TOKENS = ["tokA", "tokB", "tokC"]


def rec(tok, model=MODEL, summary="A purchase order for supplies.", **extra):
    out = {"tok": tok, "doc": "D", "model": model, "seconds": 1.0, "unclear": False, "summary": summary}
    out.update(extra)
    return out


def load(monkeypatch, tmp_path, lines):
    """Run load_ai_summaries over `lines` (written as given, newline-joined)."""
    path = tmp_path / "ai_summaries.jsonl"
    path.write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(build_site, "AI_SUMMARIES_JSONL", str(path))
    return build_site.load_ai_summaries(TOKENS)


def j(record):
    return json.dumps(record)


def test_known_model_rows_are_joined_by_token(monkeypatch, tmp_path):
    summaries, sources = load(monkeypatch, tmp_path, [j(rec("tokA")), j(rec("tokC", summary="Second."))])
    assert summaries == {0: b"A purchase order for supplies.", 2: b"Second."}
    assert sources == {0: 1, 2: 1}


def test_blank_lines_are_ignored_wherever_they_fall(monkeypatch, tmp_path):
    summaries, _ = load(monkeypatch, tmp_path, ["", j(rec("tokA")), "   ", j(rec("tokB")), "", ""])
    assert set(summaries) == {0, 1}


def test_a_torn_final_line_is_ignored_and_the_rest_load(monkeypatch, tmp_path, capsys):
    torn = j(rec("tokB"))[:30]
    summaries, _ = load(monkeypatch, tmp_path, [j(rec("tokA")), torn])
    assert set(summaries) == {0}
    assert "ends mid-write; ignoring its last line (line 2)" in capsys.readouterr().out


def test_a_torn_line_followed_by_a_blank_one_is_still_the_last_line(monkeypatch, tmp_path):
    summaries, _ = load(monkeypatch, tmp_path, [j(rec("tokA")), j(rec("tokB"))[:30], "", ""])
    assert set(summaries) == {0}


def test_corruption_with_more_lines_after_it_stops_the_build(monkeypatch, tmp_path):
    """Skipping it would publish whatever follows a line nobody can vouch for."""
    with pytest.raises(SystemExit) as stopped:
        load(monkeypatch, tmp_path, [j(rec("tokA")), "{not json}", j(rec("tokB"))])
    message = str(stopped.value)
    assert "line 2" in message and "cannot be trusted" in message


def test_a_model_the_build_does_not_list_stops_it_naming_the_cause(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as stopped:
        load(monkeypatch, tmp_path, [j(rec("tokA")), j(rec("tokB", model="llama3.3:70b")),
                                     j(rec("tokC", model="llama3.3:70b"))])
    message = str(stopped.value)
    assert "'llama3.3:70b' (2 rows)" in message
    assert "AI_MODELS" in message and "append only" in message


def test_a_summary_with_no_model_stops_the_build_too(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as stopped:
        load(monkeypatch, tmp_path, [j(rec("tokA", model=None))])
    assert "(missing)" in str(stopped.value)


def test_an_unlisted_model_on_a_token_this_build_does_not_carry_is_harmless(monkeypatch, tmp_path):
    """Only rows that would be published can disagree with aisrc.bin."""
    summaries, _ = load(monkeypatch, tmp_path, [j(rec("tokA")), j(rec("tokZ", model="llama3.3:70b"))])
    assert set(summaries) == {0}


def test_unclear_and_empty_summaries_are_skipped_not_judged(monkeypatch, tmp_path, capsys):
    summaries, _ = load(monkeypatch, tmp_path, [
        j(rec("tokA")),
        j(rec("tokB", model="llama3.3:70b", summary=None, unclear=True)),
        j({"tok": "tokC", "doc": "D", "error": "timed out"}),
        j({"error": "worker crashed: boom"}),            # no token at all
    ])
    assert set(summaries) == {0}
    assert "3 skipped (unclear or empty)" in capsys.readouterr().out


def test_a_missing_file_builds_without_summaries(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(build_site, "AI_SUMMARIES_JSONL", str(tmp_path / "none.jsonl"))
    assert build_site.load_ai_summaries(TOKENS) == ({}, {})
    assert "building without AI summaries" in capsys.readouterr().out


def test_what_it_loads_survives_the_writer_and_the_verifier(monkeypatch, tmp_path):
    """The path the nightly takes after loading: write the blocks, read them back,
    check the text and the source byte agree on every row."""
    summaries, sources = load(monkeypatch, tmp_path, [j(rec("tokA")), "", j(rec("tokC", summary="Second."))])
    out = tmp_path / "out"
    out.mkdir()
    ne_format.write_ai_blocks(str(out), summaries, len(TOKENS))
    ne_format.write_ai_sources(str(out), sources, len(TOKENS))
    build_site.verify_ai_summaries(str(out), summaries, len(TOKENS))
