# Person Week-File Vault Layout — Design

## Problem

A Person page is one file holding every rendered week, and its Activity detail
is uncapped — one member's bundle-heavy week alone produces hundreds of
evidence bullets under daily entries that already summarize them. Pages grow
without bound and read as noise in both the forge web UI and Obsidian.

## Decision summary

- **One folder per person, one file per week:**
  `Person/<Display Name>/README.md` (index) plus
  `Person/<Display Name>/Week <label>.md` per week with events. `README.md` is
  the index deliberately: the forge web UI auto-renders a folder's README, so
  opening a person's folder shows their profile.
- **Person history is full-ledger, not the render window.** Managed dirs are
  wiped every render, so window-scoped week files would silently delete older
  weeks from the vault. Rendering every week present in the ledger keeps the
  render deterministic (same ledger + today ⇒ same tree) and makes the vault a
  permanent readable archive. Work Journal, Projects, and flags keep the
  existing window.
- **Activity detail is capped at `MAX_WORK_LINES` (12) per week**, with the
  same "…and N more work items" overflow line as project pages. The synthesized
  daily entries above are the readable record; the capped bullets are sampled
  evidence, and the full set stays queryable in the ledger.
- **README = latest week inline + week index.** The newest week's synthesized
  entries render inline so the index answers "what are they doing" without a
  second click; below it, all weeks link newest-first with event counts.
- **Cross-links:** `_person_link` targets the folder README
  (`../Person/<Name>/README.md`); week files link back to the team report
  (`../../Work Journal/<label>.md`) and to their own README.

## Out of scope

- Splitting Projects or Work Journal pages the same way (not yet a size
  problem; same pattern applies later if needed).
- Backfilling synthesis for pre-window weeks — old week files render whatever
  daily summaries exist in the cache, plus capped evidence bullets.

## Success criteria

1. Rendering produces `Person/<Name>/README.md` and one `Week <label>.md` per
   week with events, including weeks older than the render window; no
   single-file person pages remain.
2. A week with more than 12 work events renders 12 bullets plus an accurate
   overflow line.
3. Work Journal and Projects person links resolve to the folder README, and
   week files link back to the matching team report.
4. Render remains byte-deterministic for a fixed (ledger, today).
