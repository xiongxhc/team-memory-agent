"""Publish one meeting note to the Team Vault and issue a receipt only after the
remote has been proven to hold it.

A receipt is a claim made to a human: Meeting Agent sends its `markdown_url` to
the organizer. Rendered locally and committed locally are not publication, so
every step that cannot be proven ends in `PublishRefused` with no receipt file
on disk. The receipt is written last, atomically, and is a pure function of the
ledger row, the verified commit and the configured web base — republishing an
unchanged revision reproduces it byte for byte.
"""

import contextlib
import json
import os
import sqlite3
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlsplit

from .identity import IdentityMaps
from .meeting_store import get_meeting, list_meetings
# `_meeting_facts` is `render.py`'s ledger-only view of one meeting, the same
# one the semantic checks hold the vault against. The receipt names the path the
# ledger says the note belongs at, not a path a renderer handed back.
from .render import _meeting_facts, render_vault, verify_vault
from .vaultgit import (PushNotVerified, commit_all, commit_contains,
                       head_commit, push_verified, remote_reaches)

RECEIPT_SCHEMA = "teammem-meeting-receipt/v1"
WEB_BASE_ENV = "TEAMMEM_VAULT_WEB_BASE"


class PublishRefused(RuntimeError):
    """Publication stopped, so no receipt exists. `stage` says where it stopped:
    `config`, `ledger`, `render`, `verify`, `commit`, `push` or `receipt`."""

    def __init__(self, stage: str, reason: str, invariants: tuple[str, ...] = ()):
        super().__init__(f"{stage}: {reason}")
        self.stage = stage
        self.reason = reason
        self.invariants = invariants


def validate_web_base(value: str | None) -> str:
    """The vault's web root, or `ValueError`. There is no default and no fallback:
    a missing base is an error at publish time, never a local path in a receipt."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{WEB_BASE_ENV} is not set; a meeting receipt cannot name "
                         "the vault without it")
    if any(character.isspace() for character in value):
        raise ValueError(f"{WEB_BASE_ENV} contains whitespace")
    parts = urlsplit(value)
    if parts.scheme != "https":
        raise ValueError(f"{WEB_BASE_ENV} must be an absolute https:// URL")
    if not parts.hostname:
        raise ValueError(f"{WEB_BASE_ENV} names no host")
    if "@" in parts.netloc:
        raise ValueError(f"{WEB_BASE_ENV} must not carry credentials")
    if parts.query or parts.fragment or "-" in parts.path.split("/"):
        # `-` opens every forge's view namespace (`/-/blob/…`, `/-/tree/…`), so a
        # base carrying one is already a page, and appending to it yields a link
        # that resolves to nothing.
        raise ValueError(f"{WEB_BASE_ENV} must be a bare repository root: no query, "
                         "no fragment, no view path")
    if any(segment in (".", "..") for segment in parts.path.split("/")):
        raise ValueError(f"{WEB_BASE_ENV} must not contain relative path segments")
    return value.rstrip("/")


def blob_url(web_base: str, commit: str, markdown_path: str) -> str:
    """An immutable, commit-specific link to one file in the vault.

    Each path segment is encoded on its own, so the separators survive and the
    spaces and square brackets in a note filename do not. A branch-relative link
    would point at whatever the note becomes; this one points at what was read.
    """
    encoded = "/".join(quote(segment, safe="") for segment in markdown_path.split("/"))
    return f"{web_base}/-/blob/{commit}/{encoded}"


def receipt_bytes(receipt: dict) -> bytes:
    """The one serialization: sorted keys, no padding, one trailing newline. Same
    inputs, same bytes, so a replay of a published revision is byte-identical."""
    return (json.dumps(receipt, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n").encode("utf-8")


def _git_detail(error: Exception) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        # Deliberately not the stderr: a remote URL can carry a credential.
        return f"git {' '.join(error.cmd[3:5])} exited {error.returncode}"
    return str(error)


def _refuse_on_drift(out: dict, occurrence_id: str, elsewhere) -> None:
    """Refuse on what this occurrence is answerable for, and on nothing else.

    Both checks are vault-wide by construction: they re-render everything and
    read every note. Refusing on all of it would mean one occurrence that renders
    badly withholds the receipt of every other occurrence, for as long as it
    stays in the ledger — the malformed-artifact blast radius the meeting path
    exists to contain. So a finding another current occurrence owns is skipped,
    and everything else still refuses: findings `verify_meeting_semantics` could
    not attribute to any single current occurrence (a missing index, a stray
    note, and every leakage finding) belong to the projection as a whole and
    block every publication until they are gone.

    `elsewhere` is called only when there is drift, and returns the note paths of
    the other current occurrences.

    Findings name a path and an invariant, never a line of a file. The offending
    file is not opened here — least of all for `meeting.leakage.forbidden-content`,
    where quoting the leak would copy it into the operator's log.
    """
    findings = [f for f in out["semantic"]
                if f["occurrence_id"] in (None, occurrence_id)]
    if findings:
        raise PublishRefused(
            "verify",
            f"the vault fails {len(findings)} meeting invariant check(s): "
            + "; ".join(f"{f['invariant']} on {f['path']}" for f in findings),
            tuple(sorted({f["invariant"] for f in findings})))
    drifted = {key: out[key] for key in ("missing", "unexpected", "differing") if out[key]}
    if drifted:
        others = elsewhere()
        drift = {key: [path for path in paths if path not in others]
                 for key, paths in drifted.items()}
        drift = {key: paths for key, paths in drift.items() if paths}
        if drift:
            raise PublishRefused(
                "verify", "the vault drifted from the ledger render: "
                + "; ".join(f"{key}: {', '.join(paths)}" for key, paths in drift.items()))


def _other_note_paths(conn: sqlite3.Connection, ids: IdentityMaps,
                      occurrence_id: str) -> set[str]:
    """Where the ledger says every *other* current occurrence's note belongs."""
    paths = set()
    for row in list_meetings(conn):
        if row["occurrence_id"] == occurrence_id:
            continue
        meeting = get_meeting(conn, row["occurrence_id"])
        if meeting is not None:
            paths.add(str(_meeting_facts(meeting, ids)["rel"]))
    return paths


