"""Every outbound request has a timeout.

requests waits forever by default. One call without a timeout (get_token, which
runs before every search) meant a half-open connection to the state could hold
the nightly job for the whole of its 180-minute budget with no output: the other
fourteen network calls in scripts/ all passed one. This is a source scan, like
test_user_agent.py, because the failure it guards against is a *new* fetch site
that no existing test would ever call. Nothing here makes a network call.
"""

import ast
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import scrape  # noqa: E402

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "request"}


def is_http_call(node):
    """requests.get(...), <something session>.post(...), urlopen(...) -- and not
    resp.headers.get(...), which is a dictionary lookup that merely looks like one."""
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr in HTTP_METHODS:
            receiver = ast.unparse(func.value)
            return receiver == "requests" or "session" in receiver.lower()
        return func.attr == "urlopen"
    return isinstance(func, ast.Name) and func.id == "urlopen"


def calls_without_timeout(source, filename="<source>"):
    found = []
    for node in ast.walk(ast.parse(source, filename)):
        if isinstance(node, ast.Call) and is_http_call(node):
            if not any(kw.arg == "timeout" for kw in node.keywords):
                found.append((node.lineno, ast.unparse(node)[:80]))
    return found


# --- the scanner itself: a guard is only worth what its detector is -------------

def test_the_scanner_flags_a_request_with_no_timeout():
    assert calls_without_timeout("session.get(URL)")
    assert calls_without_timeout("requests.post(URL, data=d)")
    assert calls_without_timeout("detail_session().get(url)")
    assert calls_without_timeout("urllib.request.urlopen(req)")


def test_the_scanner_accepts_a_request_with_one():
    assert not calls_without_timeout("session.get(URL, timeout=30)")
    assert not calls_without_timeout("urllib.request.urlopen(req, timeout=5)")


def test_the_scanner_ignores_dictionary_lookups_that_look_like_requests():
    assert not calls_without_timeout("resp.headers.get('Content-Type')")
    assert not calls_without_timeout("config.get('key', None)")


# --- the scripts ------------------------------------------------------------------

def test_every_network_call_in_scripts_has_a_timeout():
    offenders = []
    for name in sorted(os.listdir(SCRIPTS)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(SCRIPTS, name), encoding="utf-8") as fh:
            for line, call in calls_without_timeout(fh.read(), name):
                offenders.append("%s:%d %s" % (name, line, call))
    assert not offenders, "network call(s) with no timeout:\n" + "\n".join(offenders)


# --- get_token specifically ------------------------------------------------------------

class _Page:
    text = '<input name="__RequestVerificationToken" value="abc">'

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self):
        self.kwargs = None

    def get(self, url, **kwargs):
        self.kwargs = kwargs
        return _Page()


def test_get_token_passes_its_timeout():
    session = _Session()
    assert scrape.get_token(session) == "abc"
    assert session.kwargs == {"timeout": scrape.TOKEN_TIMEOUT}


def test_the_token_timeout_is_short_next_to_the_search_timeout():
    """The form is a plain page. Anything near the search's five minutes would
    let a hung connection here cost as much as a hung search."""
    assert 0 < scrape.TOKEN_TIMEOUT <= 120
    assert scrape.TOKEN_TIMEOUT < scrape.SEARCH_TIMEOUT
