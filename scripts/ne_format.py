"""On-disk layout of the published payload, and the only place that knows it.

The page needs three things, and they have very different access patterns:

  * columns you filter and sort on -- every row, every keystroke
  * document numbers -- every row, but only for substring search and the ~38
    rows actually on screen
  * link tokens -- only for rows someone clicks or exports

The first two are packed into whole files the page loads up front; the third
is split into fixed-size blocks fetched on demand. Blocks rather than byte
ranges into one file, because ranged requests are unusable here: GitHub Pages
serves a range against the *compressed* representation when the client
advertises gzip, which browsers always do and `fetch()` cannot override. Ask
for bytes 100-115 and you get bytes 100-115 of a gzip stream, plus a
Content-Range denominator that is the compressed length. See
scripts/chunk_search_db.py, which hit the same wall.

Layout, where n = row count and V = vendor count:

    meta.json        dictionaries, counts, digests -- see build_site.py
    cols.i32.bin     vendorIdx[n], begin[n], end[n]            (4 bytes each)
    cols.f64.bin     amount[n]                                 (8 bytes each)
    cols.u8.bin      status[n], entity[n], type[n], adnIdx[n],
                     viewPresent[n], docLen[n]                 (1 byte each)
    docs.bin         document numbers, packed, no separators
    vendors.bin      len[V] as u8, then vendor names packed as UTF-8
    vtok.bin         units[V] as u8, then V tokens packed as raw bytes
    tok/NNNNN.bin    per block: DN bytes then view bytes, TOKEN_BYTES each

Every section holds exactly n (or V) elements, so a section's offset is just
`n * itemsize * sectionIndex`. No header, no offset table, no padding, and
nothing to get wrong except the row count -- which meta.count states and the
reader checks against the file length. Grouping by item size is what makes
that work: a file of one item size needs no alignment padding, and a fetched
ArrayBuffer always starts aligned at offset 0.

Little-endian is assumed. Every browser this will run in is little-endian,
but meta records it and the page asserts it rather than rendering whatever
byte order it finds as dollar amounts.
"""

import array
import json
import os

FORMAT_VERSION = 1

# Rows per token block. 2048 rows is 64 KiB of tokens -- one fetch per click,
# fast enough not to be felt, while keeping the file count in the hundreds.
BLOCK_ROWS = 2048

# Decoded length of a DN or view token. Measured across all 738,195 rows: every
# view token and all but three DN tokens decode to exactly this. The three
# exceptions travel in meta.dnExceptions rather than widening every record.
TOKEN_BYTES = 16

META = "meta.json"
COLS_I32 = "cols.i32.bin"
COLS_F64 = "cols.f64.bin"
COLS_U8 = "cols.u8.bin"
DOCS = "docs.bin"
VENDORS = "vendors.bin"
VTOK = "vtok.bin"
SELFTEST = "selftest.json"
TOK_DIR = "tok"
DESC_DIR = "desc"
WORDS = "words.bin"
POSTINGS = "postings.bin"

# What the page loads before it can render anything, in the order it needs them.
RESIDENT = (COLS_I32, COLS_F64, COLS_U8, DOCS, VENDORS)

# Section order within each column file. The names are the keys used by
# write_payload/read_payload, and the order here IS the on-disk order.
I32_COLUMNS = ("vendorIdx", "begin", "end")
F64_COLUMNS = ("amount",)
U8_COLUMNS = ("status", "entity", "type", "adnIdx", "viewPresent", "docLen")

_TYPECODE = {4: "i", 8: "d", 1: "B"}


def block_path(outdir, block):
    return os.path.join(outdir, TOK_DIR, f"{block:05d}.bin")


def desc_path(outdir, block):
    return os.path.join(outdir, DESC_DIR, f"{block:05d}.bin")


def block_count(n):
    return (n + BLOCK_ROWS - 1) // BLOCK_ROWS


def _write_columns(path, names, columns, itemsize, n):
    with open(path, "wb") as f:
        for name in names:
            col = columns[name]
            if len(col) != n:
                raise ValueError(f"column {name!r} has {len(col)} items, expected {n}")
            if col.itemsize != itemsize:
                raise ValueError(f"column {name!r} is {col.itemsize}-byte, expected {itemsize}")
            f.write(col.tobytes())


def _write_packed(path, lengths, blobs):
    """A u8 length per item, then every item's bytes back to back."""
    with open(path, "wb") as f:
        f.write(bytes(lengths))
        for b in blobs:
            f.write(b)


