"""Opt-in historical synthesis repair from observed source fingerprints.

Callers hold the canonical ledger lock. Baselines observe reviewed existing
caches without interpreting their legacy hash contracts; subsequent input changes
remain pending until their derived output is persisted successfully.
"""

import json
import os
import re
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from .config import Config
from .identity import IdentityMaps
from .queries import report_context, week_monday
from .services import _report_dailies, execute_journal, execute_report
from .slices import active_person_days, daily_person_projects, daily_person_slice, slice_hash
from .store import get_summary
from .summarize import LLM, prepare_daily_journal, validate_daily_journal

BASELINE_VERSION = 1


@dataclass(frozen=True)
class HistoricalReconciliationResult:
    pending_days: tuple[str, ...]
    pending_reports: tuple[str, ...]
    orphans: tuple[str, ...]
    failures: tuple[str, ...]
    attempts: int


def baseline_path(cfg: Config) -> Path:
    ledger = cfg.db_path.resolve()
    return ledger.with_name(ledger.name + '.reconciliation.json')


def _hash(value: object) -> str:
    return slice_hash(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')))


def _scope(cfg: Config, today: date) -> str:
    start = cfg.reconcile_from
    if not start or date.fromisoformat(start).isoformat() != start or start > today.isoformat():
        raise ValueError('reconciliation requires a valid journal-start date through today')
    if not 1 <= cfg.reconcile_batch_size <= 100:
        raise ValueError('reconciliation batch size must be between 1 and 100')
    return start


def source_fingerprints(conn: sqlite3.Connection, ids: IdentityMaps, start: str, end: str) -> dict[str, str]:
    """Hash actual prompt evidence; deliberately exclude model/prompt contracts."""
    return {
        f'{person}|{day}': _hash([
            person, day, ids.display_name(person), daily_person_projects(conn, person, day),
            daily_person_slice(conn, person, day),
        ])
        for person, day in active_person_days(conn, start, end)
    }


def _weekly_fingerprints(conn, ids, sources, start, today):
    weeks = {week_monday(date.fromisoformat(key.split('|', 1)[1])) for key in sources}
    weeks.update(date.fromisoformat(key.split('|', 1)[1]) for (key,) in conn.execute(
        "SELECT key FROM summaries WHERE kind='weekly-team'"
    ))
    result = {}
    for monday in sorted(weeks):
        if not week_monday(date.fromisoformat(start)) <= monday <= week_monday(today):
            continue
        dailies = _report_dailies(conn, monday)
        context = report_context(conn, monday, today, ids, {(d.person, d.day) for d in dailies})
        result[monday.isoformat()] = _hash({
            'dailies': [[d.person, d.day, d.text, sources.get(f'{d.person}|{d.day}')] for d in dailies],
            'flags': context.effective_flags,
            'state': asdict(context.state) | {'target_monday': monday.isoformat()},
        })
    return result


def _valid_journal(record) -> bool:
    if record is None:
        return False
    try:
        validate_daily_journal(record.text)
    except ValueError:
        return False
    return True


def _write(path: Path, state: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.' + path.name, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(state, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if hasattr(os, 'O_DIRECTORY'):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _load(cfg: Config) -> dict:
    path = baseline_path(cfg)
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError
        state = json.loads(path.read_text())
        if (state['version'] != BASELINE_VERSION
                or state['ledger'] != os.path.normcase(str(cfg.db_path.resolve()))
                or state['journal_start'] != cfg.reconcile_from
                or not isinstance(state['initialized_at'], str)
                or not isinstance(state['orphans'], dict)):
            raise ValueError
        for kind in ('daily', 'weekly'):
            if not isinstance(state[kind], dict):
                raise ValueError
            for key, digest in state[kind].items():
                if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
                    raise ValueError
                day = key.split('|', 1)[1] if kind == 'daily' else key
                parsed = date.fromisoformat(day)
                if parsed.isoformat() != day:
                    raise ValueError
                if kind == 'weekly' and parsed.weekday() != 0:
                    raise ValueError
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        raise ValueError('reconciliation baseline missing or invalid; restore it or explicitly initialize') from None
    return state


def initialize_baseline(cfg: Config, ids: IdentityMaps, *, conn: sqlite3.Connection, today: date) -> Path:
    """Explicit initialization after review; refuses to replace any prior state."""
    start = _scope(cfg, today)
    path = baseline_path(cfg)
    if path.exists() or path.is_symlink():
        raise ValueError('reconciliation baseline already exists')
    sources = source_fingerprints(conn, ids, start, today.isoformat())
    state = {
        'version': BASELINE_VERSION,
        'ledger': os.path.normcase(str(cfg.db_path.resolve())),
        'journal_start': start,
        'initialized_at': datetime.now(timezone.utc).isoformat(),
        'daily': {key: digest for key, digest in sources.items()
                  if _valid_journal(get_summary(conn, 'daily-person', key))},
        'weekly': {week: digest for week, digest in _weekly_fingerprints(conn, ids, sources, start, today).items()
                   if get_summary(conn, 'weekly-team', 'team|' + week) is not None},
        'orphans': {},
    }
    _write(path, state)
    return path


def reconcile_history(cfg: Config, ids: IdentityMaps, *, conn: sqlite3.Connection,
                      today: date, daily_llm: LLM, report_llm: LLM) -> HistoricalReconciliationResult:
    start = _scope(cfg, today)
    state = _load(cfg)
    sources = source_fingerprints(conn, ids, start, today.isoformat())
    failures, orphans = [], []
    attempts = 0
    for key in sorted(state['daily'].keys() - sources.keys()):
        record = get_summary(conn, 'daily-person', key)
        if record is None:
            # The operator already disposed of this derived cache. Retain any
            # existing receipt; no source or summary is deleted by this service.
            del state['daily'][key]
            continue
        orphans.append(key)
        state['orphans'].setdefault(key, {
            'source_hash': state['daily'][key], 'summary': asdict(record),
            'observed_at': datetime.now(timezone.utc).isoformat(),
        })

    pending = set(orphans)
    for key, digest in sorted(sources.items(), key=lambda item: (item[0].split('|', 1)[1], item[0])):
        record = get_summary(conn, 'daily-person', key)
        valid = _valid_journal(record)
        if state['daily'].get(key) == digest and valid:
            continue
        person, day = key.split('|', 1)
        prepared = prepare_daily_journal(conn, person, ids.display_name(person), day, [])
        if valid and record.input_hash == prepared.input_hash:
            # Normal rolling synthesis, or a retry after cache commit but before
            # baseline replacement, already wrote the exact current inputs.
            state['daily'][key] = digest
            continue
        if attempts >= cfg.reconcile_batch_size:
            pending.add(key)
            continue
        attempts += 1
        result = execute_journal(cfg, ids, conn=conn, start_day=start, end_day=today.isoformat(),
                                 created_ts=f'{today.isoformat()}T00:00:00', llm=daily_llm,
                                 person_days=[(person, day)], force=True)
        if result.failures:
            pending.add(key)
            failures.extend(f'{f.person}|{f.day}: {f.detail}' for f in result.failures)
        else:
            state['daily'][key] = digest

    weekly = _weekly_fingerprints(conn, ids, sources, start, today)
    blocked = {week_monday(date.fromisoformat(key.split('|', 1)[1])).isoformat() for key in pending}
    pending_reports = []
    for week, digest in weekly.items():
        if state['weekly'].get(week) == digest and get_summary(conn, 'weekly-team', 'team|' + week) is not None:
            continue
        if week in blocked or attempts >= cfg.reconcile_batch_size:
            pending_reports.append(week)
            continue
        attempts += 1
        result = execute_report(cfg, ids, conn=conn, target_week=date.fromisoformat(week),
                                operator_date=today, llm=report_llm)
        if result.status in {'generated', 'cached'}:
            state['weekly'][week] = digest
        else:
            pending_reports.append(week)
            failures.append(f'{week}: {result.detail}')
    _write(baseline_path(cfg), state)
    return HistoricalReconciliationResult(tuple(sorted(pending)), tuple(pending_reports),
                                           tuple(orphans), tuple(failures), attempts)
