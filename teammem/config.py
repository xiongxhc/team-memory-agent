"""Hub configuration. Defaults are sane; every field is overridable via a TEAMMEM_*
environment variable so the launchd tick can be configured without code edits."""

import os
from dataclasses import dataclass
from pathlib import Path
import re


DEFAULT_ENV_FILE = Path("~/.config/teammem/hub.env").expanduser()
_ENVIRONMENT_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def read_env_file(path: Path) -> dict[str, str]:
    """Read literal KEY=VALUE entries from a private hub environment file."""
    path = path.expanduser().resolve()
    if not path.exists():
        return {}
    if path.stat().st_mode & 0o077:
        raise ValueError(f"environment file must be user-only: {path}")

    values = {}
    for number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        key, separator, value = raw_line.partition("=")
        if not separator or not _ENVIRONMENT_KEY.fullmatch(key):
            raise ValueError(f"invalid environment-file entry at {path}:{number}")
        values[key] = value
    return values


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
    env_file: Path = DEFAULT_ENV_FILE
    github_token: str = ""       # GitHub fine-grained token — never committed
    slack_bot_token: str = ""    # Slack bot token — never committed
    discord_bot_token: str = ""  # Discord bot token — never committed
    inbox: Path | None = None
    archive: Path | None = None
    quarantine: Path | None = None
    snapshots: Path | None = None

    @classmethod
    def load(cls, env: dict | None = None, env_file: Path | None = None) -> "Config":
        env_file = (DEFAULT_ENV_FILE if env_file is None else Path(env_file)).expanduser().resolve()
        values = read_env_file(env_file)
        values.update(os.environ if env is None else env)
        return cls(
            db_path=Path(values.get("TEAMMEM_DB", str(cls.db_path))),
            config_dir=Path(values.get("TEAMMEM_CONFIG_DIR", str(cls.config_dir))),
            gitlab_url=values.get("TEAMMEM_GITLAB_URL", cls.gitlab_url).rstrip("/"),
            gitlab_token=values.get("TEAMMEM_GITLAB_TOKEN", cls.gitlab_token),
            gitlab_group=values.get("TEAMMEM_GITLAB_GROUP", cls.gitlab_group),
            feishu_app_id=values.get("TEAMMEM_FEISHU_APP_ID", cls.feishu_app_id),
            feishu_app_secret=values.get("TEAMMEM_FEISHU_APP_SECRET", cls.feishu_app_secret),
            since_days=int(values.get("TEAMMEM_SINCE_DAYS", cls.since_days)),
            vault_dir=Path(values.get("TEAMMEM_VAULT", str(cls.vault_dir))),
            push=values.get("TEAMMEM_PUSH", "0").lower() in ("1", "true", "yes"),
            obsidian_projects=(Path(values["TEAMMEM_OBSIDIAN_PROJECTS"])
                               if values.get("TEAMMEM_OBSIDIAN_PROJECTS") else None),
            anthropic_api_key=values.get("ANTHROPIC_API_KEY", cls.anthropic_api_key),
            llm_daily_model=values.get("TEAMMEM_LLM_DAILY_MODEL", cls.llm_daily_model),
            llm_report_model=values.get("TEAMMEM_LLM_REPORT_MODEL", cls.llm_report_model),
            env_file=env_file,
            github_token=values.get("TEAMMEM_GITHUB_TOKEN", cls.github_token),
            slack_bot_token=values.get("TEAMMEM_SLACK_BOT_TOKEN", cls.slack_bot_token),
            discord_bot_token=values.get("TEAMMEM_DISCORD_BOT_TOKEN", cls.discord_bot_token),
            inbox=Path(values["TEAMMEM_INBOX"]) if values.get("TEAMMEM_INBOX") else None,
            archive=Path(values["TEAMMEM_ARCHIVE"]) if values.get("TEAMMEM_ARCHIVE") else None,
            quarantine=Path(values["TEAMMEM_QUARANTINE"]) if values.get("TEAMMEM_QUARANTINE") else None,
            snapshots=Path(values["TEAMMEM_SNAPSHOTS"]) if values.get("TEAMMEM_SNAPSHOTS") else None,
        )
