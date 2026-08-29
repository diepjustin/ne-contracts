"""The payload format has to survive a round trip, byte for byte.

Every section is verified at build time already, but only against whatever the
real data happens to contain. These build small payloads that deliberately hit
the cases real data does not reliably reach: an empty value, a row that lands
exactly on a block boundary, the very first and very last row, a block that is
not full. Those are where off-by-one errors live, and a payload that is wrong
in one row does not fail -- it shows the wrong contract's words next to the
right contract's money.
"""

import array
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import ne_format  # noqa: E402


def make_columns(n):
    """One of every dtype the format carries, with recognisable values."""
    columns = {name: array.array("i", range(n)) for name in ne_format.I32_COLUMNS}
    columns.update({name: array.array("d", [i * 1.5 for i in range(n)])
                    for name in ne_format.F64_COLUMNS})
    columns.update({name: array.array("B", [i % 256 for i in range(n)])
                   for name in ne_format.U8_COLUMNS})
    return columns


def write_minimal(outdir, n, docs, vendors=None, vtokens=None):
    vendors = vendors if vendors is not None else [b"ACME"]
    vtokens = vtokens if vtokens is not None else [b"\x01" * ne_format.TOKEN_BYTES]
    columns = make_columns(n)
    columns["docLen"] = array.array("B", [len(d) for d in docs])
    meta = {"count": n, "vendorCount": len(vendors)}
    return ne_format.write_payload(outdir, columns, docs, vendors, vtokens, meta), columns


def test_columns_and_docs_round_trip(tmp_path):
    n = 5
    docs = [b"A", b"BB", b"CCC", b"", b"EEEEE"]   # includes an empty document number
    written, columns = write_minimal(str(tmp_path), n, docs)

    got_columns, got_docs, got_vendors, got_vtok, got_meta = ne_format.read_payload(str(tmp_path))

    assert got_docs == docs
    assert got_vendors == [b"ACME"]
    assert got_meta["count"] == n
    for name, col in columns.items():
        # Bytes, not values: float equality would accept a changed amount that
        # happens to compare equal.
        assert got_columns[name].tobytes() == col.tobytes(), name


def test_bytes_map_records_real_file_sizes(tmp_path):
    """meta.bytes drives the loader's progress bar, so it must match reality."""
    written, _ = write_minimal(str(tmp_path), 3, [b"A", b"B", b"C"])
    for name, size in written["bytes"].items():
        assert os.path.getsize(os.path.join(str(tmp_path), name)) == size, name


def test_column_length_mismatch_is_rejected(tmp_path):
    columns = make_columns(4)
    columns["begin"] = array.array("i", [1, 2])          # wrong length
    with pytest.raises(ValueError, match="expected 4"):
        ne_format.write_payload(str(tmp_path), columns, [b"A"] * 4,
                                [b"V"], [b"\x00" * 16], {"count": 4, "vendorCount": 1})


def test_docs_blob_disagreeing_with_doclen_is_rejected(tmp_path):
    write_minimal(str(tmp_path), 2, [b"AA", b"BB"])
    with open(os.path.join(str(tmp_path), ne_format.DOCS), "ab") as f:
        f.write(b"junk")                                  # one byte too many
    with pytest.raises(ValueError, match="docLen"):
        ne_format.read_payload(str(tmp_path))


# --- token blocks -----------------------------------------------------------

def test_token_blocks_round_trip_across_a_boundary(tmp_path):
    """Two full blocks plus one row, so the last block is deliberately partial."""
    n = ne_format.BLOCK_ROWS * 2 + 1
    dn = [bytes([i % 251]) * ne_format.TOKEN_BYTES for i in range(n)]
    view = [b"" if i % 3 == 0 else bytes([(i + 7) % 251]) * ne_format.TOKEN_BYTES
            for i in range(n)]

    ne_format.write_token_blocks(str(tmp_path), dn, view, n)
    got_dn, got_view = ne_format.read_token_blocks(str(tmp_path), n)

    assert got_dn == dn
    for i in range(n):
        # An absent view token is written as zeroes; viewPresent is the authority.
        assert got_view[i] == (view[i] or b"\x00" * ne_format.TOKEN_BYTES), i
    assert ne_format.block_count(n) == 3


# --- descriptions -----------------------------------------------------------

