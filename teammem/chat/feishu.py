"""Narrow Feishu event normalization; SDK objects are converted at the boundary."""

import json
import re
import threading
import time
from urllib.parse import quote

import requests
from dataclasses import dataclass
from typing import Any

from .state import SessionKey


@dataclass(frozen=True)
class NormalizedEvent:
    tenant: str
    app: str
    message_id: str
    chat_id: str
    sender: str
    chat_type: str
    root_id: str
    parent_id: str
    mentions: frozenset[str]
    text: str | None
    message_type: str
    resources: tuple[dict[str, Any], ...]
    is_bot_generated: bool = False


def _get(source: Any, name: str, default: Any = None) -> Any:
    return source.get(name, default) if isinstance(source, dict) else getattr(source, name, default)


def normalize_event(source: Any, *, own_bot_open_id: str = "") -> NormalizedEvent:
    event = _get(source, "event", source)
    header = _get(source, "header", {})
    message = _get(event, "message", {})
    sender = _get(event, "sender", {})
    sender_id = _get(sender, "sender_id", {})
    mentions = _get(message, "mentions", _get(event, "mentions", [])) or []
    mention_ids = frozenset(str(_get(_get(item, "id", item), "open_id", "") or "") for item in mentions) - {""}
    message_type = str(_get(message, "message_type", "") or "")
    try:
        parsed = json.loads(_get(message, "content", ""))
        if not isinstance(parsed, dict):
            parsed = {}
    except (TypeError, ValueError):
        parsed = {}
    text = parsed.get("text") if isinstance(parsed.get("text"), str) and message_type == "text" else None
    # Keep human mentions as platform-provided names; only our bot mention is routing.
    if text is not None:
        replacements = {}
        for mention in mentions:
            key = _get(mention, "key", "")
            if not isinstance(key, str) or not key:
                continue
            open_id = _get(_get(mention, "id", mention), "open_id", "")
            name = _get(mention, "name", "")
            if own_bot_open_id and open_id == own_bot_open_id:
                replacements[key] = ""
            elif isinstance(name, str) and name.strip():
                replacements[key] = name.strip()
        if replacements:
            # Single pass avoids rewriting names or matching @_user_1 inside @_user_10.
            pattern = "(?:" + "|".join(re.escape(key) for key in sorted(replacements, key=len, reverse=True)) + r")(?!\d)"
            text = re.sub(pattern, lambda match: replacements[match.group()], text)
        text = text.strip()
    resources = _resources(message_type, parsed)
    def value(obj, key, default=""):
        return str(_get(obj, key, default) or "")
    tenant = value(header, "tenant_key", _get(source, "tenant", _get(sender, "tenant_key", "")))
    sender_tenant = value(sender, "tenant_key")
    if sender_tenant and sender_tenant != tenant:
        tenant = ""  # A different sender tenant must fail normal admission.
    return NormalizedEvent(tenant,
        value(header, "app_id", _get(source, "app", _get(event, "app_id", ""))),
        value(message,"message_id"),value(message,"chat_id"),value(sender_id,"open_id"),value(message,"chat_type"),
        value(message,"root_id"),value(message,"parent_id"),mention_ids,text,message_type,resources,
        _get(sender,"sender_type","user") != "user")


def _resources(message_type, content):
    if message_type == "file" and isinstance(content.get("file_key"), str):
        return ({"file_key":content["file_key"],"filename":content.get("file_name", "attachment"),"type":"file"},)
    if message_type == "image" and isinstance(content.get("image_key"), str):
        return ({"file_key":content["image_key"],"filename":"image.png","type":"image"},)
    return ()


def session_key(event: NormalizedEvent) -> SessionKey:
    if event.chat_type == "p2p":
        return SessionKey(event.tenant, event.app, "dm", event.sender)
    if event.chat_type == "group":
        return SessionKey(event.tenant, event.app, "thread", event.chat_id, event.root_id) if event.root_id else SessionKey(event.tenant, event.app, "group", event.chat_id)
    raise ValueError("unsupported chat type")


