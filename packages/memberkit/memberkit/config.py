"""MEMBERKIT_* config: process env overrides ~/.config/teammem/memberkit.env."""

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CONFIG_FILE = Path.home() / ".config" / "teammem" / "memberkit.env"


@dataclass(frozen=True)
class Config:
    member: str
    db: Path
    inbox_url: str
    workdir: Path
    timezone: ZoneInfo | None = None


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    pairs = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            pairs[key.strip()] = val.strip()
    return pairs


def resolve_timezone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name.removeprefix(":"))
    except ZoneInfoNotFoundError as exc:
        raise SystemExit(
            f"invalid MEMBERKIT_TIMEZONE {name!r}: "
            "use an IANA timezone such as Asia/Dubai"
        ) from exc


def load(env: dict[str, str] | None = None) -> Config:
    merged = {**_read_env_file(CONFIG_FILE), **(dict(os.environ) if env is None else env)}

    def need(key: str) -> str:
        if not merged.get(key):
            raise SystemExit(f"missing {key}: set it in {CONFIG_FILE} or the environment")
        return merged[key]

    return Config(
        member=need("MEMBERKIT_MEMBER"),
        db=Path(merged.get("MEMBERKIT_DB", "~/.claude-mem/claude-mem.db")).expanduser(),
        inbox_url=need("MEMBERKIT_INBOX_URL"),
        workdir=Path(merged.get("MEMBERKIT_WORKDIR", "~/.memberkit")).expanduser(),
        timezone=resolve_timezone(merged.get("MEMBERKIT_TIMEZONE")),
    )
