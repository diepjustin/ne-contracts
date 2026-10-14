"""The filter bar may only be sticky where it leaves room for the table.

`.controls` was `position: sticky` at every width. On a phone it wraps to
596-732px -- taller than the screen -- and fitScroller holds the table at
MIN_TABLE_H and lets the page scroll to reach it, so the pinned bar rode that
scroll down on top of the table. At 375x667, 390x664 and 320x568 not one
pixel of the table could be brought into view, and a row given focus sat
underneath the bar. Nothing failed: the data was right, the selftest
passed, and at desktop widths the page looked exactly as designed.

There is no browser in this suite, so this cannot prove what a phone shows;
the browser measurements in README.md "Things that bit us" are that proof.
What it can do is evaluate the page's own stylesheet the way the cascade does
for a given viewport, and refuse a change that puts the sticky bar back on a
phone or takes it away from the desktop it was designed for.
"""

import os
import re

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PAGE = os.path.join(ROOT, "index.html")

# Measured 29 Sep 2026 in Chrome on macOS: the bar's height at each width
# band above the page's 720px breakpoint, with the payload loaded. Re-measure
# (document.querySelector(".controls").offsetHeight) when the controls change.
BAR_HEIGHT_ABOVE_720 = [(721, 391), (768, 318), (1024, 242), (1366, 166)]


def read_page():
    with open(PAGE, encoding="utf-8") as fh:
        return fh.read()


def stylesheet(html):
    m = re.search(r"<style[^>]*>(.*?)</style>", html, re.S | re.I)
    assert m, "index.html has no <style> block"
    return re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)


def rules(css):
    """(media query or None, [selectors], declarations) in source order.

    Only the shapes this page uses: plain rules and @media one level deep.
    Anything else is refused rather than guessed at, so a new construct fails
    here loudly instead of being evaluated wrong.
    """
    out, i = [], 0
    while True:
        open_at = css.find("{", i)
        if open_at < 0:
            break
        prelude = css[i:open_at].strip()
        if prelude.startswith("@media"):
            depth, j = 1, open_at + 1
            while depth:
                depth += {"{": 1, "}": -1}.get(css[j], 0)
                j += 1
            query = prelude[len("@media"):].strip()
            for _, sels, decls in rules(css[open_at + 1:j - 1]):
                out.append((query, sels, decls))
            i = j
            continue
        assert not prelude.startswith("@"), "unhandled at-rule: " + prelude
        close_at = css.index("}", open_at)
        sels = [s.strip() for s in prelude.split(",")]
        out.append((None, sels, css[open_at + 1:close_at]))
        i = close_at + 1
    return out


def matches(query, width, height):
    """A media query list against a light-scheme viewport, in CSS px."""
    if query is None:
        return True
    for alternative in query.split(","):
        assert not re.search(r"\b(not|only|print)\b", alternative), (
            "unhandled media query: " + alternative)
        ok = True
        for cond in re.findall(r"\(([^)]*)\)", alternative):
            name, _, value = [p.strip() for p in cond.partition(":")]
            if name == "prefers-color-scheme":
                ok = ok and value == "light"
                continue
            m = re.fullmatch(r"(\d+(?:\.\d+)?)px", value)
            assert m, "unhandled media value: " + cond
            px = float(m.group(1))
            test = {"max-width": width <= px, "min-width": width >= px,
                    "max-height": height <= px, "min-height": height >= px}
            assert name in test, "unhandled media feature: " + cond
            ok = ok and test[name]
        if ok:
            return True
    return False


def page_rules(html=None):
    return rules(stylesheet(html if html is not None else read_page()))


def computed(prop, selector, width, height, parsed=None):
    """The last declaration of `prop` on `selector` that applies, as a browser
    would cascade it. Every rule naming .controls has the same specificity, so
    source order decides."""
    value = None
    for query, sels, decls in parsed if parsed is not None else page_rules():
        if selector not in sels or not matches(query, width, height):
            continue
        for decl in decls.split(";"):
            name, _, v = decl.partition(":")
            if name.strip() == prop:
                assert "!important" not in v, "the cascade here ignores !important"
                value = v.strip()
    return value


def min_table_h(html):
    m = re.search(r"var MIN_TABLE_H = (\d+);", html)
    assert m, "MIN_TABLE_H not found in index.html"
    return int(m.group(1))


# The viewports the bug was measured at, plus the common phone sizes around
# them, upright and on their side.
PHONES = [(320, 568), (320, 640), (375, 667), (390, 664), (390, 844),
          (414, 896), (430, 932)]
PHONES_LANDSCAPE = [(568, 320), (667, 375), (844, 390), (932, 430)]
# Where the sticky bar leaves the table its room, and should keep doing so.
ROOMY = [(768, 1024), (1024, 768), (1280, 800), (1440, 900), (1920, 1080)]


@pytest.mark.parametrize("width,height", PHONES + PHONES_LANDSCAPE)
def test_filter_bar_scrolls_away_on_phones(width, height):
    assert computed("position", ".controls", width, height) == "static"


@pytest.mark.parametrize("width,height", ROOMY)
def test_filter_bar_stays_pinned_where_it_fits(width, height):
    # The desktop and tablet behaviour the bar was designed for.
    assert computed("position", ".controls", width, height) == "sticky"
    assert computed("top", ".controls", width, height) == "12px"


def test_sticky_only_where_bar_and_minimum_table_both_fit():
    # The rule itself, rather than a list of devices: wherever the bar is
    # pinned, the viewport must hold its top offset, the bar, and the
    # shortest table fitScroller will draw. Walks every height at each
    # measured width band, so a threshold lowered past what the bar needs
    # fails here even if no named device sits in the gap.
    html = read_page()
    parsed = page_rules(html)
    floor = min_table_h(html)
    for width, bar in BAR_HEIGHT_ABOVE_720:
        for height in range(300, 1400, 5):
            if computed("position", ".controls", width, height, parsed) != "sticky":
                continue
            top = int(computed("top", ".controls", width, height, parsed)[:-len("px")])
            assert height >= top + bar + floor, (
                "sticky at %dx%d, where a %dpx bar leaves the table %dpx"
                % (width, height, bar, height - top - bar))


def test_the_evaluator_reads_media_queries_the_way_a_browser_does():
    # A guard is only worth what its own parser is. Pin the cases that decide
    # the answers above.
    assert matches("(max-width: 720px), (max-height: 700px)", 720, 2000)
    assert matches("(max-width: 720px), (max-height: 700px)", 2000, 700)
    assert not matches("(max-width: 720px), (max-height: 700px)", 721, 701)
    assert not matches("(prefers-color-scheme: dark)", 1280, 800)
    assert matches("(min-width: 600px) and (max-width: 900px)", 700, 500)
    assert not matches("(min-width: 600px) and (max-width: 900px)", 901, 500)
