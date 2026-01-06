# Release v0.3 — Upload-awareness & History-driven Backfill

This release extends *arr-tracker-source-tagger* beyond event-based tagging and adds two major capabilities:
history-based backfilling and upload-aware protection tagging.

The focus of this version is **library safety, traceability, and post-hoc correctness**.

---

## ✨ New Features

### History-based backfill (`/backfill/history`)
It is now possible to retroactively tag existing Sonarr and Radarr libraries.

- Uses Arr **History** instead of relying on active torrents
- Joins `grabbed` → `*Imported*` events via `downloadId`
- Extracts indexer information from history (primary signal)
- Optional fallback to qBittorrent trackers if history is incomplete
- Sonarr tagging is applied **per series** (latest successful import only) to reduce noise
- Fully idempotent and dry-run capable

Configurable via request payload:
- `dry_run`
- `limit`
- `only_missing`
- `reapply`
- `page_size`

---

### Upload-aware tagging (`/backfill/uploading`)
A new endpoint to protect content that is actively being uploaded to trackers.

- Reads torrents from a configurable qBittorrent category (default: `tracker_own_uploads`)
- Supports **dedicated upload qBittorrent instances** or fallback to the primary one
- Matches torrent names against Sonarr/Radarr using Arr’s native `/parse` endpoint
- Applies a dedicated tag (default: `uploading`)
- Sonarr tagging is restricted to **full season packs only**
- Designed to integrate cleanly with Maintainerr rules

This prevents accidental cleanup of content that is still in active upload/seed workflows.

---

## 🛠 Improvements & Fixes

- **Sonarr season pack detection fixed**
  - Now relies on `parsedEpisodeInfo.fullSeason` and `releaseType=seasonPack`
  - Compatible with Sonarr versions that populate top-level `episodes` even for season packs
- qBittorrent tracker lookup during backfill is now **non-fatal**
  - Missing torrents (HTTP 404) are handled gracefully
- qBittorrent fallback during history backfill is fully **bomb-proof**
  - Network errors or missing torrents no longer abort the run
- Improved logging around decision paths (indexer vs tracker vs fallback)

---

## ⚙️ Configuration Additions

New environment variables introduced in this release:

```env
# History backfill
HISTORY_FALLBACK_QBIT=true

# Upload protection
UPLOADING_TAG=uploading
UPLOAD_QBIT_CATEGORY=tracker_own_uploads

# Optional dedicated upload qBittorrent instance
QBIT_UPLOAD_URL=
QBIT_UPLOAD_USERNAME=
QBIT_UPLOAD_PASSWORD=
QBIT_UPLOAD_VERIFY_TLS=true
``` 

All new features are optional and backward-compatible.

## 🧠 Design Notes

No Arr state is mutated unless explicitly requested (dry_run=false)

Existing source tags (pt-*, public) are never modified by upload tagging

The tool remains stateless and automation-friendly

Designed to work equally well with single- or multi-qBittorrent setups

## 🏷 Version

Tag: v0.3

Compatibility: Sonarr v3 / Radarr v3+, qBittorrent v4+