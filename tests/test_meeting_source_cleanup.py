"""Accepted source ciphertext stays governed through failures and restarts."""

import errno
import os
import json
import sqlite3
import multiprocessing

import pytest

from teammem import meeting_importer
from teammem.meeting_bundles import occurrence_path_segment
from teammem.meeting_retention import expire_meeting_audio
from teammem.meeting_store import get_meeting
from teammem.store import open_db
from tests.test_meeting_bundles import write_meeting_bundle
from tests.test_meeting_importer import _Tree, _audio_file, _payload, _run
from tests.test_services import bench
from teammem import services


def _fail_archive(monkeypatch):
    def fail(*args):
        raise OSError(errno.EACCES, "private-source-path transcript secret")
    monkeypatch.setattr(meeting_importer, "_archive_bundle", fail)


def _debt(conn):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "meeting_source_cleanup" in tables, "accepted source cleanup must be durable"
    return conn.execute("SELECT * FROM meeting_source_cleanup WHERE completed = 0").fetchall()


def _expire(conn, tree, now="2026-09-16T14:52:00.000000Z", **kwargs):
    (tree.vault.parent / "alerts").mkdir(exist_ok=True)
    return expire_meeting_audio(
        conn, tree.audio, now=now,
        alert_path=lambda oid: tree.vault.parent / "alerts" / (occurrence_path_segment(oid) + ".json"),
        **kwargs,
    )


def test_repeated_archive_failure_is_one_durable_obligation(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "ledger.db")
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload)
    _fail_archive(monkeypatch)

    for attempt in range(3):
        result = _run(conn, tree)
        assert len(_debt(conn)) == 1
        assert getattr(result, "cleanup_pending", ()) == ((payload["occurrence_id"], 3),)
        assert (source / "audio.enc").stat().st_size == payload["audio"]["bytes"]
        assert not tree.quarantine.exists()
    assert get_meeting(conn, payload["occurrence_id"]) is not None


def test_restart_cleans_partial_source_even_after_inbox_setting_moves(tmp_path, monkeypatch):
    db = tmp_path / "ledger.db"
    conn = open_db(db)
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload)
    _fail_archive(monkeypatch)
    _run(conn, tree)
    (source / "READY").unlink()
    (source / "meeting.json").unlink()
    conn.close()
    tree.inbox = tmp_path / "new-inbox-not-created"
    monkeypatch.undo()

    conn = open_db(db)
    result = _run(conn, tree)

    assert not (source / "audio.enc").exists() or (source / "audio.enc").stat().st_size == 0
    assert _debt(conn) == []
    assert result.cleanup_pending == ()
    assert _audio_file(tree, payload).stat().st_size == payload["audio"]["bytes"]
    assert not tree.inbox.exists()


@pytest.mark.parametrize("replacement", ["file", "symlink", "directory", "occurrence_symlink"])
def test_deferred_source_cleanup_preserves_replacements(tmp_path, monkeypatch, replacement):
    conn = open_db(tmp_path / "ledger.db")
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload)
    original = (source / "audio.enc").read_bytes()
    _fail_archive(monkeypatch)
    _run(conn, tree)
    (source / "READY").unlink()
    if replacement == "occurrence_symlink":
        occurrence = source.parent
        replacement_dir = tmp_path / "replacement-occurrence"
        occurrence.rename(replacement_dir)
        occurrence.symlink_to(replacement_dir, target_is_directory=True)
    elif replacement == "directory":
        source.rename(source.with_name("parked"))
        source.mkdir()
        (source / "audio.enc").write_bytes(original)
    else:
        (source / "audio.enc").rename(source / "original-owned.enc")
        if replacement == "file":
            (source / "audio.enc").write_bytes(original)
        else:
            outside = tmp_path / "outside.enc"
            outside.write_bytes(original)
            (source / "audio.enc").symlink_to(outside)
    monkeypatch.undo()

    result = _run(conn, tree)

    assert getattr(result, "cleanup_pending", ()) == ((payload["occurrence_id"], 3),)
    assert len(_debt(conn)) == 1
    if replacement == "occurrence_symlink":
        assert source.parent.is_symlink()
    assert (source / "audio.enc").read_bytes() == original
    assert not tree.quarantine.exists()


