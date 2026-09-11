"""Deadline delivery through the real service, ledger, audio and private wake FIFO."""

import importlib
import importlib.util
import os
import select
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from teammem import cli, meeting_store, services
from teammem.config import Config
from teammem.meeting_bundles import canonical_artifact_hash
from teammem.meeting_store import StagedAudio, get_meeting, import_meeting_revision
from teammem.store import open_db, open_db_snapshot
from tests.test_meeting_retention import _audio, _import, _payload


def worker():
    assert importlib.util.find_spec("teammem.meeting_retention_worker"), "deadline worker missing"
    return importlib.import_module("teammem.meeting_retention_worker")


def config(root):
    return Config(db_path=root / "ledger.db", vault_dir=root / "vault",
                  meeting_inbox=root / "meeting-inbox", meeting_archive=root / "meeting-archive",
                  meeting_quarantine=root / "meeting-quarantine", meeting_audio_store=root / "meeting-audio",
                  meeting_receipts=root / "meeting-receipts", meeting_alerts=root / "meeting-alerts",
                  vault_web_base="https://git.example.com/team/vault")


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def stop():
    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)


def direct(conn, payload, *, source=None, before=lambda: None):
    return import_meeting_revision(
        conn, payload=payload, occurrence_id=payload["occurrence_id"], revision=payload["revision"],
        artifact_hash=canonical_artifact_hash(payload), imported_at="2026-09-02T16:00:00Z",
        audio=StagedAudio("/unused/audio.enc", payload["audio"]["sha256"], payload["audio"]["bytes"]),
        before_commit=before, source_cleanup=source)


def test_worker_waits_until_fractional_deadline_and_closes_database_before_wait(tmp_path):
    module = worker()
    payload = _payload(ended_at="2026-09-02T14:52:00.500000Z")
    conn = open_db(tmp_path / "ledger.db")
    _import(conn, tmp_path, payload)
    clock = [instant("2026-09-16T14:52:00.499999Z")]
    waits = []

    def wait(fd, timeout):
        waits.append(timeout)
        with sqlite3.connect(tmp_path / "ledger.db", timeout=0) as writer:
            writer.execute("BEGIN EXCLUSIVE")
            writer.execute("UPDATE meetings SET title=title")
        if len(waits) == 1:
            assert _audio(tmp_path, payload).exists()
            assert timeout == pytest.approx(0.000001)
            clock[0] += timedelta(microseconds=1)
        else:
            assert not _audio(tmp_path, payload).exists()
            assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] == "2026-09-16T14:52:00.500000Z"
            stop()

    old = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    assert module.watch_meeting_audio(config(tmp_path), clock=lambda: clock[0], wait=wait) == 0
    assert {sig: signal.getsignal(sig) for sig in old} == old
    assert len(waits) == 2
    conn.close()


def test_startup_overdue_keeps_original_expiry_and_reports_catchup(tmp_path, capsys):
    module = worker()
    payload = _payload()
    conn = open_db(tmp_path / "ledger.db")
    _import(conn, tmp_path, payload)
    module.watch_meeting_audio(config(tmp_path), clock=lambda: instant("2026-09-20T09:00:00Z"),
                              wait=lambda fd, timeout: stop())
    audio = get_meeting(conn, payload["occurrence_id"])["audio"]
    assert audio["expires_at"] == "2026-09-16T14:52:00.000000Z"
    assert audio["deleted_at"] == "2026-09-20T09:00:00.000000Z"
    assert "late" in capsys.readouterr().err.lower()
    conn.close()


