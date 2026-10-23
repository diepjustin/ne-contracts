"""What the OCR and summary steps tell the wrapper when a night goes wrong.

Both used to exit 0 whatever happened, so a night where every Ollama request or
every document fetch failed looked exactly like a good one. They now exit 2 when
more than half of this run's items failed, 75 when another run holds the lock,
and the summary step reads the OCR file without tripping on a torn last line.
No network and no Ollama: the model call, the fetch and the pools are replaced.
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import extract_text  # noqa: E402
import generate_ai_summaries as gas  # noqa: E402
import ocr_io  # noqa: E402
import ocr_pilot  # noqa: E402


def ocr_line(tok, text="Some OCR text"):
    return json.dumps({"tok": tok, "doc": "D-" + tok, "pages": 1,
                       "engines": {"vision": {"text": text}}}) + "\n"


@pytest.fixture
def lock(tmp_path, monkeypatch):
    path = str(tmp_path / ".ocr.lock")
    monkeypatch.setattr(ocr_io, "LOCK_PATH", path)
    monkeypatch.delenv(ocr_io.LOCK_FD_ENV, raising=False)
    return path


@pytest.fixture
def summaries(tmp_path, monkeypatch, lock):
    src, out = tmp_path / "ocr_pilot.jsonl", tmp_path / "ai_summaries.jsonl"
    monkeypatch.setattr(gas, "IN_JSONL", str(src))
    monkeypatch.setattr(gas, "OUT_JSONL", str(out))
    monkeypatch.setattr(gas, "ollama_available", lambda: True)
    monkeypatch.setattr(sys, "argv", ["generate_ai_summaries.py", "--workers", "2"])
    return src, out


def run_main(module):
    with pytest.raises(SystemExit) as exc:
        module.main()
    return exc.value.code


def fake_model(monkeypatch, failing=(), unclear=()):
    def summarize(text):
        if text in failing:
            raise OSError("connection refused")
        return gas.UNCLEAR_TOKEN if text in unclear else "A contract for road salt."
    monkeypatch.setattr(gas, "summarize", summarize)


# --- generate_ai_summaries -----------------------------------------------------------------

def test_summaries_exit_2_when_most_of_the_run_failed(summaries, monkeypatch):
    src, out = summaries
    src.write_text("".join(ocr_line(t, text=t) for t in ("a", "b", "c")))
    fake_model(monkeypatch, failing={"a", "b"})
    assert run_main(gas) == 2
    assert len(out.read_text().splitlines()) == 3  # everything still checkpointed


def test_summaries_exit_0_when_half_or_fewer_failed_and_unclear_is_not_a_failure(summaries, monkeypatch):
    src, out = summaries
    src.write_text("".join(ocr_line(t, text=t) for t in ("a", "b", "c", "d")))
    fake_model(monkeypatch, failing={"a"}, unclear={"b", "c"})
    assert run_main(gas) == 0


def test_summaries_with_nothing_to_do_exit_0(summaries, monkeypatch):
    src, out = summaries
    src.write_text("")
    fake_model(monkeypatch)
    assert run_main(gas) == 0


def test_new_summary_records_carry_generated_at(summaries, monkeypatch):
    src, out = summaries
    src.write_text(ocr_line("a"))
    fake_model(monkeypatch)
    assert run_main(gas) == 0
    record = json.loads(out.read_text())
    assert record["summary"] == "A contract for road salt."
    assert record["generated_at"].endswith("Z") and len(record["generated_at"]) == 20


def test_a_torn_last_ocr_line_is_skipped_and_left_alone(summaries, monkeypatch, capsys):
    src, out = summaries
    data = ocr_line("a") + ocr_line("b")[:25]
    src.write_text(data)
    assert set(gas.load_source_texts()) == {"a"}
    assert "ends mid-write" in capsys.readouterr().out
    assert src.read_text() == data  # someone else's file: never truncated


def test_an_ocr_tail_that_parses_but_has_no_newline_is_still_torn(summaries):
    src, _ = summaries
    src.write_text(ocr_line("a") + ocr_line("b").rstrip("\n"))
    assert set(gas.load_source_texts()) == {"a"}


def test_summaries_exit_75_while_another_run_holds_the_lock(summaries, monkeypatch, lock):
    src, out = summaries
    src.write_text(ocr_line("a"))
    fake_model(monkeypatch)
    with ocr_io.run_lock(lock):
        assert run_main(gas) == ocr_io.LOCK_BUSY_EXIT
    assert not out.exists()


# --- ocr_pilot -----------------------------------------------------------------------------

@pytest.fixture
def pilot(tmp_path, monkeypatch, lock):
    out = tmp_path / "ocr_pilot.jsonl"
    monkeypatch.setattr(ocr_pilot, "OUT_JSONL", str(out))
    sample = [("D-" + t, t, 1) for t in ("a", "b", "c", "d")]
    monkeypatch.setattr(ocr_pilot, "load_scanned_sample", lambda n, seed: (sample, sample))
    monkeypatch.setattr(ocr_pilot, "check_engine_available", lambda engine: None)
    monkeypatch.setattr(extract_text, "document_service_healthy", lambda: True)
    # Threads, so the replaced process_one below is the one that runs.
    monkeypatch.setattr(ocr_pilot, "ProcessPoolExecutor", ThreadPoolExecutor)
    monkeypatch.setattr(sys, "argv", ["ocr_pilot.py", "--engines", "vision", "--sample", "10"])
    return out


def fake_ocr(monkeypatch, outcome):
    def process_one(dn, tok, pages, engines, *rest):
        kind = outcome[tok]
        record = {"tok": tok, "doc": dn, "pages": pages}
        if kind == "fetch":
            record["fetch_error"] = "404"
        elif kind == "engine":
            record["engines"] = {"vision": {"error": "Vision failed"}}
        else:
            record["engines"] = {"vision": {"text": "" if kind == "blank" else "words",
                                            "seconds": 1, "seconds_per_page": 1, "chars": 5,
                                            "described": False, "source": None,
                                            "description": None}}
        return record
    monkeypatch.setattr(ocr_pilot, "process_one", process_one)


def test_ocr_exits_2_when_most_documents_produced_nothing(pilot, monkeypatch):
    fake_ocr(monkeypatch, {"a": "fetch", "b": "engine", "c": "fetch", "d": "ok"})
    assert run_main(ocr_pilot) == 2
    assert len(pilot.read_text().splitlines()) == 4


def test_ocr_exits_0_when_half_or_fewer_failed_and_blank_text_is_not_a_failure(pilot, monkeypatch):
    fake_ocr(monkeypatch, {"a": "fetch", "b": "blank", "c": "ok", "d": "engine"})
    assert run_main(ocr_pilot) == 0


def test_ocr_counts_only_tonight_not_old_failures_on_disk(pilot, monkeypatch):
    pilot.write_text("".join(json.dumps({"tok": "old%d" % i, "fetch_error": "404"}) + "\n"
                             for i in range(50)))
    fake_ocr(monkeypatch, {"a": "ok", "b": "ok", "c": "ok", "d": "fetch"})
    assert run_main(ocr_pilot) == 0


def test_ocr_status_exits_75_while_another_run_holds_the_lock(pilot, monkeypatch, lock):
    monkeypatch.setattr(sys, "argv", ["ocr_pilot.py", "--status"])
    with ocr_io.run_lock(lock):
        assert run_main(ocr_pilot) == ocr_io.LOCK_BUSY_EXIT


@pytest.mark.parametrize("record,failed", [
    ({"tok": "t", "fetch_error": "x"}, True),
    ({"tok": "t", "render_error": "x"}, True),
    ({"crash": "x"}, True),
    ({"tok": "t", "engines": {}}, True),
    ({"tok": "t", "engines": {"vision": {"error": "x"}, "tesseract": {"text": "y"}}}, False),
    ({"tok": "t", "engines": {"vision": {"text": ""}}}, False),
])
def test_what_counts_as_a_failed_document(record, failed):
    assert ocr_pilot.record_failed(record) is failed
