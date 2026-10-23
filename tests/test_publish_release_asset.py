"""Publishing the Mac's AI summaries and descriptions, checked and without a gap.

Two halves. scripts/check_release_asset.py decides whether a file may replace
the published one. scripts/publish_release_asset.sh is run against a stand-in
`gh` that keeps releases in a directory and logs every asset list it moves
through, so the upload order -- never delete-then-upload -- is checked, not
assumed. Nothing here touches the network.
"""

import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import check_release_asset as check  # noqa: E402
import ocr_io  # noqa: E402
from build_site import AI_MODELS  # noqa: E402

MODEL = AI_MODELS[1]
BASH = "/bin/bash"


def ai(tok, summary="A contract.", model=MODEL, **extra):
    return dict({"tok": tok, "doc": "D-" + tok, "model": model, "unclear": False,
                 "summary": summary}, **extra)


def scope(tok, description="Road salt."):
    return {"tok": tok, "doc": "D-" + tok, "source": "line_items", "description": description,
            "items": []}


def jsonl(path, records, tail="\n"):
    text = "\n".join(json.dumps(r) for r in records)
    path.write_text(text + (tail if records else ""))
    return str(path)


# --- the checker ---------------------------------------------------------------------------

def run_check(kind, new, old=None, capsys=None):
    argv = [kind, new] + (["--published", old] if old else []) + ["--env"]
    return check.main(argv)


def test_a_growing_file_may_replace_the_published_one(tmp_path, capsys):
    old = jsonl(tmp_path / "old.jsonl", [ai("a"), ai("b")])
    new = jsonl(tmp_path / "new.jsonl", [ai("a"), ai("b"), ai("c"), {"tok": "d", "error": "x"}])
    assert run_check("ai-summaries", new, old) == 0
    out = capsys.readouterr().out
    assert "RECORDS=4" in out and "WITH_TEXT=3" in out
    assert "SHA256=" + hashlib.sha256(open(new, "rb").read()).hexdigest() in out


def test_fewer_records_is_refused(tmp_path, capsys):
    old = jsonl(tmp_path / "old.jsonl", [ai("a"), ai("b"), {"tok": "b", "error": "x"}])
    new = jsonl(tmp_path / "new.jsonl", [ai("a"), ai("b")])
    assert run_check("ai-summaries", new, old) == 1
    assert "fewer records" in capsys.readouterr().err


def test_a_lost_token_is_refused_even_when_the_count_grew(tmp_path, capsys):
    old = jsonl(tmp_path / "old.jsonl", [ai("a"), ai("b")])
    new = jsonl(tmp_path / "new.jsonl", [ai("a"), ai("c"), ai("d")])
    assert run_check("ai-summaries", new, old) == 1
    assert "missing from it, e.g. b" in capsys.readouterr().err


def test_lost_text_is_refused(tmp_path, capsys):
    old = jsonl(tmp_path / "old.jsonl", [ai("a"), ai("b")])
    new = jsonl(tmp_path / "new.jsonl", [ai("a"), ai("b", summary=None, unclear=True), ai("c")])
    assert run_check("ai-summaries", new, old) == 1
    assert "have text in the published copy" in capsys.readouterr().err


def test_a_model_the_build_does_not_list_is_refused(tmp_path, capsys):
    new = jsonl(tmp_path / "new.jsonl", [ai("a"), ai("b", model="llama3.3:70b")])
    assert run_check("ai-summaries", new) == 1
    assert "llama3.3:70b" in capsys.readouterr().err


def test_a_file_copied_mid_write_is_refused(tmp_path, capsys):
    new = jsonl(tmp_path / "new.jsonl", [ai("a"), ai("b")], tail="")
    assert run_check("ai-summaries", new) == 1
    assert "does not end with a newline" in capsys.readouterr().err


def test_a_bad_line_is_refused(tmp_path):
    path = tmp_path / "new.jsonl"
    path.write_text(json.dumps(ai("a")) + "\n{not json\n" + json.dumps(ai("b")) + "\n")
    assert run_check("ai-summaries", str(path)) == 1


def test_crashed_worker_records_without_a_token_are_allowed(tmp_path):
    new = jsonl(tmp_path / "new.jsonl", [ai("a"), {"error": "worker crashed: boom"}])
    assert run_check("ai-summaries", new) == 0


