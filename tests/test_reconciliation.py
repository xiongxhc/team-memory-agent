import json
from datetime import date
from dataclasses import replace

import pytest

from teammem.config import Config
from teammem.events import Event
from teammem.identity import IdentityMaps
from teammem.store import SummaryRecord, get_summary, insert_events, open_db, put_summary


TODAY = date(2026, 8, 20)


def runtime(tmp_path, *, batch=25):
    cfg = Config(db_path=tmp_path / 'ledger.db')
    cfg.reconcile_from = '2026-07-01'
    cfg.reconcile_batch_size = batch
    ids = IdentityMaps({'members': {p: {'name': p.title()} for p in ['alex', 'sam', 'blair']}}, {})
    conn = open_db(cfg.db_path)
    insert_events(conn, [event('alex', '2026-07-14', 'first')])
    put_summary(conn, SummaryRecord('daily-person', 'alex|2026-07-14', 'unverifiable-legacy-hash',
                                    '- Existing grounded work.', 'old-model', 'old'))
    put_summary(conn, SummaryRecord('weekly-team', 'team|2026-07-13', 'legacy-report-hash',
                                    '## Shipped\n- Existing work.', 'old-model', 'old'))
    return cfg, ids, conn


def event(person, day, key):
    return Event(person=person, ts=day + 'T10:00:00Z', source='gitlab', kind='commit',
                 summary=key, hash=key, project='project-alpha')


def initialize(cfg, ids, conn):
    from teammem.reconciliation import initialize_baseline
    return initialize_baseline(cfg, ids, conn=conn, today=TODAY)


def run(cfg, ids, conn, calls, *, daily=None, report=None):
    from teammem.reconciliation import reconcile_history
    def daily_llm(_system, user):
        calls.append(('daily', user))
        return '- Reconciled grounded work.'
    def report_llm(_system, user):
        calls.append(('report', user))
        return '## Shipped\n- Reconciled outcome.'
    return reconcile_history(cfg, ids, conn=conn, today=TODAY,
                             daily_llm=daily or daily_llm, report_llm=report or report_llm)


def test_bootstrap_observes_legacy_sources_without_model_or_prompt_invalidation(tmp_path, monkeypatch):
    cfg, ids, conn = runtime(tmp_path)
    path = initialize(cfg, ids, conn)
    original = conn.execute('select * from summaries order by key').fetchall()
    monkeypatch.setattr('teammem.summarize.DAILY_PROMPT_VERSION', 'future')
    monkeypatch.setattr('teammem.summarize.DAILY_HASH_SCHEMA_VERSION', 'future')
    cfg.llm_daily_model = 'new-model'
    calls = []
    result = run(cfg, ids, conn, calls)
    assert result.pending_days == result.pending_reports == ()
    assert not result.failures and not calls
    assert conn.execute('select * from summaries order by key').fetchall() == original
    state = json.loads(path.read_text())
    assert state['version'] == 1
    assert state['journal_start'] == '2026-07-01'
    assert state['ledger'] == str(cfg.db_path.resolve())
    assert state['initialized_at']
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match='already exists'):
        initialize(cfg, ids, conn)


def test_late_historical_event_refreshes_exact_day_and_report(tmp_path):
    cfg, ids, conn = runtime(tmp_path)
    initialize(cfg, ids, conn)
    insert_events(conn, [event('alex', '2026-07-14', 'late')])
    calls = []
    result = run(cfg, ids, conn, calls)
    assert [kind for kind, _ in calls] == ['daily', 'report']
    assert 'late' in calls[0][1]
    assert result.pending_days == result.pending_reports == ()
    calls.clear()
    run(cfg, ids, conn, calls)
    assert calls == []


def test_baseline_does_not_accept_missing_days_or_pre_era_events(tmp_path):
    cfg, ids, conn = runtime(tmp_path)
    insert_events(conn, [event('sam', '2026-07-15', 'missing'),
                         event('alex', '2026-06-01', 'pre-era')])
    initialize(cfg, ids, conn)
    calls = []
    run(cfg, ids, conn, calls)
    assert [kind for kind, _ in calls] == ['daily', 'report']
    assert 'slug: sam' in calls[0][1]
    assert get_summary(conn, 'daily-person', 'alex|2026-06-01') is None


def test_combined_batch_bound_keeps_report_pending_for_next_run(tmp_path):
    cfg, ids, conn = runtime(tmp_path, batch=1)
    initialize(cfg, ids, conn)
    insert_events(conn, [event('alex', '2026-07-14', 'late')])
    calls = []
    first = run(cfg, ids, conn, calls)
    assert [kind for kind, _ in calls] == ['daily']
    assert first.pending_days == ()
    assert first.pending_reports == ('2026-07-13',)
    calls.clear()
    second = run(cfg, ids, conn, calls)
    assert [kind for kind, _ in calls] == ['report']
    assert second.pending_reports == ()


