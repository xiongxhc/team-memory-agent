# `teammem-meeting/v2`

This is the versioned meeting artifact wire format for losslessly retaining
overlapping transcript observations. It is a separate supported version beside
the frozen [`teammem-meeting/v1`](teammem-meeting-v1.md) format. It does not
supersede, weaken, or rewrite v1 artifacts.

Reference fixtures:

- `tests/fixtures/meetings/teammem-meeting-v2.minimal.json`
- `tests/fixtures/meetings/teammem-meeting-v2.overlap.json`

Other repositories may validate against those exact synthetic, non-secret
artifacts. Their byte-file SHA256 values are:

- minimal: `287b110ebe7f9f3a224b2a6cdf3dfbf0313f0b97f43179af7a450148331da397`
- overlap: `171c0be9c1899db1ff813d5d489570134870fc9ce049bc1ec2dc94100bd02802`

Their canonical artifact hashes are:

- minimal: `sha256:e243b84f155324483fabb4d88bf07af2beab6639c0c2555fecfe082a6126a7cc`
- overlap: `sha256:4efa7b78605d8c0a60a5515adda74181071079ab2bdebbfec69662a334cda9fa`

## Exact delta from v1

The only top-level shape change is:

```json
"schema": "teammem-meeting/v2"
```

Transcript observations may overlap or contain one another. Each observation
still retains the exact v1 field set and validation rules, including its
identifier, interval, wording, speaker shape, attribution confidence, source
stage, and model. Nothing is grouped, retimed, deduplicated, or normalized.

The input `transcript` array must be strictly increasing by this tuple:

1. parsed UTC `started_at` instant;
2. parsed UTC `ended_at` instant;
3. `segment_id` in ordinal order.

Times are compared as instants at the existing microsecond resolution, not by
their JSON spelling. For example, `.1Z` and `.100000Z` denote the same instant,
so their ordering proceeds to the next tuple member. Parsers reject an unsorted
array; they never sort or otherwise change the signed artifact bytes.

The v1 identifier grammar remains shared and unchanged:
`[A-Za-z0-9][A-Za-z0-9_.:-]*`, from 1 through 200 characters. Therefore the
admitted ASCII IDs sort ordinally, never by locale collation. The observable
equal-time boundary uses valid identifiers such as `A` before `a`; non-ASCII
identifiers remain invalid in both versions.

Every interval must remain inside the meeting window and have
`ended_at >= started_at`. Zero-duration observations and touching observations
remain legal. `segment_id` values remain unique and are still the only values
synthesis evidence may reference.

## Rules inherited unchanged from v1

Except for the schema literal and transcript overlap/order rule above, every v1
rule is normative for v2:

- the exact top-level, participant, attendance, transcript, speaker, synthesis,
  recording-gap, warning, provenance, and audio key sets and limits;
- the three speaker variants and reconciled-person constraints;
- participant and organizer semantics;
- evidence existence and uniqueness checks;
- recording-gap ordering and disjointness;
- UTC instant spelling, meeting-window bounds, and occurrence/path identity;
- encrypted-audio metadata, integrity screening, and 14-day authoritative
  retention derived from the meeting endpoint;
- privacy: full transcript text, warning detail, and audio facts never enter the
  Team Vault; and
- rejection without transcript text, credentials, or other sensitive values in
  error messages.

Unknown artifact versions fail closed. The transport schema remains
`teammem-meeting-manifest/v1`, and publication receipts remain v1. Their
occurrence, revision, and artifact-hash contracts do not inspect transcript
timing.

## Canonical hash, persistence, and revisions

Canonical hashing is unchanged:

```python
blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
artifact_hash = "sha256:" + hashlib.sha256(blob).hexdigest()
```

The schema discriminator and original transcript array order are therefore
content-addressed. The hub validates order but does not rewrite it. Existing
normalized segment columns, durable array position, ID-keyed evidence, and raw
artifact JSON preserve all v2 facts without a database migration.

A v1-to-v2 change at the same `(occurrence_id, revision)` is different content
and conflicts. An intentional version transition uses a higher revision. The
prior raw artifact and normalized rows remain immutable history, while the
higher revision becomes the current projection.

Meeting duration and retention are based on the meeting `started_at` and
`ended_at`; overlapping observation durations are never summed. Frozen v1
artifacts retain their original start-order and non-overlap requirement.
