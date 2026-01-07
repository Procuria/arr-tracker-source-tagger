# Release v0.4 — Stateful Upload-aware Tagging

This release introduces **stateful upload-aware tagging**, significantly reducing API load and enabling safe automation around active uploads.

---

## ✨ New Feature: Stateful Upload-aware Backfill

A new endpoint `/backfill/uploading` keeps Sonarr and Radarr in sync with torrents that are actively being uploaded.

### Key characteristics

- Tags content with `uploading` while torrents exist in a configured qBittorrent category
- Automatically **removes the tag** once the torrent disappears
- Uses a **dedicated state file** to avoid re-processing already known torrents
- Dry-run mode is strictly **side-effect free**
- Designed to integrate with Maintainerr and similar cleanup tools

---

## 🧠 Design details

- State is stored separately from source-tagging state
- Only *new* torrents trigger Arr `/parse` calls
- Sonarr tagging is restricted to **full season packs**
- Movies are matched against Radarr, series against Sonarr first
- Optional unmatched-cache avoids repeated parsing of unsupported releases

---

## ⚙️ New configuration options

```env
UPLOADING_TAG=uploading
UPLOAD_QBIT_CATEGORY=tracker_own_uploads
UPLOADING_STATE_FILE=./data/uploading_state.json
UPLOADING_SYNC_REMOVE_TAG=true
UPLOADING_UNMATCHED_TTL_HOURS=24
```

---

## 🏷 Version

- **Tag:** v0.4
