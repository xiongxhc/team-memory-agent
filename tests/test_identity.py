from pathlib import Path

import pytest

from teammem.identity import IdentityMaps

# Hermetic fixture dir: only .example files, so IdentityMaps.load's fallback is
# deterministic regardless of the operator's real config/roster.yaml.
CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"


def _maps():
    return IdentityMaps.load(CONFIG_DIR)          # falls back to .example files


def test_email_resolves_case_insensitive():
    assert _maps().person("email", "Sam.Li@example.com") == "sam"


def test_gitlab_username_resolves():
    assert _maps().person("gitlab", "alexdev") == "alex"


def test_unknown_identity_surfaces_not_drops():
    assert _maps().person("email", "ghost@example.com") == "_unmapped/ghost@example.com"


def test_empty_identity_is_unmapped_none():
    assert _maps().person("email", "") == "_unmapped/(none)"


def test_repo_maps_to_project():
    assert _maps().project_for_repo("team/alpha-tools") == "project-alpha"


def test_unknown_repo_is_none():
    assert _maps().project_for_repo("team/other") is None


def test_duplicate_email_across_members_raises():
    roster = {"members": {
        "a": {"emails": ["dup@example.com"]},
        "b": {"emails": ["dup@example.com"]},
    }}
    with pytest.raises(ValueError, match="dup@example.com"):
        IdentityMaps(roster, {})


def test_duplicate_repo_across_projects_raises():
    projects = {"projects": {
        "p1": {"gitlab_repos": ["team/x"]},
        "p2": {"gitlab_repos": ["team/x"]},
    }}
    with pytest.raises(ValueError, match="team/x"):
        IdentityMaps({}, projects)


def test_feishu_open_id_resolves():
    assert _maps().person("feishu", "OU_EXAMPLE_ALEX") == "alex"


def test_channel_maps_to_project():
    assert _maps().project_for_channel("oc_example_alpha") == "project-alpha"
    assert _maps().project_for_channel("oc_unknown") is None


def test_display_name():
    m = _maps()
    assert m.display_name("alex") == "Alex Rivera"
    assert m.display_name("_unmapped/x@y.z") == "_unmapped/x@y.z"


def test_slugs_sorted():
    assert _maps().slugs() == ["alex", "sam"]


def test_duplicate_feishu_id_across_members_raises():
    roster = {"members": {
        "a": {"feishu": ["ou_dup"]},
        "b": {"feishu": ["ou_dup"]},
    }}
    with pytest.raises(ValueError, match="ou_dup"):
        IdentityMaps(roster, {})
