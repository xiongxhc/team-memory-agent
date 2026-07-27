"""Directory importer for reviewed member bundles."""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .bundles import BundleError, bundle_events, load_bundle
from .identity import IdentityMaps
from .store import insert_events


@dataclass(frozen=True)
class ImportResult:
    accepted: int = 0
    quarantined: int = 0
    events: int = 0
    inserted: int = 0


def _archive(path: Path, raw: bytes, member: str, archive: Path) -> None:
    digest = hashlib.sha256(raw).hexdigest()
    destination = archive / member / path.name / f"{digest}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.read_bytes() == raw:
        path.unlink()
        return

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    path.unlink()


def _quarantine(path: Path, inbox: Path, quarantine: Path,
                reason: str) -> None:
    try:
        relative = path.absolute().relative_to(inbox.absolute())
    except ValueError:
        relative = Path(path.name)
    raw = b"" if path.is_symlink() else path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    destination = quarantine / relative.parent / relative.name / f"{digest}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        target = str(path.readlink())
        path.unlink()
        destination.write_text(
            json.dumps({"rejected_symlink_target": target}) + "\n",
            encoding="utf-8",
        )
    else:
        shutil.move(str(path), destination)
    reason_path = destination.with_suffix(".reason.json")
    reason_path.write_text(
        json.dumps({"error": reason}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def import_inbox(conn: sqlite3.Connection, ids: IdentityMaps, inbox: Path,
                 archive: Path, quarantine: Path,
                 dry_run: bool = False) -> ImportResult:
    accepted = quarantined = discovered_events = inserted = 0
    if not inbox.exists():
        return ImportResult()

    for path in sorted(inbox.rglob("*.json")):
        try:
            bundle = load_bundle(path, inbox)
            if bundle.member not in ids.slugs():
                raise BundleError(f"unknown member: {bundle.member}")
            events = bundle_events(bundle, bundle.member)
        except BundleError as exc:
            quarantined += 1
            if not dry_run:
                _quarantine(path, inbox, quarantine, str(exc))
            continue

        accepted += 1
        discovered_events += len(events)
        if dry_run:
            continue
        inserted += insert_events(conn, events)
        _archive(path, bundle.raw_bytes, bundle.member, archive)

    return ImportResult(
        accepted=accepted,
        quarantined=quarantined,
        events=discovered_events,
        inserted=inserted,
    )
