"""Feishu project-channel collector. Reads ONLY chats the tenant bot has been
added to (visible membership = per-channel consent) — structurally cannot see
DMs. Fetching goes through an injected fetch(path, params) -> data-dict
callable; production uses http_feishu_fetch(cfg)."""

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from .config import Config
from .events import Event
from .identity import IdentityMaps

FEISHU_BASE = "https://open.feishu.cn/open-apis"
FeishuFetch = Callable[[str, dict], dict]


def http_feishu_fetch(cfg: Config) -> FeishuFetch:
    import requests
    session = requests.Session()
    r = session.post(f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
                     json={"app_id": cfg.feishu_app_id,
                           "app_secret": cfg.feishu_app_secret}, timeout=30)
    r.raise_for_status()
    body = r.json()
    if body.get("code"):
        raise RuntimeError(f"feishu auth: {body.get('code')} {body.get('msg')}")
    tok = body["tenant_access_token"]
    session.headers["Authorization"] = f"Bearer {tok}"

    def fetch(path: str, params: dict) -> dict:
        resp = session.get(f"{FEISHU_BASE}{path}", params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") not in (0, None):
            raise RuntimeError(f"feishu {path}: {body.get('code')} {body.get('msg')}")
        return body.get("data") or {}

    return fetch


def _paginate(fetch: FeishuFetch, path: str, params: dict) -> list:
    out, token = [], ""
    while True:
        p = {**params, "page_size": 50}
        if token:
            p["page_token"] = token
        data = fetch(path, p)
        out += data.get("items") or []
        token = data.get("page_token") or ""
        if not data.get("has_more"):
            return out


def _summary(msg: dict) -> str:
    if msg.get("msg_type") == "text":
        try:
            text = json.loads(msg["body"]["content"]).get("text", "")
            return text[:100] or "[text]"
        except (KeyError, ValueError, AttributeError, TypeError):
            return "[text]"
    return f"[{msg.get('msg_type', 'unknown')}]"


def collect_feishu(cfg: Config, ids: IdentityMaps, fetch: FeishuFetch,
                   now: datetime) -> list[Event]:
    start = str(int((now - timedelta(days=cfg.since_days)).timestamp()))
    end = str(int(now.timestamp()))
    events: list[Event] = []
    names: dict[str, str] = {}
    for chat in _paginate(fetch, "/im/v1/chats", {}):
        chat_id = chat["chat_id"]
        names[chat_id] = chat.get("name") or ""
        project = ids.project_for_channel(chat_id)
        for msg in _paginate(fetch, "/im/v1/messages",
                             {"container_id_type": "chat",
                              "container_id": chat_id,
                              "start_time": start, "end_time": end}):
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
                summary=_summary(msg),
                refs=json.dumps({"message_id": msg["message_id"],
                                 "chat_id": chat_id}),
                raw=json.dumps(msg, ensure_ascii=False),
                hash=msg["message_id"],
            ))
    if names:
        try:
            (cfg.config_dir / "channel_names.json").write_text(
                json.dumps(names, ensure_ascii=False, sort_keys=True, indent=1))
        except OSError:
            pass   # name cache is best-effort; collection must not fail on it
    return events