def write_payload(outdir, columns, docs, vendors, vtokens, meta):
    """Write every file the page loads. `columns` maps name -> array.array.

    Returns the meta actually written, which gains a `bytes` map this function
    fills in from the files on disk -- so meta.json is written last.
    """
    n = meta["count"]
    os.makedirs(os.path.join(outdir, TOK_DIR), exist_ok=True)

    _write_columns(os.path.join(outdir, COLS_I32), I32_COLUMNS, columns, 4, n)
    _write_columns(os.path.join(outdir, COLS_F64), F64_COLUMNS, columns, 8, n)
    _write_columns(os.path.join(outdir, COLS_U8), U8_COLUMNS, columns, 1, n)

    # docLen already carries each document number's length, so docs.bin is
    # nothing but the bytes.
    with open(os.path.join(outdir, DOCS), "wb") as f:
        for d in docs:
            f.write(d)

    _write_packed(os.path.join(outdir, VENDORS), [len(v) for v in vendors], vendors)
    # V tokens are 16, 32, 48 or 64 bytes, so store the length in units of
    # TOKEN_BYTES and a u8 still covers far more than occurs.
    _write_packed(os.path.join(outdir, VTOK), [len(v) // TOKEN_BYTES for v in vtokens], vtokens)

    # Decompressed size per file. The page cannot ask the network for these:
    # Pages gzips every one of them, so Content-Length is the compressed length
    # while the reader yields decompressed bytes. Stating them here is what lets
    # the loader show a progress bar that means something.
    meta = dict(meta)
    meta["bytes"] = {name: os.path.getsize(os.path.join(outdir, name))
                     for name in RESIDENT + (VTOK,)}

    with open(os.path.join(outdir, META), "w", encoding="utf-8") as f:
        json.dump(meta, f, separators=(",", ":"), sort_keys=True)
    return meta


def write_token_blocks(outdir, dn, view, n):
    """Blocks of BLOCK_ROWS rows: all DN bytes, then all view bytes.

    A row with no view token gets TOKEN_BYTES of zero. Nothing reads those --
    the resident viewPresent column is the authority on whether a view URL
    exists -- but zeroing keeps the stride fixed, which is the whole point.
    """
    # Creates its own directory. It used to depend on write_payload having made
    # it first, which worked only because of call order in build_site.py and
    # failed the moment anything wrote token blocks on their own.
    os.makedirs(os.path.join(outdir, TOK_DIR), exist_ok=True)
    blank = b"\x00" * TOKEN_BYTES
    for b in range(block_count(n)):
        lo = b * BLOCK_ROWS
        hi = min(lo + BLOCK_ROWS, n)
        with open(block_path(outdir, b), "wb") as f:
            for i in range(lo, hi):
                f.write(dn[i][:TOKEN_BYTES] if dn[i] else blank)
            for i in range(lo, hi):
                f.write(view[i] if view[i] else blank)


# Descriptions the state itself wrote into each document (see
# scripts/extract_scope.py). They are 39 MB of text -- more than five times the
# whole resident payload -- so they can never be loaded up front, and they are
# deliberately kept out of every resident file: a page that has not been taught
# about them reads the build exactly as before, which is why FORMAT_VERSION
# does not move.
#
# Blocked on the same BLOCK_ROWS boundary as the tokens, so one row's block
# number is its block number everywhere. Each block is self-describing -- a u16
# length per row, then the packed UTF-8 -- so nothing resident has to say which
# rows have a description. Zero length means none, which is also what a row the
# state published no readable file for gets.
DESC_LENGTH = "H"  # u16; extract_scope caps a description far below 65535


def write_desc_blocks(outdir, descriptions, n):
    """Write one block per BLOCK_ROWS rows. `descriptions` maps row -> bytes."""
    os.makedirs(os.path.join(outdir, DESC_DIR), exist_ok=True)
    for b in range(block_count(n)):
        lo = b * BLOCK_ROWS
        hi = min(lo + BLOCK_ROWS, n)
        chunk = [descriptions.get(i, b"") for i in range(lo, hi)]
        for text in chunk:
            if len(text) > 65535:
                raise ValueError(f"a description is {len(text)} bytes; the length is a u16")
        lengths = array.array(DESC_LENGTH, [len(text) for text in chunk])
        with open(desc_path(outdir, b), "wb") as f:
            f.write(lengths.tobytes())
            for text in chunk:
                f.write(text)


def read_desc_blocks(outdir, n):
    """Every row's description bytes, reassembled from the blocks."""
    out = [b""] * n
    for b in range(block_count(n)):
        lo = b * BLOCK_ROWS
        hi = min(lo + BLOCK_ROWS, n)
        rows = hi - lo
        data = open(desc_path(outdir, b), "rb").read()
        header = rows * 2
        if len(data) < header:
            raise ValueError(f"description block {b}: {len(data)} bytes, header alone is {header}")
        lengths = array.array(DESC_LENGTH)
        lengths.frombytes(data[:header])
        pos = header
        for i, length in enumerate(lengths):
            out[lo + i] = data[pos:pos + length]
            pos += length
        if pos != len(data):
            raise ValueError(f"description block {b}: {len(data) - pos} bytes past the last row")
    return out


# The search index over those descriptions. It stores which rows contain a
# word, never the words of a row -- the text itself always comes from the
# blocks above, so nothing here can distort what a reader sees.
#
#   words.bin      every distinct word, sorted, newline-separated. Sorted so
#                  the page can binary-search it, and so a prefix query is a
#                  contiguous run rather than a scan of 267,000 entries.
#   postings.bin   per word, in the same order: the number of rows, then each
#                  row id as a delta from the one before it, varint-encoded.
#                  Deltas because sorted row ids differ by far less than they
#                  are worth, and varints because most of those deltas fit in
#                  one byte.
#
# There is deliberately no offset table. The page reads postings.bin once,
# start to finish, building whatever lookup it wants in memory -- an offset
# array would add 1 MB to the download to save work already being done.
def _varint(value):
    out = bytearray()
    while True:
        seven = value & 0x7F
        value >>= 7
        out.append(seven | (0x80 if value else 0))
        if not value:
            return bytes(out)


def write_index(outdir, postings):
    """`postings` maps word -> sorted row ids. Written in sorted word order."""
    words = sorted(postings)
    with open(os.path.join(outdir, WORDS), "wb") as f:
        f.write("\n".join(words).encode("utf-8"))
    with open(os.path.join(outdir, POSTINGS), "wb") as f:
        for word in words:
            rows = postings[word]
            blob = bytearray(_varint(len(rows)))
            previous = 0
            for row in rows:
                blob += _varint(row - previous)
                previous = row
            f.write(blob)
    return words


def read_index(outdir):
    """word -> row ids, decoded from the two files alone."""
    with open(os.path.join(outdir, WORDS), encoding="utf-8") as f:
        words = f.read().split("\n")
    blob = open(os.path.join(outdir, POSTINGS), "rb").read()

    at = 0

    def take():
        nonlocal at
        value = shift = 0
        while True:
            byte = blob[at]
            at += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7

    out = {}
    for word in words:
        rows, previous = [], 0
        for _ in range(take()):
            previous += take()
            rows.append(previous)
        out[word] = rows
    if at != len(blob):
        raise ValueError(f"{POSTINGS}: {len(blob) - at} bytes past the last word")
    return out


def _read_columns(path, names, itemsize, n):
    """Read back by computing each section's offset from the spec."""
    expected = n * itemsize * len(names)
    actual = os.path.getsize(path)
    if actual != expected:
        raise ValueError(f"{path}: {actual} bytes, expected {expected} "
                         f"({len(names)} sections x {n} x {itemsize})")
    out = {}
    with open(path, "rb") as f:
        for i, name in enumerate(names):
            f.seek(n * itemsize * i)
            col = array.array(_TYPECODE[itemsize])
            col.frombytes(f.read(n * itemsize))
            out[name] = col
    return out


def _read_packed(path, count, scale=1):
    with open(path, "rb") as f:
        lengths = list(f.read(count))
        if len(lengths) != count:
            raise ValueError(f"{path}: expected {count} lengths, got {len(lengths)}")
        blob = f.read()
    expected = sum(lengths) * scale
    if len(blob) != expected:
        raise ValueError(f"{path}: payload is {len(blob)} bytes, expected {expected}")
    out, pos = [], 0
    for ln in lengths:
        size = ln * scale
        out.append(blob[pos:pos + size])
        pos += size
    return out


def read_payload(outdir):
    """Decode everything written above, from the files alone.

    Deliberately written against the layout as documented rather than by
    reusing the writer's internals: this is the decoder that build_site.py's
    verification runs against, so it has to be able to disagree with the
    encoder.
    """
    with open(os.path.join(outdir, META), encoding="utf-8") as f:
        meta = json.load(f)
    n = meta["count"]
    v = meta["vendorCount"]

    columns = {}
    columns.update(_read_columns(os.path.join(outdir, COLS_I32), I32_COLUMNS, 4, n))
    columns.update(_read_columns(os.path.join(outdir, COLS_F64), F64_COLUMNS, 8, n))
    columns.update(_read_columns(os.path.join(outdir, COLS_U8), U8_COLUMNS, 1, n))

    doc_len = columns["docLen"]
    blob = open(os.path.join(outdir, DOCS), "rb").read()
    if len(blob) != sum(doc_len):
        raise ValueError(f"{DOCS}: {len(blob)} bytes, expected {sum(doc_len)} from docLen")
    docs, pos = [], 0
    for ln in doc_len:
        docs.append(blob[pos:pos + ln])
        pos += ln

    vendors = _read_packed(os.path.join(outdir, VENDORS), v)
    vtokens = _read_packed(os.path.join(outdir, VTOK), v, scale=TOKEN_BYTES)

    return columns, docs, vendors, vtokens, meta


def read_token_blocks(outdir, n):
    """DN and view bytes per row, reassembled from the blocks."""
    dn = [None] * n
    view = [None] * n
    for b in range(block_count(n)):
        lo = b * BLOCK_ROWS
        hi = min(lo + BLOCK_ROWS, n)
        rows = hi - lo
        data = open(block_path(outdir, b), "rb").read()
        if len(data) != rows * TOKEN_BYTES * 2:
            raise ValueError(f"block {b}: {len(data)} bytes, expected {rows * TOKEN_BYTES * 2}")
        for i in range(rows):
            off = i * TOKEN_BYTES
            dn[lo + i] = data[off:off + TOKEN_BYTES]
            off = rows * TOKEN_BYTES + i * TOKEN_BYTES
            view[lo + i] = data[off:off + TOKEN_BYTES]
    return dn, view
