"""Static built-in connector registry with no import-time network activity."""

from collections.abc import Callable
from datetime import datetime

from teammem.config import Config
from teammem.identity import IdentityMaps

from .base import CollectionResult, Connector
from .config import ConnectorSettings
from .discord import DiscordConnector
from .feishu import FeishuConnector
from .gitlab import GitLabConnector
from .slack import SlackConnector


class _ConfiguredConnector:
    def __init__(self, name: str, variables: tuple[str, ...]):
        self.name = name
        self._variables = variables

    def validate(self, cfg: Config, settings: ConnectorSettings) -> list[str]:
        fields = {
            "TEAMMEM_GITHUB_TOKEN": cfg.github_token,
            "TEAMMEM_SLACK_BOT_TOKEN": cfg.slack_bot_token,
            "TEAMMEM_DISCORD_BOT_TOKEN": cfg.discord_bot_token,
        }
        return [name for name in self._variables if not fields[name]]

    def collect(
        self,
        cfg: Config,
        ids: IdentityMaps,
        settings: ConnectorSettings,
        now: datetime,
    ) -> CollectionResult:
        raise NotImplementedError(f"{self.name} connector is not implemented")


def _github_connector() -> Connector:
    from .github import GitHubConnector

    return GitHubConnector()


_CONNECTORS: dict[str, Connector | Callable[[], Connector]] = {
    "discord": DiscordConnector(),
    "feishu": FeishuConnector(),
    "github": _github_connector,
    "gitlab": GitLabConnector(),
    "slack": SlackConnector(),
}


def connector_names() -> tuple[str, ...]:
    return tuple(_CONNECTORS)


def get_connector(name: str) -> Connector:
    try:
        connector = _CONNECTORS[name]
    except KeyError:
        raise KeyError(f"unknown connector: {name}") from None
    return connector() if callable(connector) else connector
