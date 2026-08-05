"""Hub configuration. Defaults are sane; every field is overridable via a TEAMMEM_*
environment variable so the launchd tick can be configured without code edits."""

import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Mapping


_ENVIRONMENT_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def default_env_file(
    platform: str | None = None, env: Mapping[str, str] | None = None
) -> Path:
    """Resolve the operator's default environment file at call time."""
    current = sys.platform if platform is None else platform
    if current == "win32":
        values = os.environ if env is None else env
        root = values.get("APPDATA")
        if not root:
            raise RuntimeError("APPDATA is required on Windows")
        return Path(root) / "TeamMemory" / "hub.env"
    return Path("~/.config/teammem/hub.env").expanduser()


def _env_file_path(path: Path, *, platform: str | None = None) -> Path:
    if (sys.platform if platform is None else platform) == "win32":
        return Path(path)
    expanded = Path(path).expanduser()
    absolute = expanded if expanded.is_absolute() else Path.cwd() / expanded
    return absolute.parent.resolve() / absolute.name


def _parse_env_lines(lines: list[str], path: Path) -> dict[str, str]:
    values = {}
    for number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        key, separator, value = raw_line.partition("=")
        if not separator or not _ENVIRONMENT_KEY.fullmatch(key):
            raise ValueError(f"invalid environment-file entry at {path}:{number}")
        values[key] = value
    return values


def read_env_file(
    path: Path,
    *,
    required: bool = False,
    platform: str | None = None,
    windows_api: Any = None,
) -> dict[str, str]:
    """Read literal KEY=VALUE entries from a private hub environment file."""
    current = sys.platform if platform is None else platform
    path = _env_file_path(path, platform=current)
    if current == "win32":
        from .windows_security import current_user_sid, read_windows_env_file

        try:
            lines = read_windows_env_file(
                path, current_user_sid(windows_api), windows_api
            )
        except FileNotFoundError:
            if required:
                raise ValueError(f"environment file does not exist: {path}") from None
            return {}
        return _parse_env_lines(lines, path)
    try:
        path_metadata = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise ValueError(f"environment file does not exist: {path}") from None
        return {}
    if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISREG(
        path_metadata.st_mode
    ):
        raise ValueError(
            f"environment file must be a regular non-symlink file: {path}"
        )

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as failure:
        raise ValueError(
            f"environment file must be a regular non-symlink file: {path}"
        ) from failure

    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino)
            != (path_metadata.st_dev, path_metadata.st_ino)
        ):
            raise ValueError(f"environment file changed during validation: {path}")
        current_uid = getattr(os, "getuid", lambda: metadata.st_uid)()
        if metadata.st_uid != current_uid:
            raise ValueError(f"environment file must be user-owned: {path}")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError(f"environment file mode must be exactly 0600: {path}")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            try:
                lines = handle.read().splitlines()
            except UnicodeError:
                raise ValueError(
                    f"environment file must contain UTF-8 text: {path}"
                ) from None
    finally:
        if descriptor != -1:
            os.close(descriptor)

    return _parse_env_lines(lines, path)


