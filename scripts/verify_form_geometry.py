"""Check a form parser's output against the PDF's own geometry.

`extract_scope.from_cover_sheet_form` reads doc_text.jsonl, where a flattened
PDF form has already lost every coordinate: labels arrive in one run, values in
another, and only position identifies the description. That is precisely the
kind of assumption that silently attributes one contract's words to another.

So this asks an independent question. It downloads the real PDF and looks at
where the ink actually is: what text sits to the right of, and level with, the
"DESCRIPTION OF PURCHASE" label? If that agrees with what the parser claimed,
the positional assumption held for that document.

Checking a sample this way found three defects a passing test suite did not --
two blank description fields whose term dates had slid into place, and one
document that extracted as control characters where the page reads "travel".

Needs pymupdf (`pip install pymupdf`), which is NOT in requirements.txt: this
is a verification tool, not part of the build. It downloads one PDF per sampled
document from the state's server, so keep samples small and infrequent.

    python scripts/verify_form_geometry.py candidates.json 60

where candidates.json is [[view_token, claimed_description], ...].
"""
import io, json, sys, time, random
import requests, fitz

BASE = "https://statecontracts.nebraska.gov/Search/ViewDocument?D="

def lines(page):
    out = []
    for blk in page.get_text("dict")["blocks"]:
        for ln in blk.get("lines", []):
            txt = "".join(s["text"] for s in ln["spans"]).strip()
            if txt:
                x0, y0, x1, y1 = ln["bbox"]
                out.append({"x": x0, "y": (y0 + y1) / 2, "t": txt})
    return out

def description_by_geometry(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    ls = lines(doc[0])
    label = next((l for l in ls if l["t"].upper().startswith("DESCRIPTION OF")), None)
    if not label:
        return None
    # The label wraps onto a second line ("PURCHASE"); the value box spans both,
    # so accept anything whose centre falls within that band, right of the label.
    band_lo, band_hi = label["y"] - 6, label["y"] + 20
    right = [l for l in ls if l["x"] > label["x"] + 60 and band_lo <= l["y"] <= band_hi]
    right.sort(key=lambda l: (l["y"], l["x"]))
    return " ".join(l["t"] for l in right) or ""

if __name__ == "__main__":
    cases = json.load(open(sys.argv[1]))
    random.seed(23)
    sample = random.sample(cases, min(int(sys.argv[2]), len(cases)))
    agree = differ = missing = failed = 0
    for tok, claimed in sample:
        try:
            r = requests.get(BASE + tok, timeout=60)
            if not r.content.startswith(b"%PDF-"):
                failed += 1; continue
            geo = description_by_geometry(r.content)
        except Exception as e:
            failed += 1; continue
        if geo is None:
            missing += 1; continue
        a, b = " ".join(geo.split()).lower(), " ".join(claimed.split()).lower()
        if a == b or (a and b and (a.startswith(b[:40]) or b.startswith(a[:40]))):
            agree += 1
        else:
            differ += 1
            print(f"  MISMATCH {tok[:14]}")
            print(f"    parser  : {claimed[:90]!r}")
            print(f"    geometry: {geo[:90]!r}")
        time.sleep(0.2)
    n = agree + differ
    print(f"\nagree {agree}/{n}" + (f"  ({100*agree/n:.1f}%)" if n else "") +
          f";  no label {missing};  fetch failed {failed}")
