"""Shared connector contracts with no provider imports or side effects."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from teammem.config import Config
from teammem.events import Event
from teammem.identity import IdentityMaps

from .config import ConnectorSettings


@dataclass(frozen=True)
class CollectionResult:
    events: tuple[Event, ...] = ()
    channel_names: dict[str, str] = field(default_factory=dict)


class Connector(Protocol):
    name: str

    def validate(self, cfg: Config, settings: ConnectorSettings) -> list[str]: ...

    def collect(
        self,
        cfg: Config,
        ids: IdentityMaps,
        settings: ConnectorSettings,
        now: datetime,
    ) -> CollectionResult: ...
