"""Slack bot polling restricted to mapped shared channels."""

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from teammem.config import Config
from teammem.events import Event
from teammem.identity import IdentityMaps

from .base import CollectionResult
from .config import ConnectorSettings


SLACK_API_URL = "https://slack.com/api"
_HISTORY_LIMIT = 15
SlackFetch = Callable[[str, dict], dict]


class SlackConnector:
    name = "slack"

    def __init__(self, fetch: SlackFetch | None = None):
        self._fetch = fetch

    def validate(self, cfg: Config, settings: ConnectorSettings) -> list[str]:
        if cfg.slack_bot_token.startswith("xoxb-"):
            return []
        return ["TEAMMEM_SLACK_BOT_TOKEN"]

    def http_fetch(self, cfg: Config) -> SlackFetch:
        import requests

        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {cfg.slack_bot_token}"

        def fetch(path: str, params: dict) -> dict:
            response = session.get(f"{SLACK_API_URL}/{path}", params=params, timeout=30)
            response.raise_for_status()
            body = response.json()
            if not body.get("ok"):
                raise RuntimeError(f"slack {path}: {body.get('error', 'unknown error')}")
            return body

        return fetch

    def collect(
        self,
        cfg: Config,
        ids: IdentityMaps,
        settings: ConnectorSettings,
        now: datetime,
    ) -> CollectionResult:
        fetch = self._fetch or self.http_fetch(cfg)
        oldest = str(int((now - timedelta(days=cfg.since_days)).timestamp()))
        events: list[Event] = []
        names: dict[str, str] = {}
        for channel_id, project in ids.resources("slack-channel").items():
            try:
                metadata = fetch("conversations.info", {"channel": channel_id})
            except Exception:
                continue
            channel = metadata.get("channel") or {}
            if not self._shared_channel(channel):
                continue
            if name := channel.get("name"):
                names[channel_id] = name
            try:
                messages = self._history(fetch, channel_id, oldest)
            except Exception:
                continue
            for message in messages:
                if not self._is_human_top_level(message):
                    continue
                ts = message["ts"]
                events.append(Event(
                    person=ids.person("slack", message["user"]),
                    project=project,
                    ts=datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(),
                    source="slack-channel",
                    kind="message",
                    summary=message.get("text") or "[message]",
                    refs=json.dumps({"channel_id": channel_id}),
                    raw=json.dumps(message, ensure_ascii=False),
                    hash=ts,
                ))
        return CollectionResult(events=tuple(events), channel_names=names)

    @staticmethod
    def _shared_channel(channel: dict) -> bool:
        return (
            bool(channel.get("is_channel") or channel.get("is_group"))
            and not channel.get("is_im")
            and not channel.get("is_mpim")
            and bool(channel.get("is_member"))
        )

    @staticmethod
    def _is_human_top_level(message: dict) -> bool:
        return (
            not message.get("bot_id")
            and not message.get("subtype")
            and bool(message.get("user"))
            and bool(message.get("ts"))
            and message.get("thread_ts", message.get("ts")) == message.get("ts")
        )

    @staticmethod
    def _history(fetch: SlackFetch, channel_id: str, oldest: str) -> list[dict]:
        messages: list[dict] = []
        cursor = ""
        while True:
            params = {"channel": channel_id, "oldest": oldest, "limit": _HISTORY_LIMIT}
            if cursor:
                params["cursor"] = cursor
            page = fetch("conversations.history", params)
            messages.extend(page.get("messages") or [])
            cursor = (page.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                return messages
