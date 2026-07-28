"""Compatibility wrapper for the GitLab connector."""

from datetime import datetime

from .config import Config
from .connectors.config import ConnectorSettings
from .connectors.gitlab import FetchJson, GitLabConnector
from .identity import IdentityMaps


def http_fetch_json(cfg: Config) -> FetchJson:
    return GitLabConnector().http_fetch_json(cfg)


def collect_gitlab(cfg: Config, ids: IdentityMaps, fetch_json: FetchJson,
                   now: datetime):
    settings = ConnectorSettings(name="gitlab", enabled=True, options={})
    return list(GitLabConnector(fetch_json).collect(cfg, ids, settings, now).events)
