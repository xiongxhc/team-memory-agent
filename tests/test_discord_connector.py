from datetime import datetime, timezone
from pathlib import Path

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

    result = DiscordConnector(fetch=discord_fixture).collect(
        _cfg(), IdentityMaps.load(CONFIG_DIR), _settings(), NOW
    )

    assert calls == [("/channels/9876543210", {})]
    assert result.events == ()


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
