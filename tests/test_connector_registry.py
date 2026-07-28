import subprocess
import sys

import pytest

from teammem.config import Config
from teammem.connectors.config import ConnectorSettings
from teammem.connectors.registry import connector_names, get_connector


def test_registry_lists_official_connectors_without_network():
    assert connector_names() == ("discord", "feishu", "github", "gitlab", "slack")


def test_registry_discovery_and_construction_keep_network_sentinel_armed():
    """Registry import, enumeration, and connector construction must stay local."""
    code = """
import socket

def network_forbidden(*args, **kwargs):
    raise AssertionError("registry attempted network access")

socket.create_connection = network_forbidden
socket.socket.connect = network_forbidden

from teammem.connectors.registry import connector_names, get_connector

names = connector_names()
for name in names:
    assert get_connector(name).name == name
print("network sentinel remained armed")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "network sentinel remained armed"


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


def test_registry_defers_optional_chat_adapter_imports_until_requested():
    """Eager optional adapter imports make registry discovery depend on every chat implementation."""
    code = (
        "import sys; import teammem.connectors.registry; "
        "print('teammem.connectors.slack' in sys.modules, "
        "'teammem.connectors.discord' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], check=True, capture_output=True, text=True
    )

    assert result.stdout.strip() == "False False"
