"""Completion-audit regressions use real ledgers, private roots and bare remotes."""

import multiprocessing
import os
from pathlib import Path

import pytest

from teammem import meeting_publish, meeting_retention, services
from teammem.config import Config
from teammem.identity import IdentityMaps
from teammem.meeting_store import get_meeting
from teammem.store import open_db
from tests.test_meeting_publish import _publish, _vault, _remote_ref, _git, OCCURRENCE
from tests.test_meeting_render import (
    CONFIG_DIR, TODAY, SHORT_TURNS, _short_transcript_payload, _validated_seeded,
    _import as import_revision, _payload as render_payload,
)
from tests.test_meeting_retention import (
    _import, _payload, _audio, _custody_files, _store, _alert_path, _hub, EXPIRY, LATER,
    _isolate_operator_env_file,
)


def test_root_move_destroys_final_inode_links_without_touching_external_siblings(tmp_path):
    conn = open_db(tmp_path / "ledger.db")
    payload = _payload()
    _import(conn, tmp_path, payload)
    final = _audio(tmp_path, payload)
    custody = _custody_files(_store(tmp_path))
    if os.name != "nt":
        assert custody
    sibling = final.parent / ".audio.enc.unrelated"
    sibling.write_bytes(b"unrelated external sibling")
    moved = tmp_path / "new-audio-root"
    moved.mkdir()
    for instant in (EXPIRY, LATER):
        result = meeting_retention.expire_meeting_audio(
            conn, moved, now=instant, alert_path=_alert_path(tmp_path))
        assert result.exit_code == 0, result
        assert not final.exists()
        assert all(not path.exists() or path.stat().st_size == 0 for path in custody)
        assert sibling.read_bytes() == b"unrelated external sibling"
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] == EXPIRY


@pytest.mark.skipif(os.name == "nt", reason="POSIX held-inode destruction before unlink")
def test_external_final_is_destroyed_before_unlink_can_interrupt_retention(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "ledger.db")
    payload = _payload()
    _import(conn, tmp_path, payload)
    final = _audio(tmp_path, payload)
    real_unlink = os.unlink

    def interrupt(path, *args, **kwargs):
        if Path(path) == final:
            assert final.stat().st_size == 0, "external inode must be destroyed before losing its only authority"
            raise OSError("interrupted unlink")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(meeting_retention.os, "unlink", interrupt)
    moved = tmp_path / "new-audio-root"
    moved.mkdir()
    result = meeting_retention.expire_meeting_audio(conn, moved, now=EXPIRY,
                                                   alert_path=_alert_path(tmp_path))
    assert result.failed
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] is None
    monkeypatch.undo()
    result = meeting_retention.expire_meeting_audio(conn, moved, now=LATER,
                                                   alert_path=_alert_path(tmp_path))
    assert result.deleted


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
@pytest.mark.parametrize("candidate", ["audio.enc", ".audio.enc.custody.test", ".audio.enc.staging"])
def test_later_custody_and_staging_opens_are_nonblocking(tmp_path, candidate):
    directory = tmp_path / "revision"
    directory.mkdir()
    if candidate != "audio.enc":
        (directory / "audio.enc").write_bytes(b"healthy")
    os.mkfifo(directory / candidate)

    def open_candidate():
        descriptors = ()
        try:
            descriptors = meeting_retention._open_posix_custody_descriptors(directory)
            meeting_retention._zero_posix_staging_residues(directory)
        except OSError:
            pass
        finally:
            for descriptor in descriptors:
                os.close(descriptor)

    child = multiprocessing.get_context("fork").Process(target=open_candidate)
    child.start()
    try:
        child.join(3)
        assert not child.is_alive(), "a later candidate acquisition blocked"
        assert child.exitcode == 0
    finally:
        if child.is_alive():
            child.terminate()
            child.join(3)


