import pytest

from teammem.links import load_link_origins, rewrite_links


OLD = "https://forge.old.example:8443"
NEW = "https://forge.example:9443"


def test_optional_mapping_defaults_to_no_changes(tmp_path):
    assert load_link_origins(tmp_path) == {}
    text = f"[source]({OLD}/group/repo/-/commit/abc)"
    assert rewrite_links(text, {}) == text


def test_origin_mapping_preserves_suffixes_and_is_idempotent(tmp_path):
    (tmp_path / "link-origins.yaml").write_text(
        f"origins:\n  {OLD}/: {NEW}/\n"
    )
    origins = load_link_origins(tmp_path)
    assert origins == {OLD: NEW}
    text = (
        f"[commit]({OLD}/group/repo/-/commit/abc?view=1#diff-2)\n"
        f"<{OLD}>\n{OLD}, then `{OLD}/path`\n"
        f'<a href="{OLD}/group/repo">repo</a>\n'
    )
    expected = text.replace(OLD, NEW)
    assert rewrite_links(text, origins) == expected
    assert rewrite_links(expected, origins) == expected


@pytest.mark.parametrize("text", [
    f"{OLD}.other.example/path",
    f"{OLD}@other.example/path",
    f"{OLD}:123/path",
    "https://user:secret@forge.old.example:8443/path",
    "https://forge.old.example/path",
    "http://forge.old.example:8443/path",
    "https://unrelated.example/path",
    f"https://unrelated.example/{OLD}/path",
    f"prefix{OLD}/path",
    "//forge.old.example:8443/path",
])
def test_only_exact_url_origins_are_rewritten(text):
    assert rewrite_links(text, {OLD: NEW}) == text


@pytest.mark.parametrize("suffix", [
    f"/repo?return={OLD}/other#part",
    f"/repo#source={OLD}/other",
    f"/proxy/{OLD}/other",
])
def test_nested_urls_in_suffixes_stay_unchanged(suffix):
    assert rewrite_links(OLD + suffix, {OLD: NEW}) == NEW + suffix
    unrelated = "https://unrelated.example" + suffix
    assert rewrite_links(unrelated, {OLD: NEW}) == unrelated


@pytest.mark.parametrize("text", [
    f"[one]({OLD}/a)[two]({OLD}/b)",
    f"|{OLD}/a|{OLD}/b|",
    f"[{OLD}/a][{OLD}/b]",
])
def test_adjacent_markdown_links_are_rewritten_independently(text):
    assert rewrite_links(text, {OLD: NEW}) == text.replace(OLD, NEW)


def test_ipv6_links_in_markdown_keep_nested_url_suffixes():
    old = "http://[2001:db8::1]:8080"
    suffix = f"/repo?return={old}/other#part"
    text = f"[one]({old}{suffix})[two]({old}/b)"
    assert rewrite_links(text, {old: NEW}) == (
        f"[one]({NEW}{suffix})[two]({NEW}/b)"
    )


def test_origin_case_is_normalized_but_path_case_is_preserved():
    assert rewrite_links(
        "HTTPS://FORGE.OLD.EXAMPLE:8443/Group/Repo?Q=Value#Section",
        {OLD: NEW},
    ) == f"{NEW}/Group/Repo?Q=Value#Section"


@pytest.mark.parametrize("origin", [
    "ftp://forge.example", "forge.example", "https://user@forge.example",
    "https://forge.example/path", "https://forge.example?query=1",
    "https://forge.example#fragment", "https://forge.example:bad",
    "https://forge.example:99999", "https://", "https://forge example",
    "https://forge.example\\other", "https://forge.example:",
    "https://forge.example<other>", "https://forge.example%2eother", 42, None,
])
@pytest.mark.parametrize("side", ["source", "target"])
def test_invalid_origin_mapping_is_rejected(origin, side):
    mapping = {origin: NEW} if side == "source" else {OLD: origin}
    with pytest.raises(ValueError, match="link origins"):
        rewrite_links("unchanged", mapping)


@pytest.mark.parametrize("config", ["[]", "origins: []", "other: {}", "origins: null"])
def test_invalid_config_shape_is_rejected(tmp_path, config):
    (tmp_path / "link-origins.yaml").write_text(config)
    with pytest.raises(ValueError, match="link origins"):
        load_link_origins(tmp_path)


@pytest.mark.parametrize("mapping", [
    {OLD: NEW, NEW: "https://next.example"},
    {OLD: NEW, NEW: OLD},
])
def test_chains_and_cycles_are_rejected_for_repeatable_presentation(mapping):
    with pytest.raises(ValueError, match="link origins"):
        rewrite_links(OLD, mapping)


def test_ipv6_origin_and_identity_mapping_are_supported():
    text = "[source](http://[2001:db8::1]:8080/path#section)"
    assert rewrite_links(text, {"http://[2001:db8::1]:8080": NEW}) == (
        f"[source]({NEW}/path#section)"
    )
    assert rewrite_links(OLD, {OLD: OLD}) == OLD
