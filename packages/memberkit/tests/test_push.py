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


def event(summary="kept", date="2026-07-24"):
    return {
        "ts": f"{date}T10:00:00",
        "kind": "journal-highlight",
        "summary": summary,
        "project": "p",
        "refs": None,
    }


def write_bundle(cfg, date, *, events=None, journal_md=None):
    out = cfg.workdir / "out"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"bundle-alex-{date}.json"
    path.write_text(json.dumps(
        {"schema": "teammem-bundle/v1", "member": "alex", "date": date,
         "events": events if events is not None else [],
         "journal_md": journal_md if journal_md is not None else f"## {date}"}))
    return path


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


def test_push_preserves_pending_multiplicity_after_existing_approvals(tmp_path):
    date = "2026-07-24"
    bare = tmp_path / "inbox.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True)
    cfg = make_cfg(tmp_path, str(bare))
    duplicate = event("same observation", date)
    fingerprint = event_fingerprint(duplicate, date)
    state_path = cfg.workdir / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({
        "approved": [fingerprint, fingerprint],
        "excluded": [],
        "pending": {date: [fingerprint]},
    }))
    write_bundle(cfg, date, events=[duplicate])

    push.push(cfg, date)

    saved = DraftState(state_path).snapshot()
    assert saved["approved"].count(fingerprint) == 3
    assert date not in saved["pending"]

    push.push(cfg, date)
    repeated = DraftState(state_path).snapshot()
    assert repeated["approved"].count(fingerprint) == 3


def test_push_rejects_wrong_schema_before_git_or_inbox_creation(
    tmp_path, monkeypatch,
):
    cfg = make_cfg(tmp_path, "unused")
    out = cfg.workdir / "out"
    out.mkdir(parents=True)
    (out / "bundle-alex-2026-07-24.json").write_text('{"schema": "wrong/v9"}')
    calls = []
    monkeypatch.setattr(push, "_run", lambda cmd: calls.append(cmd))

    with pytest.raises(SystemExit):
        push.push(cfg, "2026-07-24")

    assert calls == []
    assert not (cfg.workdir / "inbox").exists()


def test_push_rejects_invalid_event_before_git_or_inbox_creation(
    tmp_path, monkeypatch,
):
    cfg = make_cfg(tmp_path, "unused")
    write_bundle(
        cfg,
        "2026-07-24",
        events=[{**event(), "refs": ["private"]}],
    )
    calls = []
    monkeypatch.setattr(push, "_run", lambda cmd: calls.append(cmd))

    with pytest.raises(SystemExit, match="refs must be null"):
        push.push(cfg, "2026-07-24")

    assert calls == []
    assert not (cfg.workdir / "inbox").exists()


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
         "events": [{"ts": "2026-07-24T10:00:00",
                     "kind": "journal-highlight", "summary": "kept",
                     "project": "p", "refs": None}],
         "journal_md": "## 2026-07-24\n\n### p\n- kept\n- PRIVATE LINE"}), encoding="utf-8")

    dest = push.push(cfg, "2026-07-24")
    pushed = json.loads(dest.read_text(encoding="utf-8"))
    assert "PRIVATE" not in pushed["journal_md"] and "- kept" in pushed["journal_md"]
    saved = DraftState(cfg.workdir / "state.json").snapshot()
    assert event_fingerprint(pushed["events"][0], "2026-07-24") in saved["approved"]


def test_push_without_review_preflights_local_bundle_and_records_removal(tmp_path):
    date = "2026-07-24"
    bare = tmp_path / "inbox.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True)
    cfg = make_cfg(tmp_path, str(bare))
    kept = event("kept", date)
    removed = event("private removed", date)
    state = DraftState(cfg.workdir / "state.json")
    state.refresh(date, [kept, removed], current=None)
    src = write_bundle(
        cfg,
        date,
        events=[kept],
        journal_md=f"## {date}\n\n### p\n- kept\n- private removed",
    )

    dest = push.push(cfg, date)

    assert src.read_bytes() == dest.read_bytes()
    saved_bundle = json.loads(src.read_text(encoding="utf-8"))
    assert saved_bundle["journal_md"] == f"## {date}\n\n### p\n- kept"
    saved_state = DraftState(cfg.workdir / "state.json").snapshot()
    assert event_fingerprint(removed, date) in saved_state["excluded"]
    assert event_fingerprint(kept, date) in saved_state["approved"]


def test_push_atomic_preflight_failure_does_not_call_git_or_change_state(
    tmp_path, monkeypatch,
):
    date = "2026-07-24"
    cfg = make_cfg(tmp_path, "unused")
    kept = event("kept", date)
    removed = event("private removed", date)
    state = DraftState(cfg.workdir / "state.json")
    state.refresh(date, [kept, removed], current=None)
    src = write_bundle(cfg, date, events=[kept], journal_md="stale private text")
    original = src.read_bytes()
    before = state.snapshot()
    calls = []
    monkeypatch.setattr(push, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(
        push.bundle.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        push.push(cfg, date)

    assert calls == []
    assert not (cfg.workdir / "inbox").exists()
    assert src.read_bytes() == original
    assert state.snapshot() == before


def test_git_failure_does_not_approve_preflighted_events(tmp_path, monkeypatch):
    date = "2026-07-24"
    cfg = make_cfg(tmp_path, "unused")
    kept = event("kept", date)
    state = DraftState(cfg.workdir / "state.json")
    state.refresh(date, [kept], current=None)
    src = write_bundle(cfg, date, events=[kept], journal_md="stale")

    monkeypatch.setattr(
        push,
        "_run",
        lambda cmd: (_ for _ in ()).throw(SystemExit("git failed")),
    )

    with pytest.raises(SystemExit, match="git failed"):
        push.push(cfg, date)

    saved = state.snapshot()
    fingerprint = event_fingerprint(kept, date)
    assert fingerprint not in saved["approved"]
    assert fingerprint in saved["pending"][date]
    assert json.loads(src.read_text(encoding="utf-8"))["journal_md"] == (
        f"## {date}\n\n### p\n- kept"
    )


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