def _fifo_expiry(root, late):
    conn = open_db(root / "ledger.db")
    if late:
        real_digest = meeting_retention._digest_file
        final = _audio(root, _payload())

        def digest_then_replace(path):
            result = real_digest(path)
            if path == final:
                final.rename(final.with_name("displaced.enc"))
                os.mkfifo(final)
            return result

        meeting_retention._digest_file = digest_then_replace
    result = meeting_retention.expire_meeting_audio(
        conn, _store(root), now=LATER, alert_path=_alert_path(root))
    assert result.failed and result.exit_code == 1
    assert len(result.deleted) == 1


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
@pytest.mark.parametrize("late", [False, True])
def test_fifo_cannot_block_a_healthy_due_neighbour(tmp_path, late):
    conn = open_db(tmp_path / "ledger.db")
    bad, healthy = _payload(), _payload(day="2026-09-03")
    _import(conn, tmp_path, bad)
    _import(conn, tmp_path, healthy)
    final = _audio(tmp_path, bad)
    if not late:
        final.rename(final.with_name("displaced.enc"))
        os.mkfifo(final)
    conn.close()
    child = multiprocessing.get_context("fork").Process(target=_fifo_expiry, args=(tmp_path, late))
    child.start()
    try:
        child.join(3)
        assert not child.is_alive(), "one FIFO stalled the entire retention command"
        assert child.exitcode == 0
        assert not _audio(tmp_path, healthy).exists()
        assert final.with_name("displaced.enc").stat().st_size > 0
    finally:
        if child.is_alive():
            child.terminate()
            child.join(3)


@pytest.mark.parametrize("fault", ["meeting_receipts", "meeting_archive", "meeting_alerts", "vault_web_base"])
def test_real_retention_cli_deletes_before_reporting_ancillary_fault(tmp_path, monkeypatch, capsys, fault):
    from teammem.cli import main
    _hub(tmp_path, monkeypatch)
    conn = open_db(tmp_path / "ledger.db")
    payload = _payload()
    _import(conn, tmp_path, payload)
    conn.close()
    if fault == "vault_web_base":
        monkeypatch.setenv("TEAMMEM_VAULT_WEB_BASE", "bad-base")
    else:
        directory = tmp_path / fault.replace("_", "-")
        directory.mkdir(exist_ok=True)
        directory.chmod(0o755)
    assert main(["expire-meeting-audio", "--now", EXPIRY]) != 0
    assert not _audio(tmp_path, payload).exists()
    assert all(p.stat().st_size == 0 for p in _custody_files(_store(tmp_path)))
    assert "error:" in capsys.readouterr().err
    if fault == "meeting_alerts":
        assert not list(directory.iterdir())


def test_revision_import_during_render_keeps_receipt_and_remote_artifact_coherent(tmp_path, monkeypatch):
    payload = render_payload()
    conn = _validated_seeded(tmp_path, payload)
    other = open_db(tmp_path / "l.db")
    original_hash = get_meeting(conn, OCCURRENCE)["artifact_hash"]
    vault, bare, _ = _vault(tmp_path)
    real_render = meeting_publish.render_vault_verified

    def concurrent_revision(*args, **kwargs):
        newer = render_payload()
        newer["revision"] = 4
        newer["summary"] = "A newer approved summary."
        import_revision(other, newer)
        return real_render(*args, **kwargs)

    monkeypatch.setattr(meeting_publish, "render_vault_verified", concurrent_revision)
    receipt = _publish(conn, vault, tmp_path / "receipt.json")
    note = _git(bare, "show", f"{receipt['vault_commit']}:{receipt['markdown_path']}")
    assert receipt["revision"] == 3
    assert receipt["artifact_hash"] == original_hash
    assert "revision: 3\n" in note
    assert "A newer approved summary." not in note
    assert get_meeting(conn, OCCURRENCE)["revision"] == 4
    assert not conn.in_transaction


@pytest.mark.parametrize("rule", ["Projects/", "Person/", "Meetings/README.md", "filter"])
@pytest.mark.parametrize("consumer", ["publish", "render"])
def test_git_tree_must_contain_verified_companion_pages_and_bytes(tmp_path, rule, consumer):
    conn = _validated_seeded(tmp_path, render_payload())
    vault, bare, branch = _vault(tmp_path)
    before = _remote_ref(bare, branch)
    if rule == "filter":
        (vault / ".gitattributes").write_text("*.md filter=damage\n")
        _git(vault, "config", "filter.damage.clean", "sed s/Atlas/Changed/g")
    else:
        (vault / ".gitignore").write_text(rule + "\n")
    receipt = tmp_path / "receipt.json"
    if consumer == "publish":
        with pytest.raises(meeting_publish.PublishRefused):
            _publish(conn, vault, receipt)
        assert not receipt.exists()
    else:
        cfg = Config(db_path=tmp_path / "l.db", vault_dir=vault, config_dir=CONFIG_DIR)
        assert services.run_render(cfg, IdentityMaps.load(CONFIG_DIR), today=TODAY,
                                   conn=conn, push_requested=True) == 1
    assert _remote_ref(bare, branch) == before


