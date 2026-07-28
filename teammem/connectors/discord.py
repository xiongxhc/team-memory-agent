"""Discord bot polling restricted to mapped guild channels."""

import json
from collections.abc import Callable
from datetime import datetime, timedelta

from teammem.config import Config
from teammem.events import Event
from teammem.identity import IdentityMaps

from .base import CollectionResult
from .config import ConnectorSettings


DISCORD_API_URL = "https://discord.com/api/v10"
_MESSAGE_LIMIT = 100
DiscordFetch = Callable[[str, dict], dict | list]


class DiscordConnector:
    name = "discord"

    def __init__(self, fetch: DiscordFetch | None = None):
        self._fetch = fetch

    def validate(self, cfg: Config, settings: ConnectorSettings) -> list[str]:
        return [] if cfg.discord_bot_token else ["TEAMMEM_DISCORD_BOT_TOKEN"]

    def http_fetch(self, cfg: Config) -> DiscordFetch:
        import requests

        session = requests.Session()
        session.headers["Authorization"] = f"Bot {cfg.discord_bot_token}"

        def fetch(path: str, params: dict) -> dict | list:
            response = session.get(f"{DISCORD_API_URL}{path}", params=params, timeout=30)
            response.raise_for_status()
            return response.json()

        return fetch

    def collect(
        self,
        cfg: Config,
        ids: IdentityMaps,
        settings: ConnectorSettings,
        now: datetime,
    ) -> CollectionResult:
        fetch = self._fetch or self.http_fetch(cfg)
        since = now - timedelta(days=cfg.since_days)
        events: list[Event] = []
        names: dict[str, str] = {}
        warnings: list[str] = []
        resources = ids.resources("discord-channel")
        failed_channels = 0
        for channel_id, project in resources.items():
            try:
                channel = fetch(f"/channels/{channel_id}", {})
            except Exception:
                failed_channels += 1
                warnings.append(
                    f"discord channel {channel_id} metadata request failed"
                )
                continue
            if not isinstance(channel, dict) or not channel.get("guild_id"):
                continue
            if name := channel.get("name"):
                names[channel_id] = name
            try:
                messages = self._messages(fetch, channel_id, since)
            except Exception:
                failed_channels += 1
                warnings.append(
                    f"discord channel {channel_id} history request failed"
                )
                continue
            if not messages:
                warnings.append(
                    f"discord channel {channel_id} returned no messages; verify "
                    "READ_MESSAGE_HISTORY and MESSAGE_CONTENT access"
                )
            elif any(
                self._is_human_message(message) and not message.get("content")
                for message in messages
            ):
                warnings.append(
                    f"discord channel {channel_id} returned human messages "
                    "with unavailable content; verify MESSAGE_CONTENT access"
                )
            for message in messages:
                if not self._is_human_content(message):
                    continue
                events.append(Event(
                    person=ids.person("discord", str((message.get("author") or {}).get("id", ""))),
                    project=project,
                    ts=message["timestamp"],
                    source="discord-channel",
                    kind="message",
                    summary=message["content"],
                    refs=json.dumps({"channel_id": channel_id}),
                    raw=json.dumps(message, ensure_ascii=False),
                    hash=str(message["id"]),
                ))
        if resources and failed_channels == len(resources):
            raise RuntimeError(
                "discord collection failed for every configured channel"
            )
        return CollectionResult(
            events=tuple(events), channel_names=names, warnings=tuple(warnings)
        )

    @staticmethod
    def _is_human_message(message: dict) -> bool:
        author = message.get("author") or {}
        return (
            message.get("type") == 0
            and bool(message.get("id"))
            and bool(message.get("timestamp"))
            and bool(author.get("id"))
            and not author.get("bot")
            and not message.get("webhook_id")
        )

    @classmethod
    def _is_human_content(cls, message: dict) -> bool:
        return cls._is_human_message(message) and bool(message.get("content"))

    @staticmethod
    def _timestamp(message: dict) -> datetime:
        return datetime.fromisoformat(message["timestamp"].replace("Z", "+00:00"))

    @classmethod
    def _messages(cls, fetch: DiscordFetch, channel_id: str, since: datetime) -> list[dict]:
        messages: list[dict] = []
        before = ""
        while True:
            params = {"limit": _MESSAGE_LIMIT}
            if before:
                params["before"] = before
            page = fetch(f"/channels/{channel_id}/messages", params)
            if not isinstance(page, list) or not page:
                return messages
            messages.extend(page)
            if any(cls._timestamp(message) < since for message in page):
                return [message for message in messages if cls._timestamp(message) >= since]
            if len(page) < _MESSAGE_LIMIT:
                return messages
            before = str(page[-1]["id"])
