"""Residual privacy/publication proofs with real bundle loads and bare remotes."""

import subprocess
import sys

import pytest

from teammem import meeting_publish, services, vaultgit
from teammem.config import Config
from teammem.identity import IdentityMaps
from tests.test_meeting_publish import _git, _publish, _remote_ref, _vault
from tests.test_meeting_render import (
    CONFIG_DIR, TODAY, _payload, _short_transcript_payload, _validated_seeded,
)


@pytest.mark.parametrize("field", ["preserved", "summary"])
@pytest.mark.parametrize("scaffold", ["{0}: {0}", "**{0}:**\n{0}", "1. **{0}:** _{0}_"])
def test_overlapping_speakers_and_turns_refuse_publication(tmp_path, capsys, field, scaffold):
    turns = ("Alex", "Sam", "Jordan")
    copied = "\n".join(scaffold.format(turn) for turn in turns)
    payload = _short_transcript_payload(*turns)
    if field == "summary":
        payload["summary"] = "Status:\n" + copied
    conn = _validated_seeded(tmp_path, payload)
    vault, bare, branch = _vault(tmp_path)
    before = _remote_ref(bare, branch)
    if field == "preserved":
        (vault / "Docs" / "copied.md").write_text(copied)
    receipt = tmp_path / "receipt.json"
    with pytest.raises(meeting_publish.PublishRefused) as failure:
        _publish(conn, vault, receipt)
    assert all(turn not in str(failure.value) for turn in turns)
    captured = capsys.readouterr()
    assert all(turn not in captured.out + captured.err for turn in turns)
    assert not receipt.exists()
    assert _remote_ref(bare, branch) == before


@pytest.mark.parametrize("consumer", ["publish", "render"])
@pytest.mark.parametrize("mutation", ["ancestors", "ref"])
def test_hook_restored_tree_cannot_clear_guard_for_unowned_history(tmp_path, consumer, mutation):
    conn = _validated_seeded(tmp_path, _payload())
    vault, bare, branch = _vault(tmp_path)
    before = _remote_ref(bare, branch)
    _git(vault, "branch", "unrelated", before)
    hook = vault / ".git" / "hooks" / "post-commit"
    if mutation == "ancestors":
        body = (
            'printf "unverified hook ancestor\\n" > Docs/hook-injected.md\n'
            'git -c core.hooksPath=/dev/null add -- Docs/hook-injected.md\n'
            'git -c core.hooksPath=/dev/null commit -qm "unverified intermediate"\n'
            'git -c core.hooksPath=/dev/null rm -q -- Docs/hook-injected.md\n'
            'git -c core.hooksPath=/dev/null commit -qm "restored tree"\n'
        )
    else:
        body = 'git switch -qc hook-owned\n'
    hook.write_text("#!/bin/sh\nset -e\n" + body +
                    'printf "unowned staged bytes\\n" > Docs/pending.md\n'
                    'git add -- Docs/pending.md\n'
                    'git rev-parse HEAD > .git/hook-tip\n')
    hook.chmod(0o700)
    receipt = tmp_path / "receipt.json"

    def attempt():
        if consumer == "publish":
            with pytest.raises(meeting_publish.PublishRefused):
                _publish(conn, vault, receipt)
        else:
            cfg = Config(db_path=tmp_path / "l.db", vault_dir=vault, config_dir=CONFIG_DIR)
            assert services.run_render(cfg, IdentityMaps.load(CONFIG_DIR), today=TODAY,
                                       conn=conn, push_requested=True) == 1

    attempt()
    tip = (vault / ".git" / "hook-tip").read_text().strip()
    assert _git(vault, "rev-parse", "HEAD") == tip
    assert _git(vault, "rev-parse", "unrelated") == before
    assert _git(vault, "show", ":Docs/pending.md") == "unowned staged bytes"
    assert (vault / "Docs" / "pending.md").read_text() == "unowned staged bytes\n"
    if mutation == "ancestors":
        assert "unverified hook ancestor" in _git(vault, "log", "-p", "--", "Docs/hook-injected.md")
    assert (vault / ".git" / "teammem-publication-pending").exists()
    attempt()
    assert _git(vault, "rev-parse", "HEAD") == tip
    assert hook.exists()
    assert not receipt.exists()
    assert _remote_ref(bare, branch) == before
    for publish in (lambda: vaultgit.push(vault), lambda: vaultgit.push_verified(vault, tip)):
        with pytest.raises(vaultgit.VerifiedTreeMismatch):
            publish()
    assert _remote_ref(bare, branch) == before


@pytest.mark.parametrize("consumer", ["publish", "render"])
def test_ordinary_post_commit_hook_keeps_successful_publication(tmp_path, consumer):
    conn = _validated_seeded(tmp_path, _payload())
    vault, bare, branch = _vault(tmp_path)
    before = _remote_ref(bare, branch)
    hook = vault / ".git" / "hooks" / "post-commit"
    hook.write_text('#!/bin/sh\nprintf "checked\\n" > .git/hook-ran\n')
    hook.chmod(0o700)
    if consumer == "publish":
        assert _publish(conn, vault, tmp_path / "receipt.json")["status"] == "published"
    else:
        cfg = Config(db_path=tmp_path / "l.db", vault_dir=vault, config_dir=CONFIG_DIR)
        assert services.run_render(cfg, IdentityMaps.load(CONFIG_DIR), today=TODAY,
                                   conn=conn, push_requested=True) == 0
    tip = _remote_ref(bare, branch)
    assert tip != before
    assert _git(bare, "rev-parse", f"{tip}^") == before
    assert (vault / ".git" / "hook-ran").read_text() == "checked\n"
    assert hook.exists()
    assert not (vault / ".git" / "teammem-publication-pending").exists()


