"""Non-secret, provider-neutral connector configuration."""

from dataclasses import dataclass
from pathlib import Path

import yaml


CONNECTOR_NAMES = ("github", "gitlab", "slack", "feishu", "discord")


@dataclass(frozen=True)
class ConnectorSettings:
    name: str
    enabled: bool
    options: dict


def _read(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text()) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"connector configuration must be a mapping: {path}")
    return loaded


def _default_connector_data() -> dict:
    return {name: {"enabled": False} for name in CONNECTOR_NAMES}


def load_connector_settings(config_dir: Path) -> dict[str, ConnectorSettings]:
    """Load private settings, falling back to the disabled public example."""
    config_dir = Path(config_dir)
    configured = config_dir / "connectors.yaml"
    example = config_dir / "connectors.example.yaml"
    data = _read(configured if configured.exists() else example) if example.exists() or configured.exists() else {
        "connectors": _default_connector_data()
    }
    connectors = data.get("connectors", {})
    if not isinstance(connectors, dict):
        raise ValueError("connector configuration field 'connectors' must be a mapping")

    unknown = set(connectors) - set(CONNECTOR_NAMES)
    if unknown:
        raise ValueError(f"unknown connector: {sorted(unknown)[0]}")

    settings = {}
    for name in CONNECTOR_NAMES:
        raw = connectors.get(name, {})
        if not isinstance(raw, dict):
            raise ValueError(f"connector configuration for {name} must be a mapping")
        enabled = raw.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError(f"connector enabled flag for {name} must be a boolean")
        options = {key: value for key, value in raw.items() if key != "enabled"}
        if name == "gitlab":
            collect_mr_commits = options.setdefault("collect_mr_commits", True)
            if not isinstance(collect_mr_commits, bool):
                raise ValueError(
                    "connector option collect_mr_commits for gitlab must be a boolean"
                )
            exclude_note_authors = options.setdefault("exclude_note_authors", [])
            if (not isinstance(exclude_note_authors, list)
                    or any(not isinstance(u, str) for u in exclude_note_authors)):
                raise ValueError(
                    "connector option exclude_note_authors for gitlab must be "
                    "a list of strings"
                )
        settings[name] = ConnectorSettings(
            name=name,
            enabled=enabled,
            options=options,
        )
    return settings
