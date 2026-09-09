"""Publication races use independent processes and temporary bare remotes only."""

import json
import multiprocessing
import subprocess
import threading
from pathlib import Path

import pytest

from teammem.config import Config
from teammem.events import Event
from teammem.identity import IdentityMaps
from teammem.store import insert_events, open_db
from tests.test_meeting_publish import OCCURRENCE, WEB_BASE, _git, _remote_ref, _vault
from tests.test_meeting_render import CONFIG_DIR, TODAY, _seeded


def _assert_locked(vault):
    """Probe the real OS lock, bypassing same-thread publication reentrancy."""
    from teammem.run_lock import RunLockedError, acquire_run_lock

    with pytest.raises(RunLockedError):
        with acquire_run_lock(vault, wait_seconds=0):
            pass


def test_publication_lock_reenters_alias_but_excludes_other_threads(tmp_path):
    from teammem import run_lock

    lock = getattr(run_lock, "acquire_publication_lock", None)
    assert callable(lock), "publication needs one reentrant canonical-vault lock"
    root = tmp_path / "real"
    root.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    vault = root / "new" / "vault"
    outcomes = []

    def other_thread():
        try:
            with lock(vault, wait_seconds=0):
                outcomes.append("acquired")
        except run_lock.RunLockedError:
            outcomes.append("contended")

    with pytest.raises(LookupError, match="body failed"):
        with lock(vault, wait_seconds=0):
            with lock(alias / "new" / "vault", wait_seconds=0):
                with lock(root / "different-vault", wait_seconds=0):
                    _assert_locked(vault)
            thread = threading.Thread(target=other_thread)
            thread.start()
            thread.join(5)
            assert not thread.is_alive()
            assert outcomes == ["contended"]
            raise LookupError("body failed")
    with lock(vault, wait_seconds=0):
        pass
    assert not vault.exists()
    assert Path(f"{vault}.lock").exists()


def _fork_probe(vault, results):
    from teammem.run_lock import RunLockedError, acquire_publication_lock

    try:
        with acquire_publication_lock(vault, wait_seconds=0):
            results.put("acquired")
    except RunLockedError:
        results.put("contended")


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(),
                    reason="fork inheritance is POSIX-only")
def test_fork_does_not_inherit_reentrant_ownership(tmp_path):
    from teammem import run_lock

    lock = getattr(run_lock, "acquire_publication_lock", None)
    assert callable(lock), "publication needs PID-bound ownership"
    context = multiprocessing.get_context("fork")
    results = context.Queue()
    with lock(tmp_path / "vault", wait_seconds=0):
        child = context.Process(target=_fork_probe, args=(tmp_path / "vault", results))
        child.start()
        try:
            assert results.get(timeout=5) == "contended"
        finally:
            child.join(5)
            if child.is_alive():
                child.terminate()
                child.join(5)
        assert child.exitcode == 0


def test_standalone_docs_sync_holds_the_shared_vault_lock(tmp_path, monkeypatch):
    from dataclasses import replace
    from teammem import services

    cfg = replace(_config(tmp_path, tmp_path / "vault"),
                  obsidian_projects=tmp_path / "source")
    cfg.obsidian_projects.mkdir()
    real_sync = services.sync_docs

    def sync(*args):
        _assert_locked(cfg.vault_dir)
        return real_sync(*args)

    monkeypatch.setattr(services, "sync_docs", sync)
    assert services.run_docs_sync(cfg) == 0


def test_render_first_run_missing_parent_and_read_only_modes_create_no_lock(tmp_path):
    from teammem import services
    from teammem.render import render_vault_verified

    conn = _seeded(tmp_path)
    vault = tmp_path / "missing" / "parent" / "vault"
    cfg = _config(tmp_path, vault)
    ids = IdentityMaps.load(CONFIG_DIR)
    try:
        assert services.run_render(cfg, ids, today=TODAY, dry_run=True, conn=conn) == 0
        assert not vault.parent.exists()
        render_vault_verified(conn, ids, vault, TODAY)
        assert services.run_render(cfg, ids, today=TODAY, verify=True, conn=conn) == 0
        assert not Path(f"{vault}.lock").exists()
        assert services.run_render(cfg, ids, today=TODAY, conn=conn) == 0
        assert Path(f"{vault}.lock").exists()
        assert not any(path.endswith(".lock") for path in _commit_tree(vault, "HEAD"))
        fresh = _config(tmp_path, tmp_path / "entirely" / "missing" / "vault")
        assert services.run_render(fresh, ids, today=TODAY, conn=conn) == 0
    finally:
        conn.close()