def _integer(values: dict, key: str, default: int) -> int:
    try:
        return int(values.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be an integer") from None


def _bounded_integer(
    values: Mapping[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(values.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(
            f"{key} must be an integer from {minimum} to {maximum}"
        ) from None
    if value < minimum or value > maximum:
        raise ValueError(
            f"{key} must be an integer from {minimum} to {maximum}"
        )
    return value


@dataclass
class Config:
    db_path: Path = Path("ledger.db")
    config_dir: Path = Path("config")
    gitlab_url: str = ""       # e.g. https://gitlab.internal.example (no trailing slash)
    gitlab_token: str = ""     # group read_api token — never committed, env only
    gitlab_group: str = ""     # numeric group id or URL-encoded group path
    feishu_app_id: str = ""    # Feishu app_id — never committed, env only
    feishu_app_secret: str = ""  # Feishu app_secret — never committed, env only
    since_days: int = 7
    vault_dir: Path = Path("vault")
    push: bool = False
    obsidian_projects: Path | None = None  # docs-sync source; unset = command guarded off
    anthropic_api_key: str = ""    # ANTHROPIC_API_KEY — synthesis only, never committed
    llm_daily_model: str = "daily-summary-model"
    llm_report_model: str = "weekly-summary-model"
    llm_concurrency: int = 2
    env_file: Path = field(default_factory=default_env_file)
    github_token: str = ""       # GitHub fine-grained token — never committed
    slack_bot_token: str = ""    # Slack bot token — never committed
    discord_bot_token: str = ""  # Discord bot token — never committed
    inbox: Path | None = None
    archive: Path | None = None
    quarantine: Path | None = None
    snapshots: Path | None = None

    @classmethod
    def load(
        cls,
        env: dict | None = None,
        env_file: Path | None = None,
        *,
        require_env_file: bool = False,
        platform: str | None = None,
        windows_api: Any = None,
    ) -> "Config":
        env_file = _env_file_path(
            default_env_file(platform=platform, env=env)
            if env_file is None
            else Path(env_file),
            platform=platform,
        )
        values = read_env_file(
            env_file,
            required=require_env_file,
            platform=platform,
            windows_api=windows_api,
        )
        values.update(os.environ if env is None else env)
        return cls(
            db_path=Path(values.get("TEAMMEM_DB", str(cls.db_path))),
            config_dir=Path(values.get("TEAMMEM_CONFIG_DIR", str(cls.config_dir))),
            gitlab_url=values.get("TEAMMEM_GITLAB_URL", cls.gitlab_url).rstrip("/"),
            gitlab_token=values.get("TEAMMEM_GITLAB_TOKEN", cls.gitlab_token),
            gitlab_group=values.get("TEAMMEM_GITLAB_GROUP", cls.gitlab_group),
            feishu_app_id=values.get("TEAMMEM_FEISHU_APP_ID", cls.feishu_app_id),
            feishu_app_secret=values.get("TEAMMEM_FEISHU_APP_SECRET", cls.feishu_app_secret),
            since_days=_integer(values, "TEAMMEM_SINCE_DAYS", cls.since_days),
            vault_dir=Path(values.get("TEAMMEM_VAULT", str(cls.vault_dir))),
            push=values.get("TEAMMEM_PUSH", "0").lower() in ("1", "true", "yes"),
            obsidian_projects=(Path(values["TEAMMEM_OBSIDIAN_PROJECTS"])
                               if values.get("TEAMMEM_OBSIDIAN_PROJECTS") else None),
            anthropic_api_key=values.get("ANTHROPIC_API_KEY", cls.anthropic_api_key),
            llm_daily_model=values.get("TEAMMEM_LLM_DAILY_MODEL", cls.llm_daily_model),
            llm_report_model=values.get("TEAMMEM_LLM_REPORT_MODEL", cls.llm_report_model),
            llm_concurrency=_bounded_integer(
                values,
                "TEAMMEM_LLM_CONCURRENCY",
                cls.llm_concurrency,
                1,
                8,
            ),
            env_file=env_file,
            github_token=values.get("TEAMMEM_GITHUB_TOKEN", cls.github_token),
            slack_bot_token=values.get("TEAMMEM_SLACK_BOT_TOKEN", cls.slack_bot_token),
            discord_bot_token=values.get("TEAMMEM_DISCORD_BOT_TOKEN", cls.discord_bot_token),
            inbox=Path(values["TEAMMEM_INBOX"]) if values.get("TEAMMEM_INBOX") else None,
            archive=Path(values["TEAMMEM_ARCHIVE"]) if values.get("TEAMMEM_ARCHIVE") else None,
            quarantine=Path(values["TEAMMEM_QUARANTINE"]) if values.get("TEAMMEM_QUARANTINE") else None,
            snapshots=Path(values["TEAMMEM_SNAPSHOTS"]) if values.get("TEAMMEM_SNAPSHOTS") else None,
        )
