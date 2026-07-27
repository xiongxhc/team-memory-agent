import json
from datetime import datetime, timezone
from pathlib import Path

from teammem.config import Config
from teammem.feishu_collector import _summary, collect_feishu
from teammem.identity import IdentityMaps

NOW = datetime(2026, 7, 16, tzinfo=timezone.utc)
CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"

CHATS = {"items": [{"chat_id": "oc_example_alpha", "name": "Project Alpha"}],
         "has_more": False, "page_token": ""}
MSG = {"message_id": "om_1", "chat_id": "oc_example_alpha", "msg_type": "text",
       "create_time": "1784264400000",
       "sender": {"id": "ou_example_alex", "id_type": "open_id",
                  "sender_type": "user"},
       "body": {"content": json.dumps({"text": "deploy done, checking logs"})}}
BOT_MSG = {**MSG, "message_id": "om_2",
           "sender": {"id": "cli_x", "id_type": "app_id", "sender_type": "app"}}
IMG_MSG = {**MSG, "message_id": "om_3", "msg_type": "image",
           "body": {"content": json.dumps({"image_key": "k"})}}


def fake_fetch(path, params):
    if path == "/im/v1/chats":
        return CHATS
    if path == "/im/v1/messages":
        assert params["container_id"] == "oc_example_alpha"
        assert params["start_time"].isdigit() and params["end_time"].isdigit()
        return {"items": [MSG, BOT_MSG, IMG_MSG], "has_more": False, "page_token": ""}
    raise AssertionError(path)


def test_messages_become_events(tmp_path):
    cfg = Config.load(env={"TEAMMEM_CONFIG_DIR": str(tmp_path)})
    events = collect_feishu(cfg, IdentityMaps.load(CONFIG_DIR), fake_fetch, NOW)
    names = json.loads((tmp_path / "channel_names.json").read_text())
    assert names == {"oc_example_alpha": "Project Alpha"}          # name cache persisted
    assert len(events) == 2                                   # bot message skipped
    text = next(e for e in events if e.hash == "om_1")
    assert (text.person, text.project, text.kind) == ("alex", "project-alpha", "message")
    assert text.source == "feishu-channel"
    assert text.summary == "deploy done, checking logs"
    assert text.ts.startswith("2026-07-17T")                  # 1784264400000 ms UTC
    img = next(e for e in events if e.hash == "om_3")
    assert img.summary == "[image]"


def test_unknown_sender_is_unmapped(tmp_path):
    ghost = {**MSG, "message_id": "om_9",
             "sender": {"id": "ou_ghost", "id_type": "open_id",
                        "sender_type": "user"}}
    def fetch(path, params):
        if path == "/im/v1/chats":
            return CHATS
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
        if path == "/im/v1/chats":
            return CHATS
        return pages[1] if params.get("page_token") == "tk" else pages[0]
    events = collect_feishu(Config.load(env={"TEAMMEM_CONFIG_DIR": str(tmp_path)}),
                            IdentityMaps.load(CONFIG_DIR), fetch, NOW)
    assert {e.hash for e in events} == {"om_1", "om_3"}
