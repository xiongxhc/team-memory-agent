# `teammem-bundle/v1`

This is the frozen wire format between MemberKit and the Team Memory Agent hub.
Any incompatible change requires a new schema identifier.

## Inbox path

```text
<member>/bundle-<member>-<YYYY-MM-DD>.json
```

The directory member, filename member, and JSON `member` must match. Files are
UTF-8 JSON.

## Shape

```json
{
  "schema": "teammem-bundle/v1",
  "member": "alex",
  "date": "2026-07-27",
  "events": [
    {
      "ts": "2026-07-27T10:00:00",
      "kind": "journal-highlight",
      "summary": "Shipped the retry fix",
      "project": "project-alpha",
      "refs": null
    }
  ],
  "journal_md": "## 2026-07-27\n\n### project-alpha\n- Shipped the retry fix"
}
```

## Semantics

- `events` is authoritative; `journal_md` is a regenerated projection.
- An empty `events` array is valid.
- Event order has no semantic meaning.
- `kind` is `journal-highlight` and `refs` is `null` in version 1.
- `ts` is member-local time and must fall on the bundle's calendar date.
- The hub resolves the member slug and assigns `person`, `source`, and event hash.
- Import is idempotent per event.
- A later revision for the same member and date is valid. Unchanged events
  deduplicate; new or changed events become new evidence.
- Raw local databases, files, and direct messages are never part of this format.
