"""The README's references to things outside its own text must resolve.

Both of these rotted without a test noticing. The page footer deep-links to the
"Caveats worth knowing before you quote it" heading as its provenance statement,
so renaming that heading breaks a link on the published page. And the README sent
a person recovering from an outage to workflow files that had not existed since
the repository was split (`ne-contracts-rescue-cache.yml` is a 404), at the one
moment the runbook matters. Nothing here touches the network.
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


README = read("README.md")
INDEX = read("index.html")

# Workflows the README names that belong to other repositories, on purpose: it
# cites them to rule out a suspect for the scheduler's lateness.
OTHER_REPOSITORIES = {"unl-events-nightly.yml", "husker-markets-collect.yml"}


def slug(heading):
    """GitHub's anchor for a heading: lower-cased, punctuation dropped, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def anchors(markdown):
    """Every anchor the headings of `markdown` produce, repeats numbered the way GitHub does."""
    seen, found, fenced = {}, set(), False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        heading = None if fenced else re.match(r"#{1,6}\s+(.*?)\s*#*\s*$", line)
        if heading:
            base = slug(heading.group(1))
            count = seen.get(base, 0)
            seen[base] = count + 1
            found.add(base if count == 0 else "%s-%d" % (base, count))
    return found


# --- the slug rules, against headings whose anchors are known -----------------------------

def test_slugs_match_the_anchors_github_makes():
    assert slug("Caveats worth knowing before you quote it") == "caveats-worth-knowing-before-you-quote-it"
    assert slug("Generated and OCR'd content") == "generated-and-ocrd-content"
    assert slug("What `--daily` does (and doesn't)") == "what---daily-does-and-doesnt"
    assert slug("A & B: the \"C\" case") == "a--b-the-c-case"


def test_headings_inside_code_fences_are_not_headings():
    text = "# Real\n\n```bash\n# a comment, not a heading\n```\n\n## Second\n"
    assert anchors(text) == {"real", "second"}


def test_repeated_headings_are_numbered():
    assert anchors("## Notes\n\n## Notes\n\n## Notes\n") == {"notes", "notes-1", "notes-2"}


# --- anchors ------------------------------------------------------------------------------

def test_every_readme_anchor_the_page_links_to_exists():
    linked = set(re.findall(r"README\.md#([\w\-]+)", INDEX))
    assert linked, "index.html no longer links into the README; if that is intended, drop this test"
    missing = sorted(linked - anchors(README))
    assert not missing, "index.html links to README headings that do not exist: %s" % missing


def test_every_link_inside_the_readme_to_one_of_its_own_headings_resolves():
    linked = set(re.findall(r"\]\(#([\w\-]+)\)", README))
    assert linked, "no in-README links left; if that is intended, drop this test"
    missing = sorted(linked - anchors(README))
    assert not missing, "README links to headings it does not have: %s" % missing


# --- workflows -----------------------------------------------------------------------------

def test_every_workflow_the_readme_names_exists():
    workflows = set(os.listdir(os.path.join(ROOT, ".github", "workflows")))
    named = set(re.findall(r"[\w\-]+\.yml", README)) - OTHER_REPOSITORIES
    assert named, "the README no longer names a workflow; if that is intended, drop this test"
    missing = sorted(named - workflows)
    assert not missing, "README names workflow file(s) that are not in .github/workflows: %s" % missing


def test_the_other_repository_allowlist_stays_honest():
    """An entry that is now a real workflow here, or no longer mentioned, should go."""
    workflows = set(os.listdir(os.path.join(ROOT, ".github", "workflows")))
    for name in OTHER_REPOSITORIES:
        assert name not in workflows, "%s exists here now; remove it from OTHER_REPOSITORIES" % name
        assert name in README, "README no longer mentions %s; remove it from OTHER_REPOSITORIES" % name
