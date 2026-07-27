from teammem.docs_sync import sync_docs


def _obsidian(tmp_path):
    src = tmp_path / "obsidian"
    (src / "Award").mkdir(parents=True)
    (src / "Award" / "architecture.md").write_text(
        "# Award — Architecture\nSee [[Award]] and [[Deploy Notes|notes]].\n")
    (src / "Award" / "Award.md").write_text("personal activity index")
    (src / "Smart App").mkdir()
    (src / "Smart App" / "summary.md").write_text("# Smart Example — Summary\n")
    return src


def test_sync_copies_matched_docs_and_flattens_wikilinks(tmp_path):
    vault = tmp_path / "vault"
    out = sync_docs({"projects": {"award": {}}}, _obsidian(tmp_path), vault)
    assert out == {"projects": 1, "copied": 1}
    text = (vault / "Docs" / "award" / "architecture.md").read_text()
    assert "See Award and notes." in text                         # wikilinks flattened
    assert not (vault / "Docs" / "award" / "Award.md").exists()   # main note stays personal


def test_sync_obsidian_override_and_idempotent(tmp_path):
    vault = tmp_path / "vault"
    projects = {"projects": {"smart-example": {"obsidian": "Smart App"},
                             "turkey-rnd": {}}}          # no Obsidian folder -> skipped
    src = _obsidian(tmp_path)
    assert sync_docs(projects, src, vault)["copied"] == 1
    assert (vault / "Docs" / "smart-example" / "summary.md").exists()
    assert sync_docs(projects, src, vault) == {"projects": 1, "copied": 0}
