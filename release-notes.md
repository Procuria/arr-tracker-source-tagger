# v0.6 – Uploading-aware Quality Profile Enforcement

## Added

- **Uploading tag ↔ Quality Profile enforcement**
  - When the `uploading` tag is applied, the service now enforces a configurable
    Custom Quality Profile (CQP), e.g. `No Upgrades`, on the affected Sonarr/Radarr item.
  - The previously active quality profile is stored in `uploading_state.json` and
    automatically restored once the `uploading` tag is removed.

- **Drift correction while uploading**
  - If an item is already tagged as `uploading` but its quality profile was changed
    manually or by another tool, the service re-applies the configured CQP to ensure
    upgrades remain blocked.

- **State-aware and safe by design**
  - All profile switches are tracked per torrent hash.
  - `dry_run=true` remains fully side‑effect free (no tags, no profile changes, no state writes).

## Configuration

New environment variables:

- `UPLOADING_CQP_NAME` (default: `No Upgrades`)
- `UPLOADING_CQP_ENFORCE` (default: `true`)
- `UPLOADING_CQP_RESTORE` (default: `true`)

If the configured CQP cannot be found in an Arr instance, profile switching is
disabled for that Arr while tagging continues to function.

## Why

This release prevents accidental upgrades, re-downloads, or replacements of media
that is actively being uploaded to trackers, while cleanly restoring the original
setup once the upload is finished.

---

Quietly practical. Nothing magical — just fewer surprises.
