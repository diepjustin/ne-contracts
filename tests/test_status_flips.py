"""Refusing to expire contracts on the strength of an empty answer.

On 17-18 Aug 2026 the state's search returned "No results found" for every
entity for two days. `scrape_entity` treats an empty first page as a clean
finish, so `--daily` saw zero active records, computed `known - seen` as
everything, and marked all 44,063 active records in the database Expired in a
single run. Every step succeeded; the run was green.

It could not repair itself either -- `--daily` only ever flips Active to
Expired, so the damage was permanent until the data was rebuilt by hand.

These tests pin the rule that a flip must be evidence of an ending rather than
of an absence, and -- just as important -- that ordinary expiries still happen.
"""

import os
import sys

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import scrape  # noqa: E402


def decide(known, seen):
    """The flip decision as run_daily makes it: (should_flip, vanished)."""
    vanished = known - seen
    share = len(vanished) / len(known) if known else 0
    if known and not seen:
        return False, vanished
    if len(known) >= 20 and share > 0.5:
        return False, vanished
    return True, vanished


def urls(*names):
    return {f"https://example.test/detail?D={n}" for n in names}


def test_an_empty_answer_never_expires_anything():
    """The 17 Aug shape: the entity had active contracts and returned nothing."""
    known = urls(*range(50))
    flip, _ = decide(known, set())
    assert flip is False


def test_a_wholesale_disappearance_is_refused():
    """A partial outage: most records missing at once is not 900 endings."""
    known = urls(*range(100))
    seen = urls(*range(10))
    flip, vanished = decide(known, seen)
    assert flip is False
    assert len(vanished) == 90


def test_an_ordinary_expiry_still_flips():
    """The guard must not freeze the database: a few contracts ending is normal
    and is the entire point of --daily."""
    known = urls(*range(100))
    seen = urls(*range(3, 100))
    flip, vanished = decide(known, seen)
    assert flip is True
    assert len(vanished) == 3


def test_a_small_entity_losing_everything_is_still_allowed():
    """Below the threshold, a genuine clear-out must not be blocked -- a body
    with four contracts can legitimately end all four."""
    known = urls(*range(4))
    seen = urls(0)
    flip, vanished = decide(known, seen)
    assert flip is True
    assert len(vanished) == 3


def test_nothing_known_means_nothing_to_flip():
    flip, vanished = decide(set(), set())
    assert vanished == set()
    assert flip is True


def test_the_guard_thresholds_are_the_ones_in_scrape():
    """Guard against the test drifting from the code it claims to pin."""
    source = open(os.path.join(SCRIPTS, "scrape.py"), encoding="utf-8").read()
    assert "if known and not seen:" in source
    assert "len(known) >= 20 and share > 0.5" in source
