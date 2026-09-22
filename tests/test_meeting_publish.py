"""Strict publication, and the receipt that may exist only after the remote was
proven to hold the note.

Every remote in this file is a local bare repository under `tmp_path`, pushed to
for real: no network, no credentials, no forge. A receipt written when the
remote does not have the commit is the one bug this module exists to make
impossible, so most cases here assert the *absence* of a receipt.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

from teammem import meeting_publish, meeting_render, vaultgit
from teammem.identity import IdentityMaps
from teammem.meeting_publish import (PublishRefused, blob_url, publish_meeting,
                                     receipt_bytes, validate_web_base)
from teammem.vaultgit import commit_all, ensure_repo
from tests.test_meeting_render import (CONFIG_DIR, NOTE, TODAY, WEEK, _import,
                                       _payload, _seeded, _validated_seeded)

WEB_BASE = "https://git.example.com/example-org/team-vault"
REL = f"Meetings/{WEEK}/{NOTE}"
OCCURRENCE = _payload()["occurrence_id"]
RECEIPT_KEYS = {"schema", "occurrence_id", "revision", "artifact_hash", "status",
                "vault_commit", "markdown_path", "markdown_url"}


# --- fixtures ---------------------------------------------------------------


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _vault(tmp_path, name="vault"):
    """A vault repo whose only remote is a bare repository beside it.

    Seeded with one commit under `Docs/` (unmanaged, so no render touches it) so
    every branch here has a commit to reset back to.
    """
    vault = tmp_path / name
    ensure_repo(vault)
    bare = tmp_path / f"{name}-remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    _git(vault, "remote", "add", "origin", str(bare))
    branch = _git(vault, "symbolic-ref", "--short", "HEAD")
    _git(vault, "config", f"branch.{branch}.remote", "origin")
    _git(vault, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")
    (vault / "Docs").mkdir(parents=True)
    (vault / "Docs" / "seed.md").write_text("seed\n", encoding="utf-8")
    commit_all(vault, "seed")
    _git(vault, "push", "-q", "origin", f"{branch}:refs/heads/{branch}")
    return vault, bare, branch


def _remote_ref(bare: Path, branch: str) -> str | None:
    done = subprocess.run(["git", "-C", str(bare), "rev-parse", "--verify",
                           "-q", f"refs/heads/{branch}"],
                          capture_output=True, text=True)
    return done.stdout.strip() or None


def _publish(conn, vault, receipt, **kw):
    kw.setdefault("occurrence_id", OCCURRENCE)
    kw.setdefault("web_base", WEB_BASE)
    return publish_meeting(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY,
                           receipt_path=receipt, **kw)


@pytest.fixture
def bench(tmp_path):
    """Ledger, vault with a bare remote, and an unwritten receipt path."""
    conn = _seeded(tmp_path)
    vault, bare, branch = _vault(tmp_path)
    return conn, vault, bare, branch, tmp_path / "receipts" / "meeting.json"


# --- the configured web base ------------------------------------------------


@pytest.mark.parametrize("value", [
    None, "", "   ",
    "http://git.example.com/team/vault",          # not https
    "ftp://git.example.com/team/vault",
    "git.example.com/team/vault",                 # not absolute
    "/srv/teammem/vault",                         # a local path
    "file:///srv/teammem/vault",
    "https:///team/vault",                        # no host
    "https://git.example.com/team/vault?ref=main",
    "https://git.example.com/team/vault#readme",
    "https://git.example.com/team/vault/-/blob/main",      # already a blob page
    "https://git.example.com/team/vault/-/tree/main/Meetings",
    "https://git.example.com/team/vault/-",
    "https://user:token@git.example.com/team/vault",   # credentials
    "https://git.example.com/team/vault ",             # stray whitespace
    "https://git.example.com/team/../vault",
])
def test_a_web_base_that_is_not_an_absolute_bare_https_url_is_refused(value):
    with pytest.raises(ValueError):
        validate_web_base(value)


def test_a_bare_https_base_is_accepted_and_trailing_slashes_are_dropped():
    assert validate_web_base(WEB_BASE) == WEB_BASE
    assert validate_web_base(WEB_BASE + "/") == WEB_BASE
    assert validate_web_base("https://git.example.com") == "https://git.example.com"


def test_the_markdown_url_encodes_each_segment_of_the_real_note_path():
    commit = "a" * 40
    url = blob_url(WEB_BASE, commit, REL)
    assert url == (f"{WEB_BASE}/-/blob/{commit}/Meetings/Week%202026-08-31-04/"
                   "2026-09-02%2014-00%20Atlas%20Launch%20Readiness%20%5B02ef68db%5D.md")
    # separators survive, everything inside a segment is encoded
    assert url.split(f"/-/blob/{commit}/")[1].count("/") == 2
    assert " " not in url and "[" not in url and "]" not in url


def test_the_markdown_url_names_the_commit_and_never_a_branch():
    commit = "b" * 40
    url = blob_url(WEB_BASE, commit, REL)
    assert f"/-/blob/{commit}/" in url
    assert "/-/blob/main/" not in url and "/-/blob/master/" not in url


def test_a_meeting_id_with_a_slash_still_yields_one_path_below_the_commit():
    # The occurrence id is not in the URL, but the note title is, and a title
    # holding a slash must not open a directory level.
    url = blob_url(WEB_BASE, "c" * 40, "Meetings/Week 1/a b [1].md")
    assert url.endswith("/Meetings/Week%201/a%20b%20%5B1%5D.md")


# --- the happy path ---------------------------------------------------------


def test_publication_writes_a_receipt_naming_the_commit_the_remote_holds(bench):
    conn, vault, bare, branch, receipt = bench

    out = _publish(conn, vault, receipt)

    assert set(out) == RECEIPT_KEYS
    assert out["schema"] == "teammem-meeting-receipt/v1"
    assert out["status"] == "published"
    assert out["occurrence_id"] == OCCURRENCE
    assert out["revision"] == 3
    assert out["artifact_hash"].startswith("sha256:")
    assert out["markdown_path"] == REL
    assert out["vault_commit"] == _remote_ref(bare, branch)
    assert len(out["vault_commit"]) == 40
    assert out["markdown_url"] == blob_url(WEB_BASE, out["vault_commit"], REL)
    # the receipt on disk is what was returned
    assert json.loads(receipt.read_text(encoding="utf-8")) == out
    # and the remote really carries the note at that commit
    assert subprocess.run(["git", "-C", str(bare), "cat-file", "-e",
                           f"{out['vault_commit']}:{REL}"]).returncode == 0


@pytest.mark.parametrize("title", [
    "Agenda [draft",
    "Agenda final]",
    "Agenda \\ path",
    "Agenda [draft] \\ path",
    "Sprint [3] Review",
])
def test_title_metacharacters_publish_to_a_real_bare_remote(tmp_path, title):
    payload = _payload()
    payload["title"] = title
    conn = _validated_seeded(tmp_path, payload)
    vault, bare, branch = _vault(tmp_path)
    receipt = tmp_path / "receipts" / "meeting.json"

    out = _publish(conn, vault, receipt)

    assert out["status"] == "published"
    assert out["vault_commit"] == _remote_ref(bare, branch)
    assert subprocess.run([
        "git", "-C", str(bare), "cat-file", "-e",
        f"{out['vault_commit']}:{out['markdown_path']}",
    ]).returncode == 0
    assert json.loads(receipt.read_text(encoding="utf-8")) == out


def test_the_receipt_never_carries_a_local_path_or_anything_but_an_https_url(bench):
    conn, vault, bare, branch, receipt = bench

    out = _publish(conn, vault, receipt)

    text = receipt.read_text(encoding="utf-8")
    assert out["markdown_url"].startswith(WEB_BASE + "/")
    assert str(vault) not in text and str(bare) not in text
    assert "file://" not in text and str(Path.home()) not in text


def test_the_receipt_holds_no_transcript_and_no_audio(bench):
    conn, vault, bare, branch, receipt = bench
    payload = _payload()

    _publish(conn, vault, receipt)

    text = receipt.read_text(encoding="utf-8")
    assert payload["transcript"][0]["text"] not in text
    assert payload["audio"]["sha256"] not in text
    assert payload["audio"]["filename"] not in text


# --- refusals ---------------------------------------------------------------


def test_a_missing_web_base_refuses_before_anything_is_rendered(bench):
    conn, vault, bare, branch, receipt = bench
    before = _remote_ref(bare, branch)

    with pytest.raises(PublishRefused) as caught:
        _publish(conn, vault, receipt, web_base=None)

    assert caught.value.stage == "config"
    assert not receipt.exists()
    assert not (vault / "Meetings").exists()
    assert _remote_ref(bare, branch) == before


def test_an_occurrence_the_ledger_does_not_hold_is_refused(bench):
    conn, vault, bare, branch, receipt = bench

    with pytest.raises(PublishRefused) as caught:
        _publish(conn, vault, receipt, occurrence_id="nope@2026-09-02T14:00:00Z")

    assert caught.value.stage == "ledger"
    assert not receipt.exists()
    assert not (vault / "Meetings").exists()


def test_a_withheld_meeting_gets_no_receipt_and_its_neighbour_still_publishes(tmp_path):
    """Two meetings whose project names collide on casefold: the render leaves
    the losing one out and files the other, so one producer's casing choice
    cannot hold every other occurrence unpublished."""
    other = _payload(day="2026-09-01")
    other["provider_meeting_id"] = "board-sync-2"
    other["occurrence_id"] = f"board-sync-2@{other['started_at']}"
    for project in other["projects"]:
        project["name"] = project["name"].upper()
    conn = _seeded(tmp_path, _payload(), other)
    vault, bare, branch = _vault(tmp_path)
    receipt = tmp_path / "receipts" / "meeting.json"
    before = _remote_ref(bare, branch)

    with pytest.raises(PublishRefused) as caught:
        _publish(conn, vault, receipt)

    assert caught.value.stage == "render"
    assert "filename collision" in caught.value.reason
    assert not receipt.exists()
    assert _remote_ref(bare, branch) == before

    published = _publish(conn, vault, receipt,
                         occurrence_id=other["occurrence_id"])
    assert published["status"] == "published"
    assert _remote_ref(bare, branch) == published["vault_commit"]


def test_a_semantic_finding_refuses_publication_and_pushes_nothing(bench, monkeypatch):
    conn, vault, bare, branch, receipt = bench
    before = _remote_ref(bare, branch)
    real = meeting_render.project_backlinks

    def _forgets_cygnus(notes):
        return dict(real(notes)) | {"project-cygnus": ()}

    monkeypatch.setattr(meeting_render, "project_backlinks", _forgets_cygnus)

    with pytest.raises(PublishRefused) as caught:
        _publish(conn, vault, receipt)

    assert caught.value.stage == "verify"
    assert caught.value.invariants == ("meeting.project.backlinks",)
    assert "Projects/project-cygnus/README.md" in str(caught.value)
    assert not receipt.exists()
    assert _remote_ref(bare, branch) == before          # nothing pushed
    assert vaultgit.head_commit(vault) == before        # nothing committed


def test_leaked_transcript_text_refuses_without_quoting_the_file(bench):
    conn, vault, bare, branch, receipt = bench
    leak = vault / "Docs" / "notes.md"
    sentence = _payload()["transcript"][0]["text"]
    leak.write_text(f"# jottings\n\n{sentence}\n", encoding="utf-8")
    before = _remote_ref(bare, branch)

    with pytest.raises(PublishRefused) as caught:
        _publish(conn, vault, receipt)

    assert caught.value.stage == "verify"
    assert caught.value.invariants == ("meeting.leakage.forbidden-content",)
    assert "Docs/notes.md" in str(caught.value)
    # the refusal names the file; it never repeats what was in it
    assert sentence not in str(caught.value)
    assert "walk the Atlas launch checklist" not in str(caught.value)
    assert not receipt.exists()
    assert _remote_ref(bare, branch) == before


def test_a_local_commit_failure_leaves_no_receipt_and_no_push(bench):
    conn, vault, bare, branch, receipt = bench
    hook = vault / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    before = _remote_ref(bare, branch)

    with pytest.raises(PublishRefused) as caught:
        _publish(conn, vault, receipt)

    assert caught.value.stage == "commit"
    assert not receipt.exists()
    assert vaultgit.head_commit(vault) == before
    assert _remote_ref(bare, branch) == before


def test_a_note_kept_out_of_the_commit_by_gitignore_is_not_published(bench):
    # The note is on disk, so the render diff and the semantic checks are both
    # clean; git never took it. Only the tree check at the commit sees this.
    conn, vault, bare, branch, receipt = bench
    (vault / ".gitignore").write_text("Meetings/\n", encoding="utf-8")
    before = _remote_ref(bare, branch)

    with pytest.raises(PublishRefused) as caught:
        _publish(conn, vault, receipt)

    assert caught.value.stage == "commit"
    assert REL in str(caught.value)
    assert not receipt.exists()
    assert _remote_ref(bare, branch) == before   # refused before the push
    assert (vault / REL).is_file()               # rendered, just not committed


def test_a_push_that_cannot_reach_the_remote_leaves_no_receipt(bench):
    conn, vault, bare, branch, receipt = bench
    shutil.rmtree(bare)

    with pytest.raises(PublishRefused) as caught:
        _publish(conn, vault, receipt)

    assert caught.value.stage == "push"
    assert not receipt.exists()
    # the commit is retained locally, exactly as the daily run retains it
    assert vaultgit.commit_contains(vault, vaultgit.head_commit(vault), REL)


def test_a_vault_with_no_remote_configured_cannot_publish(tmp_path):
    conn = _seeded(tmp_path)
    vault = tmp_path / "vault"
    ensure_repo(vault)
    receipt = tmp_path / "receipts" / "meeting.json"

    with pytest.raises(PublishRefused) as caught:
        _publish(conn, vault, receipt)

    assert caught.value.stage == "push"
    assert not receipt.exists()


def test_a_push_that_lands_on_another_ref_is_not_a_publication(bench):
    # `git push` succeeds and says nothing is wrong; the branch the receipt
    # would name never moved.
    conn, vault, bare, branch, receipt = bench
    _git(vault, "config", "remote.origin.push", "HEAD:refs/heads/somewhere-else")
    before = _remote_ref(bare, branch)

    with pytest.raises(PublishRefused) as caught:
        _publish(conn, vault, receipt)

    assert caught.value.stage == "push"
    assert not receipt.exists()
    assert _remote_ref(bare, branch) == before
    assert _remote_ref(bare, "somewhere-else") == vaultgit.head_commit(vault)


def test_a_remote_reset_behind_the_commit_is_repaired_by_a_clean_republish(bench):
    # The commit is already in local history, so sending it again is a repair,
    # not a new claim: the receipt may be issued once the remote holds it.
    conn, vault, bare, branch, receipt = bench
    first = _publish(conn, vault, receipt)
    receipt.unlink()
    commits = _git(vault, "rev-list", "--count", "HEAD")
    _git(bare, "update-ref", f"refs/heads/{branch}",
         _git(vault, "rev-parse", f"{first['vault_commit']}^"))

    second = _publish(conn, vault, receipt)

    assert second == first
    assert _remote_ref(bare, branch) == first["vault_commit"]
    assert _git(vault, "rev-list", "--count", "HEAD") == commits   # no empty commit


def test_a_diverged_remote_that_cannot_be_pushed_to_refuses_a_receipt(bench):
    # The remote branch now carries an unrelated commit, so the push is rejected
    # and nothing can make the receipt true.
    conn, vault, bare, branch, receipt = bench
    first = _publish(conn, vault, receipt)
    receipt.unlink()
    _git(bare, "config", "user.name", "someone")
    _git(bare, "config", "user.email", "someone@local")
    empty_tree = _git(bare, "hash-object", "-t", "tree", "-w", os.devnull)
    stranger = _git(bare, "commit-tree", "-m", "unrelated", empty_tree)
    _git(bare, "update-ref", f"refs/heads/{branch}", stranger)

    with pytest.raises(PublishRefused) as caught:
        _publish(conn, vault, receipt)

    assert caught.value.stage == "push"
    assert not receipt.exists()
    assert _remote_ref(bare, branch) == stranger     # the remote was not rewritten
    assert first["vault_commit"] != stranger


def test_a_same_day_retry_after_a_failed_push_publishes(bench):
    # No date changes between the two attempts: the retry must stand on its own
    # rather than on tomorrow's re-render dirtying the tree.
    conn, vault, bare, branch, receipt = bench
    shutil.rmtree(bare)
    with pytest.raises(PublishRefused) as caught:
        _publish(conn, vault, receipt)
    assert caught.value.stage == "push"
    assert not receipt.exists()
    committed = vaultgit.head_commit(vault)
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)

    out = _publish(conn, vault, receipt)

    assert out["vault_commit"] == committed          # the same commit, same day
    assert _remote_ref(bare, branch) == committed
    assert json.loads(receipt.read_text(encoding="utf-8")) == out


def test_a_crash_between_fsync_and_rename_leaves_no_receipt_at_all(bench, monkeypatch):
    conn, vault, bare, branch, receipt = bench
    real = os.replace

    def crash(src, dst, **kw):
        if Path(dst) == receipt:
            raise OSError("crash between fsync and rename")
        return real(src, dst, **kw)

    monkeypatch.setattr(meeting_publish.os, "replace", crash)

    with pytest.raises(PublishRefused) as caught:
        _publish(conn, vault, receipt)

    assert caught.value.stage == "receipt"
    assert not receipt.exists()
    assert list(receipt.parent.iterdir()) == []   # no staging file left behind


def test_a_receipt_can_be_written_after_a_crashed_attempt(bench, monkeypatch):
    conn, vault, bare, branch, receipt = bench
    real = os.replace

    def crash(src, dst, **kw):
        if Path(dst) == receipt:
            raise OSError("boom")
        return real(src, dst, **kw)

    monkeypatch.setattr(meeting_publish.os, "replace", crash)
    with pytest.raises(PublishRefused):
        _publish(conn, vault, receipt)
    monkeypatch.setattr(meeting_publish.os, "replace", real)

    out = _publish(conn, vault, receipt)

    assert out["vault_commit"] == _remote_ref(bare, branch)
    assert json.loads(receipt.read_text(encoding="utf-8")) == out


# --- clean re-render and replay ---------------------------------------------


def test_republishing_an_unchanged_note_reproduces_the_receipt_byte_for_byte(bench):
    conn, vault, bare, branch, receipt = bench
    first = _publish(conn, vault, receipt)
    first_bytes = receipt.read_bytes()
    commits = _git(vault, "rev-list", "--count", "HEAD")

    second = _publish(conn, vault, receipt)

    assert second == first
    assert receipt.read_bytes() == first_bytes
    # nothing changed, so no empty commit was made to pad the history
    assert _git(vault, "rev-list", "--count", "HEAD") == commits
    assert second["vault_commit"] == _remote_ref(bare, branch)


def test_a_new_revision_publishes_a_new_commit_and_a_new_receipt(bench):
    conn, vault, bare, branch, receipt = bench
    first = _publish(conn, vault, receipt)
    payload = _payload()
    payload["revision"] = 4
    payload["summary"] = payload["summary"] + " Reviewed again."
    _import(conn, payload)

    second = _publish(conn, vault, receipt)

    assert second["revision"] == 4
    assert second["vault_commit"] != first["vault_commit"]
    assert second["artifact_hash"] != first["artifact_hash"]
    assert second["markdown_path"] == first["markdown_path"]
    assert second["markdown_url"].endswith(quote(NOTE))
    assert second["vault_commit"] == _remote_ref(bare, branch)


def test_the_receipt_bytes_are_sorted_compact_json_with_one_newline():
    receipt = {"schema": "teammem-meeting-receipt/v1", "status": "published",
               "occurrence_id": OCCURRENCE, "revision": 3,
               "artifact_hash": "sha256:" + "0" * 64, "vault_commit": "f" * 40,
               "markdown_path": REL, "markdown_url": blob_url(WEB_BASE, "f" * 40, REL)}
    blob = receipt_bytes(receipt)

    assert blob.endswith(b"\n")
    assert b", " not in blob and b'": ' not in blob
    assert json.loads(blob) == receipt
    assert list(json.loads(blob)) == sorted(receipt)
    assert receipt_bytes(dict(reversed(list(receipt.items())))) == blob


# --- the strict push helper -------------------------------------------------


def test_push_verified_returns_the_commit_the_remote_ends_up_holding(tmp_path):
    vault, bare, branch = _vault(tmp_path)
    (vault / "Docs" / "next.md").write_text("more\n", encoding="utf-8")
    commit_all(vault, "more")

    commit = vaultgit.push_verified(vault, vaultgit.head_commit(vault))

    assert commit == vaultgit.head_commit(vault)
    assert commit == _remote_ref(bare, branch)


def test_push_verified_refuses_when_the_remote_ref_did_not_move(tmp_path):
    vault, bare, branch = _vault(tmp_path)
    _git(vault, "config", "remote.origin.push", "HEAD:refs/heads/elsewhere")
    (vault / "Docs" / "next.md").write_text("more\n", encoding="utf-8")
    commit_all(vault, "more")
    before = _remote_ref(bare, branch)

    with pytest.raises(vaultgit.PushNotVerified):
        vaultgit.push_verified(vault, vaultgit.head_commit(vault))

    assert _remote_ref(bare, branch) == before


def test_remote_reaches_is_true_only_for_commits_the_remote_holds(tmp_path):
    vault, bare, branch = _vault(tmp_path)
    seed = vaultgit.head_commit(vault)
    (vault / "Docs" / "next.md").write_text("more\n", encoding="utf-8")
    commit_all(vault, "more")
    ahead = vaultgit.head_commit(vault)

    assert vaultgit.remote_reaches(vault, seed) is True
    assert vaultgit.remote_reaches(vault, ahead) is False
    vaultgit.push_verified(vault, ahead)
    assert vaultgit.remote_reaches(vault, ahead) is True
    assert vaultgit.remote_reaches(vault, seed) is True   # still reachable


def test_push_verified_vouches_for_the_commit_it_was_given_not_for_head(tmp_path):
    # A second hand committing to the same vault between the tree check and the
    # push must not put an unchecked commit into anyone's receipt.
    vault, bare, branch = _vault(tmp_path)
    (vault / "Docs" / "checked.md").write_text("checked\n", encoding="utf-8")
    commit_all(vault, "checked")
    checked = vaultgit.head_commit(vault)
    (vault / "Docs" / "later.md").write_text("later\n", encoding="utf-8")
    commit_all(vault, "a concurrent hand")

    assert vaultgit.push_verified(vault, checked) == checked
    assert vaultgit.head_commit(vault) != checked
    assert _remote_ref(bare, branch) == vaultgit.head_commit(vault)


def test_commit_contains_sees_the_tree_and_not_the_working_copy(tmp_path):
    vault, bare, branch = _vault(tmp_path)
    commit = vaultgit.head_commit(vault)
    (vault / "Docs" / "later.md").write_text("later\n", encoding="utf-8")

    assert vaultgit.commit_contains(vault, commit, "Docs/seed.md") is True
    assert vaultgit.commit_contains(vault, commit, "Docs/later.md") is False
    assert vaultgit.commit_contains(vault, commit, "Meetings/nothing.md") is False


def test_the_plain_push_helper_is_unchanged(tmp_path):
    vault, bare, branch = _vault(tmp_path)
    (vault / "Docs" / "next.md").write_text("more\n", encoding="utf-8")
    commit_all(vault, "more")

    assert vaultgit.push(vault) is None
    assert _remote_ref(bare, branch) == vaultgit.head_commit(vault)
    with pytest.raises(subprocess.CalledProcessError):
        vaultgit.push(tmp_path / "not-a-repo")