def test_scope_needs_tokens_and_descriptions_and_no_blank_lines(tmp_path):
    good = jsonl(tmp_path / "good.jsonl", [scope("a"), scope("b")])
    assert run_check("scope", good) == 0
    assert run_check("scope", jsonl(tmp_path / "e.jsonl", [scope("a", description="")])) == 1
    blank = tmp_path / "blank.jsonl"
    blank.write_text(json.dumps(scope("a")) + "\n\n")
    assert run_check("scope", str(blank)) == 1


def test_an_empty_file_is_refused(tmp_path):
    (tmp_path / "empty.jsonl").write_text("")
    assert run_check("scope", str(tmp_path / "empty.jsonl")) == 1


# --- the upload, against a stand-in gh -----------------------------------------------------

FAKE_GH = r'''
import fnmatch, json, os, shutil, sys

store = os.environ["FAKE_GH_STORE"]
state_path = os.path.join(store, "state.json")
state = json.load(open(state_path)) if os.path.exists(state_path) else {"releases": {}, "next_id": 1}
args = sys.argv[1:]
with open(os.path.join(store, "calls.log"), "a") as f:
    f.write(" ".join(args) + "\n")


def save(event):
    json.dump(state, open(state_path, "w"))
    with open(os.path.join(store, "events.jsonl"), "a") as f:
        f.write(json.dumps({"event": event, "assets": {t: sorted(r["assets"]) for t, r in
                                                       state["releases"].items()}}) + "\n")


def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default


def release(tag):
    if tag not in state["releases"]:
        sys.stderr.write("release not found\n")
        sys.exit(1)
    return state["releases"][tag]


def by_id(asset_id):
    for r in state["releases"].values():
        for name, a in r["assets"].items():
            if a["id"] == asset_id:
                return r, name
    sys.stderr.write("HTTP 404: Not Found\n")
    sys.exit(1)


if args[0] == "release":
    sub, tag = args[1], args[2] if len(args) > 2 else None
    if sub == "view":
        release(tag)
    elif sub == "list":
        tags = sorted(t for t in state["releases"] if t.startswith("extraction-data-"))
        print(tags[-1] if tags else "")
    elif sub == "download":
        r = release(tag)
        names = [n for n in r["assets"] if fnmatch.fnmatch(n, opt("--pattern"))]
        if not names:
            sys.stderr.write("no assets match the file pattern\n")
            sys.exit(1)
        for n in names:
            shutil.copy(os.path.join(store, "blobs", str(r["assets"][n]["id"])),
                        os.path.join(opt("--dir"), n))
    elif sub == "create":
        state["releases"][tag] = {"assets": {}, "notes": opt("--notes", ""),
                                  "latest": "--latest=false" not in args}
        save("create")
    elif sub == "upload":
        r = release(tag)
        path = args[3]
        name = os.path.basename(path)
        if name in r["assets"] and "--clobber" not in args:
            sys.stderr.write("asset under the same name already exists\n")
            sys.exit(1)
        os.makedirs(os.path.join(store, "blobs"), exist_ok=True)
        r["assets"][name] = {"id": state["next_id"]}
        shutil.copy(path, os.path.join(store, "blobs", str(state["next_id"])))
        state["next_id"] += 1
        save("upload")
    elif sub == "edit":
        release(tag)["notes"] = opt("--notes")
        save("edit")
elif args[0] == "api":
    method = opt("-X", "GET")
    path = [a for a in args[1:] if a.startswith("repos/")][0]
    if method == "GET":
        r = release(path.rsplit("/", 1)[1])
        for name, a in r["assets"].items():
            print(a["id"], name)
    else:
        r, name = by_id(int(path.rsplit("/", 1)[1]))
        asset = r["assets"].pop(name)
        if method == "PATCH":
            r["assets"][opt("-f").split("=", 1)[1]] = asset
        save(method)
'''


