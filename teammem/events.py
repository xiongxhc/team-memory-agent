"""One row per attributed fact. The ledger's row shape — nothing else lives here."""

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    person: str          # canonical slug from roster.yaml, or "_unmapped/<raw>"
    ts: str              # ISO 8601
    source: str          # "gitlab" | "feishu-channel" | "calendar" | "bundle:<member>"
    kind: str            # "commit" | "mr" | "message" | "meeting" | "journal-highlight"
    summary: str         # one attributed line
    hash: str            # dedup key within (person, source)
    project: str | None = None
    refs: str | None = None   # JSON string: sha / url / ids
    raw: str | None = None    # JSON payload as received (replayable)


def event_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()
