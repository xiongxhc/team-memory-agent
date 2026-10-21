"""Hub configuration. Defaults are sane; every field is overridable via a TEAMMEM_*
environment variable so the launchd tick can be configured without code edits."""

import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Mapping

# The retention window is the ceiling on the pre-expiry warning window: a
# warning longer than the window itself would fire before the meeting ended,
# and both live in the store beside the deadline they describe.
from .meeting_store import MEETING_AUDIO_RETENTION_DAYS, MEETING_AUDIO_WARN_DAYS


_ENVIRONMENT_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CODEX_MODEL = "gpt-5.6-sol"


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


def _path(values: Mapping[str, Any], key: str) -> Path | None:
    """An optional path setting; an empty value is unset, as everywhere else."""
    return Path(values[key]) if values.get(key) else None


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


MEETING_PATH_SETTINGS = (
    ("meeting_inbox", "TEAMMEM_MEETING_INBOX"),
    ("meeting_archive", "TEAMMEM_MEETING_ARCHIVE"),
    ("meeting_quarantine", "TEAMMEM_MEETING_QUARANTINE"),
    ("meeting_audio_store", "TEAMMEM_MEETING_AUDIO_STORE"),
    ("meeting_receipts", "TEAMMEM_MEETING_RECEIPTS"),
    ("meeting_alerts", "TEAMMEM_MEETING_ALERTS"),
)
_GIT_REF = re.compile(r"refs/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+")
_GIT_REMOTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _path_key(path: Path) -> tuple[str, ...]:
    """A filesystem-comparison key, independent of spelling and case."""
    return tuple(os.path.normcase(part).casefold() for part in Path(path).parts)


def _nested(inner: Path, outer: Path) -> bool:
    inner_key = _path_key(inner)
    outer_key = _path_key(outer)
    return (
        len(inner_key) > len(outer_key)
        and inner_key[:len(outer_key)] == outer_key
    )


def _physical_path(path: Path) -> Path:
    """Resolve the existing prefix of `path`, preserving a missing suffix.

    `Path.resolve(strict=False)` can change meaning when a missing component is
    created later. Resolve only the nearest existing ancestor instead, then
    append the components that do not exist yet without following a future
    link at those names.
    """
    absolute = Path(os.path.abspath(Path(path).expanduser()))
    missing: list[str] = []
    current = absolute
    while not os.path.lexists(current):
        parent = current.parent
        if parent == current:
            break
        missing.append(current.name)
        current = parent
    resolved = current.resolve(strict=True)
    for component in reversed(missing):
        resolved /= component
    return resolved


def _path_within(path: Path, root: Path) -> bool:
    """Whether `path` is equal to or below `root` after caller resolution."""
    path_key = _path_key(path)
    root_key = _path_key(root)
    return len(path_key) >= len(root_key) and path_key[:len(root_key)] == root_key