def test_earlier_import_wakes_wait_before_archive_and_deletes_at_new_deadline(tmp_path, monkeypatch):
    module = worker()
    conn = open_db(tmp_path / "ledger.db")
    late = _payload(day="2026-09-03")
    _import(conn, tmp_path, late)
    early = _payload(ended_at="2026-09-02T14:52:00.500000Z")
    clock = [instant("2026-09-16T14:51:59.500000Z")]
    calls = []

    def wait(fd, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            from teammem import meeting_importer
            archive = meeting_importer._archive_owned_source

            def checked(*args):
                assert select.select([fd], [], [], 0)[0] == [fd]
                assert get_meeting(conn, early["occurrence_id"]) is not None
                return archive(*args)

            monkeypatch.setattr(meeting_importer, "_archive_owned_source", checked)
            _import(conn, tmp_path, early)
        elif len(calls) == 2:
            assert timeout == 1
            clock[0] += timedelta(seconds=1)
        else:
            assert not _audio(tmp_path, early).exists()
            assert _audio(tmp_path, late).exists()
            stop()

    module.watch_meeting_audio(config(tmp_path), clock=lambda: clock[0], wait=wait)
    assert len(calls) == 3
    conn.close()


def test_commit_between_deadline_query_and_wait_is_not_drained_away(tmp_path, monkeypatch):
    module = worker()
    conn = open_db(tmp_path / "ledger.db")
    assert hasattr(meeting_store, "retention_deadlines"), "fresh stored deadline query missing"
    original = meeting_store.retention_deadlines
    committed = []

    def query(*args, **kwargs):
        answer = original(*args, **kwargs)
        if not committed:
            direct(conn, _payload())
            committed.append(True)
        return answer

    monkeypatch.setattr(meeting_store, "retention_deadlines", query)

    def wait(fd, timeout):
        assert select.select([fd], [], [], 0)[0] == [fd]
        stop()

    module.watch_meeting_audio(config(tmp_path), clock=lambda: instant("2026-09-02T16:00:00Z"), wait=wait)
    conn.close()


def test_durable_store_import_and_replay_notify_but_rollback_and_snapshot_do_not(tmp_path):
    module = worker()
    conn = open_db(tmp_path / "ledger.db")
    payload = _payload()
    with module.retention_channel(tmp_path / "ledger.db") as channel:
        direct(conn, payload)
        assert select.select([channel.read_fd], [], [], 0)[0]
        channel.drain()
        direct(conn, payload)
        assert not select.select([channel.read_fd], [], [], 0)[0]
        source = meeting_store.SourceCleanup(str(tmp_path), "source", "identity")
        direct(conn, payload, source=source)
        assert select.select([channel.read_fd], [], [], 0)[0]
        channel.drain()
        with pytest.raises(RuntimeError, match="^rollback$"):
            direct(conn, _payload(revision=4), before=lambda: (_ for _ in ()).throw(RuntimeError("rollback")))
        assert not select.select([channel.read_fd], [], [], 0)[0]
        snapshot = open_db_snapshot(tmp_path / "ledger.db")
        direct(snapshot, _payload(revision=4))
        snapshot.close()
        assert not select.select([channel.read_fd], [], [], 0)[0]
    conn.close()


@pytest.mark.parametrize("replay", [False, True])
def test_notification_failure_cannot_discard_committed_import(tmp_path, monkeypatch, replay):
    module = worker()
    conn = open_db(tmp_path / "ledger.db")
    payload = _payload()
    if replay:
        _import(conn, tmp_path, payload)
    monkeypatch.setattr(module, "notify_committed_import", lambda conn: (_ for _ in ()).throw(OSError("secret path")))
    _import(conn, tmp_path, payload)
    assert get_meeting(conn, payload["occurrence_id"]) is not None
    assert _audio(tmp_path, payload).stat().st_size > 0
    assert not list((tmp_path / "meeting-quarantine").iterdir())
    conn.close()


def test_warning_boundary_does_not_spin_and_expiry_still_wins(tmp_path):
    module = worker()
    conn = open_db(tmp_path / "ledger.db")
    payload = _payload(transcript_complete=False)
    _import(conn, tmp_path, payload)
    clock = [instant("2026-09-13T14:51:59Z")]
    calls = []

    def wait(fd, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            assert timeout == 1
            assert not list((tmp_path / "meeting-alerts").glob("*.json"))
            clock[0] += timedelta(seconds=1)
        else:
            assert timeout > 0
            assert list((tmp_path / "meeting-alerts").glob("*.json"))
            stop()

    module.watch_meeting_audio(config(tmp_path), clock=lambda: clock[0], wait=wait)
    conn.close()


def test_old_failed_audio_does_not_starve_next_healthy_deadline(tmp_path, monkeypatch, capsys):
    module = worker()
    conn = open_db(tmp_path / "ledger.db")
    old, new = _payload(), _payload(day="2026-09-03")
    _import(conn, tmp_path, old)
    _import(conn, tmp_path, new)
    real_unlink = os.unlink

    def denied(path, *args, **kwargs):
        if Path(path) == _audio(tmp_path, old):
            raise PermissionError("injected failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", denied)
    clock = [instant("2026-09-17T14:51:59Z")]
    calls = []

    def wait(fd, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            assert timeout == 1
            clock[0] += timedelta(seconds=1)
        else:
            assert timeout > 0
            assert _audio(tmp_path, old).exists()
            assert not _audio(tmp_path, new).exists()
            stop()

    module.watch_meeting_audio(config(tmp_path), clock=lambda: clock[0], wait=wait)
    assert "FAILED" in capsys.readouterr().err
    conn.close()


def test_channel_singleton_nonblocking_coalescing_and_replacement_safe_cleanup(tmp_path):
    module = worker()
    db = tmp_path / "ledger.db"
    conn = open_db(db)
    with module.retention_channel(db) as channel:
        with pytest.raises(RuntimeError):
            with module.retention_channel(db):
                pytest.fail("second worker admitted")
        while True:
            try:
                os.write(channel.write_fd, b"x" * 4096)
            except BlockingIOError:
                break
        module.notify_committed_import(conn)
        assert select.select([channel.read_fd], [], [], 0)[0]
        fifo = Path(str(db) + ".retention") / "wake"
        fifo.unlink()
        fifo.write_text("replacement")
    assert fifo.read_text() == "replacement"
    conn.close()


@pytest.mark.parametrize("kind", ["symlink", "regular", "public"])
def test_unowned_channel_objects_are_preserved(tmp_path, kind):
    module = worker()
    db = tmp_path / "ledger.db"
    conn = open_db(db)
    directory = Path(str(db) + ".retention")
    directory.mkdir(mode=0o700)
    fifo = directory / "wake"
    target = tmp_path / "outside"
    target.write_text("keep")
    if kind == "symlink":
        fifo.symlink_to(target)
    elif kind == "regular":
        fifo.write_text("keep")
    else:
        os.mkfifo(fifo, 0o666)
        fifo.chmod(0o666)
    metadata = fifo.lstat()
    module.notify_committed_import(conn)
    with pytest.raises((OSError, RuntimeError)):
        with module.retention_channel(db):
            pytest.fail("unsafe channel admitted")
    assert fifo.lstat().st_ino == metadata.st_ino
    assert target.read_text() == "keep"
    conn.close()


@pytest.mark.parametrize("arguments", [["--watch", "--dry-run"], ["--watch", "--now", "2026-09-02T16:00:00Z"]])
def test_watch_conflicts_fail_before_loading_configuration(monkeypatch, arguments):
    monkeypatch.setattr(cli, "_load_config", lambda *a, **k: pytest.fail("configuration loaded before validation"))
    assert "--watch" in cli._parser().format_help() or any(
        "--watch" in parser.format_help() for action in cli._parser()._actions
        for parser in (getattr(action, "choices", None) or {}).values())
    with pytest.raises(SystemExit) as error:
        cli.main(["expire-meeting-audio", *arguments])
    assert error.value.code == 2


def test_unsupported_watch_rejected_before_configuration(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "default_env_file", lambda: Path("unused.env"))
    monkeypatch.setattr(cli, "_load_config", lambda *a, **k: pytest.fail("loaded"))
    with pytest.raises(SystemExit) as error:
        cli.main(["expire-meeting-audio", "--watch"])
    assert error.value.code == 2


@pytest.mark.parametrize("movement", [-10, 10])
def test_clock_movement_recomputes_before_destruction(tmp_path, movement, capsys):
    module = worker()
    payload = _payload()
    conn = open_db(tmp_path / "ledger.db")
    _import(conn, tmp_path, payload)
    clock = [instant("2026-09-16T14:51:59Z")]
    calls = []
    def wait(fd, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            clock[0] += timedelta(seconds=movement)
        elif movement < 0 and len(calls) == 2:
            assert _audio(tmp_path, payload).exists()
            assert timeout == 11
            clock[0] += timedelta(seconds=11)
        else:
            assert not _audio(tmp_path, payload).exists()
            stop()
    module.watch_meeting_audio(config(tmp_path), clock=lambda: clock[0], wait=wait)
    if movement > 0:
        assert "late" in capsys.readouterr().err
    conn.close()


def test_missed_notification_is_recovered_without_extending_expiry(tmp_path, monkeypatch, capsys):
    module = worker()
    conn = open_db(tmp_path / "ledger.db")
    clock = [instant("2026-09-16T14:51:59Z")]
    calls = []
    payload = _payload()
    def wait(fd, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            monkeypatch.setattr(module, "notify_committed_import", lambda conn: None)
            _import(conn, tmp_path, payload)
            assert not select.select([fd], [], [], 0)[0]
            assert timeout == 60
            clock[0] += timedelta(seconds=timeout)
        else:
            assert not _audio(tmp_path, payload).exists()
            assert get_meeting(conn, payload["occurrence_id"])["audio"]["expires_at"] == "2026-09-16T14:52:00.000000Z"
            stop()
    module.watch_meeting_audio(config(tmp_path), clock=lambda: clock[0], wait=wait)
    assert "late" in capsys.readouterr().err
    conn.close()


def test_zero_warning_and_current_complete_revision_clear_old_alert(tmp_path):
    module = worker()
    conn = open_db(tmp_path / "ledger.db")
    payload = _payload(transcript_complete=False)
    _import(conn, tmp_path, payload)
    cfg = config(tmp_path)
    calls = []
    def wait(fd, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            assert list(cfg.meeting_alerts.glob("*.json"))
            _import(conn, tmp_path, _payload(revision=4))
        else:
            assert not list(cfg.meeting_alerts.glob("*.json"))
            stop()
    module.watch_meeting_audio(cfg, clock=lambda: instant("2026-09-14T14:52:00Z"), wait=wait)
    cfg.meeting_warn_days = 0
    next_at, _ = meeting_store.retention_deadlines(conn, after=instant("2026-09-13T14:51:59Z"), warn_days=0)
    assert next_at == instant("2026-09-16T14:52:00Z")
    conn.close()


def test_notifier_does_not_create_state_when_worker_is_absent(tmp_path):
    module = worker()
    conn = open_db(tmp_path / "ledger.db")
    before = set(tmp_path.iterdir())
    direct(conn, _payload())
    assert set(tmp_path.iterdir()) == before
    conn.close()


def test_sigint_wakes_idle_channel_and_releases_singleton(tmp_path):
    module = worker()
    db = tmp_path / "ledger.db"
    open_db(db).close()
    previous = signal.getsignal(signal.SIGINT)
    def wait(fd, timeout):
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        assert select.select([fd], [], [], 0)[0] == [fd]
    assert module.watch_meeting_audio(config(tmp_path), wait=wait) == 0
    assert signal.getsignal(signal.SIGINT) == previous
    with module.retention_channel(db):
        pass


def test_time_spent_opening_ledger_does_not_use_stale_deletion_timestamp(tmp_path, monkeypatch):
    module = worker()
    from teammem import store
    conn = open_db(tmp_path / "ledger.db")
    payload = _payload()
    _import(conn, tmp_path, payload)
    clock = [instant("2026-09-16T14:52:00Z")]
    original = store.open_db
    def delayed(path):
        clock[0] += timedelta(seconds=2)
        return original(path)
    monkeypatch.setattr(store, "open_db", delayed)
    module.watch_meeting_audio(config(tmp_path), clock=lambda: clock[0], wait=lambda fd, timeout: stop())
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] == "2026-09-16T14:52:02.000000Z"
    conn.close()


def test_historical_source_debt_after_deleted_mark_is_cleaned_on_worker_restart(tmp_path, monkeypatch):
    module = worker()
    from teammem import meeting_importer
    conn = open_db(tmp_path / "ledger.db")
    payload = _payload()
    _import(conn, tmp_path, payload)
    cfg = config(tmp_path)
    module.watch_meeting_audio(cfg, clock=lambda: instant("2026-09-16T14:52:00Z"), wait=lambda fd, timeout: stop())
    monkeypatch.setattr(meeting_importer, "_archive_owned_source", lambda *args: (_ for _ in ()).throw(OSError("archive failed")))
    _import(conn, tmp_path, payload)
    debts = meeting_store.pending_source_cleanup(conn)
    assert debts
    sources = list(cfg.meeting_inbox.rglob("audio.enc"))
    assert any(path.stat().st_size > 0 for path in sources)
    next_at, overdue = meeting_store.retention_deadlines(conn, after=instant("2026-09-17T00:00:00Z"), warn_days=3)
    assert overdue == instant("2026-09-16T14:52:00Z")
    conn.close()
    module.watch_meeting_audio(cfg, clock=lambda: instant("2026-09-17T00:00:00Z"), wait=lambda fd, timeout: stop())
    conn = open_db(cfg.db_path)
    assert meeting_store.pending_source_cleanup(conn) == []
    assert all(not path.exists() or path.stat().st_size == 0 for path in sources)
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] == "2026-09-16T14:52:00.000000Z"
    conn.close()


def test_worker_preserves_former_root_siblings_but_destroys_recorded_final(tmp_path):
    module = worker()
    conn = open_db(tmp_path / "ledger.db")
    payload = _payload()
    _import(conn, tmp_path, payload)
    final = _audio(tmp_path, payload)
    sibling = final.with_name(".audio.enc.unowned")
    sibling.write_bytes(b"unrelated old sibling")
    custody = next(final.parent.glob(".audio.enc.custody.*"))
    cfg = config(tmp_path)
    cfg.meeting_audio_store = tmp_path / "new-audio"
    cfg.meeting_audio_store.mkdir(mode=0o700)
    module.watch_meeting_audio(cfg, clock=lambda: instant("2026-09-16T14:52:00Z"), wait=lambda fd, timeout: stop())
    assert not final.exists()
    assert custody.read_bytes() == b""
    assert sibling.read_bytes() == b"unrelated old sibling"
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] is not None
    conn.close()


def test_broken_notification_diagnostic_cannot_escape_after_commit(tmp_path, monkeypatch):
    module = worker()
    conn = open_db(tmp_path / "ledger.db")
    monkeypatch.setattr(module, "notify_committed_import", lambda conn: (_ for _ in ()).throw(OSError("failed")))
    logger = meeting_store.logging.getLogger(meeting_store.__name__)
    monkeypatch.setattr(logger, "warning", lambda *a, **k: (_ for _ in ()).throw(OSError("broken log")))
    _import(conn, tmp_path, _payload())
    assert get_meeting(conn, _payload()["occurrence_id"]) is not None
    assert _audio(tmp_path, _payload()).stat().st_size > 0
    conn.close()


def test_directory_disappearing_after_open_closes_descriptor(tmp_path, monkeypatch):
    module = worker()
    db = tmp_path / "ledger.db"
    directory = Path(str(db) + ".retention")
    directory.mkdir(mode=0o700)
    original = os.open
    opened = []
    def removed(path, flags, *args, **kwargs):
        fd = original(path, flags, *args, **kwargs)
        if Path(path) == directory:
            opened.append(fd)
            directory.rmdir()
        return fd
    monkeypatch.setattr(os, "open", removed)
    with pytest.raises(FileNotFoundError):
        with module.retention_channel(db):
            pytest.fail("missing directory used")
    assert opened
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_unexpected_service_failure_retries_without_missing_known_earlier_expiry(tmp_path, monkeypatch, capsys):
    module = worker()
    conn = open_db(tmp_path / "ledger.db")
    payload = _payload()
    _import(conn, tmp_path, payload)
    clock = [instant("2026-09-16T14:51:59Z")]
    real = services.execute_expire_meeting_audio
    attempts = []
    def transient(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("credential-secret")
        return real(*args, **kwargs)
    monkeypatch.setattr(services, "execute_expire_meeting_audio", transient)
    cfg = config(tmp_path)
    cfg.gitlab_token = "credential-secret"
    calls = []
    def wait(fd, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            assert timeout == 1
            clock[0] += timedelta(seconds=1)
        else:
            assert not _audio(tmp_path, payload).exists()
            stop()
    module.watch_meeting_audio(cfg, clock=lambda: clock[0], wait=wait)
    err = capsys.readouterr().err
    assert "retry required" in err and "credential-secret" not in err
    conn.close()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_real_signal_interrupts_native_wait_and_releases_worker(tmp_path, sig):
    module = worker()
    db = tmp_path / "ledger.db"
    open_db(db).close()
    script = """
import sys
from pathlib import Path
from tests.test_meeting_retention_worker import config
from teammem.meeting_retention_worker import watch_meeting_audio, _wait
def waiting(fd, timeout):
    print('worker-waiting', flush=True)
    _wait(fd, timeout)
raise SystemExit(watch_meeting_audio(config(Path(sys.argv[1])), wait=waiting))
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert select.select([process.stdout], [], [], 5)[0], "worker did not reach wait"
        output = os.read(process.stdout.fileno(), 65536)
        assert b"worker-waiting" in output
        process.send_signal(sig)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, (stdout, stderr)
        assert not (Path(str(db) + ".retention") / "wake").exists()
        with module.retention_channel(db):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
