import pytest

from teammem.connectors.config import load_connector_settings


def test_all_example_connectors_are_disabled(tmp_path):
    settings = load_connector_settings(tmp_path)
    assert set(settings) == {"github", "gitlab", "slack", "feishu", "discord"}
    assert not any(item.enabled for item in settings.values())


def test_connector_settings_read_options_from_operator_configuration(tmp_path):
    (tmp_path / "connectors.yaml").write_text(
        "connectors:\n"
        "  github:\n"
        "    enabled: true\n"
        "    api_url: https://github.example\n"
    )
    settings = load_connector_settings(tmp_path)
    assert settings["github"].enabled is True
    assert settings["github"].options == {"api_url": "https://github.example"}
    assert settings["gitlab"].enabled is False


def test_github_count_weeks_accepts_an_integer_in_the_supported_range(tmp_path):
    (tmp_path / "connectors.yaml").write_text(
        "connectors:\n"
        "  github:\n"
        "    enabled: true\n"
        "    count_weeks: 2\n"
    )

    settings = load_connector_settings(tmp_path)

    assert settings["github"].options["count_weeks"] == 2


@pytest.mark.parametrize(
    "configured",
    ["true", "2.5", '"2"', "0", "53", "null"],
)
def test_github_count_weeks_rejects_non_integer_or_out_of_range_config(
    tmp_path, configured
):
    (tmp_path / "connectors.yaml").write_text(
        "connectors:\n"
        "  github:\n"
        "    enabled: true\n"
        f"    count_weeks: {configured}\n"
    )

    with pytest.raises(
        ValueError,
        match="connector option count_weeks for github must be an integer from 1 to 52",
    ):
        load_connector_settings(tmp_path)


def test_unknown_connector_key_is_rejected(tmp_path):
    (tmp_path / "connectors.yaml").write_text(
        "connectors:\n  unknown:\n    enabled: true\n"
    )
    with pytest.raises(ValueError, match="unknown connector: unknown"):
        load_connector_settings(tmp_path)


def test_gitlab_collect_mr_commits_defaults_true_at_collection_boundary(tmp_path):
    (tmp_path / "connectors.yaml").write_text(
        "connectors:\n"
        "  gitlab:\n"
        "    enabled: true\n"
    )

    settings = load_connector_settings(tmp_path)

    assert settings["gitlab"].options["collect_mr_commits"] is True


@pytest.mark.parametrize("configured", ["true", "false"])
def test_gitlab_collect_mr_commits_accepts_yaml_boolean(tmp_path, configured):
    (tmp_path / "connectors.yaml").write_text(
        "connectors:\n"
        "  gitlab:\n"
        "    enabled: true\n"
        f"    collect_mr_commits: {configured}\n"
    )

    settings = load_connector_settings(tmp_path)

    assert settings["gitlab"].options["collect_mr_commits"] is (configured == "true")


@pytest.mark.parametrize("configured", ['\"false\"', "null", "0", "1.0"])
def test_gitlab_collect_mr_commits_rejects_non_boolean_yaml(tmp_path, configured):
    (tmp_path / "connectors.yaml").write_text(
        "connectors:\n"
        "  gitlab:\n"
        "    enabled: true\n"
        f"    collect_mr_commits: {configured}\n"
    )

    with pytest.raises(
        ValueError,
        match="connector option collect_mr_commits for gitlab must be a boolean",
    ):
        load_connector_settings(tmp_path)


def test_gitlab_exclude_note_authors_defaults_empty(tmp_path):
    (tmp_path / "connectors.yaml").write_text(
        "connectors:\n"
        "  gitlab:\n"
        "    enabled: true\n"
    )
    settings = load_connector_settings(tmp_path)
    assert settings["gitlab"].options["exclude_note_authors"] == []


def test_gitlab_reclaim_origins_defaults_empty(tmp_path):
    (tmp_path / "connectors.yaml").write_text(
        "connectors:\n"
        "  gitlab:\n"
        "    enabled: true\n"
    )

    settings = load_connector_settings(tmp_path)

    assert settings["gitlab"].options["reclaim_origins"] == []


def test_gitlab_reclaim_origins_accepts_explicit_http_origins(tmp_path):
    (tmp_path / "connectors.yaml").write_text(
        "connectors:\n"
        "  gitlab:\n"
        "    enabled: true\n"
        "    reclaim_origins:\n"
        "      - https://gitlab-history.example\n"
        "      - http://gitlab-archive.example:8080/\n"
    )

    settings = load_connector_settings(tmp_path)

    assert settings["gitlab"].options["reclaim_origins"] == [
        "https://gitlab-history.example",
        "http://gitlab-archive.example:8080/",
    ]


@pytest.mark.parametrize(
    "configured",
    [
        "https://gitlab-history.example",
        "{origin: https://gitlab-history.example}",
        "[https://gitlab-history.example, 42]",
    ],
)
def test_gitlab_reclaim_origins_rejects_non_string_lists(tmp_path, configured):
    (tmp_path / "connectors.yaml").write_text(
        "connectors:\n"
        "  gitlab:\n"
        "    enabled: true\n"
        f"    reclaim_origins: {configured}\n"
    )

    with pytest.raises(
        ValueError,
        match="connector option reclaim_origins for gitlab must be a list of strings",
    ):
        load_connector_settings(tmp_path)


@pytest.mark.parametrize(
    "configured",
    [
        "gitlab-history.example",
        "ftp://gitlab-history.example",
        "https://gitlab-history.example:invalid",
        "https://user@gitlab-history.example",
        "https://gitlab-history.example/group",
        "https://gitlab-history.example?group=1",
        "https://gitlab-history.example#group",
        '"\\u0000https://gitlab-history.example"',
        '"https://gitlab-history.example:"',
        '"https://gitlab-history.example?"',
        '"https://gitlab-history.example#"',
    ],
)
def test_gitlab_reclaim_origins_rejects_values_that_are_not_origins(
    tmp_path, configured
):
    (tmp_path / "connectors.yaml").write_text(
        "connectors:\n"
        "  gitlab:\n"
        "    enabled: true\n"
        "    reclaim_origins:\n"
        f"      - {configured}\n"
    )

    with pytest.raises(
        ValueError,
        match="connector option reclaim_origins for gitlab contains an invalid origin",
    ):
        load_connector_settings(tmp_path)


@pytest.mark.parametrize("configured", ["fgbot", "true", "{a: 1}", "[1, 2]"])
def test_gitlab_exclude_note_authors_rejects_non_string_list(tmp_path, configured):
    (tmp_path / "connectors.yaml").write_text(
        "connectors:\n"
        "  gitlab:\n"
        "    enabled: true\n"
        f"    exclude_note_authors: {configured}\n"
    )
    with pytest.raises(
        ValueError,
        match="connector option exclude_note_authors for gitlab must be a list of strings",
    ):
        load_connector_settings(tmp_path)
