"""Every outbound request identifies the project.

The scraper spent a year telling a state government server it was
"Mozilla/5.0 (compatible; research scraper)" -- anonymous, and in the Mozilla
part not true. A full run is 20+ hours of traffic; an administrator who notices
it can either find the operator or block the client, and the first should not
take detective work.

These are cheap tests for a thing that is easy to undo by accident: a new
script, or a copy-pasted header, and half the traffic goes back to being
unattributable. Nothing here makes a network call.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import scrape  # noqa: E402

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")


def test_the_agent_names_the_project_and_where_to_find_it():
    ua = scrape.USER_AGENT
    assert "ne-contracts" in ua
    assert "https://diepjustin.github.io/ne-contracts/" in ua
    assert "Mozilla" not in ua


def test_the_scraper_sends_it():
    assert scrape.build_session().headers["User-Agent"] == scrape.USER_AGENT


def test_the_extractor_sends_it():
    import extract_text
    assert extract_text.build_session().headers["User-Agent"] == scrape.USER_AGENT


def test_nothing_carries_a_second_hand_written_agent():
    """One definition. A literal anywhere else is a copy waiting to go stale."""
    pattern = re.compile(r'["\']User-Agent["\']\s*:\s*["\']')
    offenders = []
    for name in sorted(os.listdir(SCRIPTS)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(SCRIPTS, name), encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                if pattern.search(line):
                    offenders.append("%s:%d %s" % (name, n, line.strip()))
    assert not offenders, "hard-coded User-Agent; import scrape.USER_AGENT:\n" + "\n".join(offenders)


def test_every_script_that_fetches_says_who_it_is():
    """A module making requests without reaching for USER_AGENT is unattributed.

    Deliberately a source scan rather than a mock: the failure this guards
    against is a *new* fetch site, which no existing test would ever call.
    """
    fetches = re.compile(r"\brequests\.(get|post)\(|requests\.Session\(\)")
    missing = []
    for name in sorted(os.listdir(SCRIPTS)):
        if not name.endswith(".py") or name == "serve_site.py":
            continue
        with open(os.path.join(SCRIPTS, name), encoding="utf-8") as fh:
            src = fh.read()
        if fetches.search(src) and "USER_AGENT" not in src:
            missing.append(name)
    assert not missing, "makes requests without USER_AGENT: %s" % ", ".join(missing)