def test_verified_commit_does_not_create_a_hook_mutated_local_ancestor(tmp_path):
    conn = _validated_seeded(tmp_path, render_payload())
    vault, bare, branch = _vault(tmp_path)
    before = _git(vault, "rev-parse", "HEAD")
    hook = vault / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nprintf 'changed by hook\\n' >> Projects/README.md\ngit add Projects/README.md\n")
    hook.chmod(0o700)
    with pytest.raises(meeting_publish.PublishRefused):
        _publish(conn, vault, tmp_path / "receipt.json")
    assert _git(vault, "rev-parse", "HEAD") == before
    assert _remote_ref(bare, branch) == before
    assert "changed by hook" in (vault / "Projects" / "README.md").read_text()
    assert "changed by hook" not in _git(vault, "log", "-p", "--all", "--", "Projects/README.md")


def test_verified_commit_preserves_operator_hook_checks(tmp_path):
    conn = _validated_seeded(tmp_path, render_payload())
    vault, _, _ = _vault(tmp_path)
    hook = vault / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nprintf 'checked\\n' > .git/hook-ran\n")
    hook.chmod(0o700)
    _publish(conn, vault, tmp_path / "receipt.json")
    assert (vault / ".git" / "hook-ran").read_text() == "checked\n"


def test_failed_rollback_preserves_unrelated_ref_and_blocks_later_publication(tmp_path, monkeypatch):
    from teammem import vaultgit
    conn = _validated_seeded(tmp_path, render_payload())
    vault, bare, branch = _vault(tmp_path)
    before = _remote_ref(bare, branch)
    hook = vault / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nprintf 'changed by hook\\n' >> Projects/README.md\ngit add Projects/README.md\n")
    hook.chmod(0o700)
    real_reject = vaultgit._reject_owned_commit
    independent = []

    def concurrent_commit(path, ref, parent, rejected):
        hook.unlink()
        _git(vault, "commit", "--allow-empty", "-m", "independent temporary writer")
        independent.append(_git(vault, "rev-parse", "HEAD"))
        return real_reject(path, ref, parent, rejected)

    monkeypatch.setattr(vaultgit, "_reject_owned_commit", concurrent_commit)
    with pytest.raises(meeting_publish.PublishRefused, match="rollback refused"):
        _publish(conn, vault, tmp_path / "receipt.json")
    assert _git(vault, "rev-parse", "HEAD") == independent[0]
    assert _remote_ref(bare, branch) == before
    monkeypatch.undo()
    with pytest.raises(meeting_publish.PublishRefused):
        _publish(conn, vault, tmp_path / "receipt.json")
    assert _git(vault, "rev-parse", "HEAD") == independent[0]
    assert _remote_ref(bare, branch) == before
    with pytest.raises(vaultgit.VerifiedTreeMismatch):
        vaultgit.push(vault)


def test_rejected_first_commit_restores_unborn_ref(tmp_path):
    from teammem import vaultgit
    vault = tmp_path / "empty-vault"
    vaultgit.ensure_repo(vault)
    (vault / "README.md").write_text("verified\n")
    hook = vault / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nprintf 'changed\\n' > README.md\ngit add README.md\n")
    hook.chmod(0o700)
    with pytest.raises(vaultgit.VerifiedTreeMismatch):
        vaultgit.commit_all(vault, "first", verified_manifest={"README.md": b"verified\n"})
    import subprocess
    assert subprocess.run(["git", "-C", str(vault), "rev-parse", "--verify", "HEAD"],
                          capture_output=True).returncode != 0


