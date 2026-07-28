import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from teammem.config import Config
from teammem.connectors.config import ConnectorSettings
from teammem.connectors.feishu import FeishuConnector
from teammem.feishu_collector import _summary, collect_feishu
from teammem.identity import IdentityMaps

NOW = datetime(2026, 7, 16, tzinfo=timezone.utc)
CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"

MSG = {"message_id": "om_1", "chat_id": "oc_example_alpha", "msg_type": "text",
       "create_time": "1784264400000",
       "sender": {"id": "ou_example_alex", "id_type": "open_id",
                  "sender_type": "user"},
       "body": {"content": json.dumps({"text": "deploy done, checking logs"})}}
BOT_MSG = {**MSG, "message_id": "om_2",
           "sender": {"id": "cli_x", "id_type": "app_id", "sender_type": "app"}}
IMG_MSG = {**MSG, "message_id": "om_3", "msg_type": "image",
           "body": {"content": json.dumps({"image_key": "k"})}}


def _ids_with_channels(*channel_ids):
    return IdentityMaps(
        {"members": {"alex": {"feishu": ["ou_example_alex"]}}},
        {"projects": {
            f"project-{index}": {"feishu_channels": [channel_id]}
            for index, channel_id in enumerate(channel_ids, start=1)
        }},
    )


def fake_fetch(path, params):
    if path == "/im/v1/chats/oc_example_alpha":
        return {"name": "Project Alpha", "chat_mode": "group"}
    if path == "/im/v1/messages":
        assert params["container_id"] == "oc_example_alpha"
        assert params["start_time"].isdigit() and params["end_time"].isdigit()
        return {"items": [MSG, BOT_MSG, IMG_MSG], "has_more": False, "page_token": ""}
    raise AssertionError(path)


def test_messages_become_events(tmp_path):
    cfg = Config.load(env={"TEAMMEM_CONFIG_DIR": str(tmp_path)})
    events = collect_feishu(cfg, IdentityMaps.load(CONFIG_DIR), fake_fetch, NOW)
    assert len(events) == 2                                   # bot message skipped
    text = next(e for e in events if e.hash == "om_1")
    assert (text.person, text.project, text.kind) == ("alex", "project-alpha", "message")
    assert text.source == "feishu-channel"
    assert text.summary == "deploy done, checking logs"
    assert text.ts.startswith("2026-07-17T")                  # 1784264400000 ms UTC
    img = next(e for e in events if e.hash == "om_3")
    assert img.summary == "[image]"


def test_feishu_fetches_only_project_mapped_channels():
    calls = []

    def recording_fetch(path, params):
        calls.append((path, params))
        if path == "/im/v1/chats/oc_example_alpha":
            return {"name": "Project Alpha", "chat_mode": "group"}
        if path == "/im/v1/messages":
            return {"items": [MSG], "has_more": False, "page_token": ""}
        raise AssertionError(path)

    cfg = Config.load(env={})
    ids = IdentityMaps.load(CONFIG_DIR)
    settings = ConnectorSettings(name="feishu", enabled=True, options={})
    result = FeishuConnector(fetch=recording_fetch).collect(cfg, ids, settings, NOW)

    message_calls = [params for path, params in calls if path == "/im/v1/messages"]
    assert {call["container_id"] for call in message_calls} == {"oc_example_alpha"}
    assert result.channel_names == {"oc_example_alpha": "Project Alpha"}
    assert all(event.source == "feishu-channel" for event in result.events)


def test_feishu_never_fetches_messages_for_a_mapped_p2p_chat():
    calls = []

    def fetch(path, params):
        calls.append((path, params))
        if path == "/im/v1/chats/oc_example_alpha":
            return {"name": "Alex", "chat_mode": "p2p"}
        if path == "/im/v1/messages":
            return {"items": [MSG], "has_more": False, "page_token": ""}
        raise AssertionError(path)

    result = FeishuConnector(fetch=fetch).collect(
        Config.load(env={}),
        IdentityMaps.load(CONFIG_DIR),
        ConnectorSettings(name="feishu", enabled=True, options={}),
        NOW,
    )

    assert not [path for path, _ in calls if path == "/im/v1/messages"]
    assert result.events == ()
    assert result.channel_names == {}