def test_desc_blocks_round_trip_including_edges(tmp_path):
    n = ne_format.BLOCK_ROWS + 3
    descriptions = {
        0: "first row".encode(),
        ne_format.BLOCK_ROWS - 1: "last row of block 0".encode(),
        ne_format.BLOCK_ROWS: "first row of block 1".encode(),
        n - 1: "very last row".encode(),
        7: "unicode: – — é 中".encode("utf-8"),
    }
    ne_format.write_desc_blocks(str(tmp_path), descriptions, n)
    got = ne_format.read_desc_blocks(str(tmp_path), n)

    assert len(got) == n
    for row in range(n):
        assert got[row] == descriptions.get(row, b""), row


def test_desc_length_over_u16_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="u16"):
        ne_format.write_desc_blocks(str(tmp_path), {0: b"x" * 70000}, 1)


def test_truncated_desc_block_is_rejected(tmp_path):
    ne_format.write_desc_blocks(str(tmp_path), {0: b"hello"}, 2)
    path = ne_format.desc_path(str(tmp_path), 0)
    with open(path, "rb") as f:
        data = f.read()
    with open(path, "wb") as f:
        f.write(data[:-2])                                # lose part of the text
    with pytest.raises(ValueError):
        ne_format.read_desc_blocks(str(tmp_path), 2)


# --- search index -----------------------------------------------------------

def test_index_round_trips(tmp_path):
    postings = {
        "asbestos": [3, 9, 1000, 700000],       # large gaps -> multi-byte varints
        "abatement": [9],
        "zzz": [0],                             # row 0 is a delta of 0 from the start
        "1234": list(range(50)),                # a dense run
    }
    words = ne_format.write_index(str(tmp_path), postings)

    assert words == sorted(postings), "words must be written sorted for binary search"
    assert ne_format.read_index(str(tmp_path)) == postings


def test_index_survives_a_word_in_every_row(tmp_path):
    postings = {"the": list(range(5000))}
    ne_format.write_index(str(tmp_path), postings)
    assert ne_format.read_index(str(tmp_path)) == postings


def test_trailing_bytes_in_postings_are_rejected(tmp_path):
    ne_format.write_index(str(tmp_path), {"a": [1]})
    with open(os.path.join(str(tmp_path), ne_format.POSTINGS), "ab") as f:
        f.write(b"\x00")
    with pytest.raises(ValueError, match="past the last word"):
        ne_format.read_index(str(tmp_path))


# --- document blocks: the rows that publish more than one -------------------

def _documents(*names):
    return [{"token": bytes([i + 1]) * ne_format.TOKEN_BYTES,
             "name": n, "size": "2Mb"} for i, n in enumerate(names)]


def test_a_rows_document_list_survives_the_round_trip(tmp_path):
    packed = {3: ne_format.pack_documents(_documents("DOC1", "DOC2", "DOC3"))}
    ne_format.write_xdoc_blocks(str(tmp_path), packed, 10)
    back = ne_format.read_xdoc_blocks(str(tmp_path), 10)

    got = ne_format.unpack_documents(back[3])
    assert [d["name"] for d in got] == ["DOC1", "DOC2", "DOC3"]
    assert [d["size"] for d in got] == ["2Mb"] * 3
    assert got[0]["token"] == b"\x01" * ne_format.TOKEN_BYTES


def test_rows_with_one_document_cost_nothing_in_these_blocks(tmp_path):
    """~90% of the corpus. Their document is already the row's view token."""
    ne_format.write_xdoc_blocks(str(tmp_path), {}, 10)
    assert ne_format.read_xdoc_blocks(str(tmp_path), 10) == [b""] * 10
    # header only: a u16 length per row, no payload
    assert os.path.getsize(ne_format.xdoc_path(str(tmp_path), 0)) == 20


def test_the_states_own_name_and_size_survive_verbatim(tmp_path):
    """Both are reproduced, not reformatted -- a reader checks our list against
    the state's page with them, and the names are not all of one shape: some
    records use DOC ids and some use UUIDs."""
    docs = [{"token": b"\x07" * ne_format.TOKEN_BYTES,
             "name": "AA77C1E2-3C42-4E8B-8BB8-89E376C80E8C", "size": "923Kb"}]
    ne_format.write_xdoc_blocks(str(tmp_path), {0: ne_format.pack_documents(docs)}, 1)
    back = ne_format.unpack_documents(ne_format.read_xdoc_blocks(str(tmp_path), 1)[0])
    assert back == docs


def test_a_wrong_length_token_fails_the_build(tmp_path):
    """Never widen every record for an outlier, and never write a short one."""
    with pytest.raises(ValueError, match="expected"):
        ne_format.pack_documents([{"token": b"short", "name": "D", "size": "1Mb"}])
