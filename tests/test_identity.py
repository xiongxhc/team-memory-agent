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


def test_same_text_can_identify_resources_from_different_providers():
    ids = IdentityMaps(
        {"members": {"alex": {"github": ["alex-gh"], "slack": ["U1"]}}},
        {"projects": {
            "one": {"github_repos": ["same"]},
            "two": {"slack_channels": ["same"]},
        }},
    )
    assert ids.project("github-repo", "same") == "one"
    assert ids.project("slack-channel", "same") == "two"


def test_resources_return_only_the_requested_provider_kind():
    ids = IdentityMaps(
        {"members": {}},
        {"projects": {
            "one": {"github_repos": ["team/one"]},
            "two": {"slack_channels": ["C0123"]},
        }},
    )
    assert ids.resources("github-repo") == {"team/one": "one"}
    assert ids.resources("slack-channel") == {"C0123": "two"}


def test_existing_gitlab_and_feishu_helpers_remain_compatible():
    ids = IdentityMaps.load(CONFIG_DIR)
    assert ids.project_for_repo("team/project-alpha") == "project-alpha"
    assert ids.project_for_channel("oc_example_alpha") == "project-alpha"


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


def test_projection_classifies_projects_areas_hidden_and_unknown():
    projects = {
        "projects": {
            "full": {"gitlab_repos": ["group/full"]},
            "counts": {
                "projection": "count-only",
                "github_repos": ["owner/counts"],
            },
        },
        "areas": {
            "coordination": {"feishu_channels": ["oc_coordination"]},
        },
        "hidden_projects": ["IdeaProjects"],
    }
    ids = IdentityMaps({"members": {}}, projects)

    assert ids.projection("full") == "full"
    assert ids.projection("counts") == "count-only"
    assert ids.projection("coordination") == "area"
    assert ids.projection("IdeaProjects") == "hidden"
    assert ids.projection("unknown") == "unclassified"
    assert ids.project_for_channel("oc_coordination") == "coordination"
    assert ids.resources("feishu-channel") == {"oc_coordination": "coordination"}


def test_explicit_registry_slugs_are_sorted_without_hidden_or_unclassified():
    ids = IdentityMaps(
        {"members": {}},
        {
            "projects": {
                "z-counts": {"projection": "count-only"},
                "alpha": {},
            },
            "areas": {"z-operations": {}, "coordination": {}},
            "hidden_projects": ["IdeaProjects"],
        },
    )

    assert ids.project_slugs() == ["alpha", "z-counts"]
    assert ids.area_slugs() == ["coordination", "z-operations"]
    assert "IdeaProjects" not in ids.project_slugs()
    assert "legacy-unclassified" not in ids.project_slugs()
    assert "IdeaProjects" not in ids.area_slugs()
    assert "legacy-unclassified" not in ids.area_slugs()


@pytest.mark.parametrize("projection", ["area", "hidden", "invalid", ""])
def test_invalid_project_projection_is_rejected(projection):
    with pytest.raises(ValueError, match="projection"):
        IdentityMaps(
            {"members": {}},
            {"projects": {"project": {"projection": projection}}},
        )


@pytest.mark.parametrize(
    "projects",
    [
        {"projects": {"shared": {}}, "areas": {"shared": {}}},
        {"projects": {"shared": {}}, "hidden_projects": ["shared"]},
        {"areas": {"shared": {}}, "hidden_projects": ["shared"]},
    ],
)
def test_slug_cannot_be_declared_in_multiple_categories(projects):
    with pytest.raises(ValueError, match="slug collision"):
        IdentityMaps({"members": {}}, projects)


@pytest.mark.parametrize(
    "hidden_projects",
    ["IdeaProjects", None, {"IdeaProjects": True}, [1], [""], ["   "]],
)
def test_hidden_projects_must_be_a_list_of_non_empty_strings(hidden_projects):
    with pytest.raises(
        ValueError,
        match="hidden_projects must be a list of non-empty strings",
    ):
        IdentityMaps(
            {"members": {}},
            {"hidden_projects": hidden_projects},
        )


def test_resource_cannot_be_claimed_by_project_and_area():
    projects = {
        "projects": {"project": {"feishu_channels": ["oc_shared"]}},
        "areas": {"area": {"feishu_channels": ["oc_shared"]}},
    }
    with pytest.raises(ValueError, match="resource collision"):
        IdentityMaps({"members": {}}, projects)