@pytest.mark.parametrize("scaffold", ["- {}", "+ {}", "1. {}", "> **Speaker {}:** {}", "Speaker {}: {}", "- **{}**", "- _{}_", "- __{}__", "- **Speaker {}**: {}", "**Speaker {}:**\n{}", "### Speaker {}:\n{}"])
@pytest.mark.parametrize("field", ["preserved", "summary"])
def test_complete_ordered_markdown_turns_cannot_reach_remote(tmp_path, scaffold, field):
    copied = "\n".join(scaffold.format(index, turn) if scaffold.count("{}") == 2
                       else scaffold.format(turn) for index, turn in enumerate(SHORT_TURNS))
    payload = _short_transcript_payload()
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
    assert all(turn not in str(failure.value) for turn in SHORT_TURNS)
    assert not receipt.exists()
    assert _remote_ref(bare, branch) == before


def test_spoken_colon_turn_is_not_erased_as_speaker_scaffolding(tmp_path):
    conn = _validated_seeded(tmp_path, _short_transcript_payload("Yes:", "No."))
    vault, _, _ = _vault(tmp_path)
    (vault / "Docs" / "copied.md").write_text("**Speaker:**\n- Yes:\n**Speaker:**\n- No.\n")
    with pytest.raises(meeting_publish.PublishRefused):
        _publish(conn, vault, tmp_path / "receipt.json")


@pytest.mark.parametrize(("copied", "leaks"), [("_Yes_", True), ("__Yes__", True),
                                               ("___Yes___", True),
                                               ("prefix_Yes_suffix", False),
                                               ("prefix___Yes___suffix", False),
                                               ("prefix__Yes__", False),
                                               ("__Yes__suffix", False)])
def test_tiny_transcript_markdown_emphasis_is_not_an_identifier(tmp_path, copied, leaks):
    conn = _validated_seeded(tmp_path, _short_transcript_payload("Yes"))
    vault, _, _ = _vault(tmp_path)
    (vault / "Docs" / "copied.md").write_text(copied)
    if leaks:
        with pytest.raises(meeting_publish.PublishRefused):
            _publish(conn, vault, tmp_path / "receipt.json")
    else:
        assert _publish(conn, vault, tmp_path / "receipt.json")["status"] == "published"


@pytest.mark.parametrize("prefix", ["- ", "1. ", "> ", "**Speaker:** ", "### Speaker:\n", "1. **Speaker:** "])
@pytest.mark.parametrize("wrapper", ["*{}*", "_{}_", "**{}**", "__{}__", "`{}`", "~~{}~~",
                                     "***{}***", "___{}___", "~~**_{}_**~~", "**`{}`**"])
@pytest.mark.parametrize("field", ["preserved", "summary"])
def test_complete_short_transcript_formatting_matrix(tmp_path, prefix, wrapper, field):
    copied = "\n".join(prefix + wrapper.format(turn) for turn in SHORT_TURNS)
    payload = _short_transcript_payload()
    if field == "summary":
        payload["summary"] = "Status:\n" + copied
    conn = _validated_seeded(tmp_path, payload)
    vault, bare, branch = _vault(tmp_path)
    before = _remote_ref(bare, branch)
    if field == "preserved":
        (vault / "Docs" / "copied.md").write_text(copied)
    with pytest.raises(meeting_publish.PublishRefused) as failure:
        _publish(conn, vault, tmp_path / "receipt.json")
    assert all(turn not in str(failure.value) for turn in SHORT_TURNS)
    assert not (tmp_path / "receipt.json").exists()
    assert _remote_ref(bare, branch) == before


@pytest.mark.parametrize("prefix", ["李雷： ", "李雷：\n"])
@pytest.mark.parametrize("field", ["preserved", "summary"])
def test_multilingual_speaker_labels_preserve_complete_turn_detection(tmp_path, prefix, field):
    copied = "\n".join(prefix + turn for turn in SHORT_TURNS)
    payload = _short_transcript_payload()
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