def test_failures_preserve_caches_and_pending_work(tmp_path):
    cfg, ids, conn = runtime(tmp_path)
    initialize(cfg, ids, conn)
    before = get_summary(conn, 'daily-person', 'alex|2026-07-14')
    insert_events(conn, [event('alex', '2026-07-14', 'late')])
    def invalid(_s, _u):
        return 'Could you provide more information?'
    failed = run(cfg, ids, conn, [], daily=invalid)
    assert failed.pending_days == ('alex|2026-07-14',)
    assert failed.failures
    assert get_summary(conn, 'daily-person', 'alex|2026-07-14') == before
    def offline(_s, _u):
        raise RuntimeError('offline')
    failed_report = run(cfg, ids, conn, [], report=offline)
    assert failed_report.pending_days == ()
    assert failed_report.pending_reports == ('2026-07-13',)
    calls = []
    recovered = run(cfg, ids, conn, calls)
    assert [kind for kind, _ in calls] == ['report']
    assert not recovered.failures


def test_noop_reinsert_and_normal_journal_refresh_avoid_extra_models(tmp_path):
    from teammem.services import execute_journal
    cfg, ids, conn = runtime(tmp_path)
    initialize(cfg, ids, conn)
    with conn:
        conn.execute('delete from events')
    insert_events(conn, [event('alex', '2026-07-14', 'first')])
    calls = []
    run(cfg, ids, conn, calls)
    assert calls == []
    insert_events(conn, [event('alex', '2026-07-14', 'late')])
    execute_journal(cfg, ids, conn=conn, start_day='2026-07-14', end_day='2026-07-14',
                    created_ts='now', llm=lambda _s, _u: '- Updated normal journal.')
    run(cfg, ids, conn, calls)
    assert [kind for kind, _ in calls] == ['report']


def test_mapping_and_unmapped_changes_refresh_following_gap_flags(tmp_path):
    cfg, ids, conn = runtime(tmp_path)
    insert_events(conn, [event('sam', '2026-07-21', 'second-week')])
    put_summary(conn, SummaryRecord('daily-person', 'sam|2026-07-21', 'legacy',
                                    '- Second week.', 'old', 'old'))
    put_summary(conn, SummaryRecord('weekly-team', 'team|2026-07-20', 'legacy',
                                    '## Shipped\n- Second week.', 'old', 'old'))
    initialize(cfg, ids, conn)
    insert_events(conn, [event('blair', '2026-07-14', 'new-person'),
                         event('_unmapped/new@example.com', '2026-07-21', 'unmapped')])
    calls = []
    run(cfg, ids, conn, calls)
    reports = [prompt for kind, prompt in calls if kind == 'report']
    assert len(reports) == 2
    later = next(p for p in reports if 'Week of 2026-07-20' in p)
    assert 'blair' in later and '_unmapped/new@example.com' in later


def test_disappeared_sources_keep_summary_and_orphan_receipt(tmp_path):
    cfg, ids, conn = runtime(tmp_path)
    path = initialize(cfg, ids, conn)
    previous = get_summary(conn, 'daily-person', 'alex|2026-07-14')
    with conn:
        conn.execute('delete from events')
    calls = []
    result = run(cfg, ids, conn, calls)
    assert result.orphans == ('alex|2026-07-14',)
    assert result.pending_days == ('alex|2026-07-14',)
    assert not calls
    assert get_summary(conn, 'daily-person', previous.key) == previous
    receipt = json.loads(path.read_text())['orphans'][previous.key]
    assert receipt['summary']['text'] == previous.text
    assert receipt['source_hash']


@pytest.mark.parametrize('damage', ['missing', 'invalid-json', 'future-version', 'wrong-ledger'])
def test_state_damage_fails_closed_without_models_or_reset(tmp_path, damage):
    cfg, ids, conn = runtime(tmp_path)
    path = initialize(cfg, ids, conn)
    if damage == 'missing':
        path.unlink()
    elif damage == 'invalid-json':
        path.write_text('broken')
    else:
        state = json.loads(path.read_text())
        state['version' if damage == 'future-version' else 'ledger'] = 999
        path.write_text(json.dumps(state))
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(ValueError, match='baseline'):
        run(cfg, ids, conn, [])
    assert (path.read_bytes() if path.exists() else None) == before