def executable(path, text):
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(SCRIPTS, root / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    (root / "data").mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "gh_fake.py").write_text(FAKE_GH)
    executable(bindir / "gh", f'#!/bin/bash\nexec "{sys.executable}" "{tmp_path / "gh_fake.py"}" "$@"\n')
    executable(bindir / "python3", f'#!/bin/bash\nexec "{sys.executable}" "$@"\n')
    store = tmp_path / "gh"
    store.mkdir()

    class Repo:
        pass

    r = Repo()
    r.root, r.store = root, store
    r.env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path),
             "TMPDIR": str(tmp_path), "FAKE_GH_STORE": str(store)}

    def publish(script="publish_ai_summaries.sh", *args):
        return subprocess.run([BASH, str(root / "scripts" / script), *args], env=r.env,
                              capture_output=True, text=True, timeout=120)

    def state():
        path = store / "state.json"
        return json.loads(path.read_text()) if path.exists() else {"releases": {}}

    def asset(tag, name):
        st = state()
        blob = store / "blobs" / str(st["releases"][tag]["assets"][name]["id"])
        return gzip.decompress(blob.read_bytes()).decode()

    def events():
        path = store / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def seed(tag, name, records):
        st = state()
        os.makedirs(store / "blobs", exist_ok=True)
        rid = st.get("next_id", 1)
        st.setdefault("releases", {}).setdefault(tag, {"assets": {}, "notes": "", "latest": True})
        st["releases"][tag]["assets"][name] = {"id": rid}
        st["next_id"] = rid + 1
        (store / "blobs" / str(rid)).write_bytes(
            gzip.compress(("".join(json.dumps(x) + "\n" for x in records)).encode()))
        (store / "state.json").write_text(json.dumps(st))

    r.publish, r.state, r.asset, r.events, r.seed = publish, state, asset, events, seed
    return r


needs_bash = pytest.mark.skipif(not os.path.exists(BASH), reason="needs /bin/bash")


@needs_bash
def test_first_publish_compares_with_the_extraction_release_and_creates_the_stable_one(repo):
    repo.seed("extraction-data-2026-08-17", "ai_summaries.jsonl.gz", [ai("a"), ai("b")])
    jsonl(repo.root / "data" / "ai_summaries.jsonl", [ai("a"), ai("b"), ai("c")])
    out = repo.publish()
    assert out.returncode == 0, out.stdout + out.stderr
    assert "extraction-data-2026-08-17" in out.stdout

    release = repo.state()["releases"]["ai-summaries-data"]
    assert sorted(release["assets"]) == ["ai_summaries.jsonl.gz"]
    assert release["latest"] is False
    local = (repo.root / "data" / "ai_summaries.jsonl").read_text()
    assert repo.asset("ai-summaries-data", "ai_summaries.jsonl.gz") == local
    assert "records: 3 (3 with text" in release["notes"]
    assert hashlib.sha256(local.encode()).hexdigest() in release["notes"]
    # The extraction release itself is left alone.
    assert sorted(repo.state()["releases"]["extraction-data-2026-08-17"]["assets"]) == \
        ["ai_summaries.jsonl.gz"]


@needs_bash
def test_a_republish_never_leaves_the_asset_missing_for_an_upload(repo):
    jsonl(repo.root / "data" / "ai_summaries.jsonl", [ai("a"), ai("b")])
    assert repo.publish().returncode == 0
    jsonl(repo.root / "data" / "ai_summaries.jsonl", [ai("a"), ai("b"), ai("c")])
    before = len(repo.events())
    out = repo.publish()
    assert out.returncode == 0, out.stdout + out.stderr

    names = [e["assets"]["ai-summaries-data"] for e in repo.events()[before:]]
    events = [e["event"] for e in repo.events()[before:]]
    assert "upload" in events and events.index("upload") < events.index("PATCH")
    missing = [i for i, n in enumerate(names) if "ai_summaries.jsonl.gz" not in n]
    assert len(missing) <= 1, "the asset was absent across more than one call: %s" % names
    assert names[-1] == ["ai_summaries.jsonl.gz"]
    assert "c" in repo.asset("ai-summaries-data", "ai_summaries.jsonl.gz")
    assert "--clobber" not in (repo.store / "calls.log").read_text()


@needs_bash
def test_a_shrinking_file_is_refused_and_nothing_changes(repo):
    repo.seed("ai-summaries-data", "ai_summaries.jsonl.gz", [ai("a"), ai("b"), ai("c")])
    jsonl(repo.root / "data" / "ai_summaries.jsonl", [ai("a"), ai("b")])
    before = repo.state()
    out = repo.publish()
    assert out.returncode == 1
    assert "fewer records" in out.stderr
    assert repo.state() == before


