"""Feishu project-channel connector with an explicit channel allowlist."""

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from teammem.config import Config
from teammem.events import Event
from teammem.identity import IdentityMaps

from .base import CollectionResult
from .config import ConnectorSettings


FEISHU_BASE = "https://open.feishu.cn/open-apis"
FeishuFetch = Callable[[str, dict], dict]


class FeishuConnector:
    name = "feishu"

    def __init__(self, fetch: FeishuFetch | None = None):
        self._fetch = fetch

    def validate(self, cfg: Config, settings: ConnectorSettings) -> list[str]:
        fields = (
            ("TEAMMEM_FEISHU_APP_ID", cfg.feishu_app_id),
            ("TEAMMEM_FEISHU_APP_SECRET", cfg.feishu_app_secret),
        )
        return [name for name, value in fields if not value]

    def http_fetch(self, cfg: Config) -> FeishuFetch:
        import requests

        session = requests.Session()
        response = session.post(
            f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": cfg.feishu_app_id, "app_secret": cfg.feishu_app_secret},
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code"):
            raise RuntimeError(f"feishu auth: {body.get('code')} {body.get('msg')}")
        session.headers["Authorization"] = f"Bearer {body['tenant_access_token']}"

        def fetch(path: str, params: dict) -> dict:
            response = session.get(f"{FEISHU_BASE}{path}", params=params, timeout=30)
            response.raise_for_status()
            body = response.json()
            if body.get("code") not in (0, None):
                raise RuntimeError(f"feishu {path}: {body.get('code')} {body.get('msg')}")
            return body.get("data") or {}

        return fetch

    def collect(
        self,
        cfg: Config,
        ids: IdentityMaps,
        settings: ConnectorSettings,
        now: datetime,
    ) -> CollectionResult:
        fetch = self._fetch or self.http_fetch(cfg)
        start = str(int((now - timedelta(days=cfg.since_days)).timestamp()))
        end = str(int(now.timestamp()))
        events: list[Event] = []
        names: dict[str, str] = {}
        for chat_id, project in ids.resources("feishu-channel").items():
            try:
                chat = fetch(f"/im/v1/chats/{chat_id}", {})
            except RuntimeError:
                chat = {}
            if name := chat.get("name"):
                names[chat_id] = name
            for msg in self._paginate(fetch, "/im/v1/messages", {
                "container_id_type": "chat",
                "container_id": chat_id,
                "start_time": start,
                "end_time": end,
            }):
                sender = msg.get("sender") or {}
                if sender.get("sender_type") != "user":
                    continue
                ts = datetime.fromtimestamp(int(msg["create_time"]) / 1000,
                                            tz=timezone.utc).isoformat()
                events.append(Event(
                    person=ids.person("feishu", sender.get("id", "")),
                    project=project,
                    ts=ts,
                    source="feishu-channel",
                    kind="message",
                    summary=self._summary(msg),
                    refs=json.dumps({"message_id": msg["message_id"], "chat_id": chat_id}),
                    raw=json.dumps(msg, ensure_ascii=False),
                    hash=msg["message_id"],
                ))
        return CollectionResult(events=tuple(events), channel_names=names)

    @staticmethod
    def _paginate(fetch: FeishuFetch, path: str, params: dict) -> list:
        out, token = [], ""
        while True:
            request_params = {**params, "page_size": 50}
            if token:
                request_params["page_token"] = token
            data = fetch(path, request_params)
            out += data.get("items") or []
            token = data.get("page_token") or ""
            if not data.get("has_more"):
                return out

    @staticmethod
    def _summary(msg: dict) -> str:
        if msg.get("msg_type") == "text":
            try:
                text = json.loads(msg["body"]["content"]).get("text", "")
                return text[:100] or "[text]"
            except (KeyError, ValueError, AttributeError, TypeError):
                return "[text]"
        return f"[{msg.get('msg_type', 'unknown')}]"
