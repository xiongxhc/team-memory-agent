from pathlib import Path
import traceback
from zoneinfo import ZoneInfo

import pytest

from memberkit import config


def test_load_reads_required_and_defaults(tmp_path):
    cfg = config.load(
        {"MEMBERKIT_MEMBER": "alex", "MEMBERKIT_INBOX_URL": "git@x:y.git"},
        config_file=tmp_path / "memberkit.env",
    )
    assert cfg.member == "alex"
    assert cfg.inbox_url == "git@x:y.git"
    assert cfg.db == Path.home() / ".claude-mem" / "claude-mem.db"
    assert cfg.workdir == Path.home() / ".memberkit"


def test_load_missing_member_exits(tmp_path):
    with pytest.raises(SystemExit, match="MEMBERKIT_MEMBER"):
        config.load(
            {"MEMBERKIT_INBOX_URL": "git@x:y.git"},
            config_file=tmp_path / "memberkit.env",
        )


def test_env_file_used_when_env_lacks_key(monkeypatch, tmp_path):
    envfile = tmp_path / "memberkit.env"
    envfile.write_text("MEMBERKIT_MEMBER=filemember\n# comment\nMEMBERKIT_INBOX_URL=git@f:i.git\n")
    cfg = config.load({}, config_file=envfile)
    assert cfg.member == "filemember"
    cfg2 = config.load(
        {"MEMBERKIT_MEMBER": "envmember", "MEMBERKIT_INBOX_URL": "git@e:v.git"},
        config_file=envfile,
    )
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
    from_file = config.load({}, config_file=envfile)
    assert from_file.timezone == ZoneInfo("America/Los_Angeles")

    monkeypatch.setenv("MEMBERKIT_MEMBER", "alex")
    monkeypatch.setenv("MEMBERKIT_INBOX_URL", "git@e:v.git")
    monkeypatch.setenv("MEMBERKIT_TIMEZONE", "Asia/Tokyo")

    from_process = config.load(config_file=envfile)

    assert from_process.timezone == ZoneInfo("Asia/Tokyo")


def test_invalid_explicit_timezone_is_rejected(tmp_path):
    with pytest.raises(SystemExit, match="invalid MEMBERKIT_TIMEZONE"):
        config.load(
            {
                "MEMBERKIT_MEMBER": "alex",
                "MEMBERKIT_INBOX_URL": "git@x:y.git",
                "MEMBERKIT_TIMEZONE": "Mars/Olympus_Mons",
            },
            config_file=tmp_path / "memberkit.env",
        )


def test_invalid_timezone_does_not_expose_value_through_exception_chain(tmp_path):
    secret = "secret-timezone-47a9"

    with pytest.raises(SystemExit) as raised:
        config.load(
            {
                "MEMBERKIT_MEMBER": "alex",
                "MEMBERKIT_INBOX_URL": "https://inbox.example.invalid",
                "MEMBERKIT_TIMEZONE": secret,
            },
            config_file=tmp_path / "memberkit.env",
        )

    error = raised.value
    traceback_text = "".join(traceback.format_exception(error))

    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in traceback_text


def test_default_config_file_uses_appdata_on_windows():
    assert config.default_config_file(
        platform="win32",
        env={"APPDATA": r"C:\Users\Alex\AppData\Roaming"},
    ) == Path(r"C:\Users\Alex\AppData\Roaming") / "TeamMemory" / "memberkit.env"


def test_default_config_file_requires_appdata_on_windows():
    with pytest.raises(RuntimeError, match="^APPDATA is required on Windows$"):
        config.default_config_file(platform="win32", env={})


def test_default_config_file_retains_posix_location():
    assert config.default_config_file(platform="darwin", env={}) == (
        Path.home() / ".config" / "teammem" / "memberkit.env"
    )


def test_windows_load_reads_through_validated_private_reader(monkeypatch):
    from memberkit import windows_security

    config_file = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\memberkit.env")
    api = object()
    calls = []
    monkeypatch.setattr(windows_security, "current_user_sid", lambda actual: "S-1-5-21-alex")

    def read_private_text(path, sid, actual):
        calls.append((path, sid, actual))
        return (
            "MEMBERKIT_MEMBER=file-alex\n"
            "MEMBERKIT_INBOX_URL=https://file.example.invalid/inbox\n"
            "MEMBERKIT_TIMEZONE=America/Los_Angeles\n"
        )

    monkeypatch.setattr(windows_security, "read_windows_private_text", read_private_text)

    cfg = config.load(
        {
            "MEMBERKIT_MEMBER": "process-alex",
            "MEMBERKIT_INBOX_URL": "https://process.example.invalid/inbox",
        },
        config_file=config_file,
        platform="win32",
        windows_api=api,
    )

    assert calls == [(config_file, "S-1-5-21-alex", api)]
    assert cfg.member == "process-alex"
    assert cfg.inbox_url == "https://process.example.invalid/inbox"
    assert cfg.timezone == ZoneInfo("America/Los_Angeles")


def test_windows_write_uses_private_atomic_writer_in_stable_key_order(monkeypatch):
    from memberkit import windows_security

    config_file = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\memberkit.env")
    api = object()
    calls = []
    monkeypatch.setattr(windows_security, "current_user_sid", lambda actual: "S-1-5-21-alex")
    monkeypatch.setattr(
        windows_security,
        "atomic_write_windows_private_text",
        lambda path, text, sid, actual: calls.append((path, text, sid, actual)) or path,
    )

    result = config.write_config(
        {
            "MEMBERKIT_WORKDIR": r"C:\Users\Alex\memberkit",
            "MEMBERKIT_MEMBER": "alex",
            "MEMBERKIT_INBOX_URL": "https://inbox.example.invalid",
            "MEMBERKIT_TIMEZONE": "Asia/Dubai",
            "UNRELATED": "not-rendered",
        },
        config_file=config_file,
        platform="win32",
        windows_api=api,
    )

    assert result == config_file
    assert calls == [
        (
            config_file,
            "MEMBERKIT_MEMBER=alex\n"
            "MEMBERKIT_INBOX_URL=https://inbox.example.invalid\n"
            "MEMBERKIT_WORKDIR=C:\\Users\\Alex\\memberkit\n"
            "MEMBERKIT_TIMEZONE=Asia/Dubai\n",
            "S-1-5-21-alex",
            api,
        )
    ]


def test_write_config_retains_posix_private_file_permissions(tmp_path):
    config_file = tmp_path / "nested" / "memberkit.env"

    result = config.write_config(
        {"MEMBERKIT_MEMBER": "alex", "MEMBERKIT_INBOX_URL": "https://inbox.example.invalid"},
        config_file=config_file,
        platform="darwin",
    )

    assert result == config_file
    assert config_file.read_text(encoding="utf-8") == (
        "MEMBERKIT_MEMBER=alex\n"
        "MEMBERKIT_INBOX_URL=https://inbox.example.invalid\n"
    )
    assert config_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("value", ["line\rreturn", "line\nfeed", "nul\0byte"])
def test_write_config_rejects_control_characters_in_values(tmp_path, value):
    with pytest.raises(ValueError, match="control characters"):
        config.write_config(
            {"MEMBERKIT_MEMBER": value},
            config_file=tmp_path / "memberkit.env",
        )
