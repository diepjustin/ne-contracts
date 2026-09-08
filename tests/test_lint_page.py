"""The inline-script extraction that lets eslint report index.html line numbers.

The lint run itself needs Node and the network, so it lives in CI. What is
tested here is the only part that can be quietly wrong: a report saying line
1468 has to mean line 1468 of index.html, or the linter costs more time than it
saves.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import lint_page  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def test_body_lands_on_its_own_line_numbers():
    html = "<html>\n<body>\n<script>\nvar a = 1;\nvar b = 2;\n</script>\n</html>\n"
    js = lint_page.as_padded_js(html).split("\n")
    assert js[3] == "var a = 1;"    # line 4 of the html, 0-based here
    assert js[4] == "var b = 2;"
    assert js[0] == "" and js[1] == "" and js[2] == ""


def test_markup_is_not_carried_into_the_javascript():
    html = "<script>\nvar a = 1;\n</script>\n<p>not code</p>\n"
    js = lint_page.as_padded_js(html)
    assert "<p>" not in js and "script" not in js


def test_external_scripts_are_skipped():
    html = '<script src="x.js"></script>\n<script>\nvar a = 1;\n</script>\n'
    assert [start for start, _ in lint_page.extract(html)] == [2]


def test_two_inline_blocks_both_land():
    html = "<script>\nvar a = 1;\n</script>\n<script>\nvar b = 2;\n</script>\n"
    js = lint_page.as_padded_js(html).split("\n")
    assert js[1] == "var a = 1;"
    assert js[4] == "var b = 2;"


def test_the_real_page_lines_up():
    with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as fh:
        html = fh.read()
    page = html.split("\n")
    js = lint_page.as_padded_js(html).split("\n")
    # Every line the extraction kept must be that same line of index.html.
    kept = [i for i, line in enumerate(js) if line.strip()]
    assert kept, "no script body extracted from index.html"
    for i in kept:
        assert js[i] == page[i], "line %d drifted" % (i + 1)
