"""Provider-neutral hub connector contracts and configuration."""

from .base import CollectionResult, Connector
from .config import ConnectorSettings, load_connector_settings

__all__ = ["CollectionResult", "Connector", "ConnectorSettings", "load_connector_settings"]
