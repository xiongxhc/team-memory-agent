"""Compatibility wrapper for the Feishu connector."""

from datetime import datetime

from .config import Config
from .connectors.config import ConnectorSettings
from .connectors.feishu import FEISHU_BASE, FeishuConnector, FeishuFetch
from .identity import IdentityMaps


def http_feishu_fetch(cfg: Config) -> FeishuFetch:
    return FeishuConnector().http_fetch(cfg)


def _summary(msg: dict) -> str:
    return FeishuConnector._summary(msg)


def collect_feishu(cfg: Config, ids: IdentityMaps, fetch: FeishuFetch,
                   now: datetime):
    settings = ConnectorSettings(name="feishu", enabled=True, options={})
    return list(FeishuConnector(fetch).collect(cfg, ids, settings, now).events)