@pytest.mark.parametrize("field", ["preserved", "summary"])
def test_direct_render_blocks_or_withholds_overlapping_transcript(tmp_path, capsys, field):
    turns = ("Alex", "Sam", "Jordan")
    copied = "\n".join(f"{turn}: {turn}" for turn in turns)
    payload = _short_transcript_payload(*turns)
    if field == "summary":
        payload["summary"] = "Status:\n" + copied
    conn = _validated_seeded(tmp_path, payload)
    vault, bare, branch = _vault(tmp_path)
    before = _remote_ref(bare, branch)
    if field == "preserved":
        (vault / "Docs" / "copied.md").write_text(copied)
    cfg = Config(db_path=tmp_path / "l.db", vault_dir=vault, config_dir=CONFIG_DIR)
    code = services.run_render(cfg, IdentityMaps.load(CONFIG_DIR), today=TODAY,
                               conn=conn, push_requested=True)
    if field == "preserved":
        assert code == 1
        assert _remote_ref(bare, branch) == before
    else:
        assert code == 0
        paths = _git(bare, "ls-tree", "-r", "--name-only", branch).splitlines()
        assert [path for path in paths if path.startswith("Meetings/")] == ["Meetings/README.md"]
        for path in paths:
            content = _git(bare, "show", f"{branch}:{path}")
            assert all(turn not in content for turn in turns)
    captured = capsys.readouterr()
    assert all(turn not in captured.out + captured.err for turn in turns)


@pytest.mark.parametrize("copied", [
    "Alex: Alex\nSam: Sam",  # incomplete, even with overlapping labels
    "Jordan: Jordan\nSam: Sam\nAlex: Alex",  # reordered
    "Alex: Alexander\nSam: Samson\nJordan: Jordana",  # near words
    "Speaker: prefix_Alex_suffix\nSpeaker: Sam\nSpeaker: Jordan",  # identifiers
    "Alex: alex\nSam: sam\nJordan: jordan",  # spoken case differs
    "Alex: Alex\nUnrelated prose.\nSam: Sam\nJordan: Jordan",
])
def test_overlap_controls_still_publish(tmp_path, copied):
    conn = _validated_seeded(tmp_path, _short_transcript_payload("Alex", "Sam", "Jordan"))
    vault, bare, _ = _vault(tmp_path)
    (vault / "Docs" / "control.md").write_text(copied)
    receipt = _publish(conn, vault, tmp_path / "receipt.json")
    assert _git(bare, "show", f"{receipt['vault_commit']}:Docs/control.md") == copied


def test_ambiguous_repeated_turns_do_not_expand_exponentially():
    # Each colon turn is also valid scaffolding: there are combinatorially
    # many possible partial paths, but no placement for the last spoken turn.
    done = subprocess.run([sys.executable, "-c", """
import re
from teammem.render import _ordered_turns_present
turns = (re.compile('Echo:'),) * 30 + (re.compile('Missing'),)
assert not _ordered_turns_present('Echo:\\n' * 100, turns)
"""], capture_output=True, text=True, timeout=5)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("field", ["preserved", "summary"])
def test_viable_spoken_phrase_can_overlap_an_earlier_phrase_match(tmp_path, field):
    copied = "A: A A A\nSpeaker: Z"
    payload = _short_transcript_payload("A", "A A", "Z")
    if field == "summary":
        payload["summary"] = "Status:\n" + copied
    conn = _validated_seeded(tmp_path, payload)
    vault, bare, branch = _vault(tmp_path)
    before = _remote_ref(bare, branch)
    if field == "preserved":
        (vault / "Docs" / "copied.md").write_text(copied)
    with pytest.raises(meeting_publish.PublishRefused):
        _publish(conn, vault, tmp_path / "receipt.json")
    assert _remote_ref(bare, branch) == before
    assert not (tmp_path / "receipt.json").exists()


def test_failed_rollback_cas_retains_independent_commit_and_guard(tmp_path, monkeypatch):
    conn = _validated_seeded(tmp_path, _payload())
    vault, bare, branch = _vault(tmp_path)
    before = _remote_ref(bare, branch)
    hook = vault / ".git" / "hooks" / "pre-commit"
    hook.write_text('#!/bin/sh\nprintf "changed\\n" >> Projects/README.md\ngit add Projects/README.md\n')
    hook.chmod(0o700)
    real_git = vaultgit._git
    independent = []

    def advance_before_cas(path, *args):
        if args[0] == "update-ref":
            _git(vault, "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-m", "independent")
            independent.append(_git(vault, "rev-parse", "HEAD"))
        return real_git(path, *args)

    monkeypatch.setattr(vaultgit, "_git", advance_before_cas)
    with pytest.raises(meeting_publish.PublishRefused, match="rollback refused"):
        _publish(conn, vault, tmp_path / "receipt.json")
    assert len(independent) == 1
    assert _git(vault, "rev-parse", "HEAD") == independent[0]
    assert (vault / ".git" / "teammem-publication-pending").exists()
    assert _remote_ref(bare, branch) == before
    assert not (tmp_path / "receipt.json").exists()
