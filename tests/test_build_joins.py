"""load_document_counts and write_vendor_groups: the two joins no test ran.

The first stitches data/documents.jsonl onto the payload's rows. It decides every
row's document count, which rows have a list, and whether a row's description now
describes a document the state has withdrawn -- and it was monkeypatched out of
every build test. The second attaches the reviewed vendor spellings and stops the
build when one has gone missing from the data, because a silently ignored
spelling shrinks a published company total. Both are pure file-in, value-out.
"""

import array
import base64
import json
import os
import sys
from urllib.parse import quote

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import build_site  # noqa: E402
import ne_format  # noqa: E402
import scrape  # noqa: E402

UNKNOWN = ne_format.DOC_COUNT_UNKNOWN


# --- load_document_counts ------------------------------------------------------------

def token(seed):
    """A document token as documents.jsonl stores it: percent-encoded base64 of 16 bytes."""
    return quote(base64.b64encode(bytes([seed]) * ne_format.TOKEN_BYTES).decode(), safe="")


def urls(n):
    return [f"{build_site.DETAIL_BASE}?A=a&D=d&DN=dn{i}&N=n&DT=dt&V=v" for i in range(n)]


def entry(url, n, *seeds):
    out = {"k": scrape.document_key(url), "doc": "D", "entity": "E", "n": n, "seen": "2026-10-01"}
    if seeds:
        out["u"] = url
        out["documents"] = [{"name": f"DOC{s}", "size": f"{s}Kb", "token": token(s)} for s in seeds]
    return out


