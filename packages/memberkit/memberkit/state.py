"""Local review state for scheduled drafts.

Only fingerprints and review decisions are stored here. Bundle contents remain in
the member-visible JSON draft files.
"""

import hashlib
import json
from pathlib import Path


def event_fingerprint(event: dict, date: str) -> str:
    payload = json.dumps(
        {"date": date, "event": event},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DraftState:
    def __init__(self, path: Path):
        self.path = path
        self._data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"approved": [], "excluded": [], "pending": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return {
            "approved": list(data.get("approved") or []),
            "excluded": list(data.get("excluded") or []),
            "pending": dict(data.get("pending") or {}),
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def snapshot(self) -> dict:
        return json.loads(json.dumps(self._data))

    def pending_dates(self) -> list[str]:
        return sorted(
            date for date, fingerprints in self._data["pending"].items()
            if fingerprints
        )

    def refresh(self, date: str, discovered: list[dict],
                current: dict | None) -> list[dict]:
        approved = set(self._data["approved"])
        excluded = set(self._data["excluded"])
        previous = set(self._data["pending"].get(date) or [])
        current_events = list((current or {}).get("events") or [])
        current_fingerprints = {
            event_fingerprint(event, date) for event in current_events
        }

        if current is not None:
            excluded.update(previous - current_fingerprints)

        output: list[dict] = []
        output_fingerprints: set[str] = set()
        for event in [*current_events, *discovered]:
            fingerprint = event_fingerprint(event, date)
            if fingerprint in approved or fingerprint in excluded:
                continue
            if fingerprint not in output_fingerprints:
                output.append(event)
                output_fingerprints.add(fingerprint)

        self._data["approved"] = sorted(approved)
        self._data["excluded"] = sorted(excluded)
        if output_fingerprints:
            self._data["pending"][date] = sorted(output_fingerprints)
        else:
            self._data["pending"].pop(date, None)
        self._save()
        return output

    def record_push(self, date: str, pushed: list[dict]) -> None:
        approved = set(self._data["approved"])
        excluded = set(self._data["excluded"])
        previous = set(self._data["pending"].get(date) or [])
        included = {event_fingerprint(event, date) for event in pushed}

        approved.update(included)
        excluded.update(previous - included)
        excluded.difference_update(included)
        self._data["approved"] = sorted(approved)
        self._data["excluded"] = sorted(excluded)
        self._data["pending"].pop(date, None)
        self._save()

    def dismiss(self, date: str) -> None:
        excluded = set(self._data["excluded"])
        excluded.update(self._data["pending"].get(date) or [])
        self._data["excluded"] = sorted(excluded)
        self._data["pending"].pop(date, None)
        self._save()