def test_exact_expiry_destroys_primary_and_all_source_copies(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "ledger.db")
    tree = _Tree(tmp_path)
    payload = _payload()
    first = write_meeting_bundle(tree.inbox, payload)
    _fail_archive(monkeypatch)
    _run(conn, tree)
    tree.inbox = tmp_path / "second-inbox"
    tree.inbox.mkdir()
    second = write_meeting_bundle(tree.inbox, payload)
    _run(conn, tree)

    assert len(_debt(conn)) == 2
    before = _expire(conn, tree, "2026-09-16T14:51:59.999999Z")
    assert before.deleted == ()
    assert all((source / "audio.enc").stat().st_size > 0 for source in (first, second))
    expired = _expire(conn, tree)
    assert expired.deleted == ((payload["occurrence_id"], 3),)
    assert not _audio_file(tree, payload).exists()
    assert all(not (source / "audio.enc").exists() or (source / "audio.enc").stat().st_size == 0
               for source in (first, second))
    assert _debt(conn) == []
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] == "2026-09-16T14:52:00.000000Z"


def test_expiry_source_failure_still_destroys_primary_and_retries_mark(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "ledger.db")
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload)
    _fail_archive(monkeypatch)
    _run(conn, tree)
    audio = source / "audio.enc"
    owned = audio.read_bytes()
    original_open = os.open

    def deny_source(path, flags, *args, **kwargs):
        if os.fspath(path) == str(audio) or (os.fspath(path) == "audio.enc" and flags & os.O_RDWR):
            raise OSError(errno.EACCES, "private-source-path transcript secret")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny_source)
    result = _expire(conn, tree)
    assert result.exit_code == 1
    assert any(f.stage == "source_cleanup" for f in result.failed)
    assert result.deleted == ()
    assert not _audio_file(tree, payload).exists()
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] is None
    assert audio.read_bytes() == owned
    assert all("private-source-path" not in f.detail for f in result.failed)
    monkeypatch.undo()

    retried = _expire(conn, tree)
    assert retried.deleted == ((payload["occurrence_id"], 3),)
    assert _debt(conn) == []


def test_source_debt_is_inside_revision_transaction(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "ledger.db")
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload)
    real_import = meeting_importer.import_meeting_revision

    def inspect_transaction(*args, **kwargs):
        original_guard = kwargs["before_commit"]

        def reject():
            assert len(_debt(conn)) == 1
            original_guard()
            raise OSError("injected commit refusal")

        kwargs["before_commit"] = reject
        return real_import(*args, **kwargs)

    monkeypatch.setattr(meeting_importer, "import_meeting_revision", inspect_transaction)
    with pytest.raises(OSError, match="injected commit refusal"):
        _run(conn, tree)
    assert _debt(conn) == []
    assert get_meeting(conn, payload["occurrence_id"]) is None
    assert (source / "audio.enc").stat().st_size > 0


def test_cleanup_failure_alerts_nonzero_without_blocking_publication(bench, monkeypatch, capsys):
    payload = _payload()
    source = bench.drop(payload)
    _fail_archive(monkeypatch)

    code = bench.run()
    assert code == 1
    assert bench.receipt(payload["occurrence_id"]).is_file()
    alert = services.meeting_alert_path(bench.cfg, payload["occurrence_id"])
    cleanup_alert = alert.with_name(alert.stem + ".source-cleanup.json")
    data = json.loads(cleanup_alert.read_text())
    assert data["status"] == "source_cleanup_pending"
    assert data["revisions"] == [3]
    output = capsys.readouterr()
    assert "source_cleanup_pending" in output.err
    for private in (str(source), payload["title"], payload["audio"]["sha256"],
                    "private-source-path transcript secret", payload["transcript"][0]["text"]):
        assert private not in output.err + cleanup_alert.read_text()
    alert.write_text("independent incomplete-transcript warning")
    monkeypatch.undo()

    assert bench.run() == 0
    assert not cleanup_alert.exists()
    assert alert.read_text() == "independent incomplete-transcript warning"


