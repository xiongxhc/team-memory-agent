import pytest

from teammem.chat.access import (
    AccessDenied,
    authorize,
    project_policy_from_source_config,
)
from teammem.chat.state import SessionKey


def test_group_cannot_inherit_requesters_private_project():
    """Removing the scope intersection would expose a member's DM-only project."""
    config = {"access": {"default": "deny", "users": {
        "alice": ["a", "b"]}, "groups": {"g": ["a"]},
        "group_admins": {"g": ["alice"]}}}

    key = SessionKey("t", "app", "group", "g")

    assert authorize(config, key, "alice") == frozenset({"a"})


def test_dm_requires_its_app_scoped_sender_identity():
    """Accepting any configured user for a DM would cross private conversations."""
    config = {"access": {"default": "deny", "users": {
        "alice-new-app": ["alpha"]}, "groups": {}, "group_admins": {}}}

    with pytest.raises(AccessDenied):
        authorize(
            config,
            SessionKey("tenant", "new-app", "dm", "alice-new-app"),
            "alice-collector-app",
        )


@pytest.mark.parametrize(
    "config",
    [
        {"access": {"default": "deny", "users": {}, "groups": {}, "group_admins": {}}},
        {"access": {"default": "deny", "users": {
            "bob": ["a"],
        }, "groups": {}, "group_admins": {}}},
    ],
)
def test_unlisted_user_cannot_start_a_chat(config):
    """Treating a missing user as an empty scope starts forbidden work."""
    with pytest.raises(AccessDenied):
        authorize(config, SessionKey("t", "app", "dm", "alice"), "alice")


def test_enabled_user_with_empty_grants_can_chat_without_evidence():
    """Rejecting an enabled empty grant prevents permitted casual chat."""
    config = {"access": {"default": "deny", "users": {
        "alice": [],
    }, "groups": {}, "group_admins": {}}}

    assert authorize(config, SessionKey("t", "app", "dm", "alice"), "alice") == frozenset()


def test_direct_message_toggle_denies_dm_without_revoking_the_identity():
    config = {"feishu": {"direct_messages": False}, "access": {"default": "deny", "users": {
        "alice": [],
    }, "groups": {}, "group_admins": {}}}

    with pytest.raises(AccessDenied):
        authorize(config, SessionKey("t", "app", "dm", "alice"), "alice")


def test_duplicate_project_grants_do_not_expand_the_evidence_scope():
    """Passing duplicate grants through to SQL could break bounded project filtering."""
    config = {"access": {"default": "deny", "users": {
        "alice": ["alpha", "alpha"],
    }, "groups": {}, "group_admins": {}}}

    assert authorize(config, SessionKey("t", "app", "dm", "alice"), "alice") == frozenset({"alpha"})


def test_revoked_user_loses_scope_on_the_next_authorization_check():
    """Caching an old grant would let queued work survive revocation."""
    config = {"access": {"default": "deny", "users": {
        "alice": ["alpha"],
    }, "groups": {}, "group_admins": {}}}
    key = SessionKey("t", "app", "dm", "alice")
    assert authorize(config, key, "alice") == frozenset({"alpha"})

    del config["access"]["users"]["alice"]

    with pytest.raises(AccessDenied):
        authorize(config, key, "alice")


def test_policy_comes_from_validated_source_config_not_ledger_text():
    """Mapping an unknown project as detailed would disclose unclassified evidence."""
    source_config = {
        "projects": {
            "detail": {"projection": "full"},
            "counts": {"projection": "count-only"},
        },
        "areas": {"operations": {}},
        "hidden_projects": ["secret"],
    }

    assert project_policy_from_source_config(source_config) == {
        "detail": "detail",
        "counts": "count_only",
        "operations": "detail",
        "secret": "hidden",
    }
