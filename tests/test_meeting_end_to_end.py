"""The whole meeting chain in one run: bundle, ledger, note, remote, receipt, ashes.

Every other meeting module is tested against its own seam. This file is the
plan's proof that the seams meet: one bundle is dropped into an inbox and
followed until the encrypted audio is destroyed, asserting at every stage rather
than only at the end.

Nothing here touches the deployed hub. The ledger, the audio store, the vault,
the receipts and the alerts all live under `tmp_path`, and the only remote is a
bare repository beside the vault, pushed to for real: no network, no forge, no
credentials.
"""

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from teammem.identity import IdentityMaps
from teammem.meeting_store import get_meeting, list_meetings
from teammem.render import verify_meeting_semantics, verify_vault
from teammem.services import (meeting_receipt_path, run_expire_meeting_audio,
                              run_process_meetings)
from teammem.store import open_db
from tests.test_meeting_bundles import write_meeting_bundle
from tests.test_meeting_publish import _git, _remote_ref
from teammem.meeting_render import NOTE_HEADINGS
from tests.test_meeting_render import NOTE, WEEK, _payload
from tests.test_services import MEETING_WEB_BASE, bench          # noqa: F401  (fixture)

RELATIVE_NOTE = f"Meetings/{WEEK}/{NOTE}"
NOW = "2026-09-02T16:00:00Z"
# The run stamps the note from `now`, so every date assertion derives from it.
TODAY = datetime.fromisoformat(NOW.replace("Z", "+00:00")).date()
WEEK_MONDAY = WEEK.split()[1].rsplit("-", 1)[0]


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _vault_text(vault) -> str:
    """Every byte the vault holds outside `.git`, as one searchable string."""
    return "\n".join(path.read_text(encoding="utf-8", errors="ignore")
                     for path in sorted(vault.rglob("*"))
                     if path.is_file() and ".git" not in path.relative_to(vault).parts)


def test_a_meeting_goes_from_bundle_to_receipt_to_destroyed_audio(bench):  # noqa: F811
    payload = _payload()
    occurrence_id = payload["occurrence_id"]
    ids = IdentityMaps.load(bench.cfg.config_dir)

    # ---- stage 1: a ready bundle, and a hub that has never seen this meeting
    bundle = bench.drop(payload)
    assert (bundle / "meeting.json").is_file()
    assert list_meetings(open_db(bench.cfg.db_path)) == []   # ordinary events only
    assert not (bench.vault / RELATIVE_NOTE).exists()
    assert not meeting_receipt_path(bench.cfg, occurrence_id).exists()
    seed_commit = _remote_ref(bench.bare, bench.branch)
    assert seed_commit is not None

    # ---- stage 2: one run imports, renders, verifies, pushes and receipts
    assert run_process_meetings(bench.cfg, bench.ids, now=NOW) == 0

    # the ledger holds the revision the bundle carried
    conn = open_db(bench.cfg.db_path)
    assert [row["occurrence_id"] for row in list_meetings(conn)] == [occurrence_id]
    meeting = get_meeting(conn, occurrence_id)
    assert meeting["revision"] == payload["revision"]
    assert meeting["title"] == payload["title"]
    assert len(meeting["transcript"]) == len(payload["transcript"])

    # the encrypted bytes are in the audio store, at the path the ledger names,
    # and the inbox has been drained
    audio = meeting["audio"]
    stored = Path(audio["stored_path"])
    assert stored.is_file()
    assert stored.is_relative_to(bench.cfg.meeting_audio_store)
    assert stored.stat().st_size == audio["bytes"]
    assert audio["deleted_at"] is None
    assert not list(bench.cfg.meeting_inbox.rglob("meeting.json"))
    assert list(bench.cfg.meeting_archive.rglob("meeting.json"))

    # the canonical note is on disk under the meeting's week, with the front
    # matter that names the revision and all nine sections in canonical order
    note = bench.vault / RELATIVE_NOTE
    assert note.is_file()
    page = note.read_text(encoding="utf-8")
    assert page.startswith(
        f'---\noccurrence_id: "{occurrence_id}"\n'
        f"revision: {payload['revision']}\n"
        f"week: {WEEK_MONDAY}\ngenerated: {TODAY.isoformat()}\n---\n"
        f"# {payload['title']}\n")
    assert [line[3:] for line in page.splitlines()
            if line.startswith("## ")] == list(NOTE_HEADINGS)

    # the vault satisfies both verifications: re-rendering into a fresh tree
    # reproduces every managed byte, and the meeting invariants all hold
    assert verify_vault(conn, ids, bench.vault, TODAY, semantic=True) == {
        "missing": [], "unexpected": [], "differing": [], "semantic": []}

    # and holds no transcript line, no audio digest and no store path anywhere
    text = _vault_text(bench.vault)
    for segment in payload["transcript"]:
        assert segment["text"] not in text
    assert audio["sha256"] not in text
    assert audio["stored_path"] not in text
    assert str(bench.cfg.meeting_audio_store) not in text

    # the remote really has the commit and the note inside it
    published = _remote_ref(bench.bare, bench.branch)
    assert published != seed_commit
    assert subprocess.run(
        ["git", "-C", str(bench.bare), "cat-file", "-e", f"{published}:{RELATIVE_NOTE}"],
    ).returncode == 0

    # ---- stage 3: the receipt describes that publication and nothing else
    receipt = json.loads(
        meeting_receipt_path(bench.cfg, occurrence_id).read_text(encoding="utf-8"))
    assert receipt["status"] == "published"
    assert receipt["occurrence_id"] == occurrence_id
    assert receipt["revision"] == payload["revision"]
    assert receipt["markdown_path"] == RELATIVE_NOTE
    assert receipt["vault_commit"] == published            # what the remote holds
    assert receipt["vault_commit"] == _git(bench.vault, "rev-parse", "HEAD")

    url = receipt["markdown_url"]
    assert url.startswith(MEETING_WEB_BASE + "/-/blob/")
    assert receipt["vault_commit"] in url                  # commit-specific, not a branch
    assert f"/-/blob/{bench.branch}/" not in url
    assert not url.startswith(("file:", "/"))              # not a filesystem path
    assert str(bench.vault) not in url and str(bench.tmp_path) not in url
    assert url.startswith("https://")

    # ---- stage 4: a second before the boundary, the recording is still there
    expires_at = _instant(audio["expires_at"])
    assert expires_at == _instant(payload["ended_at"]) + timedelta(days=14)

    assert run_expire_meeting_audio(
        bench.cfg, now=_stamp(expires_at - timedelta(seconds=1))) == 0
    assert stored.is_file()
    assert get_meeting(conn, occurrence_id)["audio"]["deleted_at"] is None

    # ---- stage 5: at the boundary the bytes go, the record of them stays
    boundary = _stamp(expires_at)
    assert run_expire_meeting_audio(bench.cfg, now=boundary) == 0

    assert not stored.exists()
    after = get_meeting(conn, occurrence_id)["audio"]
    assert after["deleted_at"] == boundary
    for key in ("filename", "format", "encryption", "duration_seconds", "bytes",
                "sha256", "expires_at", "stored_path"):
        assert after[key] == audio[key], key

    # the meeting itself is untouched: note, receipt, remote, transcript
    assert note.is_file()
    assert meeting_receipt_path(bench.cfg, occurrence_id).is_file()
    assert _remote_ref(bench.bare, bench.branch) == published
    assert len(get_meeting(conn, occurrence_id)["transcript"]) == len(payload["transcript"])
    assert verify_meeting_semantics(conn, ids, bench.vault) == []