def is_own_bot_mention(event: NormalizedEvent, expected_bot_open_id: str) -> bool:
    return expected_bot_open_id in event.mentions


class FeishuError(RuntimeError):
    """Safe platform failure; provider bodies and credentials are never surfaced."""


def _reply_content(text):
    """Render generated citation footers as native Feishu links."""
    rows = []
    for line in text.split("\n"):
        citation = re.fullmatch(r"(\[E\d+\] )\[((?:\\.|[^\]\\])*)\]\((https?://[^\s()]*)\)", line)
        if citation:
            prefix, label, url = citation.groups()
            rows.append([{"tag":"text", "text":prefix},
                         {"tag":"a", "text":re.sub(r"\\(.)", r"\1", label), "href":url}])
        else:
            rows.append([{"tag":"text", "text":line}])
    return {"en_us":{"title":"", "content":rows}}


class FeishuClient:
    """Official SDK long connection plus narrow tenant-token REST operations."""

    def __init__(self, app_id: str, app_secret: str, *, http=None):
        self.app_id, self.app_secret = app_id, app_secret
        self.http = http or requests.Session()
        self.http.trust_env = False
        self._token, self._expires = "", 0
        self._token_lock = threading.Lock()

    def _auth(self, *, deadline=None):
        wait = max(0, deadline-time.monotonic()) if deadline is not None else -1
        if not self._token_lock.acquire(timeout=wait):
            raise FeishuError("Bot authentication timed out.")
        try:
            if time.monotonic() < self._expires:
                return self._token
            timeout = min(10, max(.1, deadline-time.monotonic())) if deadline is not None else 10
            try:
                with self.http.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id":self.app_id,"app_secret":self.app_secret},timeout=timeout,allow_redirects=False) as response:
                    body = response.json()
                    if response.status_code != 200 or body.get("code") != 0:
                        raise FeishuError("Bot authentication failed.")
                self._token = body["tenant_access_token"]
                self._expires = time.monotonic() + max(0, int(body.get("expire", 0))-60)
                return self._token
            except (requests.RequestException, ValueError, KeyError) as exc:
                raise FeishuError("Bot authentication failed.") from exc
        finally:
            self._token_lock.release()

    def _json(self, method, path, *, cancel_event=None, deadline=None, **kwargs):
        try:
            token = self._auth(deadline=deadline)
            if (cancel_event is not None and cancel_event.is_set()) or (deadline is not None and time.monotonic() >= deadline):
                raise FeishuError("Reply delivery was cancelled or timed out.")
            timeout = min(10, max(.1, deadline-time.monotonic())) if deadline is not None else 10
            with self.http.request(method,"https://open.feishu.cn/open-apis"+path,
                headers={"Authorization":"Bearer "+token}, timeout=timeout, allow_redirects=False, **kwargs) as response:
                body = response.json()
                if response.status_code != 200 or body.get("code") != 0:
                    raise FeishuError("Feishu could not access this message or resource. It may be restricted, external, or older than the bot's access.")
                return body
        except (requests.RequestException, ValueError) as exc:
            raise FeishuError("Feishu could not complete the request.") from exc

    def verify_identity(self, expected_bot_open_id: str) -> bool:
        body = self._json("GET", "/bot/v3/info")
        bot = body.get("bot", {})
        if bot.get("open_id") != expected_bot_open_id or bot.get("activate_status") != 2:
            raise FeishuError("The bot identity does not match the configured app.")
        return True

    def get_message(self, message_id, chat_id):
        body = self._json("GET", "/im/v1/messages/"+quote(message_id, safe=""))
        items = body.get("data", {}).get("items", [])
        item = next((item for item in items if item.get("message_id") == message_id), None)
        if item is None or item.get("chat_id") != chat_id:
            raise FeishuError("The referenced file is not in this conversation.")
        if item.get("deleted"):
            raise FeishuError("The referenced message was deleted.")
        return item

    def message_resources(self, message_id, chat_id):
        item = self.get_message(message_id, chat_id)
        try:
            content = json.loads(item.get("body", {}).get("content", "{}"))
        except ValueError as exc:
            raise FeishuError("The referenced message has invalid attachment content.") from exc
        resources = _resources(item.get("msg_type"), content if isinstance(content, dict) else {})
        if not resources:
            raise FeishuError("This message does not contain a supported file or image. Card and forwarded-message resources are not supported.")
        return item, resources

    def download_resource(self, message_id, chat_id, resource, *, max_bytes, deadline, cancel_event=None):
        # Re-read the exact source: callers cannot authorize a cross-message file key.
        _, allowed = self.message_resources(message_id, chat_id)
        if resource not in allowed:
            raise FeishuError("The resource does not belong to the referenced message.")
        url = "https://open.feishu.cn/open-apis/im/v1/messages/"+quote(message_id,safe="")+"/resources/"+quote(resource['file_key'],safe="")
        try:
            with self.http.get(url, params={"type":resource['type']},
                headers={"Authorization":"Bearer "+self._auth()},stream=True,allow_redirects=False,timeout=(5,5)) as response:
                if response.status_code != 200 or 'application/json' in response.headers.get('Content-Type',''):
                    raise FeishuError("This file could not be downloaded with the bot's message permissions.")
                if int(response.headers.get('Content-Length',0)) > max_bytes:
                    raise FeishuError("This file exceeds the attachment size limit.")
                total = 0
                for chunk in response.iter_content(65536):
                    if time.monotonic() >= deadline or (cancel_event is not None and cancel_event.is_set()):
                        raise FeishuError("File download was cancelled or timed out.")
                    total += len(chunk)
                    if total > max_bytes:
                        raise FeishuError("This file exceeds the attachment size limit.")
                    yield chunk
        except (requests.RequestException, ValueError) as exc:
            raise FeishuError("The attachment download failed.") from exc

    def send_reply(self, chat_id, message_id, reply_id, text, *, cancel_event=None, deadline=None):
        # Verify the persisted target rather than accepting an arbitrary destination.
        if cancel_event is not None and cancel_event.is_set():
            raise FeishuError("Reply delivery was cancelled.")
        original = self.get_message(message_id, chat_id)
        body = self._json("POST", "/im/v1/messages/"+quote(message_id,safe="")+"/reply",
            json={"msg_type":"post","content":json.dumps(_reply_content(text),ensure_ascii=False),"uuid":reply_id,
                  "reply_in_thread":bool(original and original.get("root_id"))},
            cancel_event=cancel_event, deadline=deadline)
        message_id = body.get("data", {}).get("message_id")
        if not message_id:
            raise FeishuError("Feishu did not confirm reply delivery.")
        return message_id

    def create_reaction(self, message_id, emoji_type="Typing", *, deadline=None):
        body = self._json(
            "POST", "/im/v1/messages/"+quote(message_id, safe="")+"/reactions",
            json={"reaction_type":{"emoji_type":emoji_type}}, deadline=deadline,
        )
        reaction_id = body.get("data", {}).get("reaction_id")
        if not reaction_id:
            raise FeishuError("Feishu did not confirm reaction creation.")
        return str(reaction_id)

    def delete_reaction(self, message_id, reaction_id, *, deadline=None):
        self._json(
            "DELETE", "/im/v1/messages/"+quote(message_id, safe="")+"/reactions/"+quote(reaction_id, safe=""),
            deadline=deadline,
        )

    def start(self, callback):
        import lark_oapi as lark
        dispatcher = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(callback).build()
        return lark.ws.Client(self.app_id, self.app_secret, event_handler=dispatcher, log_level=lark.LogLevel.ERROR).start()
