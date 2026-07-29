from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from memberkit import config


@pytest.fixture(autouse=True)
def isolate_config_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "memberkit.env")


def test_load_reads_required_and_defaults():
    cfg = config.load({"MEMBERKIT_MEMBER": "alex", "MEMBERKIT_INBOX_URL": "git@x:y.git"})
    assert cfg.member == "alex"
    assert cfg.inbox_url == "git@x:y.git"
    assert cfg.db == Path.home() / ".claude-mem" / "claude-mem.db"
    assert cfg.workdir == Path.home() / ".memberkit"


def test_load_missing_member_exits():
    with pytest.raises(SystemExit, match="MEMBERKIT_MEMBER"):
        config.load({"MEMBERKIT_INBOX_URL": "git@x:y.git"})


def test_env_file_used_when_env_lacks_key(monkeypatch, tmp_path):
    envfile = tmp_path / "memberkit.env"
    envfile.write_text("MEMBERKIT_MEMBER=filemember\n# comment\nMEMBERKIT_INBOX_URL=git@f:i.git\n")
    monkeypatch.setattr(config, "CONFIG_FILE", envfile)
    cfg = config.load({})
    assert cfg.member == "filemember"
    cfg2 = config.load({"MEMBERKIT_MEMBER": "envmember", "MEMBERKIT_INBOX_URL": "git@e:v.git"})
    assert cfg2.member == "envmember"  # explicit env beats file


def test_timezone_loads_from_private_file_with_process_env_override(
    monkeypatch, tmp_path,
):
    envfile = tmp_path / "memberkit.env"
    envfile.write_text(
        "MEMBERKIT_MEMBER=alex\n"
        "MEMBERKIT_INBOX_URL=git@f:i.git\n"
        "MEMBERKIT_TIMEZONE=America/Los_Angeles\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", envfile)

    from_file = config.load({})
    assert from_file.timezone == ZoneInfo("America/Los_Angeles")

    monkeypatch.setenv("MEMBERKIT_MEMBER", "alex")
    monkeypatch.setenv("MEMBERKIT_INBOX_URL", "git@e:v.git")
    monkeypatch.setenv("MEMBERKIT_TIMEZONE", "Asia/Tokyo")

    from_process = config.load()

    assert from_process.timezone == ZoneInfo("Asia/Tokyo")


def test_invalid_explicit_timezone_is_rejected():
    with pytest.raises(SystemExit, match="invalid MEMBERKIT_TIMEZONE"):
        config.load({
            "MEMBERKIT_MEMBER": "alex",
            "MEMBERKIT_INBOX_URL": "git@x:y.git",
            "MEMBERKIT_TIMEZONE": "Mars/Olympus_Mons",
        })
