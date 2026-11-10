"""The Agency box's other names must hang off agencies the scraper collects.

index.html carries AGENCY_ALIASES: what people call an agency when it is not what the
state's list calls it ("dot" for "Roads, Department of"). A key is matched against the
page's agency list by exact string, so one misspelled key, or one the state has since
renamed, is an alias that silently finds nothing, and nothing on the page would say so.

There is no browser in this suite. What the box does with the table is checked by
scripts/check_page.cjs; this reads the table out of the page and checks it against the
two lists the scraper works from.
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import scrape  # noqa: E402

with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as _f:
    PAGE = _f.read()
with open(os.path.join(ROOT, "scripts", "state_entities.json"), encoding="utf-8") as _f:
    AGENCIES = set(json.load(_f)) | set(scrape.HIGHER_ED_ENTITIES)


def aliases(page=PAGE):
    m = re.search(r"var AGENCY_ALIASES = (\{.*?\n  \});", page, re.S)
    assert m, "AGENCY_ALIASES not found in index.html"
    # Plain JSON on purpose, so this needs no JavaScript parser.
    return json.loads(m.group(1))


def plain(text):
    """plainWords() in index.html: lowercase words, '&' read as 'and'."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower().replace("&", " and ")).strip()


def test_every_alias_belongs_to_an_agency_the_scraper_collects():
    unknown = sorted(set(aliases()) - AGENCIES)
    assert not unknown, "aliases for agencies that are not in the scraper's lists: %s" % unknown


def test_every_agency_has_at_least_one_alias_and_each_is_a_name():
    for agency, names in aliases().items():
        assert isinstance(names, list) and names, agency
        for name in names:
            assert isinstance(name, str) and plain(name), (agency, name)
            assert name == name.strip(), (agency, name)


def test_no_alias_repeats_what_the_agency_name_already_answers_to():
    # The name is tried first, by every typed word being somewhere in it. An alias
    # whose words are all in the name can never be the reason an agency is offered.
    for agency, names in aliases().items():
        own = plain(agency)
        for name in names:
            assert not all(word in own for word in plain(name).split()), (
                "%r is already found by its own name %r" % (name, agency))


def test_no_agency_lists_the_same_alias_twice():
    for agency, names in aliases().items():
        folded = [plain(n) for n in names]
        assert len(folded) == len(set(folded)), agency


def test_the_names_reporters_tried_and_missed_are_covered():
    # Typed into the box on 9 Nov 2026, every one of these found nothing.
    table = aliases()
    for typed, agency in [("DOT", "Roads, Department of"),
                          ("Transportation, Department of", "Roads, Department of"),
                          ("DHHS", "Health & Human Services, Department of"),
                          ("UNL", "University of Nebraska Lincoln"),
                          ("UNMC", "University of Nebraska Medical Center")]:
        assert typed in table[agency], (typed, agency)


def test_the_reader_is_guarded_against_a_key_that_is_not_json():
    # The parser above is the guard for everything else here: a trailing comma or a
    # comment inside the table must fail loudly, not be read as an empty table.
    broken = PAGE.replace('"Roads, Department of": [', '"Roads, Department of": [ // was NDOR\n', 1)
    assert broken != PAGE
    try:
        aliases(broken)
    except ValueError:
        return
    raise AssertionError("a comment inside AGENCY_ALIASES was read as JSON")
