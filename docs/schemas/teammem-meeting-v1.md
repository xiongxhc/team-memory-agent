# `teammem-meeting/v1`

This is the frozen wire format between the Meeting Agent and the Team Memory
Agent hub. Any incompatible change requires a new schema identifier: the hub
rejects every `schema` value other than `teammem-meeting/v1`.

It is a separate protocol from [`teammem-bundle/v1`](../../schemas/teammem-bundle-v1.md).
Meetings have their own parser (`teammem/meeting_bundles.py`), their own inbox,
and their own tables. Nothing about member bundles applies here.

Reference fixtures: `tests/fixtures/meetings/teammem-meeting-v1.minimal.json`
and `tests/fixtures/meetings/teammem-meeting-v1.complete.json`. Other
repositories validate against those exact bytes.

## Bundle layout

A meeting arrives as a directory, not a file. The producer writes into private
staging, fsyncs, renames atomically, and creates `READY` last.

```text
<inbox>/<occurrence-segment>/<revision>/
    meeting.json      the artifact payload described below
    audio.enc         encrypted source audio; TeamMem never decrypts it
    manifest.json     transport integrity claims
    READY             empty marker written last; content ignored
```

`load_meeting_bundle(path, inbox)` receives the revision directory as `path`
and the inbox root as `inbox`. It rejects a bundle when:

- `path` or any bundle file is a symlink;
- the resolved `path` is not inside the resolved `inbox`;
- `path` is not exactly two segments below the inbox;
- the revision directory is not a positive decimal integer without leading zeros;
- any of the four files is missing, is not a regular file, or an unexpected
  fifth entry exists (a partial producer directory is never consumed).

## Occurrence identity

One occurrence is the provider meeting ID plus the occurrence start, so a
recurring meeting produces distinct records:

```text
logical (JSON):  <meeting-id>@<UTC start>          vc:9f3a/board-sync@2026-09-02T14:00:00Z
path / URL:      <encoded meeting-id>_<compact>    vc%3A9f3a%2Fboard-sync_20260902T140000Z
```

The path segment is
`f"{urllib.parse.quote(meeting_id, safe='')}_{start.strftime('%Y%m%dT%H%M%SZ')}"`.
TeamMem supports Windows, so the raw ID — which contains `:` — never becomes a
path. The directory segment must equal the segment derived from
`occurrence_id`, and the revision directory must equal `revision`.

The short occurrence ID used in rendered note filenames and publication
receipts is the first 8 lowercase hex characters of
`sha256(<occurrence path segment>)`. It hashes the encoded segment, not the raw
ID, so it stays stable under the encoding rule.

Public helpers: `parse_occurrence_id`, `occurrence_path_segment`,
`short_occurrence_id`, `canonical_artifact_hash`.

## Instants

Every instant is UTC with a literal `Z`:
`YYYY-MM-DDTHH:MM:SS[.ffffff]Z`. Offsets such as `+04:00` are rejected.

`started_at` and the instant inside `occurrence_id` additionally carry no
fractional seconds, because they are the source of the compact path spelling.
`started_at` must equal the `occurrence_id` start exactly.

## `meeting.json`

The top-level key set is exact. A missing key and an unknown key are both
rejected; optional structures are present-but-empty or explicitly `null`,
never absent.