@pytest.mark.parametrize("caller_connection", [False, True])
def test_retention_dry_run_migrates_only_snapshot(bench, caller_connection, capsys):
    conn = open_db(bench.cfg.db_path)
    conn.execute("DROP TABLE IF EXISTS meeting_source_cleanup")
    conn.commit()
    before_bytes = bench.cfg.db_path.read_bytes()
    before_dump = tuple(conn.iterdump())

    code = services.run_expire_meeting_audio(
        bench.cfg, now="2026-09-16T14:52:00Z", dry_run=True,
        conn=conn if caller_connection else None,
    )

    assert code == 0
    assert bench.cfg.db_path.read_bytes() == before_bytes
    assert tuple(conn.iterdump()) == before_dump
    assert conn.execute("SELECT 1").fetchone() == (1,)
    assert not bench.cfg.meeting_alerts.exists()
    assert not bench.cfg.meeting_audio_store.exists()
    conn.close()


def test_retention_pending_source_writes_separate_redacted_alert(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "ledger.db")
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload)
    _fail_archive(monkeypatch)
    _run(conn, tree)
    (source / "audio.enc").rename(source / "original-owned.enc")
    (source / "audio.enc").write_bytes(b"replacement")

    result = _expire(conn, tree)

    assert result.exit_code == 1
    alerts = list((tmp_path / "alerts").glob("*.source-cleanup.json"))
    assert len(alerts) == 1
    assert json.loads(alerts[0].read_text())["status"] == "source_cleanup_pending"
    assert str(source) not in alerts[0].read_text()


def test_replay_after_prior_expiry_still_governs_new_source_copy(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "ledger.db")
    tree = _Tree(tmp_path)
    payload = _payload()
    write_meeting_bundle(tree.inbox, payload)
    _run(conn, tree)
    _expire(conn, tree)
    source = write_meeting_bundle(tree.inbox, payload)
    _fail_archive(monkeypatch)
    _run(conn, tree, now="2026-09-17T00:00:00Z")

    _expire(conn, tree, "2026-09-17T00:00:00Z")

    assert not (source / "audio.enc").exists() or (source / "audio.enc").stat().st_size == 0
    assert _debt(conn) == []
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] == "2026-09-16T14:52:00.000000Z"


def test_source_replaced_before_commit_cannot_acquire_cleanup_ownership(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "ledger.db")
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload)
    original = (source / "audio.enc").read_bytes()
    real_stage = meeting_importer._stage_audio

    def swap_source(*args):
        adopted = real_stage(*args)
        (source / "audio.enc").rename(source / "original-owned.enc")
        (source / "audio.enc").write_bytes(original)
        return adopted

    monkeypatch.setattr(meeting_importer, "_stage_audio", swap_source)

    with pytest.raises(OSError, match="source cleanup"):
        _run(conn, tree)
    assert get_meeting(conn, payload["occurrence_id"]) is None
    assert _debt(conn) == []
    assert (source / "audio.enc").read_bytes() == original


def test_failed_debt_clear_retries_after_ciphertext_already_destroyed(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "ledger.db")
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload)
    _fail_archive(monkeypatch)
    _run(conn, tree)
    (source / "READY").unlink()

    def fail_clear(*args):
        raise sqlite3.OperationalError("injected clear failure")

    monkeypatch.setattr(meeting_importer, "clear_source_cleanup", fail_clear)
    assert _run(conn, tree).cleanup_pending
    assert (source / "audio.enc").stat().st_size == 0
    assert len(_debt(conn)) == 1
    monkeypatch.undo()

    assert _run(conn, tree).cleanup_pending == ()
    assert _debt(conn) == []