@pytest.fixture
def log(monkeypatch, tmp_path):
    """Write data/documents.jsonl under a scratch ROOT, and point build_site at it."""
    monkeypatch.setattr(build_site, "ROOT", str(tmp_path))
    (tmp_path / "data").mkdir()

    def write(*entries):
        with open(tmp_path / "data" / "documents.jsonl", "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    return write


def test_with_no_log_every_row_is_unknown_and_nothing_is_checked(log):
    counts, packed, drift = build_site.load_document_counts(urls(3), ["", "", ""])
    assert list(counts) == [UNKNOWN] * 3 and packed == {}
    assert drift == {"checked": 0, "moved": 0, "gone": 0, "clamped": 0}


def test_a_row_the_log_has_is_counted_and_the_rest_stay_unknown(log):
    rows = urls(3)
    log(entry(rows[0], 1), entry(rows[2], 4))
    counts, packed, drift = build_site.load_document_counts(rows, ["", "", ""])
    assert list(counts) == [1, UNKNOWN, 4]
    assert drift["checked"] == 2 and packed == {}


def test_the_rows_are_matched_by_their_full_detail_url(log):
    """The key is a hash of the whole URL: a row sharing every other part is another row."""
    rows = urls(2)
    log(entry(rows[0], 2))
    counts, _packed, _drift = build_site.load_document_counts(
        [rows[0], rows[0].replace("DN=dn0", "DN=dn9")], ["", ""])
    assert list(counts) == [2, UNKNOWN]


@pytest.mark.parametrize("listed, written, clamped", [
    (253, 253, 0),
    (254, 254, 0),
    (255, 254, 1),    # 255 is the unknown sentinel, so a real 255 must not be written as it
    (300, 254, 1),
])
def test_a_list_too_long_for_a_u8_stops_at_254_and_is_counted(log, listed, written, clamped):
    rows = urls(1)
    log(entry(rows[0], listed))
    counts, _packed, drift = build_site.load_document_counts(rows, [""])
    assert counts[0] == written and drift["clamped"] == clamped


def test_moved_and_gone_are_told_apart_and_neither_changes_what_is_written(log):
    """moved: the CSV's document is still listed, no longer first (harmless).
    gone: it is not listed at all, so the row's description is about a withdrawn file.
    Three rows still first, one moved, two gone: no two counts are equal, so a swapped
    condition cannot give the right totals by accident."""
    rows = urls(6)
    log(*[entry(url, 2, 1, 2) for url in rows])
    counts, packed, drift = build_site.load_document_counts(
        rows, [token(1), token(1), token(1), token(2), token(9), token(8)])
    assert list(counts) == [2] * 6
    assert drift == {"checked": 6, "moved": 1, "gone": 2, "clamped": 0}
    assert sorted(packed) == [0, 1, 2, 3, 4, 5]


def test_a_row_with_no_document_of_its_own_is_neither_moved_nor_gone(log):
    rows = urls(1)
    log(entry(rows[0], 2, 1, 2))
    counts, packed, drift = build_site.load_document_counts(rows, [""])
    assert counts[0] == 2 and 0 in packed
    assert drift["moved"] == 0 and drift["gone"] == 0


def test_the_packed_list_decodes_to_the_states_own_names_sizes_and_tokens(log):
    rows = urls(1)
    log(entry(rows[0], 2, 3, 4))
    _counts, packed, _drift = build_site.load_document_counts(rows, [token(3)])
    assert ne_format.unpack_documents(packed[0]) == [
        {"token": bytes([3]) * ne_format.TOKEN_BYTES, "name": "DOC3", "size": "3Kb"},
        {"token": bytes([4]) * ne_format.TOKEN_BYTES, "name": "DOC4", "size": "4Kb"}]


def test_a_single_document_row_stores_only_its_count(log):
    rows = urls(1)
    log(entry(rows[0], 1))
    counts, packed, drift = build_site.load_document_counts(rows, [token(1)])
    assert counts[0] == 1 and packed == {}
    assert drift["moved"] == 0 and drift["gone"] == 0


def test_the_latest_entry_for_a_row_wins(log):
    rows = urls(1)
    log(entry(rows[0], 1), entry(rows[0], 3, 1, 2, 3))
    counts, packed, _drift = build_site.load_document_counts(rows, [token(1)])
    assert counts[0] == 3 and len(ne_format.unpack_documents(packed[0])) == 3


# --- write_vendor_groups -----------------------------------------------------------------

def attach(monkeypatch, tmp_path, mapping, vendors, spend=None):
    """Run write_vendor_groups over `vendors` (one row each, amounts from `spend`)."""
    path = tmp_path / "vendor_groups.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    monkeypatch.setattr(build_site, "VENDOR_GROUPS_JSON", str(path))
    columns = {"vendorIdx": array.array("i", range(len(vendors))),
               "amount": array.array("d", spend or [1.0] * len(vendors))}
    meta = {"count": len(vendors)}
    build_site.write_vendor_groups(str(tmp_path), [v.encode() for v in vendors], columns, meta)
    return meta


def test_groups_are_numbered_in_sorted_order_and_ungrouped_vendors_are_minus_one(monkeypatch, tmp_path):
    meta = attach(monkeypatch, tmp_path,
                  {"ZED CO": ["Z1", "Z2"], "ABE CO": ["A1"]}, ["Z1", "OTHER", "A1", "Z2"])
    assert meta["vendorGroups"] == ["ABE CO", "ZED CO"]
    assert list(ne_format.read_vendor_groups(str(tmp_path), 4)) == [1, -1, 0, 1]


def test_keys_that_start_with_an_underscore_are_comments(monkeypatch, tmp_path):
    meta = attach(monkeypatch, tmp_path, {"_note": ["not a spelling"], "ABE CO": ["A1"]}, ["A1"])
    assert meta["vendorGroups"] == ["ABE CO"]


def test_the_printed_total_is_the_spend_of_grouped_vendors_only(monkeypatch, tmp_path, capsys):
    attach(monkeypatch, tmp_path, {"ACME": ["A1", "A2"]}, ["A1", "OTHER", "A2"],
           spend=[3e9, 9e9, 2e9])
    assert "1 companies covering 2 spellings, $5.00 B" in capsys.readouterr().out


def test_a_spelling_that_has_left_the_data_stops_the_build_and_is_named(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as stop:
        attach(monkeypatch, tmp_path, {"ACME": ["ACME INC", "ACME INC."], "ABE CO": ["A1"]},
               ["ACME INC", "A1"])
    message = str(stop.value)
    assert "1 vendor spelling(s)" in message and "ACME: 'ACME INC.'" in message
    assert "'ACME INC'" not in message, "a spelling that is in the data is not reported missing"
    assert not os.path.exists(tmp_path / ne_format.VGROUP), "nothing is written for a refused build"


def test_every_stale_spelling_is_counted_but_only_ten_are_listed(monkeypatch, tmp_path):
    spellings = [f"GONE {i}" for i in range(12)]
    with pytest.raises(SystemExit) as stop:
        attach(monkeypatch, tmp_path, {"ACME": spellings}, ["SOMETHING ELSE"])
    message = str(stop.value)
    assert "12 vendor spelling(s)" in message
    assert "GONE 9" in message and "GONE 10" not in message


def test_no_file_means_no_groups_and_nothing_is_written(monkeypatch, tmp_path):
    monkeypatch.setattr(build_site, "VENDOR_GROUPS_JSON", str(tmp_path / "absent.json"))
    meta = {"count": 1}
    build_site.write_vendor_groups(str(tmp_path), [b"A1"], {"vendorIdx": array.array("i", [0]),
                                                           "amount": array.array("d", [1.0])}, meta)
    assert meta["vendorGroups"] == [] and not os.path.exists(tmp_path / ne_format.VGROUP)


def test_an_empty_file_groups_nothing_but_still_writes_the_column(monkeypatch, tmp_path):
    meta = attach(monkeypatch, tmp_path, {}, ["A1", "A2"])
    assert meta["vendorGroups"] == []
    assert list(ne_format.read_vendor_groups(str(tmp_path), 2)) == [-1, -1]