def validate_meeting_settings(cfg: "Config") -> None:
    """Meetings are one complete set of directories, or nothing at all.

    A hub that never sees a meeting configures none of these and is validated as
    it always was. Configure any one of them and every one is required, because
    a half-configured set is how an importer ends up archiving into its own
    inbox: the inbox is walked recursively, so an archive inside it would be
    re-read on the next scan, and a receipt or a bundle inside the vault would be
    published. Distinctness therefore means neither equal nor nested.

    Filesystem metadata is read but never mutated here. A `Config.load` on a
    machine that has not created the directories yet still succeeds, and the
    daily pipeline never fails on a meeting directory's permissions.
    """
    from .meeting_publish import validate_web_base

    configured = [getattr(cfg, field) for field, _ in MEETING_PATH_SETTINGS]
    if not any(configured) and not cfg.vault_web_base:
        return

    for field, key in MEETING_PATH_SETTINGS:
        value = getattr(cfg, field)
        if value is None:
            raise ValueError(f"{key} is required once any meeting setting is set")
        if not value.is_absolute():
            raise ValueError(f"{key} must be an absolute path")
        if ".." in value.parts:
            raise ValueError(f"{key} must not contain a relative path segment")
    if not cfg.vault_web_base:
        raise ValueError(
            "TEAMMEM_VAULT_WEB_BASE is required once any meeting setting is set"
        )
    validate_web_base(cfg.vault_web_base)
    if not _GIT_REMOTE.fullmatch(cfg.vault_remote):
        raise ValueError("TEAMMEM_VAULT_REMOTE must be a plain git remote name")
    if not _GIT_REF.fullmatch(cfg.vault_ref):
        raise ValueError("TEAMMEM_VAULT_REF must be a full ref, e.g. refs/heads/main")

    named = [(key, getattr(cfg, field)) for field, key in MEETING_PATH_SETTINGS]
    # A relative vault is still a real directory under the working directory, and
    # a receipt outbox inside it would still be published; resolve it rather than
    # exempt it.
    named.append(("TEAMMEM_VAULT", cfg.vault_dir if cfg.vault_dir.is_absolute()
                  else Path.cwd() / cfg.vault_dir))
    physical = []
    for key, path in named:
        try:
            physical.append((key, _physical_path(path)))
        except (OSError, RuntimeError) as failure:
            raise ValueError(f"{key} cannot be resolved safely: {path}") from failure
    for position, (key, path) in enumerate(physical):
        for other_key, other in physical[position + 1:]:
            if _path_key(path) == _path_key(other):
                raise ValueError(
                    f"{key} and {other_key} name the same directory"
                )
            if _nested(path, other) or _nested(other, path):
                raise ValueError(
                    f"{key} and {other_key} must not be nested in one another"
                )


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
    llm_provider: str = "claude"
    codex_bin: str = "codex"
    llm_daily_model: str = ""
    llm_report_model: str = ""
    llm_concurrency: int = 2
    env_file: Path = field(default_factory=default_env_file)
    github_token: str = ""       # GitHub fine-grained token — never committed
    slack_bot_token: str = ""    # Slack bot token — never committed
    discord_bot_token: str = ""  # Discord bot token — never committed
    inbox: Path | None = None
    archive: Path | None = None
    quarantine: Path | None = None
    snapshots: Path | None = None
    meeting_inbox: Path | None = None       # meetings: one complete set or none
    meeting_archive: Path | None = None
    meeting_quarantine: Path | None = None
    meeting_audio_store: Path | None = None  # encrypted bytes only; never decrypted
    meeting_receipts: Path | None = None     # one receipt file per occurrence
    meeting_alerts: Path | None = None       # one operator alert file per occurrence
    # warn this long before audio expiry; 0 warns never, and it never extends it
    meeting_warn_days: int = MEETING_AUDIO_WARN_DAYS
    vault_web_base: str = ""     # https root of the published vault; no default
    vault_remote: str = "origin"          # the remote a receipt's commit must reach
    vault_ref: str = "refs/heads/main"    # and the ref on it

    @property
    def meetings_enabled(self) -> bool:
        """`Config.load` accepts the meeting settings only as a complete set, so
        one configured directory is the whole feature being switched on."""
        return self.meeting_inbox is not None

    def __post_init__(self) -> None:
        if self.llm_provider not in ("claude", "codex"):
            raise ValueError(
                "TEAMMEM_LLM_PROVIDER must be one of: claude, codex"
            )
        self.llm_daily_model = self.llm_daily_model.strip()
        self.llm_report_model = self.llm_report_model.strip()
        if self.llm_provider == "codex":
            self.llm_daily_model = CODEX_MODEL
            self.llm_report_model = CODEX_MODEL

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
        llm_provider = values.get("TEAMMEM_LLM_PROVIDER", cls.llm_provider)
        cfg = cls(
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
            llm_provider=llm_provider,
            codex_bin=values.get("TEAMMEM_CODEX_BIN", cls.codex_bin),
            llm_daily_model=values.get(
                "TEAMMEM_LLM_DAILY_MODEL", cls.llm_daily_model
            ),
            llm_report_model=values.get(
                "TEAMMEM_LLM_REPORT_MODEL", cls.llm_report_model
            ),
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
            meeting_inbox=_path(values, "TEAMMEM_MEETING_INBOX"),
            meeting_archive=_path(values, "TEAMMEM_MEETING_ARCHIVE"),
            meeting_quarantine=_path(values, "TEAMMEM_MEETING_QUARANTINE"),
            meeting_audio_store=_path(values, "TEAMMEM_MEETING_AUDIO_STORE"),
            meeting_receipts=_path(values, "TEAMMEM_MEETING_RECEIPTS"),
            meeting_alerts=_path(values, "TEAMMEM_MEETING_ALERTS"),
            meeting_warn_days=_bounded_integer(
                values,
                "TEAMMEM_MEETING_WARN_DAYS",
                cls.meeting_warn_days,
                0,
                MEETING_AUDIO_RETENTION_DAYS,
            ),
            vault_web_base=values.get(
                "TEAMMEM_VAULT_WEB_BASE", cls.vault_web_base).rstrip("/"),
            vault_remote=values.get("TEAMMEM_VAULT_REMOTE", cls.vault_remote),
            vault_ref=values.get("TEAMMEM_VAULT_REF", cls.vault_ref),
        )
        # Deliberately not validated here. `Config.load` runs for every command,
        # and a half-written meeting configuration in a live env file must not
        # stop the daily timer from collecting and rendering. The meeting
        # commands validate before they do anything, so they still fail loudly.
        return cfg