def test_both_dry_runs_leave_pending_source_and_alerts_unchanged(bench, monkeypatch):
    payload = _payload()
    source = bench.drop(payload)
    _fail_archive(monkeypatch)
    assert bench.run() == 1
    (source / "READY").unlink()  # retry must neither need nor repair this marker

    def snapshot():
        return {
            str(path): (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
            for root in (bench.cfg.meeting_inbox, bench.cfg.meeting_archive,
                         bench.cfg.meeting_audio_store, bench.cfg.meeting_alerts)
            if root.exists() for path in root.rglob("*") if path.is_file()
        }, bench.cfg.db_path.read_bytes()

    before = snapshot()
    assert bench.run(dry_run=True) == 1
    assert snapshot() == before
    assert services.run_expire_meeting_audio(
        bench.cfg, now="2026-09-16T14:52:00Z", dry_run=True) == 1
    assert snapshot() == before


def test_deferred_destruction_zeros_owned_inode_without_unlinking_racing_replacement(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "ledger.db")
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload)
    _fail_archive(monkeypatch)
    _run(conn, tree)
    (source / "READY").unlink()
    real_destroy = meeting_importer._discard_owned_descriptor
    escaped = tmp_path / "displaced-owned.enc"

    def replace_then_destroy(descriptor):
        (source / "audio.enc").rename(escaped)
        (source / "audio.enc").write_bytes(b"untouched replacement")
        real_destroy(descriptor)

    monkeypatch.setattr(meeting_importer, "_discard_owned_descriptor", replace_then_destroy)
    _run(conn, tree)
    assert escaped.read_bytes() == b""
    assert (source / "audio.enc").read_bytes() == b"untouched replacement"
    assert _debt(conn) == []


@pytest.mark.parametrize("when", ["retry_validation", "after_commit"])
def test_source_replacement_before_archive_is_preserved(tmp_path, monkeypatch, when):
    conn = open_db(tmp_path / "ledger.db")
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload)
    if when == "retry_validation":
        _fail_archive(monkeypatch)
        _run(conn, tree)
        monkeypatch.undo()
        target = "load_meeting_bundle"
    else:
        target = "import_meeting_revision"
    real_call = getattr(meeting_importer, target)

    def replace_after_call(*args, **kwargs):
        result = real_call(*args, **kwargs)
        source.rename(source.with_name("old-owned"))
        write_meeting_bundle(tree.inbox, payload)
        return result

    monkeypatch.setattr(meeting_importer, target, replace_after_call)
    result = _run(conn, tree)
    assert source.is_dir(), "replaced source directory must not be archived or deleted"
    assert (source / "audio.enc").stat().st_size == payload["audio"]["bytes"]
    assert result.cleanup_pending == ((payload["occurrence_id"], 3),)


def test_windows_partial_source_cleanup_uses_owned_handle(tmp_path, monkeypatch):
    from tests.test_meeting_importer import _WindowsCustodyAttackOS, _force_windows

    conn = open_db(tmp_path / "ledger.db")
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload)
    _fail_archive(monkeypatch)
    _run(conn, tree)
    (source / "READY").unlink()
    windows = _WindowsCustodyAttackOS(tmp_path / "unexpected-replacement")
    windows.store_started = True
    _force_windows(monkeypatch, meeting_importer, windows)

    result = _run(conn, tree)

    assert result.cleanup_pending == ()
    assert not (source / "audio.enc").exists()
    assert _debt(conn) == []
    assert _audio_file(tree, payload).stat().st_size == payload["audio"]["bytes"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO replacement")
def test_fifo_source_replacement_reports_pending_without_blocking(tmp_path, monkeypatch):
    db = tmp_path / "ledger.db"
    conn = open_db(db)
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload)
    _fail_archive(monkeypatch)
    _run(conn, tree)
    (source / "READY").unlink()
    (source / "audio.enc").rename(source / "original-owned.enc")
    os.mkfifo(source / "audio.enc")
    conn.close()

    def retry():
        with open_db(db) as reopened:
            assert _run(reopened, tree).cleanup_pending == ((payload["occurrence_id"], 3),)

    process = multiprocessing.get_context("fork").Process(target=retry)
    process.start()
    try:
        process.join(timeout=2)
        assert process.exitcode == 0, "source cleanup must not block opening a replacement FIFO"
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=2)
    assert (source / "original-owned.enc").stat().st_size == payload["audio"]["bytes"]


