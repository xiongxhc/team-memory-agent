from datetime import datetime, timezone
from pathlib import Path

from teammem.config import Config
from teammem.connectors.config import ConnectorSettings
from teammem.connectors.slack import SlackConnector
from teammem.identity import IdentityMaps


NOW = datetime(2026, 7, 15, tzinfo=timezone.utc)
CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"


def _cfg():
    return Config(slack_bot_token="xoxb-test-token", since_days=7)


def _settings():
    return ConnectorSettings(name="slack", enabled=True, options={})


def _metadata(**channel):
    return {"ok": True, "channel": {
        "id": "C0123", "name": "project-alpha", "is_channel": True,
        "is_group": False, "is_im": False, "is_mpim": False, "is_member": True,
        **channel,
    }}


def test_slack_queries_only_allowlisted_channels_and_skips_threads_and_bots():
    """Calling history before shared membership, including replies, or storing bot/thread data breaks this."""
    calls = []

    def slack_fixture(path, params):
        calls.append((path, params))
        if path == "conversations.info":
            return _metadata()
        if path == "conversations.history":
            return {"ok": True, "has_more": False, "response_metadata": {}, "messages": [
                {"type": "message", "ts": "1784077200.000001", "user": "U0123", "text": "human top-level"},
                {"type": "message", "ts": "1784077201.000001", "user": "U0123", "text": "reply", "thread_ts": "1784077200.000001"},
                {"type": "message", "ts": "1784077202.000001", "user": "U0123", "text": "bot id", "bot_id": "B1"},
                {"type": "message", "ts": "1784077203.000001", "user": "U0123", "text": "bot subtype", "subtype": "bot_message"},
                {"type": "message", "ts": "1784077204.000001", "text": "no sender"},
            ]}
        raise AssertionError(path)

    result = SlackConnector(fetch=slack_fixture).collect(
        _cfg(), IdentityMaps.load(CONFIG_DIR), _settings(), NOW
    )

    assert {path for path, _ in calls} == {"conversations.info", "conversations.history"}
    assert [event.summary for event in result.events] == ["human top-level"]
    assert result.events[0].person == "alex"
    assert result.events[0].source == "slack-channel"
    assert result.events[0].kind == "message"
    assert result.events[0].hash == "1784077200.000001"
    assert result.events[0].refs == '{"channel_id": "c0123"}'
    assert not any("conversations.replies" in path for path, _ in calls)


def test_slack_uses_oldest_cursor_pagination_and_non_marketplace_page_size():
    """Dropping lookback/cursor pagination or increasing the history request above 15 breaks this."""
    calls = []

    def slack_fixture(path, params):
        calls.append((path, params))
        if path == "conversations.info":
            return _metadata()
        if path == "conversations.history":
            if params.get("cursor") == "next-page":
                return {"ok": True, "has_more": False, "response_metadata": {}, "messages": [
                    {"type": "message", "ts": "1784077201.000001", "user": "U0456", "text": "second page"},
                ]}
            return {"ok": True, "has_more": True, "response_metadata": {"next_cursor": "next-page"}, "messages": [
                {"type": "message", "ts": "1784077200.000001", "user": "U0123", "text": "first page"},
            ]}
        raise AssertionError(path)

    result = SlackConnector(fetch=slack_fixture).collect(
        _cfg(), IdentityMaps.load(CONFIG_DIR), _settings(), NOW
    )

    history_calls = [params for path, params in calls if path == "conversations.history"]
    assert history_calls == [
        {"channel": "c0123", "oldest": "1783468800", "limit": 15},
        {"channel": "c0123", "oldest": "1783468800", "limit": 15, "cursor": "next-page"},
    ]
    assert [event.person for event in result.events] == ["alex", "sam"]


def test_slack_never_requests_history_for_direct_messages_or_non_member_channels():
    """Treating a DM or non-member channel as a shared project channel breaks this privacy boundary."""
    for channel in (_metadata(is_channel=False, is_im=True), _metadata(is_member=False)):
        calls = []

        def slack_fixture(path, params, response=channel):
            calls.append((path, params))
            if path == "conversations.info":
                return response
            raise AssertionError(path)

        result = SlackConnector(fetch=slack_fixture).collect(
            _cfg(), IdentityMaps.load(CONFIG_DIR), _settings(), NOW
        )

        assert calls == [("conversations.info", {"channel": "c0123"})]
        assert result.events == ()


def test_slack_never_requests_history_when_metadata_fails():
    """Failing open after channel-metadata errors would expose unverified conversations."""
    calls = []

    def slack_fixture(path, params):
        calls.append((path, params))
        if path == "conversations.info":
            raise RuntimeError("metadata unavailable")
        raise AssertionError(path)

    result = SlackConnector(fetch=slack_fixture).collect(
        _cfg(), IdentityMaps.load(CONFIG_DIR), _settings(), NOW
    )

    assert calls == [("conversations.info", {"channel": "c0123"})]
    assert result.events == ()


def test_slack_validation_requires_only_the_bot_token():
    """Accepting a user token configuration would broaden collection beyond the bot's memberships."""
    assert SlackConnector().validate(Config(), _settings()) == ["TEAMMEM_SLACK_BOT_TOKEN"]