def test_feishu_never_fetches_messages_when_chat_metadata_fails():
    calls = []

    def fetch(path, params):
        calls.append((path, params))
        if path == "/im/v1/chats/oc_example_alpha":
            raise RuntimeError("metadata unavailable")
        if path == "/im/v1/messages":
            return {"items": [MSG], "has_more": False, "page_token": ""}
        raise AssertionError(path)

    with pytest.raises(
        RuntimeError,
        match="feishu collection failed for every configured channel",
    ):
        FeishuConnector(fetch=fetch).collect(
            Config.load(env={}),
            IdentityMaps.load(CONFIG_DIR),
            ConnectorSettings(name="feishu", enabled=True, options={}),
            NOW,
        )

    assert not [path for path, _ in calls if path == "/im/v1/messages"]


def test_unknown_sender_is_unmapped(tmp_path):
    ghost = {**MSG, "message_id": "om_9",
             "sender": {"id": "ou_ghost", "id_type": "open_id",
                        "sender_type": "user"}}
    def fetch(path, params):
        if path == "/im/v1/chats/oc_example_alpha":
            return {"chat_mode": "group"}
        return {"items": [ghost], "has_more": False, "page_token": ""}
    events = collect_feishu(Config.load(env={"TEAMMEM_CONFIG_DIR": str(tmp_path)}),
                            IdentityMaps.load(CONFIG_DIR), fetch, NOW)
    assert events[0].person == "_unmapped/ou_ghost"


def test_summary_handles_valid_json_non_dict_body():
    assert _summary({"msg_type": "text", "body": {"content": "123"}}) == "[text]"


def test_pagination_via_page_token(tmp_path):
    pages = [{"items": [MSG], "has_more": True, "page_token": "tk"},
             {"items": [IMG_MSG], "has_more": False, "page_token": ""}]
    def fetch(path, params):
        if path == "/im/v1/chats/oc_example_alpha":
            return {"chat_mode": "group"}
        return pages[1] if params.get("page_token") == "tk" else pages[0]
    events = collect_feishu(Config.load(env={"TEAMMEM_CONFIG_DIR": str(tmp_path)}),
                            IdentityMaps.load(CONFIG_DIR), fetch, NOW)
    assert {e.hash for e in events} == {"om_1", "om_3"}


def test_feishu_warns_for_one_failed_allowlisted_chat_and_keeps_other_events():
    """Silently dropping one failed allowlisted group makes partial history look complete."""
    def fetch(path, params):
        if path.startswith("/im/v1/chats/"):
            return {"chat_mode": "group"}
        if params["container_id"] == "oc_failed":
            raise RuntimeError("history failed with credential value")
        return {"items": [MSG], "has_more": False, "page_token": ""}

    result = FeishuConnector(fetch=fetch).collect(
        Config.load(env={}),
        _ids_with_channels("oc_example_alpha", "oc_failed"),
        ConnectorSettings(name="feishu", enabled=True, options={}),
        NOW,
    )

    assert [event.summary for event in result.events] == [
        "deploy done, checking logs"
    ]
    assert result.warnings == ("feishu channel oc_failed history request failed",)
    assert "credential" not in result.warnings[0]


def test_feishu_raises_when_every_allowlisted_chat_request_fails():
    """Returning success after every configured group errors would hide a failed daily run."""
    def fetch(path, params):
        if path == "/im/v1/chats/oc_metadata_failed":
            raise RuntimeError("metadata unavailable")
        if path.startswith("/im/v1/chats/"):
            return {"chat_mode": "group"}
        raise RuntimeError("history unavailable")

    with pytest.raises(
        RuntimeError, match="feishu collection failed for every configured channel"
    ):
        FeishuConnector(fetch=fetch).collect(
            Config.load(env={}),
            _ids_with_channels("oc_metadata_failed", "oc_history_failed"),
            ConnectorSettings(name="feishu", enabled=True, options={}),
            NOW,
        )