@pytest.mark.parametrize("when", ["initial_archive", "retry_archive"])
def test_archive_metadata_write_race_preserves_replacement_and_destroys_owned_inode(
    tmp_path, monkeypatch, when,
):
    conn = open_db(tmp_path / "ledger.db")
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload)
    if when == "retry_archive":
        _fail_archive(monkeypatch)
        _run(conn, tree)
        monkeypatch.undo()
    original = meeting_importer._write_atomic
    escaped = tmp_path / "renamed-owned-source"
    raced = False

    def replace_during_metadata(path, blob):
        nonlocal raced
        if path.is_relative_to(tree.archive) and not raced:
            raced = True
            source.rename(escaped)
            write_meeting_bundle(tree.inbox, payload)
            (source / "replacement-marker").write_text("unowned replacement")
        return original(path, blob)

    monkeypatch.setattr(meeting_importer, "_write_atomic", replace_during_metadata)
    result = _run(conn, tree)

    assert raced
    assert source.is_dir(), "metadata I/O must not permit deleting a replacement directory"
    assert (source / "replacement-marker").read_text() == "unowned replacement"
    assert (source / "audio.enc").stat().st_size == 2048
    assert (escaped / "audio.enc").stat().st_size == 0
    assert result.cleanup_pending == ()
    assert _debt(conn) == []


def test_completed_source_placeholder_is_skipped_on_repeated_normal_runs(tmp_path):
    conn = open_db(tmp_path / "ledger.db")
    tree = _Tree(tmp_path)
    source = write_meeting_bundle(tree.inbox, _payload())
    assert _run(conn, tree).accepted == 1
    assert source.is_dir(), "POSIX archive completion retains owned empty placeholders"
    assert (source / "audio.enc").stat().st_size == 0
    for _ in range(3):
        result = _run(conn, tree)
        assert result.quarantined == 0
        assert result.cleanup_pending == ()
        assert not tree.quarantine.exists()


@pytest.mark.parametrize("delivery", ["same_inode", "replacement_directory", "new_revision"])
def test_completed_tombstone_does_not_hide_fresh_delivery(tmp_path, monkeypatch, delivery):
    conn = open_db(tmp_path / "ledger.db")
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload)
    assert _run(conn, tree).accepted == 1
    assert source.exists(), "archive must not remove source namespace by pathname"
    if delivery == "replacement_directory":
        source.rename(tmp_path / "completed-old-source")
    elif delivery == "new_revision":
        payload = _payload(revision=4)
    source = write_meeting_bundle(tree.inbox, payload)
    _fail_archive(monkeypatch)

    result = _run(conn, tree)

    assert result.quarantined == 0
    assert result.unchanged == (delivery != "new_revision")
    assert result.activated == (delivery == "new_revision")
    assert result.cleanup_pending == ((payload["occurrence_id"], payload["revision"]),)
    assert (source / "audio.enc").stat().st_size == 2048
    assert len(_debt(conn)) == 1


def test_existing_cleanup_table_migrates_idempotently_without_source_io(tmp_path, monkeypatch):
    db = tmp_path / "ledger.db"
    conn = open_db(db)
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload)
    _fail_archive(monkeypatch)
    _run(conn, tree)
    captured = conn.execute(
        "SELECT occurrence_id, revision, source_root, relative_path, identity FROM meeting_source_cleanup"
    ).fetchall()
    conn.execute("ALTER TABLE meeting_source_cleanup DROP COLUMN completed")
    conn.commit()
    conn.close()
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in source.iterdir()}

    for _ in range(2):
        conn = open_db(db)  # migration precedes every subsequent store/API query
        assert conn.execute(
            "SELECT occurrence_id, revision, source_root, relative_path, identity FROM meeting_source_cleanup"
        ).fetchall() == captured
        assert conn.execute("SELECT completed FROM meeting_source_cleanup").fetchall() == [(0,)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()
    assert {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in source.iterdir()} == before
