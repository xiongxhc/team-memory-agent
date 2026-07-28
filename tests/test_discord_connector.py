from datetime import datetime, timezone
from pathlib import Path

import pytest

from teammem.config import Config
from teammem.connectors.config import ConnectorSettings
from teammem.connectors.discord import DiscordConnector
from teammem.identity import IdentityMaps


NOW = datetime(2026, 7, 15, tzinfo=timezone.utc)
CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"


def _cfg():
    return Config(discord_bot_token="discord-test-token", since_days=7)


def _settings():
    return ConnectorSettings(name="discord", enabled=True, options={})


def _ids_with_channels(*channel_ids):
    return IdentityMaps(
        {"members": {"alex": {"discord": ["1234567890"]}}},
        {"projects": {
            f"project-{index}": {"discord_channels": [channel_id]}
            for index, channel_id in enumerate(channel_ids, start=1)
        }},
    )


def _channel(**extra):
    return {"id": "9876543210", "type": 0, "guild_id": "123456789", "name": "project-alpha", **extra}


def test_discord_queries_only_mapped_channels_and_skips_bots_and_webhooks():
    """Fetching unmapped channels or keeping bot/webhook messages would defeat project and author controls."""
    calls = []

    def discord_fixture(path, params):
        calls.append((path, params))
        if path == "/channels/9876543210":
            return _channel()
        if path == "/channels/9876543210/messages":
            return [
                {"id": "1", "timestamp": "2026-07-14T09:00:00+00:00", "content": "human message", "type": 0, "author": {"id": "1234567890", "bot": False}},
                {"id": "2", "timestamp": "2026-07-14T09:01:00+00:00", "content": "bot", "type": 0, "author": {"id": "1234567890", "bot": True}},
                {"id": "3", "timestamp": "2026-07-14T09:02:00+00:00", "content": "webhook", "type": 0, "webhook_id": "99", "author": {"id": "1234567890", "bot": False}},
                {"id": "4", "timestamp": "2026-07-14T09:03:00+00:00", "content": "", "type": 7, "author": {"id": "1234567890", "bot": False}},
            ]
        raise AssertionError(path)

    result = DiscordConnector(fetch=discord_fixture).collect(
        _cfg(), IdentityMaps.load(CONFIG_DIR), _settings(), NOW
    )

    assert {path for path, _ in calls} == {"/channels/9876543210", "/channels/9876543210/messages"}
    assert [event.person for event in result.events] == ["alex"]
    assert [(event.source, event.kind, event.summary, event.hash) for event in result.events] == [
        ("discord-channel", "message", "human message", "1"),
    ]
    assert result.events[0].refs == '{"channel_id": "9876543210"}'


def test_discord_pages_backward_until_the_lookback_boundary():
    """Dropping the before cursor or including messages older than the configured lookback breaks collection."""
    calls = []

    def discord_fixture(path, params):
        calls.append((path, params))
        if path == "/channels/9876543210":
            return _channel()
        if path == "/channels/9876543210/messages" and not params.get("before"):
            return [
                {"id": "20", "timestamp": "2026-07-14T09:00:00+00:00", "content": "recent", "type": 0, "author": {"id": "1234567890", "bot": False}}, *[
                    {"id": str(number), "timestamp": "2026-07-14T09:00:00+00:00", "content": "bot", "type": 0, "author": {"id": "1234567890", "bot": True}}
                    for number in range(99, 0, -1)
                ]]
        if path == "/channels/9876543210/messages":
            return [
                {"id": "10", "timestamp": "2026-07-07T23:59:59+00:00", "content": "stale", "type": 0, "author": {"id": "1234567890", "bot": False}},
            ]
        raise AssertionError(path)

    result = DiscordConnector(fetch=discord_fixture).collect(
        _cfg(), IdentityMaps.load(CONFIG_DIR), _settings(), NOW
    )

    assert [params for path, params in calls if path.endswith("/messages")] == [
        {"limit": 100}, {"limit": 100, "before": "1"},
    ]
    assert [event.hash for event in result.events] == ["20"]


