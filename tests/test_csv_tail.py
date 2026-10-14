"""repair_csv_tail: a killed write must not leave a stump for the next append to glue onto.

See the function's docstring for what the glued row does to the site build.
These are the unit tests; test_scrape_entity.py runs it inside a real --daily.
"""

import csv
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import scrape  # noqa: E402


def write(path, data):
    with open(path, "wb") as f:
        f.write(data)
    return path


WHOLE = b"a,b,c\r\n1,2,3\r\n4,5,6\r\n"


def test_a_file_ending_in_a_newline_is_left_alone(tmp_path, capsys):
    path = write(tmp_path / "rows.csv", WHOLE)
    assert scrape.repair_csv_tail(str(path)) == 0
    assert path.read_bytes() == WHOLE
    assert capsys.readouterr().out == ""


def test_a_stump_is_cut_back_to_the_last_newline(tmp_path, capsys):
    path = write(tmp_path / "rows.csv", WHOLE + b"7,8")
    assert scrape.repair_csv_tail(str(path)) == 3
    assert path.read_bytes() == WHOLE
    assert "ends mid-row; cutting 3 bytes" in capsys.readouterr().out


def test_a_stump_that_looks_like_a_whole_row_is_cut_too(tmp_path):
    """Cut just after the Detail URL a row has its eleven fields and an empty View
    URL, which would say 'this record has no document'. It must not survive."""
    header = ",".join(scrape.CSV_HEADER)
    whole_looking = "D1,CN,051,A,V,$1.00,01/01/2026,12/31/2026,Active,https://x/y,"
    path = write(tmp_path / "rows.csv", (header + "\r\n" + whole_looking).encode())
    assert scrape.repair_csv_tail(str(path)) == len(whole_looking)
    assert path.read_bytes() == (header + "\r\n").encode()
    with open(path, newline="", encoding="utf-8") as f:
        assert len(list(csv.reader(f))) == 1


def test_multibyte_text_in_the_stump_is_cut_whole(tmp_path):
    path = write(tmp_path / "rows.csv", WHOLE + "7,Ñoño ’s caf".encode("utf-8")[:-1])
    scrape.repair_csv_tail(str(path))
    assert path.read_bytes() == WHOLE


def test_a_file_that_is_all_stump_is_emptied(tmp_path):
    """A header cut mid-write: nothing before it, so nothing to keep. The caller
    then sees an empty file and writes the header afresh."""
    path = write(tmp_path / "rows.csv", b"Document Number,Docum")
    assert scrape.repair_csv_tail(str(path)) == 21
    assert path.read_bytes() == b""


def test_a_missing_or_empty_file_is_not_touched(tmp_path):
    assert scrape.repair_csv_tail(str(tmp_path / "missing.csv")) == 0
    assert not (tmp_path / "missing.csv").exists()
    path = write(tmp_path / "empty.csv", b"")
    assert scrape.repair_csv_tail(str(path)) == 0
    assert path.read_bytes() == b""


def test_a_stump_longer_than_the_look_back_window_is_still_found(monkeypatch, tmp_path):
    monkeypatch.setattr(scrape, "CSV_TAIL_WINDOW", 16)
    path = write(tmp_path / "rows.csv", WHOLE * 50 + b"x" * 10)
    assert scrape.repair_csv_tail(str(path)) == 10
    assert path.read_bytes() == WHOLE * 50


def test_no_newline_anywhere_in_the_window_is_left_for_a_person(monkeypatch, tmp_path, capsys):
    """A megabyte without a newline is not a half-written row; cutting it could
    destroy good data, so it is reported and left."""
    monkeypatch.setattr(scrape, "CSV_TAIL_WINDOW", 16)
    data = WHOLE + b"y" * 40
    path = write(tmp_path / "rows.csv", data)
    assert scrape.repair_csv_tail(str(path)) == 0
    assert path.read_bytes() == data
    assert "Leaving the file for a person" in capsys.readouterr().out
