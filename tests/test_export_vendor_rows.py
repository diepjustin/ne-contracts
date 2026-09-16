"""build_vendor_rows() feeds ne-connect's per-vendor itemized fetch."""

import os
import sys

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import export_vendor_rows  # noqa: E402

HEADER = (
    "Document Number,Document Type,Entity Code,Entity Name,Vendor,Amount,"
    "Begin Date,End Date,Status,Detail URL,View URL\n"
)


def _write_contracts(tmp_path, *rows):
    path = tmp_path / "nu_contracts.csv"
    path.write_text(HEADER + "".join(rows), encoding="utf-8")
    return path


def test_rows_grouped_by_exact_vendor_string(tmp_path, monkeypatch):
    row = (
        "80-2-198,CN,050,Chadron State College,HEARTLAND PAYMENT SYSTEMS,"
        '"$262,182.88",01/06/2014,01/06/2030,Active,https://example.gov/1,\n'
    )
    monkeypatch.setattr(export_vendor_rows, "DATA_DIR", _write_contracts(tmp_path, row).parent)

    rows = export_vendor_rows.build_vendor_rows()

    entry = rows["HEARTLAND PAYMENT SYSTEMS"][0]
    assert entry == ["80-2-198", "CN", "Chadron State College", 262182.88, "01/06/2014", "01/06/2030", "Active", "https://example.gov/1"]


def test_rows_with_no_vendor_are_skipped(tmp_path, monkeypatch):
    row = ",CN,050,Chadron State College,,$0.00,01/06/2014,01/06/2030,Active,https://example.gov/1,\n"
    monkeypatch.setattr(export_vendor_rows, "DATA_DIR", _write_contracts(tmp_path, row).parent)

    assert export_vendor_rows.build_vendor_rows() == {}


def test_purchase_orders_merge_into_the_same_vendor_key(tmp_path, monkeypatch):
    contract_row = (
        "80-2-198,CN,050,Chadron State College,ACME CO,$100.00,01/06/2014,"
        "01/06/2030,Active,https://example.gov/1,\n"
    )
    po_row = (
        "PO-9,PO,050,Chadron State College,ACME CO,$50.00,01/06/2015,"
        "01/06/2016,Active,https://example.gov/2,\n"
    )
    data_dir = _write_contracts(tmp_path, contract_row).parent
    (data_dir / "nu_purchase_orders.csv").write_text(HEADER + po_row, encoding="utf-8")
    monkeypatch.setattr(export_vendor_rows, "DATA_DIR", data_dir)

    rows = export_vendor_rows.build_vendor_rows()

    assert len(rows["ACME CO"]) == 2


def test_unparseable_amount_defaults_to_zero():
    assert export_vendor_rows.to_amount("not a number") == 0.0


def test_shard_key_uppercases_the_first_letter():
    assert export_vendor_rows.shard_key("amazon capital services") == "A"


def test_shard_key_falls_back_for_non_letter_leading_names():
    assert export_vendor_rows.shard_key("3M COMPANY") == "_"
    assert export_vendor_rows.shard_key("") == "_"


def test_main_writes_one_shard_file_per_first_letter(tmp_path, monkeypatch, capsys):
    row_a = (
        "1,CN,050,Some College,ACME CO,$100.00,01/06/2014,01/06/2030,"
        "Active,https://example.gov/1,\n"
    )
    row_b = (
        "2,CN,050,Some College,BETA LLC,$50.00,01/06/2014,01/06/2030,"
        "Active,https://example.gov/2,\n"
    )
    monkeypatch.setattr(export_vendor_rows, "DATA_DIR", _write_contracts(tmp_path, row_a, row_b).parent)
    out_dir = tmp_path / "d" / "rows"
    monkeypatch.setattr(export_vendor_rows, "OUT_DIR", out_dir)

    export_vendor_rows.main()

    assert (out_dir / "A.json").exists()
    assert (out_dir / "B.json").exists()
    assert not (out_dir / "C.json").exists()


def test_main_clears_stale_shards_from_a_previous_run(tmp_path, monkeypatch):
    row = (
        "1,CN,050,Some College,ACME CO,$100.00,01/06/2014,01/06/2030,"
        "Active,https://example.gov/1,\n"
    )
    monkeypatch.setattr(export_vendor_rows, "DATA_DIR", _write_contracts(tmp_path, row).parent)
    out_dir = tmp_path / "d" / "rows"
    out_dir.mkdir(parents=True)
    (out_dir / "Z.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(export_vendor_rows, "OUT_DIR", out_dir)

    export_vendor_rows.main()

    assert not (out_dir / "Z.json").exists()
