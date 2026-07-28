"""Slack bot polling restricted to mapped public or private channels."""

import json
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone

from teammem.config import Config
from teammem.events import Event
from teammem.identity import IdentityMaps

from .base import CollectionResult
from .config import ConnectorSettings


SLACK_API_URL = "https://slack.com/api"
_HISTORY_LIMIT = 15
_HISTORY_INTERVAL_SECONDS = 60
SlackFetch = Callable[[str, dict], dict]


class SlackRateLimited(RuntimeError):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Slack rate limited; retry after {retry_after} seconds")


class SlackConnector:
    name = "slack"

    def __init__(
        self,
        fetch: SlackFetch | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._fetch = fetch
        self._sleep = sleep

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
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", _HISTORY_INTERVAL_SECONDS)
                try:
                    raise SlackRateLimited(float(retry_after))
                except ValueError:
                    raise SlackRateLimited(_HISTORY_INTERVAL_SECONDS) from None
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
        warnings: list[str] = []
        resources = ids.resources("slack-channel")
        failed_channels = 0
        history_started = False

        def fetch_history(params: dict) -> dict:
            nonlocal history_started
            if history_started:
                self._sleep(_HISTORY_INTERVAL_SECONDS)
            history_started = True
            while True:
                try:
                    return fetch("conversations.history", params)
                except SlackRateLimited as error:
                    self._sleep(error.retry_after)

        for channel_id, project in resources.items():
            try:
                metadata = fetch("conversations.info", {"channel": channel_id})
                if not isinstance(metadata, Mapping):
                    raise ValueError("malformed Slack metadata response")
                channel = metadata.get("channel")
                if not isinstance(channel, Mapping):
                    raise ValueError("malformed Slack channel metadata")
            except Exception:
                failed_channels += 1
                warnings.append(
                    f"slack channel {channel_id} metadata request failed"
                )
                continue
            if not self._shared_channel(channel):
                continue
            if name := channel.get("name"):
                names[channel_id] = name
            try:
                messages = self._history(fetch_history, channel_id, oldest)
            except Exception:
                failed_channels += 1
                warnings.append(
                    f"slack channel {channel_id} history request failed"
                )
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
        if resources and failed_channels == len(resources):
            raise RuntimeError(
                "slack collection failed for every configured channel"
            )
        return CollectionResult(
            events=tuple(events),
            channel_names=names,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _shared_channel(channel: Mapping) -> bool:
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

    def _history(
        self,
        fetch_history: Callable[[dict], dict],
        channel_id: str,
        oldest: str,
    ) -> list[dict]:
        messages: list[dict] = []
        cursor = ""
        while True:
            params = {"channel": channel_id, "oldest": oldest, "limit": _HISTORY_LIMIT}
            if cursor:
                params["cursor"] = cursor
            page = fetch_history(params)
            messages.extend(page.get("messages") or [])
            cursor = (page.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                return messages