def test_four_following_report_dependencies_stop_at_fifth_week(tmp_path):
    from datetime import timedelta
    cfg, ids, conn = runtime(tmp_path)
    for offset in range(1, 6):
        day = (date(2026, 7, 14) + timedelta(weeks=offset)).isoformat()
        week = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        insert_events(conn, [event('sam', day, f'week-{offset}')])
        put_summary(conn, SummaryRecord('daily-person', f'sam|{day}', 'legacy',
                                        '- Following activity.', 'old', 'old'))
        put_summary(conn, SummaryRecord('weekly-team', f'team|{week}', 'legacy',
                                        '## Shipped\n- Following activity.', 'old', 'old'))
    initialize(cfg, ids, conn)
    insert_events(conn, [event('blair', '2026-07-14', 'new-gap-evidence')])
    calls = []
    result = run(cfg, ids, conn, calls)
    reports = [prompt for kind, prompt in calls if kind == 'report']
    assert len(reports) == 5  # own week plus four subsequent gap windows
    assert not any('Week of 2026-08-17' in prompt for prompt in reports)
    assert not result.pending_reports


def test_atomic_replace_failure_preserves_state_and_retry_uses_written_caches(tmp_path, monkeypatch):
    import teammem.reconciliation as reconciliation
    cfg, ids, conn = runtime(tmp_path)
    path = initialize(cfg, ids, conn)
    original = path.read_bytes()
    insert_events(conn, [event('alex', '2026-07-14', 'late')])
    real_replace = reconciliation.os.replace
    monkeypatch.setattr(reconciliation.os, 'replace', lambda *_: (_ for _ in ()).throw(OSError('disk unavailable')))
    with pytest.raises(OSError, match='disk unavailable'):
        run(cfg, ids, conn, [])
    assert path.read_bytes() == original
    assert not list(path.parent.glob('.' + path.name + '*'))
    monkeypatch.setattr(reconciliation.os, 'replace', real_replace)
    calls = []
    result = run(cfg, ids, conn, calls)
    assert not calls and not result.pending_days and not result.pending_reports


def test_missing_report_cache_is_recreated_even_when_fingerprint_unchanged(tmp_path):
    cfg, ids, conn = runtime(tmp_path)
    initialize(cfg, ids, conn)
    with conn:
        conn.execute("DELETE FROM summaries WHERE kind='weekly-team'")
    calls = []
    result = run(cfg, ids, conn, calls)
    assert [kind for kind, _ in calls] == ['report']
    assert not result.pending_reports


def test_directory_sync_failure_surfaces_and_persisted_cache_retry_is_safe(tmp_path, monkeypatch):
    import os
    import stat
    import teammem.reconciliation as reconciliation
    if not hasattr(os, 'O_DIRECTORY'):
        pytest.skip('directory fsync is not supported on this platform')
    cfg, ids, conn = runtime(tmp_path)
    path = initialize(cfg, ids, conn)
    insert_events(conn, [event('alex', '2026-07-14', 'late')])
    real_fsync = os.fsync
    def failed_directory_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('directory sync unavailable')
        return real_fsync(fd)
    monkeypatch.setattr(reconciliation.os, 'fsync', failed_directory_sync)
    with pytest.raises(OSError, match='directory sync unavailable'):
        run(cfg, ids, conn, [])
    assert json.loads(path.read_text())['version'] == 1
    monkeypatch.setattr(reconciliation.os, 'fsync', real_fsync)
    calls = []
    result = run(cfg, ids, conn, calls)
    assert not calls and not result.pending_days and not result.pending_reports


def test_baseline_write_without_directory_open_support(tmp_path, monkeypatch):
    import teammem.reconciliation as reconciliation
    monkeypatch.delattr(reconciliation.os, 'O_DIRECTORY', raising=False)
    cfg, ids, conn = runtime(tmp_path)
    assert initialize(cfg, ids, conn).exists()


@pytest.mark.parametrize('output', ['', '   \n', 'Could you provide the daily journals?',
                                    '## Shipped\n\nI need more context to write the report.'])
def test_invalid_weekly_generation_retains_good_report_and_retries(tmp_path, output):
    cfg, ids, conn = runtime(tmp_path)
    initialize(cfg, ids, conn)
    original = get_summary(conn, 'weekly-team', 'team|2026-07-13')
    insert_events(conn, [event('alex', '2026-07-14', 'late')])
    result = run(cfg, ids, conn, [], report=lambda _s, _u: output)
    assert result.pending_reports == ('2026-07-13',)
    assert result.failures
    assert get_summary(conn, 'weekly-team', original.key) == original
    calls = []
    recovered = run(cfg, ids, conn, calls)
    assert [kind for kind, _ in calls] == ['report']
    assert not recovered.pending_reports and not recovered.failures
