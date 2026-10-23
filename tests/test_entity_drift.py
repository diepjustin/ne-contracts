"""check_entity_drift.py warns by default and fails only under --strict.

It runs in daily.yml before the guard rail and the deploy. It used to exit 1 on any change
to the state's agency list and on any network error, so one new agency, or one dropped
connection, stopped the site updating until someone edited state_entities.json (audit
sc-02). Nothing here touches the network: fetch_entities is replaced.
"""

import json
import os
import sys

import pytest
import requests

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import check_entity_drift as drift  # noqa: E402
import scrape  # noqa: E402

with open(drift.STATE_ENTITIES_JSON, encoding="utf-8") as _f:
    COMMITTED_STATE = json.load(_f)


@pytest.fixture
def state(monkeypatch):
    """Script the site: `answers` is a list per query type, consumed one per request;
    an exception instance in it is raised instead."""
    asked = []
    monkeypatch.setattr(drift.time, "sleep", lambda _s: None)

    def script(state_answers, higher_ed_answers=None):
        queues = {"State": list(state_answers),
                  "Higher Education": list(higher_ed_answers or [dict(scrape.HIGHER_ED_ENTITIES)])}

        def fetch(query_type):
            asked.append(query_type)
            answer = queues[query_type].pop(0) if len(queues[query_type]) > 1 else queues[query_type][0]
            if isinstance(answer, Exception):
                raise answer
            return dict(answer)

        monkeypatch.setattr(drift, "fetch_entities", fetch)
        return asked
    return script


def with_new_agency():
    return dict(COMMITTED_STATE, **{"Brand New Commission": "Brand New Commission-99-001"})


def test_no_drift_exits_zero_either_way(state, capsys):
    state([COMMITTED_STATE])
    assert drift.main([]) == 0
    assert drift.main(["--strict"]) == 0
    assert "::warning" not in capsys.readouterr().out


def test_a_new_agency_warns_and_exits_zero_by_default(state, capsys):
    state([with_new_agency()])
    assert drift.main([]) == 0
    out = capsys.readouterr().out
    assert ("::warning title=Entity list drift::State: the state added 'Brand New Commission'"
            in out)
    assert sum(line.startswith("::warning") for line in out.splitlines()) == 1, "one annotation per change"


def test_strict_fails_on_drift(state):
    state([with_new_agency()])
    assert drift.main(["--strict"]) == 1


def test_a_network_error_is_retried_once_and_a_second_success_is_drift_free(state, capsys):
    asked = state([requests.exceptions.ConnectionError("down"), COMMITTED_STATE])
    assert drift.main(["--strict"]) == 0
    assert asked.count("State") == 2
    assert "::warning" not in capsys.readouterr().out


def test_a_network_error_twice_warns_and_exits_zero(state, capsys):
    asked = state([requests.exceptions.ConnectionError("state endpoint down")])
    assert drift.main([]) == 0
    assert asked.count("State") == 2, "one retry, no more"
    out = capsys.readouterr().out
    assert "::warning title=Entity list drift::State: could not ask the state" in out
    assert "state endpoint down" in out


def test_a_network_error_twice_fails_under_strict(state):
    state([requests.exceptions.Timeout("slow")])
    assert drift.main(["--strict"]) == 1


def test_an_empty_answer_is_a_failure_not_every_agency_removed(state, capsys):
    state([{}])
    assert drift.main([]) == 0
    out = capsys.readouterr().out
    assert "REMOVED" not in out
    assert "could not ask the state" in out and "broken answer" in out
    state([{}])
    assert drift.main(["--strict"]) == 1


def test_main_ignores_the_callers_argv(state, monkeypatch):
    """Under pytest sys.argv holds pytest's own arguments; main([]) must not read them."""
    state([COMMITTED_STATE])
    monkeypatch.setattr(sys, "argv", ["pytest", "--strict-markers", "-q"])
    assert drift.main([]) == 0
