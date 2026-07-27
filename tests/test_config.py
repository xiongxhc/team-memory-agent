from pathlib import Path

from teammem.config import Config


def test_defaults():
    cfg = Config.load(env={})
    assert cfg.db_path == Path("ledger.db")
    assert cfg.since_days == 7
    assert cfg.gitlab_url == ""


def test_env_overrides():
    cfg = Config.load(env={
        "TEAMMEM_DB": "/tmp/x.db",
        "TEAMMEM_GITLAB_URL": "https://gitlab.internal",
        "TEAMMEM_GITLAB_TOKEN": "tok",
        "TEAMMEM_GITLAB_GROUP": "42",
        "TEAMMEM_SINCE_DAYS": "14",
    })
    assert cfg.db_path == Path("/tmp/x.db")
    assert cfg.gitlab_url == "https://gitlab.internal"
    assert cfg.gitlab_token == "tok"
    assert cfg.gitlab_group == "42"
    assert cfg.since_days == 14


def test_gitlab_url_trailing_slash_stripped_and_config_dir_override():
    cfg = Config.load(env={
        "TEAMMEM_GITLAB_URL": "https://gitlab.internal/",
        "TEAMMEM_CONFIG_DIR": "/etc/teammem",
    })
    assert cfg.gitlab_url == "https://gitlab.internal"
    assert cfg.config_dir == Path("/etc/teammem")
    assert Config.load(env={}).config_dir == Path("config")


def test_vault_and_push_config():
    cfg = Config.load(env={"TEAMMEM_VAULT": "/tmp/v", "TEAMMEM_PUSH": "1"})
    assert cfg.vault_dir == Path("/tmp/v") and cfg.push is True
    assert Config.load(env={}).push is False