def test_direct_publisher_holds_lock_through_push_and_receipt(tmp_path, monkeypatch):
    from teammem import meeting_publish

    conn = _seeded(tmp_path)
    vault, bare, branch = _vault(tmp_path)
    real_push, real_receipt = meeting_publish.push_verified, meeting_publish._write_receipt
    seen = []

    def push(*args):
        _assert_locked(vault)
        result = real_push(*args)
        seen.append("push")
        return result

    def receipt(*args):
        _assert_locked(vault)
        real_receipt(*args)
        _assert_locked(vault)
        seen.append("receipt")

    monkeypatch.setattr(meeting_publish, "push_verified", push)
    monkeypatch.setattr(meeting_publish, "_write_receipt", receipt)
    try:
        output = meeting_publish.publish_meeting(
            conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY,
            occurrence_id=OCCURRENCE, receipt_path=tmp_path / "receipt.json",
            web_base=WEB_BASE)
        assert seen == ["push", "receipt"]
        assert _remote_ref(bare, branch) == output["vault_commit"]
    finally:
        conn.close()


def test_lock_contention_is_retryable_and_redacted_for_public_callers(tmp_path, monkeypatch, capsys):
    from dataclasses import replace
    from teammem import daily, meeting_publish, services
    from teammem.run_lock import acquire_publication_lock, acquire_run_lock
    from tests.test_daily import NOW, _settings

    vault, _bare, _branch = _vault(tmp_path)
    conn = _seeded(tmp_path)
    cfg = replace(_config(tmp_path, vault), obsidian_projects=tmp_path / "source")
    ids = IdentityMaps.load(CONFIG_DIR)
    for module in (daily, meeting_publish, services):
        monkeypatch.setattr(module, "acquire_publication_lock",
                            lambda path: acquire_publication_lock(path, wait_seconds=0))
    monkeypatch.setattr(daily, "resolve_llm_backend", lambda *args: None)
    try:
        with acquire_run_lock(vault, wait_seconds=0):
            assert services.run_render(cfg, ids, today=TODAY, conn=conn) == 1
            assert services.run_docs_sync(cfg) == 1
            with pytest.raises(meeting_publish.PublishRefused) as failure:
                meeting_publish.publish_meeting(
                    conn, ids, vault, TODAY, occurrence_id=OCCURRENCE,
                    receipt_path=tmp_path / "receipt.json", web_base=WEB_BASE)
            assert failure.value.stage == "lock"
            result = daily.run_daily(cfg, ids, _settings(), NOW, connectors={})
            assert result.exit_code == 1
            assert result.status("render") == "failed"
            assert result.status("push") == "skipped"
            assert str(tmp_path) not in result.step("render").detail
        assert not (tmp_path / "receipt.json").exists()
        output = capsys.readouterr()
        assert "publication lock unavailable" in output.err
        assert str(tmp_path) not in output.err
    finally:
        conn.close()


def test_daily_holds_ledger_then_vault_through_docs_render_and_separate_push(
        tmp_path, monkeypatch):
    from dataclasses import replace
    from teammem import daily
    from tests.test_daily import NOW, _settings

    vault, bare, branch = _vault(tmp_path)
    cfg = replace(_config(tmp_path, vault), push=True,
                  obsidian_projects=tmp_path / "source")
    cfg.obsidian_projects.mkdir()
    monkeypatch.setattr(daily, "resolve_llm_backend", lambda *args: None)
    observed = []

    def reporter(event):
        if event.stage in {"docs-sync", "render", "push"}:
            _assert_locked(cfg.db_path)
            _assert_locked(vault)
            observed.append(event.stage)

    result = daily.run_daily(cfg, IdentityMaps.load(CONFIG_DIR), _settings(), NOW,
                             connectors={}, reporter=reporter)
    assert result.exit_code == 0
    assert {"docs-sync", "render", "push"} <= set(observed)
    assert _remote_ref(bare, branch) == _git(vault, "rev-parse", "HEAD")


def _config(root, vault, ledger_name="l.db"):
    return Config.load(env={
        "TEAMMEM_DB": str(root / ledger_name),
        "TEAMMEM_CONFIG_DIR": str(CONFIG_DIR),
        "TEAMMEM_VAULT": str(vault),
        "TEAMMEM_PUSH": "false",
    })


