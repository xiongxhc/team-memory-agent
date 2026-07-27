"""Hub configuration. Defaults are sane; every field is overridable via a TEAMMEM_*
environment variable so the launchd tick can be configured without code edits."""

import os
from dataclasses import dataclass
from pathlib import Path


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

    @classmethod
    def load(cls, env: dict | None = None) -> "Config":
        env = os.environ if env is None else env
        return cls(
            db_path=Path(env.get("TEAMMEM_DB", str(cls.db_path))),
            config_dir=Path(env.get("TEAMMEM_CONFIG_DIR", str(cls.config_dir))),
            gitlab_url=env.get("TEAMMEM_GITLAB_URL", cls.gitlab_url).rstrip("/"),
            gitlab_token=env.get("TEAMMEM_GITLAB_TOKEN", cls.gitlab_token),
            gitlab_group=env.get("TEAMMEM_GITLAB_GROUP", cls.gitlab_group),
            feishu_app_id=env.get("TEAMMEM_FEISHU_APP_ID", cls.feishu_app_id),
            feishu_app_secret=env.get("TEAMMEM_FEISHU_APP_SECRET", cls.feishu_app_secret),
            since_days=int(env.get("TEAMMEM_SINCE_DAYS", cls.since_days)),
            vault_dir=Path(env.get("TEAMMEM_VAULT", str(cls.vault_dir))),
            push=env.get("TEAMMEM_PUSH", "0").lower() in ("1", "true", "yes"),
            obsidian_projects=(Path(env["TEAMMEM_OBSIDIAN_PROJECTS"])
                               if env.get("TEAMMEM_OBSIDIAN_PROJECTS") else None),
            anthropic_api_key=env.get("ANTHROPIC_API_KEY", cls.anthropic_api_key),
            llm_daily_model=env.get("TEAMMEM_LLM_DAILY_MODEL", cls.llm_daily_model),
            llm_report_model=env.get("TEAMMEM_LLM_REPORT_MODEL", cls.llm_report_model),
        )
