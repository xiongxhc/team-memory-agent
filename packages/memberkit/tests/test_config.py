from pathlib import Path

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