@needs_bash
def test_an_unknown_model_is_refused_before_anything_is_uploaded(repo):
    jsonl(repo.root / "data" / "ai_summaries.jsonl", [ai("a", model="mystery:1b")])
    out = repo.publish()
    assert out.returncode == 1
    assert repo.state()["releases"] == {}


@needs_bash
def test_dry_run_checks_and_uploads_nothing(repo):
    jsonl(repo.root / "data" / "ai_summaries.jsonl", [ai("a")])
    out = repo.publish("publish_ai_summaries.sh", "--dry-run")
    assert out.returncode == 0, out.stderr
    assert "dry run: would publish" in out.stdout
    assert repo.state()["releases"] == {}


@needs_bash
def test_a_publish_waits_for_no_one_while_the_nightly_holds_the_lock(repo):
    jsonl(repo.root / "data" / "ai_summaries.jsonl", [ai("a")])
    with ocr_io.run_lock(str(repo.root / "data" / ".ocr.lock")):
        out = repo.publish()
    assert out.returncode == 75
    assert repo.state()["releases"] == {}


@needs_bash
def test_scope_goes_to_its_own_stable_release(repo):
    repo.seed("extraction-data-2026-09-10", "scope.jsonl.gz", [scope("a")])
    jsonl(repo.root / "data" / "scope.jsonl", [scope("a"), scope("b")])
    out = repo.publish("publish_scope.sh")
    assert out.returncode == 0, out.stdout + out.stderr
    assert sorted(repo.state()["releases"]["scope-data"]["assets"]) == ["scope.jsonl.gz"]
    assert '"b"' in repo.asset("scope-data", "scope.jsonl.gz")


# A publish killed between its two renames leaves the old asset as prev-<asset>, the new one
# as next-<asset>, and nothing under <asset>. Until Oct 2026 the next run took that as "no
# baseline" and its leftovers cleanup deleted prev-<asset>, the only real copy.
NAME = "ai_summaries.jsonl.gz"


def died_between_renames(repo):
    repo.seed("ai-summaries-data", "prev-" + NAME, [ai("a"), ai("b"), ai("c")])
    repo.seed("ai-summaries-data", "next-" + NAME, [ai("a"), ai("b"), ai("c"), ai("x")])


def published_throughout(repo):
    """Every state the release moved through held the asset under one of its two names."""
    for e in repo.events():
        names = e["assets"].get("ai-summaries-data", [])
        assert NAME in names or "prev-" + NAME in names, names


@needs_bash
def test_a_rerun_restores_the_asset_a_killed_publish_left_as_prev(repo):
    died_between_renames(repo)
    jsonl(repo.root / "data" / "ai_summaries.jsonl", [ai("a"), ai("b"), ai("c"), ai("d")])
    out = repo.publish()
    assert out.returncode == 0, out.stdout + out.stderr
    assert "restored prev-" + NAME in out.stderr
    assert "comparing with the copy published on ai-summaries-data" in out.stdout
    assert sorted(repo.state()["releases"]["ai-summaries-data"]["assets"]) == [NAME]
    assert '"d"' in repo.asset("ai-summaries-data", NAME)
    published_throughout(repo)


@needs_bash
def test_the_restored_asset_is_the_baseline_a_shrinking_file_is_checked_against(repo):
    died_between_renames(repo)
    jsonl(repo.root / "data" / "ai_summaries.jsonl", [ai("a"), ai("b")])
    out = repo.publish()
    assert out.returncode == 1
    assert "fewer records" in out.stderr
    assets = repo.state()["releases"]["ai-summaries-data"]["assets"]
    assert NAME in assets and "prev-" + NAME not in assets
    assert '"c"' in repo.asset("ai-summaries-data", NAME)
    published_throughout(repo)


@needs_bash
def test_a_dry_run_compares_with_prev_and_changes_nothing(repo):
    died_between_renames(repo)
    jsonl(repo.root / "data" / "ai_summaries.jsonl", [ai("a"), ai("b")])
    before = repo.state()
    out = repo.publish("publish_ai_summaries.sh", "--dry-run")
    assert out.returncode == 1
    assert "fewer records" in out.stderr
    assert repo.state() == before
