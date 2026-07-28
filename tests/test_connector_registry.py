import pytest

from teammem.config import Config
from teammem.connectors.config import ConnectorSettings
from teammem.connectors.registry import connector_names, get_connector


def test_registry_lists_official_connectors_without_network():
    assert connector_names() == ("discord", "feishu", "github", "gitlab", "slack")


@pytest.mark.parametrize(
    ("name", "missing"),
    [
        ("discord", ["TEAMMEM_DISCORD_BOT_TOKEN"]),
        ("feishu", ["TEAMMEM_FEISHU_APP_ID", "TEAMMEM_FEISHU_APP_SECRET"]),
        ("github", ["TEAMMEM_GITHUB_TOKEN"]),
        ("gitlab", ["TEAMMEM_GITLAB_URL", "TEAMMEM_GITLAB_TOKEN", "TEAMMEM_GITLAB_GROUP"]),
        ("slack", ["TEAMMEM_SLACK_BOT_TOKEN"]),
    ],
)
def test_registry_connectors_report_exact_missing_environment_names(name, missing):
    connector = get_connector(name)
    settings = ConnectorSettings(name=name, enabled=True, options={})
    assert connector.validate(Config(), settings) == missing


def test_registry_rejects_unknown_connector():
    with pytest.raises(KeyError, match="unknown connector: unknown"):
        get_connector("unknown")
