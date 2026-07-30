"""MEMBERKIT_* configuration with platform-selected private storage."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


CONFIG_KEYS = (
    "MEMBERKIT_MEMBER",
    "MEMBERKIT_INBOX_URL",
    "MEMBERKIT_DB",
    "MEMBERKIT_WORKDIR",
    "MEMBERKIT_TIMEZONE",
)

# Temporary compatibility for the existing CLI. Task 6 moves that caller to
# default_config_file(), which resolves the platform path at invocation time.
CONFIG_FILE = Path.home() / ".config" / "teammem" / "memberkit.env"


@dataclass(frozen=True)
class Config:
    member: str
    db: Path
    inbox_url: str
    workdir: Path
    timezone: ZoneInfo | None = None


def _is_windows(platform: str | None) -> bool:
    return (sys.platform if platform is None else platform) == "win32"


def default_config_file(
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Return the platform's MemberKit configuration path."""
    if _is_windows(platform):
        appdata = (os.environ if env is None else env).get("APPDATA")
        if not appdata:
            raise RuntimeError("APPDATA is required on Windows")
        return Path(appdata) / "TeamMemory" / "memberkit.env"
    return Path.home() / ".config" / "teammem" / "memberkit.env"


def _parse_env_text(text: str) -> dict[str, str]:
    pairs = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            pairs[key.strip()] = val.strip()
    return pairs


def _read_posix_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return _parse_env_text(path.read_text(encoding="utf-8"))


def _read_windows_env_file(path: Path, windows_api: Any) -> dict[str, str]:
    from .windows_security import current_user_sid, read_windows_private_text

    sid = current_user_sid(windows_api)
    try:
        return _parse_env_text(read_windows_private_text(path, sid, windows_api))
    except FileNotFoundError:
        return {}


def _render_config(values: Mapping[str, str]) -> str:
    lines = []
    for key in CONFIG_KEYS:
        if key not in values:
            continue
        value = values[key]
        if any(character in value for character in ("\r", "\n", "\0")):
            raise ValueError(f"{key} must not contain control characters")
        lines.append(f"{key}={value}")
    return "\n".join(lines) + ("\n" if lines else "")


def write_config(
    values: Mapping[str, str],
    *,
    config_file: Path | None = None,
    platform: str | None = None,
    windows_api: Any = None,
) -> Path:
    """Write only recognized configuration keys to private platform storage."""
    path = default_config_file(platform) if config_file is None else Path(config_file)
    text = _render_config(values)
    if _is_windows(platform):
        from .windows_security import atomic_write_windows_private_text, current_user_sid

        return atomic_write_windows_private_text(
            path,
            text,
            current_user_sid(windows_api),
            windows_api,
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def resolve_timezone(name: str | None, *, config_file: Path | None = None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name.removeprefix(":"))
    except ZoneInfoNotFoundError:
        pass

    location = f" in {config_file}" if config_file is not None else ""
    raise SystemExit(
        "invalid MEMBERKIT_TIMEZONE"
        f"{location}: use an IANA timezone such as Asia/Dubai"
    )


def load(
    env: dict[str, str] | None = None,
    *,
    config_file: Path | None = None,
    platform: str | None = None,
    windows_api: Any = None,
) -> Config:
    process_env = dict(os.environ) if env is None else dict(env)
    path = (
        default_config_file(platform, process_env)
        if config_file is None
        else Path(config_file)
    )
    file_values = (
        _read_windows_env_file(path, windows_api)
        if _is_windows(platform)
        else _read_posix_env_file(path)
    )
    merged = {**file_values, **process_env}

    def need(key: str) -> str:
        if not merged.get(key):
            raise SystemExit(f"missing {key}: set it in {path} or the environment")
        return merged[key]

    return Config(
        member=need("MEMBERKIT_MEMBER"),
        db=Path(merged.get("MEMBERKIT_DB", "~/.claude-mem/claude-mem.db")).expanduser(),
        inbox_url=need("MEMBERKIT_INBOX_URL"),
        workdir=Path(merged.get("MEMBERKIT_WORKDIR", "~/.memberkit")).expanduser(),
        timezone=resolve_timezone(merged.get("MEMBERKIT_TIMEZONE"), config_file=path),
    )
