import pytest

from teammem.connectors.config import load_connector_settings


def test_all_example_connectors_are_disabled(tmp_path):
    settings = load_connector_settings(tmp_path)
    assert set(settings) == {"github", "gitlab", "slack", "feishu", "discord"}
    assert not any(item.enabled for item in settings.values())


def test_connector_settings_read_options_from_operator_configuration(tmp_path):
    (tmp_path / "connectors.yaml").write_text(
        "connectors:\n"
        "  github:\n"
        "    enabled: true\n"
        "    api_url: https://github.example\n"
    )
    settings = load_connector_settings(tmp_path)
    assert settings["github"].enabled is True
    assert settings["github"].options == {"api_url": "https://github.example"}
    assert settings["gitlab"].enabled is False


def test_unknown_connector_key_is_rejected(tmp_path):
    (tmp_path / "connectors.yaml").write_text(
        "connectors:\n  unknown:\n    enabled: true\n"
    )
    with pytest.raises(ValueError, match="unknown connector: unknown"):
        load_connector_settings(tmp_path)