| Field | Type | Rules |
|---|---|---|
| `schema` | string | Exactly `teammem-meeting/v1`. |
| `occurrence_id` | string | `<meeting-id>@<UTC start>`, ≤ 400 chars, no control characters in the meeting id. |
| `revision` | integer | 1 ≤ n ≤ 1000000. Booleans, floats, and strings are rejected. |
| `provider` | string | Lowercase token, e.g. `feishu`. |
| `provider_meeting_id` | string | Must equal the meeting-id half of `occurrence_id`. |
| `title` | string | Non-blank, ≤ 200 chars. |
| `started_at` | instant | Whole seconds; equals the `occurrence_id` start. |
| `ended_at` | instant | ≥ `started_at`. Drives audio expiry. |
| `organizer` | object or `null` | See [Organizer](#organizer). |
| `organizer_source` | string or `null` | `reservation` or `calendar_event`; `null` only when `organizer` is `null`. |
| `organizer_unresolved_reason` | string or `null` | Exactly `instant_meeting` when `organizer` is `null`; `null` otherwise. |
| `transcript_complete` | boolean | `false` means Meeting Agent still owes a higher revision. |
| `summary` | string | Non-blank, ≤ 20000 chars. The whole-meeting note synthesis. |
| `participants` | array | Non-empty. See [Participants](#participants). |
| `transcript` | array | Ordered, may be empty. See [Transcript](#transcript). |
| `projects` | array | Substantively discussed projects with evidence. |
| `decisions` | array | Explicitly stated decisions with evidence. |
| `action_items` | array | See [Action items](#action-items). |
| `discussion_points` | array | Key discussion points with evidence. |
| `person_summaries` | array | See [Per-person summaries](#per-person-summaries). |
| `recording_gaps` | array | Ordered, disjoint, inside the meeting window. |
| `processing_warnings` | array | `{code, detail}`; `code` is a lowercase token, `detail` ≤ 500 chars. The note renders the code; `detail` is kept in the ledger and never published. |
| `provenance` | object | See [Provenance](#provenance). |
| `audio` | object | See [Encrypted audio](#encrypted-audio). |

All arrays are bounded: ≤ 500 participants, ≤ 50000 transcript segments,
≤ 500 entries in every synthesis array, ≤ 100 evidence IDs per item, ≤ 100
attendance intervals per person.

### Organizer

The organizer is the person who created the meeting: the Feishu reservation
`reserve_user_id` (`organizer_source: "reservation"`) or the calendar event
organizer (`organizer_source: "calendar_event"`). It is never `host_user`,
never the current host or co-host, never whoever invited the agent, and never
inferred from the attendee list.

```json
"organizer": {"person_id": "ou_ada", "display_name": "Ada Chen"},
"organizer_source": "reservation",
"organizer_unresolved_reason": null
```

An instant meeting has no reservation and no calendar event, so it has no
organizer. That case is `"organizer": null`, `"organizer_source": null`,
`"organizer_unresolved_reason": "instant_meeting"`. The meeting is still
stored and published; only the organizer notification fails closed. No other
unresolved reason is accepted in v1.

The organizer need not be a participant.

### Participants

`participants` holds actual, observed joiners only. Invitees and no-shows are
not participants, which is enforced by requiring at least one observed
attendance interval per person.

```json
{
  "person_id": "ou_chi",
  "display_name": "Chidi Okafor",
  "attendance": [
    {"joined_at": "2026-09-02T14:00:20Z", "left_at": "2026-09-02T14:20:00Z"},
    {"joined_at": "2026-09-02T14:24:05Z", "left_at": "2026-09-02T14:52:00Z"}
  ]
}
```

- `person_id` is unique across the array; `display_name` is non-blank.
- `attendance` is non-empty, ordered, and disjoint.
- `joined_at` may precede `started_at` (people join before the meeting starts)
  but may not follow `ended_at`.
- `left_at` is `null` when the person was still present at the end; otherwise
  it is ≥ `joined_at` and ≤ `ended_at`. An interval with `left_at: null` must
  be the last one for that person.
- A person who joined but never spoke stays a participant and has no
  per-person summary.

### Transcript

Segments are ordered by `started_at` and must not overlap; touching segments
(`next.started_at == previous.ended_at`) are legal. Timing lies inside
`[started_at, ended_at]`. `segment_id` is unique within the artifact and is the
only thing evidence references.

```json
{
  "segment_id": "s001",
  "started_at": "2026-09-02T14:00:05Z",
  "ended_at": "2026-09-02T14:00:35Z",
  "text": "Let us walk the Atlas launch checklist top to bottom before anything else.",
  "speaker": {"kind": "person", "person_id": "ou_ada", "display_name": "Ada Chen", "reconciled": true},
  "attribution_confidence": 0.94,
  "provenance": {"stage": "final_batch", "model": "batch-diarize-1"}
}
```

`attribution_confidence` is a number in `[0, 1]`. `provenance.stage` is
`live_stream` or `final_batch`.

`speaker` has exactly three shapes, matching the three outcomes of final
reconciliation:

| `kind` | Keys | Meaning |
|---|---|---|
| `person` | `kind, person_id, display_name, reconciled` | A confirmed participant. `person_id` must be an actual participant, `display_name` must match that participant's, and `reconciled` must be exactly `true`. |
| `anonymous` | `kind, cluster_id` | A retained anonymous speaker cluster. |
| `ambiguous` | `kind` | Overlapping speech that stayed ambiguous. |

The system never guesses identity. A `person` speaker with `reconciled: false`
is rejected rather than downgraded, because a provisional live guess must not
reach durable storage.

### Projects, decisions, discussion points

Each is `{"text" | "name", "evidence_segment_ids"}`. `evidence_segment_ids` is
non-empty, free of duplicates, and every ID must exist in `transcript`. A
project link without supporting transcript segments is rejected: inferred
project links are only as good as their evidence. Project names are unique.

### Action items

```json
{
  "text": "Write the Borealis migration runbook and circulate it.",
  "owner": {"person_id": "ou_dana", "display_name": "Dana Whitfield", "explicitly_stated": true},
  "due_date": {"date": "2026-09-03", "explicitly_stated": true},
  "evidence_segment_ids": ["s011"]
}
```

`owner` and `due_date` are `null` when the meeting did not state them. When
present, `explicitly_stated` must be exactly `true` — an inferred owner or an
inferred deadline is rejected, not stored with a lower confidence. `due_date.date`
is a real calendar date in `YYYY-MM-DD`. The owner need not be a participant
(work is sometimes assigned to someone who was not in the room).

### Per-person summaries

```json
{"person_id": "ou_ada", "display_name": "Ada Chen", "text": "Ran the launch checklist ..."}
```

One entry per person at most. A summary is accepted only for a person who is an
actual participant **and** has at least one `speaker.kind == "person"` segment
attributed to them. Summaries for silent joiners, for invitees, and for
non-participants are rejected. `display_name` must match the participant's.

### Provenance

```json
{
  "runtime_profile": "online",
  "stt_provider": "openai",
  "stt_model": "stt-batch-preview",
  "text_provider": "openai",
  "text_model": "text-structured-preview",
  "agent_version": "meeting-agent/0.1.0",
  "processed_at": "2026-09-02T15:10:22Z"
}
```

`runtime_profile` is the profile pinned for the whole session — `offline` or
`online`. There is no silent profile switching, so this value is recorded, not
negotiated.

### Encrypted audio

```json
{
  "filename": "audio.enc",
  "format": "opus",
  "encryption": "age-x25519",
  "duration_seconds": 3120.0,
  "bytes": 2048,
  "sha256": "sha256:10fc3c...",
  "expires_at": "2026-10-02T14:52:00Z"
}
```

- `filename` must be exactly `audio.enc`.
- `format` names the plaintext container inside the encryption; `encryption`
  names the scheme. TeamMem never holds the key and never decrypts.
- `duration_seconds` is > 0 and ≤ 86400; `bytes` is > 0 and ≤ 8 GiB.
- `sha256` is `sha256:<64 lowercase hex>` over the encrypted bytes and must
  match `audio.enc` on disk and the manifest.
- `expires_at` is **advisory and never authoritative**. TeamMem computes
  retention as `ended_at + 14 days` and ignores a longer caller-supplied value.
  The complete fixture deliberately declares a longer expiry to pin this rule.

## `manifest.json`

The transport-level integrity claim. Its key set is exact:

```json
{
  "schema": "teammem-meeting-manifest/v1",
  "occurrence_id": "vc:9f3a/board-sync@2026-09-02T14:00:00Z",
  "revision": 3,
  "artifact_hash": "sha256:...",
  "audio_sha256": "sha256:...",
  "audio_bytes": 2048
}
```

`occurrence_id` and `revision` must match `meeting.json`. `audio_sha256` and
`audio_bytes` must match `audio.enc` on disk **and** the `audio` block in
`meeting.json`.

## Canonical artifact hash

TeamMem never trusts the manifest's `artifact_hash`. It recomputes it from the
parsed payload:

```python
blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
artifact_hash = "sha256:" + hashlib.sha256(blob).hexdigest()
```

The hash is therefore insensitive to how `meeting.json` was formatted on the
wire and sensitive to every field value. A manifest whose `artifact_hash`
disagrees with the recomputed value is rejected. `MeetingBundle.artifact_hash`
always carries the recomputed value.

## Plaintext-audio screening and its limitation

TeamMem does not hold the recording key, so it cannot prove that `audio.enc`
is ciphertext. It can only refuse bytes that announce themselves as a
plaintext container. The first bytes are screened against:

| Magic | Offset |
|---|---|
| `RIFF` | 0 |
| `OggS` | 0 |
| `fLaC` | 0 |
| `ID3` | 0 |
| `ftyp` | 4 |
| MPEG frame sync (`0xFF`, then `0xE0`–`0xFF`) | 0 |

This is a screen, not a proof. It catches a producer that forgot to encrypt;
it cannot detect plaintext in an unlisted container, and a ciphertext whose
first bytes coincidentally match a magic number is rejected as a false
positive. Recovery from that false positive is resubmission — the same content
under a new revision has different bytes.

## Idempotency and revisions

Identity is `(occurrence_id, revision)`; content identity is `artifact_hash`.

- **Same occurrence, same revision, same hash** — no-op. Replay changes nothing.
- **Same occurrence, same revision, different hash** — rejected. A revision is
  immutable once imported.
- **Lower revision than the current one** — rejected.
- **Higher revision** — becomes the current projection atomically. Prior
  artifacts and their normalized rows remain as provenance and stay queryable.

Backfill is the Meeting Agent's job. When `transcript_complete` is `false`, the
agent re-runs reconciliation from its own encrypted copy and submits a higher
revision. TeamMem never re-transcribes; it only alerts the operator when the
current revision is still incomplete inside the pre-expiry window.

## What never crosses this boundary

- TeamMem never receives the recording key and never decrypts audio.
- Team Vault never receives the full transcript or any audio. The transcript
  lives in the bundle and in TeamMem's database only.
- Rejection reasons are machine-readable and name fields, IDs, and paths —
  never transcript text and never credentials.