def test_multilingual_speaker_normalization_does_not_change_spoken_punctuation(tmp_path):
    copied = "\n".join("李雷： " + turn.replace(".", "。") for turn in SHORT_TURNS)
    conn = _validated_seeded(tmp_path, _short_transcript_payload())
    vault, bare, _ = _vault(tmp_path)
    (vault / "Docs" / "control.md").write_text(copied)
    receipt = _publish(conn, vault, tmp_path / "receipt.json")
    assert _git(bare, "show", f"{receipt['vault_commit']}:Docs/control.md") == copied


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_read_view_preserves_caller_transaction_and_sees_its_uncommitted_rows(tmp_path, journal_mode):
    from teammem.store import ledger_read_view
    conn = _validated_seeded(tmp_path, render_payload())
    conn.execute(f"PRAGMA journal_mode={journal_mode}")
    conn.execute("UPDATE meeting_revisions SET summary='uncommitted summary' WHERE revision=3")
    with ledger_read_view(conn) as view:
        assert get_meeting(view, OCCURRENCE)["summary"] == "uncommitted summary"
        conn.execute("UPDATE meeting_revisions SET summary='later summary' WHERE revision=3")
        assert get_meeting(view, OCCURRENCE)["summary"] == "uncommitted summary"
    assert conn.in_transaction
    conn.rollback()
    assert get_meeting(conn, OCCURRENCE)["summary"] == render_payload()["summary"]


@pytest.mark.parametrize("field", ["preserved", "summary"])
def test_direct_render_does_not_commit_ordered_markdown_transcripts(tmp_path, field, capsys):
    copied = "\n".join("- **Speaker:** " + turn for turn in SHORT_TURNS)
    payload = _short_transcript_payload()
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
        assert code == 0  # the invalid meeting was withheld from a healthy projection
        paths = _git(bare, "ls-tree", "-r", "--name-only", branch).splitlines()
        assert [path for path in paths if path.startswith("Meetings/")] == ["Meetings/README.md"]
        for path in paths:
            content = _git(bare, "show", f"{branch}:{path}")
            assert all(turn not in content for turn in SHORT_TURNS)
    diagnostics = capsys.readouterr().err
    assert all(turn not in diagnostics for turn in SHORT_TURNS)


@pytest.mark.parametrize("copied", [
    "- Red fox seven.",
    "- Gold moon thirteen.\n- Blue kite eleven.\n- Red fox seven.",
    "- Red fox seven.\n- Blue kite eleventh.\n- Gold moon thirteen.",
    "- red fox seven.\n- Blue kite eleven.\n- Gold moon thirteen.",
    "- NotRed fox seven.\n- Blue kite eleven.\n- Gold moon thirteen.",
    "- Red fox seven\n- Blue kite eleven\n- Gold moon thirteen",
    "- Red fox seven.\nUnrelated intervening prose.\n- Blue kite eleven.\n- Gold moon thirteen.",
])
def test_markdown_controls_remain_publishable(tmp_path, copied):
    conn = _validated_seeded(tmp_path, _short_transcript_payload())
    vault, bare, _ = _vault(tmp_path)
    (vault / "Docs" / "control.md").write_text(copied)
    receipt = _publish(conn, vault, tmp_path / "receipt.json")
    assert _git(bare, "show", f"{receipt['vault_commit']}:Docs/control.md") == copied


def test_generated_publication_intervals_leave_processing_room_at_every_clock_phase(tmp_path):
    import plistlib
    import re
    from teammem import schedule_unix
    from tests.test_schedule import _meeting_cfg

    cfg = _meeting_cfg(tmp_path)
    units = schedule_unix._meeting_unit_text(cfg, 18, 20, "/temporary/teammem")
    timer = units[schedule_unix.SYSTEMD_MEETINGS_TIMER].decode()
    interval_minutes = int(re.search(r"OnCalendar=\*-\*-\* \*:00/(\d+):00", timer)[1])
    jobs = dict(schedule_unix._launchd_jobs(cfg, 18, 20, "/temporary/teammem", tmp_path))
    plist = plistlib.loads(jobs[schedule_unix.MEETINGS_LABEL])
    for interval in (interval_minutes * 60, plist["StartInterval"]):
        # Virtual healthy-online clock only: actual upstream processing and OS
        # wake delays require acceptance evidence, not a scheduler unit test.
        for milliseconds_after_poll in range(1, 60_000, 137):
            ready_at = milliseconds_after_poll / 1000
            next_poll = (int(ready_at // interval) + 1) * interval
            assert next_poll - ready_at <= 60
            assert next_poll - ready_at + 8 * 60 < 10 * 60
