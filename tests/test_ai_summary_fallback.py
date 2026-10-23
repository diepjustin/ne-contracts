"""build_site.load_ai_summaries: summaries of a record's other documents, and which one was read.

ai-01 (audit 2026-10-22): all 19,638 summaries the build reported as "unmatched" were of
a record's amendments and attachments, and 2,516 records with such a summary showed
none at all. The loader now falls back to the first summarized other document when the
row's own has no summary -- never displacing one it has -- and records which document
it used, so the page can say "Summary of <document>" (fe-04). These pin that rule, the
fan-out to every row sharing a view token, and the aidoc.bin round trip.
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

# Row 0 publishes three documents: A1, P1 (its own View URL, listed second), A2.
# Rows 1 and 2 carry the same View URL, P2, whose record also publishes B1.
# Row 3 publishes one document, S. X is listed under no row this build carries.
TOKENS = ["P1", "P2", "P2", "S"]
WHERE = {
    "P1": (0, 1, True), "A1": (0, 0, False), "A2": (0, 2, False),
    "P2": (2, 0, True), "B1": (2, 1, False),
    "S": (3, 0, True),
}
# Each row's own list, as row_document_tokens reads it from the xdoc lists. Row 3,
# publishing one document, has none.
ROW_DOCS = {0: ["A1", "P1", "A2"], 1: ["P2", "B1"], 2: ["P2", "B1"]}


@pytest.fixture(autouse=True)
def documents(monkeypatch):
    monkeypatch.setattr(build_site, "document_positions", lambda tokens: dict(WHERE))


def line(tok, summary="Summary.", **extra):
    out = {"tok": tok, "doc": "D", "model": MODEL, "unclear": False, "summary": summary}
    out.update(extra)
    return json.dumps(out)


def load(monkeypatch, tmp_path, lines, tokens=TOKENS, row_documents=ROW_DOCS):
    path = tmp_path / "ai_summaries.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(build_site, "AI_SUMMARIES_JSONL", str(path))
    return build_site.load_ai_summaries(tokens, row_documents)


def test_the_rows_own_document_wins_wherever_it_falls_in_the_file(monkeypatch, tmp_path):
    for order in ([line("A1", "amendment"), line("P1", "own")],
                  [line("P1", "own"), line("A1", "amendment")]):
        summaries, _, docs = load(monkeypatch, tmp_path, order)
        assert summaries[0] == b"own"
        assert docs[0] == 1          # P1 is second in the state's list


def test_without_its_own_the_first_summarized_other_document_stands_in(monkeypatch, tmp_path, capsys):
    summaries, sources, docs = load(monkeypatch, tmp_path,
                                    [line("A2", "later amendment"), line("A1", "first amendment")])
    assert summaries[0] == b"first amendment" and docs[0] == 0
    assert sources[0] == build_site.AI_MODEL_CODE[MODEL]
    assert "1 of those summarize a document other than the row's own" in capsys.readouterr().out


def test_an_unclear_own_summary_lets_another_document_stand_in(monkeypatch, tmp_path):
    summaries, _, docs = load(monkeypatch, tmp_path,
                              [line("P1", "", unclear=True), line("A2", "amendment")])
    assert summaries[0] == b"amendment" and docs[0] == 2


def test_a_summary_of_a_shared_view_token_reaches_every_row_carrying_it(monkeypatch, tmp_path):
    summaries, _, docs = load(monkeypatch, tmp_path, [line("P2", "shared")])
    assert summaries[1] == summaries[2] == b"shared"
    assert docs[1] == docs[2] == 0


def test_another_document_of_a_shared_record_reaches_every_row_too(monkeypatch, tmp_path):
    summaries, _, docs = load(monkeypatch, tmp_path, [line("B1", "attachment")])
    assert summaries == {1: b"attachment", 2: b"attachment"}
    assert docs == {1: 1, 2: 1}


# Review of the fan-out (2026-10-23): rows sharing a View URL can belong to records that
# publish different documents, and document_positions keeps only one record's list per
# token. Rows 0 and 1 both carry P2; WHERE holds row 1's record.
SHARED = ["P2", "P2"]
SHARED_WHERE = {"P2": (1, 0, True), "B1": (1, 1, False)}


def written_shared(monkeypatch, tmp_path, lines, row_documents, doc_counts, where=SHARED_WHERE):
    monkeypatch.setattr(build_site, "document_positions", lambda tokens: dict(where))
    summaries, sources, docs = load(monkeypatch, tmp_path, lines, SHARED, row_documents)
    out = tmp_path / "out"
    out.mkdir()
    build_site.write_ai_files(str(out), summaries, sources, docs, len(SHARED), {})
    build_site.verify_ai_summaries(str(out), summaries, len(SHARED), docs, doc_counts)
    return summaries, docs


def test_another_document_skips_a_sibling_whose_record_does_not_publish_it(monkeypatch, tmp_path):
    # Scenario A: row 0 publishes only P2. Copying row 1's B1 summary to it at
    # position 1 made verify_ai_summaries exit -- the whole nightly build.
    summaries, docs = written_shared(monkeypatch, tmp_path, [line("B1", "amendment")],
                                     {1: ["P2", "B1"]}, [1, 2])
    assert summaries == {1: b"amendment"}
    assert docs == {1: 1}


def test_another_document_is_not_shown_under_a_siblings_different_document(monkeypatch, tmp_path):
    # Scenario B: row 0 publishes [P2, C1]. Row 1's B1 summary at position 1
    # would have read "Summary of C1" over B1's text. Both records' primary is P2,
    # so document_positions files both C1 and B1 under row 1, the last row
    # carrying it.
    summaries, docs = written_shared(monkeypatch, tmp_path,
                                     [line("B1", "B1 amendment"), line("C1", "C1 amendment")],
                                     {0: ["P2", "C1"], 1: ["P2", "B1"]}, [2, 2],
                                     dict(SHARED_WHERE, C1=(1, 1, False)))
    assert summaries == {0: b"C1 amendment", 1: b"B1 amendment"}
    assert docs == {0: 1, 1: 1}


def test_another_document_takes_its_place_in_each_siblings_own_list(monkeypatch, tmp_path):
    summaries, docs = written_shared(monkeypatch, tmp_path, [line("B1", "amendment")],
                                     {0: ["P2", "C1", "B1"], 1: ["P2", "B1"]}, [3, 2])
    assert summaries == {0: b"amendment", 1: b"amendment"}
    assert docs == {0: 2, 1: 1}


def test_a_shared_own_document_takes_its_place_in_each_rows_own_list(monkeypatch, tmp_path):
    # Row 0 lists P2 second, row 1 first; neither borrows the other's position.
    summaries, docs = written_shared(monkeypatch, tmp_path, [line("P2", "own")],
                                     {0: ["C1", "P2"], 1: ["P2", "B1"]}, [2, 2],
                                     {"P2": (1, 0, True), "B1": (1, 1, False), "C1": (1, 0, False)})
    assert summaries == {0: b"own", 1: b"own"}
    assert docs == {0: 1, 1: 0}


def test_a_shared_own_document_on_a_single_document_sibling_is_position_zero(monkeypatch, tmp_path):
    _, docs = written_shared(monkeypatch, tmp_path, [line("P2", "own")],
                             {1: ["B1", "P2"]}, [1, 2], {"P2": (1, 1, True), "B1": (1, 0, False)})
    assert docs == {0: 0, 1: 1}


def test_row_document_tokens_reads_the_xdoc_lists_in_view_token_form():
    import base64
    from urllib.parse import quote
    raw = [bytes([i]) * ne_format.TOKEN_BYTES for i in (1, 2)]
    blob = ne_format.pack_documents([{"token": t, "name": "n", "size": "1 KB"} for t in raw])
    want = [quote(base64.b64encode(t).decode(), safe="") for t in raw]
    assert build_site.row_document_tokens({4: blob}) == {4: want}
    assert build_site.row_document_tokens([b"", blob]) == {1: want}


def test_a_single_document_row_is_summarized_at_position_zero(monkeypatch, tmp_path):
    _, _, docs = load(monkeypatch, tmp_path, [line("S")])
    assert docs == {3: 0}


def test_a_row_whose_own_token_is_filed_under_another_record_marks_no_document(monkeypatch, tmp_path):
    monkeypatch.setattr(build_site, "document_positions",
                        lambda tokens: dict(WHERE, S=(0, 3, False)))
    _, _, docs = load(monkeypatch, tmp_path, [line("S")])
    assert docs[3] == ne_format.AI_DOC_UNKNOWN


def test_a_token_no_row_carries_is_unmatched(monkeypatch, tmp_path, capsys):
    summaries, _, _ = load(monkeypatch, tmp_path, [line("X")])
    assert summaries == {}
    assert "1 unmatched" in capsys.readouterr().out


def written(monkeypatch, tmp_path, lines):
    summaries, sources, docs = load(monkeypatch, tmp_path, lines)
    out = tmp_path / "out"
    out.mkdir()
    meta = {}
    build_site.write_ai_files(str(out), summaries, sources, docs, len(TOKENS), meta)
    return str(out), summaries, docs, meta


def test_the_document_positions_survive_the_writer_and_the_verifier(monkeypatch, tmp_path):
    out, summaries, docs, meta = written(monkeypatch, tmp_path, [line("A2"), line("B1"), line("S")])
    build_site.verify_ai_summaries(out, summaries, len(TOKENS), docs, [3, 2, 2, 1])
    assert list(ne_format.read_ai_documents(out, len(TOKENS))) == [2, 1, 1, 0]
    assert meta["aiFormatVersion"] == 2
    assert ne_format.AI_DOC in meta["digests"] and ne_format.AI_DOC in meta["bytes"]


def test_the_verifier_catches_a_position_that_did_not_survive(monkeypatch, tmp_path):
    out, summaries, docs, _ = written(monkeypatch, tmp_path, [line("A2")])
    with pytest.raises(SystemExit, match="document round trip failed"):
        build_site.verify_ai_summaries(out, summaries, len(TOKENS), {**docs, 0: 1})


def test_the_verifier_refuses_a_document_the_row_does_not_publish(monkeypatch, tmp_path):
    out, summaries, docs, _ = written(monkeypatch, tmp_path, [line("A2")])
    with pytest.raises(SystemExit, match="publishes 2"):
        build_site.verify_ai_summaries(out, summaries, len(TOKENS), docs, [2, 1, 1, 1])


def test_the_verifier_refuses_a_missing_aidoc_file(monkeypatch, tmp_path):
    out, summaries, docs, _ = written(monkeypatch, tmp_path, [line("A2")])
    os.remove(os.path.join(out, ne_format.AI_DOC))
    with pytest.raises(SystemExit, match="was not written"):
        build_site.verify_ai_summaries(out, summaries, len(TOKENS), docs)


def test_a_v1_build_without_aidoc_reads_as_none(tmp_path):
    assert ne_format.read_ai_documents(str(tmp_path), 4) is None
