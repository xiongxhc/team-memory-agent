from pathlib import Path

import pytest

from teammem.config import Config, default_env_file, read_env_file


@pytest.fixture
def empty_env_file(tmp_path):
    return tmp_path / "missing-hub.env"


def test_defaults(empty_env_file):
    cfg = Config.load(env={}, env_file=empty_env_file)
    assert cfg.db_path == Path("ledger.db")
    assert cfg.since_days == 7
    assert cfg.gitlab_url == ""
    assert cfg.llm_concurrency == 2


def test_env_overrides(empty_env_file):
    cfg = Config.load(env={
        "TEAMMEM_DB": "/tmp/x.db",
        "TEAMMEM_GITLAB_URL": "https://gitlab.internal",
        "TEAMMEM_GITLAB_TOKEN": "tok",
        "TEAMMEM_GITLAB_GROUP": "42",
        "TEAMMEM_SINCE_DAYS": "14",
    }, env_file=empty_env_file)
    assert cfg.db_path == Path("/tmp/x.db")
    assert cfg.gitlab_url == "https://gitlab.internal"
    assert cfg.gitlab_token == "tok"
    assert cfg.gitlab_group == "42"
    assert cfg.since_days == 14


def test_gitlab_url_trailing_slash_stripped_and_config_dir_override(empty_env_file):
    cfg = Config.load(env={
        "TEAMMEM_GITLAB_URL": "https://gitlab.internal/",
        "TEAMMEM_CONFIG_DIR": "/etc/teammem",
    }, env_file=empty_env_file)
    assert cfg.gitlab_url == "https://gitlab.internal"
    assert cfg.config_dir == Path("/etc/teammem")
    assert Config.load(env={}, env_file=empty_env_file).config_dir == Path("config")


def test_vault_and_push_config(empty_env_file):
    cfg = Config.load(
        env={"TEAMMEM_VAULT": "/tmp/v", "TEAMMEM_PUSH": "1"},
        env_file=empty_env_file,
    )
    assert cfg.vault_dir == Path("/tmp/v") and cfg.push is True
    assert Config.load(env={}, env_file=empty_env_file).push is False


def test_process_environment_overrides_user_only_hub_env(tmp_path):
    env_file = tmp_path / "hub.env"
    env_file.write_text("TEAMMEM_GITHUB_TOKEN=file-token\nTEAMMEM_SINCE_DAYS=3\n")
    env_file.chmod(0o600)
    cfg = Config.load(
        env={"TEAMMEM_GITHUB_TOKEN": "process-token"},
        env_file=env_file,
    )
    assert cfg.github_token == "process-token"
    assert cfg.since_days == 3


def test_hub_env_file_accepts_only_literal_assignments_and_requires_user_only_mode(tmp_path):
    env_file = tmp_path / "hub.env"
    env_file.write_text("# operator secrets\n\nTEAMMEM_GITHUB_TOKEN=file-token\nVALUE=a=b\n")
    env_file.chmod(0o600)
    assert read_env_file(env_file) == {
        "TEAMMEM_GITHUB_TOKEN": "file-token",
        "VALUE": "a=b",
    }

    env_file.write_text("export TEAMMEM_GITHUB_TOKEN=file-token\n")
    with pytest.raises(ValueError, match="invalid environment-file entry"):
        read_env_file(env_file)

    env_file.write_text("TEAMMEM_GITHUB_TOKEN=file-token\n")
    env_file.chmod(0o644)
    with pytest.raises(ValueError, match=str(env_file)) as error:
        read_env_file(env_file)
    assert "file-token" not in str(error.value)


def test_connector_and_daily_paths_load_from_hub_environment(tmp_path):
    env_file = tmp_path / "hub.env"
    env_file.write_text(
        "TEAMMEM_SLACK_BOT_TOKEN=slack-token\n"
        "TEAMMEM_DISCORD_BOT_TOKEN=discord-token\n"
        "TEAMMEM_INBOX=/runtime/inbox\n"
        "TEAMMEM_ARCHIVE=/runtime/archive\n"
        "TEAMMEM_QUARANTINE=/runtime/quarantine\n"
        "TEAMMEM_SNAPSHOTS=/runtime/snapshots\n"
    )
    env_file.chmod(0o600)
    cfg = Config.load(env={}, env_file=env_file)
    assert cfg.env_file == env_file.resolve()
    assert cfg.slack_bot_token == "slack-token"
    assert cfg.discord_bot_token == "discord-token"
    assert cfg.inbox == Path("/runtime/inbox")
    assert cfg.archive == Path("/runtime/archive")
    assert cfg.quarantine == Path("/runtime/quarantine")
    assert cfg.snapshots == Path("/runtime/snapshots")


def test_windows_default_env_file_is_resolved_at_call_time():
    assert default_env_file(
        platform="win32", env={"APPDATA": r"C:\\Users\\Alex\\AppData\\Roaming"}
    ) == Path(r"C:\\Users\\Alex\\AppData\\Roaming") / "TeamMemory" / "hub.env"
    with pytest.raises(RuntimeError, match="APPDATA is required"):
        default_env_file(platform="win32", env={})


def test_windows_config_reads_env_through_injected_same_handle_api():
    env_file = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\hub.env")

    class Api:
        def current_process_sid(self):
            return "S-1-5-21-test"

        def open_file(self, path, *, directory=False):
            return {"path": path}

        def file_info(self, handle):
            return {"regular": True, "reparse_point": False, "file_type": "disk"}

        def owner_sid(self, handle):
            return "S-1-5-21-test"

        def allow_aces(self, handle):
            return []

        def read_lines(self, handle):
            return ["TEAMMEM_SINCE_DAYS=13\n"]

        def close_handle(self, handle):
            pass

    api = Api()
    cfg = Config.load(
        env={}, env_file=env_file, platform="win32", windows_api=api
    )
    assert cfg.since_days == 13
    assert cfg.env_file == env_file


@pytest.mark.parametrize("value", ["1", "8"])
def test_llm_concurrency_accepts_the_inclusive_bounds(empty_env_file, value):
    cfg = Config.load(
        env={"TEAMMEM_LLM_CONCURRENCY": value}, env_file=empty_env_file
    )
    assert cfg.llm_concurrency == int(value)


@pytest.mark.parametrize("value", ["0", "9", "two"])
def test_llm_concurrency_rejects_out_of_range_or_non_integer_values(
    empty_env_file, value
):
    with pytest.raises(
        ValueError,
        match="^TEAMMEM_LLM_CONCURRENCY must be an integer from 1 to 8$",
    ):
        Config.load(
            env={"TEAMMEM_LLM_CONCURRENCY": value}, env_file=empty_env_file
        )
