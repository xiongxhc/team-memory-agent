# Architecture

```text
central collectors ───────────────┐
                                  ├─> SQLite event ledger ─> Markdown views
MemberKit -> reviewed v1 bundle ─>│
                    inbox importer┘
```

The ledger stores one attributed fact per row. Its
`UNIQUE(person, source, hash)` constraint makes collector replays and bundle
revisions safe. Rendered vault files are projections: operators regenerate them
rather than editing them as source data.

MemberKit and the hub are separate Python distributions in one repository. They
share no runtime imports. Their integration seam is the frozen
`teammem-bundle/v1` JSON file.

The importer validates a complete bundle before inserting anything. Accepted
input is archived by content hash through a synced temporary file and atomic
replacement, so interrupted archive writes are safely repairable and multiple
reviewed revisions for one date are preserved. Invalid input is quarantined with
machine-readable error metadata.

Scheduling is member-side and local. The portable `memberkit scheduled-run`
command prepares yesterday/today drafts and continues reminding for every older
pending date. Invalid member-edited drafts are never regenerated or overwritten.
The first installer targets macOS launchd; other schedulers can invoke the same
run-once command.