def test_discord_never_requests_messages_for_direct_messages_or_missing_guild():
    """Treating a direct-message channel as a guild channel would bypass the shared-channel boundary."""
    for metadata in (_channel(guild_id=None, type=1), _channel(guild_id="")):
        calls = []

        def discord_fixture(path, params, response=metadata):
            calls.append((path, params))
            if path == "/channels/9876543210":
                return response
            raise AssertionError(path)

        result = DiscordConnector(fetch=discord_fixture).collect(
            _cfg(), IdentityMaps.load(CONFIG_DIR), _settings(), NOW
        )

        assert calls == [("/channels/9876543210", {})]
        assert result.events == ()


def test_discord_never_requests_messages_when_metadata_fails():
    """Failing open after channel metadata errors would risk collecting an unverified direct message."""
    calls = []

    def discord_fixture(path, params):
        calls.append((path, params))
        if path == "/channels/9876543210":
            raise RuntimeError("metadata unavailable")
        raise AssertionError(path)

    with pytest.raises(
        RuntimeError,
        match="discord collection failed for every configured channel",
    ):
        DiscordConnector(fetch=discord_fixture).collect(
            _cfg(), IdentityMaps.load(CONFIG_DIR), _settings(), NOW
        )

    assert calls == [("/channels/9876543210", {})]


def test_discord_validation_requires_the_bot_token():
    """Running Discord collection without the bot credential would not satisfy its permission boundary."""
    assert DiscordConnector().validate(Config(), _settings()) == ["TEAMMEM_DISCORD_BOT_TOKEN"]


def test_discord_warns_when_empty_history_cannot_verify_read_or_content_permissions():
    """Treating an empty response as proof of an empty channel hides missing history or content access."""
    def discord_fixture(path, params):
        if path == "/channels/9876543210":
            return _channel()
        if path == "/channels/9876543210/messages":
            return []
        raise AssertionError(path)

    result = DiscordConnector(fetch=discord_fixture).collect(
        _cfg(), IdentityMaps.load(CONFIG_DIR), _settings(), NOW
    )

    assert result.events == ()
    assert result.warnings == (
        "discord channel 9876543210 returned no messages; verify READ_MESSAGE_HISTORY and MESSAGE_CONTENT access",
    )


def test_discord_warns_when_human_messages_have_unavailable_content():
    """Treating blank human content as an ordinary skipped message hides MESSAGE_CONTENT loss."""
    def discord_fixture(path, params):
        if path == "/channels/9876543210":
            return _channel()
        if path == "/channels/9876543210/messages":
            return [{
                "id": "1",
                "timestamp": "2026-07-14T09:00:00+00:00",
                "content": "",
                "type": 0,
                "author": {"id": "1234567890", "bot": False},
            }]
        raise AssertionError(path)

    result = DiscordConnector(fetch=discord_fixture).collect(
        _cfg(), IdentityMaps.load(CONFIG_DIR), _settings(), NOW
    )

    assert result.events == ()
    assert result.warnings == (
        "discord channel 9876543210 returned human messages with unavailable "
        "content; verify MESSAGE_CONTENT access",
    )


def test_discord_warns_for_one_failed_allowlisted_channel_and_keeps_other_events():
    """Silently dropping one failed allowlisted guild channel makes collection look complete."""
    def discord_fixture(path, params):
        channel_id = path.split("/")[2]
        if path.count("/") == 2:
            return _channel(id=channel_id)
        if channel_id == "222":
            raise RuntimeError("history failed with credential value")
        return [{
            "id": "1",
            "timestamp": "2026-07-14T09:00:00+00:00",
            "content": "collected",
            "type": 0,
            "author": {"id": "1234567890", "bot": False},
        }]

    result = DiscordConnector(fetch=discord_fixture).collect(
        _cfg(), _ids_with_channels("111", "222"), _settings(), NOW
    )

    assert [event.summary for event in result.events] == ["collected"]
    assert result.warnings == ("discord channel 222 history request failed",)
    assert "credential" not in result.warnings[0]


def test_discord_raises_when_every_allowlisted_channel_request_fails():
    """Returning success after every configured guild channel errors would hide a failed run."""
    def discord_fixture(path, params):
        channel_id = path.split("/")[2]
        if path.count("/") == 2 and channel_id == "111":
            raise RuntimeError("metadata unavailable")
        if path.count("/") == 2:
            return _channel(id=channel_id)
        raise RuntimeError("history unavailable")

    with pytest.raises(
        RuntimeError, match="discord collection failed for every configured channel"
    ):
        DiscordConnector(fetch=discord_fixture).collect(
            _cfg(), _ids_with_channels("111", "222"), _settings(), NOW
        )