def _working_tree(vault):
    """Independent full add -A candidate map, without touching Git's index."""
    paths = _git(vault, "ls-files", "-c", "-o", "--exclude-standard", "-z")
    result = {}
    for rel in set(paths.strip("\0").split("\0")):
        path = vault / rel
        if not path.is_file():
            continue
        mode = "100755" if path.stat().st_mode & 0o111 else "100644"
        blob = subprocess.run(
            ["git", "hash-object", "--stdin"], input=path.read_bytes(),
            capture_output=True, check=True).stdout.decode().strip()
        result[rel] = (mode, blob)
    return result


def _commit_tree(vault, commit):
    result = {}
    for entry in _git(vault, "ls-tree", "-r", "-z", commit).strip("\0").split("\0"):
        metadata, rel = entry.split("\t", 1)
        mode, _kind, blob = metadata.split()
        result[rel] = (mode, blob)
    return result


def _publish_paused(root, vault, verified, release, results):
    from teammem import meeting_publish

    real_render = meeting_publish.render_vault_verified

    def render(*args, **kwargs):
        output = real_render(*args, **kwargs)
        results.put(("verified", _working_tree(vault)))
        verified.set()
        if not release.wait(20):
            raise RuntimeError("test did not release verified publisher")
        return output

    meeting_publish.render_vault_verified = render
    conn = open_db(root / "l.db")
    try:
        receipt = meeting_publish.publish_meeting(
            conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY,
            occurrence_id=OCCURRENCE, receipt_path=root / "receipt.json",
            web_base=WEB_BASE)
        results.put(("published", receipt))
    except BaseException as error:
        results.put(("error", repr(error)))
        raise
    finally:
        conn.close()


def _competing_render(root, vault, ledger_name, outcome, finished):
    from teammem import run_lock, services

    real_try = run_lock._try_lock
    signaled = False

    def attempt(*args):
        nonlocal signaled
        acquired = real_try(*args)
        if not acquired and not signaled:
            signaled = True
            outcome.put("contended")
        return acquired

    run_lock._try_lock = attempt
    conn = open_db(root / ledger_name)
    try:
        code = services.run_render(_config(root, vault, ledger_name), IdentityMaps.load(CONFIG_DIR),
                                   today=TODAY, push_requested=True, conn=conn)
        outcome.put(("finished", code))
    finally:
        conn.close()
        finished.set()


@pytest.mark.parametrize("ledger_name", ["l.db", "other-ledger.db"])
def test_verified_meeting_tree_cannot_be_replaced_by_another_process(tmp_path, ledger_name):
    """Dropping either public entry's lock lets B replace A's verified bytes."""
    _seeded(tmp_path).close()
    if ledger_name != "l.db":
        _seeded(tmp_path, name=ledger_name).close()
    vault, bare, branch = _vault(tmp_path)
    context = multiprocessing.get_context("spawn")
    verified, release, finished = (context.Event() for _ in range(3))
    results, outcome = context.Queue(), context.Queue()
    publisher = context.Process(target=_publish_paused,
                                args=(tmp_path, vault, verified, release, results))
    competitor = context.Process(target=_competing_render,
                                 args=(tmp_path, vault, ledger_name, outcome, finished))
    publisher.start()
    try:
        assert verified.wait(15), "publisher did not reach verification"
        kind, expected_tree = results.get(timeout=5)
        assert kind == "verified"
        conn = open_db(tmp_path / ledger_name)
        try:
            insert_events(conn, [Event(
                person="alex", project="project-alpha", ts="2026-09-03T09:00:00Z",
                source="gitlab", kind="commit", summary="concurrent-marker-unique",
                hash="concurrent-marker-unique")])
        finally:
            conn.close()
        competitor.start()
        assert outcome.get(timeout=15) == "contended", "B rendered while A was verified"
        assert not finished.is_set()
        release.set()
        kind, receipt = results.get(timeout=15)
        assert kind == "published", receipt
        assert outcome.get(timeout=15) == ("finished", 0)
        publisher.join(10)
        competitor.join(10)
        assert publisher.exitcode == competitor.exitcode == 0
        assert _commit_tree(vault, receipt["vault_commit"]) == expected_tree
        final = _remote_ref(bare, branch)
        assert final != receipt["vault_commit"]
        _git(vault, "merge-base", "--is-ancestor", receipt["vault_commit"], final)
        assert "concurrent-marker-unique" in _git(vault, "grep", "concurrent-marker-unique", final)
        assert json.loads((tmp_path / "receipt.json").read_text()) == receipt
        assert not any(path.endswith(".lock") for path in _commit_tree(vault, final))
    finally:
        release.set()
        for process in (publisher, competitor):
            if process.pid is not None:
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
