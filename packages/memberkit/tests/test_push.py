import json
import subprocess
from pathlib import Path

import pytest

from memberkit import push
from memberkit.config import Config
from memberkit.state import DraftState, event_fingerprint


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    for key, val in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                     "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}.items():
        monkeypatch.setenv(key, val)


def make_cfg(tmp_path, inbox_url):
    return Config(member="alex", db=Path("/nonexistent"),
                  inbox_url=inbox_url, workdir=tmp_path / "work")


def write_bundle(cfg, date):
    out = cfg.workdir / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"bundle-alex-{date}.json").write_text(json.dumps(
        {"schema": "teammem-bundle/v1", "member": "alex", "date": date,
         "events": [], "journal_md": f"## {date}"}))


def test_push_clones_commits_pushes(tmp_path):
    bare = tmp_path / "inbox.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True)
    cfg = make_cfg(tmp_path, str(bare))
    write_bundle(cfg, "2026-07-24")

    dest = push.push(cfg, "2026-07-24")
    assert dest.name == "bundle-alex-2026-07-24.json"

    listing = subprocess.run(
        ["git", "-C", str(bare), "ls-tree", "-r", "--name-only", "main"],
        capture_output=True, text=True, check=True).stdout
    assert "alex/bundle-alex-2026-07-24.json" in listing


def test_push_same_bundle_twice_is_noop(tmp_path):
    bare = tmp_path / "inbox.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True)
    cfg = make_cfg(tmp_path, str(bare))
    write_bundle(cfg, "2026-07-24")
    push.push(cfg, "2026-07-24")
    push.push(cfg, "2026-07-24")   # must not raise "nothing to commit"


def test_push_rejects_wrong_schema(tmp_path):
    cfg = make_cfg(tmp_path, "unused")
    out = cfg.workdir / "out"
    out.mkdir(parents=True)
    (out / "bundle-alex-2026-07-24.json").write_text('{"schema": "wrong/v9"}')
    try:
        push.push(cfg, "2026-07-24")
        raise AssertionError("expected SystemExit")
    except SystemExit:
        pass


def test_push_reconciles_with_remote_updates(tmp_path):
    bare = tmp_path / "inbox.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True)
    cfg = make_cfg(tmp_path, str(bare))
    write_bundle(cfg, "2026-07-24")
    push.push(cfg, "2026-07-24")

    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(bare), str(other)], check=True, capture_output=True)
    (other / "note.txt").write_text("hub side\n")
    subprocess.run(["git", "-C", str(other), "add", "note.txt"], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "-m", "hub: note"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(other), "push"], check=True, capture_output=True)

    write_bundle(cfg, "2026-07-25")
    push.push(cfg, "2026-07-25")
    listing = subprocess.run(
        ["git", "-C", str(bare), "ls-tree", "-r", "--name-only", "main"],
        capture_output=True, text=True, check=True).stdout
    assert "alex/bundle-alex-2026-07-25.json" in listing and "note.txt" in listing


def test_push_regenerates_journal_from_events(tmp_path):
    bare = tmp_path / "inbox.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True)
    cfg = make_cfg(tmp_path, str(bare))
    out = cfg.workdir / "out"
    out.mkdir(parents=True)
    (out / "bundle-alex-2026-07-24.json").write_text(json.dumps(
        {"schema": "teammem-bundle/v1", "member": "alex", "date": "2026-07-24",
         "events": [{"ts": "t", "kind": "journal-highlight", "summary": "kept",
                     "project": "p", "refs": None}],
         "journal_md": "## 2026-07-24\n\n### p\n- kept\n- PRIVATE LINE"}), encoding="utf-8")

    dest = push.push(cfg, "2026-07-24")
    pushed = json.loads(dest.read_text(encoding="utf-8"))
    assert "PRIVATE" not in pushed["journal_md"] and "- kept" in pushed["journal_md"]
    saved = DraftState(cfg.workdir / "state.json").snapshot()
    assert event_fingerprint(pushed["events"][0], "2026-07-24") in saved["approved"]


def test_push_recovers_stuck_unpushed_commit(tmp_path):
    bare = tmp_path / "inbox.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True)
    cfg = make_cfg(tmp_path, str(bare))
    write_bundle(cfg, "2026-07-24")
    push.push(cfg, "2026-07-24")

    clone = cfg.workdir / "inbox"
    (clone / "alex" / "bundle-alex-2026-07-25.json").write_text(
        (cfg.workdir / "out" / "bundle-alex-2026-07-24.json").read_text())
    subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-m", "stuck"],
                   check=True, capture_output=True)

    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(bare), str(other)], check=True, capture_output=True)
    (other / "note.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(other), "add", "note.txt"], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "-m", "hub"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(other), "push"], check=True, capture_output=True)

    push.push(cfg, "2026-07-24")   # unchanged bundle; must still publish the stuck commit
    listing = subprocess.run(
        ["git", "-C", str(bare), "ls-tree", "-r", "--name-only", "main"],
        capture_output=True, text=True, check=True).stdout
    assert "alex/bundle-alex-2026-07-25.json" in listing