def _write_receipt(path: Path, blob: bytes) -> None:
    """Stage beside the destination, fsync, rename. A crash before the rename
    leaves no receipt and no leftovers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, staged = tempfile.mkstemp(dir=path.parent, prefix=".receipt-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(blob)
            out.flush()
            os.fsync(out.fileno())
        os.replace(staged, path)
        staged = None
    finally:
        if staged is not None:
            os.unlink(staged)
    with contextlib.suppress(OSError):       # not every platform fsyncs a directory
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def publish_meeting(conn: sqlite3.Connection, ids: IdentityMaps, vault_dir: Path,
                    today: date, *, occurrence_id: str, receipt_path: Path,
                    web_base: str | None, weeks: int = 4,
                    channel_names: dict | None = None) -> dict:
    """Render, verify, commit, push, prove, then write the receipt. Returns it.

    Raises `PublishRefused` at the first step that cannot be proven. The receipt
    file exists only when the configured remote ref was seen to hold a commit
    whose tree carries the note, so its existence is the publication.
    """
    try:
        base = validate_web_base(web_base)
    except ValueError as error:
        raise PublishRefused("config", str(error)) from error

    meeting = get_meeting(conn, occurrence_id)
    if meeting is None:
        raise PublishRefused("ledger", f"no current revision of {occurrence_id}")
    rel = str(_meeting_facts(meeting, ids)["rel"])

    try:
        render_vault(conn, ids, vault_dir, today, weeks=weeks,
                     channel_names=channel_names)
    except Exception as error:
        raise PublishRefused("render", f"the vault render failed: {error}") from error

    _refuse_on_drift(
        verify_vault(conn, ids, vault_dir, today, weeks=weeks,
                     channel_names=channel_names, semantic=True),
        occurrence_id,
        lambda: _other_note_paths(conn, ids, occurrence_id),
    )

    try:
        # A clean tree is not a failure: the note is already committed, and the
        # receipt then names the current HEAD. An empty commit would pollute the
        # vault's history to say nothing.
        committed = commit_all(
            vault_dir, f"meeting: {occurrence_id} revision {meeting['revision']}")
        commit = head_commit(vault_dir)
    except subprocess.CalledProcessError as error:
        raise PublishRefused(
            "commit", f"the vault commit failed: {_git_detail(error)}") from error
    if not commit_contains(vault_dir, commit, rel):
        raise PublishRefused("commit", f"{commit} does not carry {rel}")

    try:
        # A clean tree whose commit the remote is missing is repaired, not
        # refused: the commit is already in local history, so sending it makes no
        # new claim, and it is what the daily run would do anyway. Refuse only if
        # the push cannot land, or lands and still does not reach the commit.
        if committed or not remote_reaches(vault_dir, commit):
            push_verified(vault_dir, commit)
    except (PushNotVerified, subprocess.CalledProcessError) as error:
        raise PublishRefused(
            "push", f"the note was not published: {_git_detail(error)}") from error

    receipt = {
        "schema": RECEIPT_SCHEMA,
        "occurrence_id": occurrence_id,
        "revision": meeting["revision"],
        "artifact_hash": meeting["artifact_hash"],
        "status": "published",
        "vault_commit": commit,
        "markdown_path": rel,
        "markdown_url": blob_url(base, commit, rel),
    }
    try:
        _write_receipt(receipt_path, receipt_bytes(receipt))
    except OSError as error:
        raise PublishRefused(
            "receipt", f"the receipt could not be written: {error}") from error
    return receipt
