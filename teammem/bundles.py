"""Strict validation and conversion for the frozen teammem-bundle/v1 protocol."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .events import Event


SCHEMA = "teammem-bundle/v1"
_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
_FILENAME = re.compile(
    r"bundle-(?P<member>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)-"
    r"(?P<date>\d{4}-\d{2}-\d{2})\.json"
)
_TOP_KEYS = {"schema", "member", "date", "events", "journal_md"}
_EVENT_KEYS = {"ts", "kind", "summary", "project", "refs"}


class BundleError(ValueError):
    pass


@dataclass(frozen=True)
class Bundle:
    path: Path
    member: str
    date: str
    events: tuple[dict, ...]
    journal_md: str
    raw_bytes: bytes


def _error(message: str) -> BundleError:
    return BundleError(message)


def load_bundle(path: Path, inbox: Path) -> Bundle:
    if path.is_symlink():
        raise _error("bundle path is a symlink")
    try:
        relative = path.absolute().relative_to(inbox.absolute())
    except ValueError as exc:
        raise _error("bundle path is outside inbox") from exc
    if len(relative.parts) != 2:
        raise _error("bundle path must be <member>/<filename>")

    directory_member = relative.parts[0]
    if not _SLUG.fullmatch(directory_member):
        raise _error("invalid member slug in path")
    match = _FILENAME.fullmatch(relative.name)
    if not match:
        raise _error("invalid bundle filename")
    if match["member"] != directory_member:
        raise _error("filename member does not match path member")

    try:
        raw_bytes = path.read_bytes()
        data = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error(f"invalid bundle JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise _error("top-level bundle must be an object")
    if set(data) != _TOP_KEYS:
        raise _error("invalid top-level fields")
    if data["schema"] != SCHEMA:
        raise _error(f"invalid schema: expected {SCHEMA}")
    if data["member"] != directory_member:
        raise _error("JSON member does not match path member")
    if not isinstance(data["date"], str):
        raise _error("date must be a string")
    try:
        date.fromisoformat(data["date"])
    except ValueError as exc:
        raise _error("date must be YYYY-MM-DD") from exc
    if data["date"] != match["date"]:
        raise _error("filename date does not match JSON date")
    if not isinstance(data["journal_md"], str):
        raise _error("journal_md must be a string")
    if not isinstance(data["events"], list):
        raise _error("events must be an array")

    events: list[dict] = []
    for index, event in enumerate(data["events"]):
        prefix = f"event {index}"
        if not isinstance(event, dict) or set(event) != _EVENT_KEYS:
            raise _error(f"{prefix} has invalid fields")
        if not isinstance(event["ts"], str):
            raise _error(f"{prefix} ts must be a string")
        try:
            datetime.fromisoformat(
                event["ts"].replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise _error(f"{prefix} ts is invalid") from exc
        if event["kind"] != "journal-highlight":
            raise _error(f"{prefix} kind is invalid")
        if not isinstance(event["summary"], str) or not event["summary"].strip():
            raise _error(f"{prefix} summary must be non-empty")
        if event["project"] is not None and not isinstance(event["project"], str):
            raise _error(f"{prefix} project must be a string or null")
        if event["refs"] is not None:
            raise _error(f"{prefix} refs must be null in v1")
        events.append(event)

    return Bundle(
        path=path,
        member=data["member"],
        date=data["date"],
        events=tuple(events),
        journal_md=data["journal_md"],
        raw_bytes=raw_bytes,
    )


def bundle_events(bundle: Bundle, person: str) -> list[Event]:
    output = []
    for item in bundle.events:
        raw = json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        identity = json.dumps(
            {
                "schema": SCHEMA,
                "member": bundle.member,
                "date": bundle.date,
                "event": item,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        output.append(
            Event(
                person=person,
                ts=item["ts"],
                source=f"bundle:{bundle.member}",
                kind=item["kind"],
                summary=item["summary"],
                project=item["project"],
                refs=None,
                raw=raw,
                hash=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            )
        )
    return output
