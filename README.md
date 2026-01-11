
# Arr Tracker Source Tagger

A unified, **reliable source-tagging solution for Sonarr and Radarr** that derives tags from the **actual torrent trackers used in qBittorrent**.

This project was designed to solve a long-standing problem in the *arr ecosystem:

> **Which tracker did this media actually come from — and how can we automate that knowledge?**

Instead of relying on fragile Arr history or indexer metadata, this tool uses the **torrent tracker announce URLs from qBittorrent as the single source of truth**.


## 🚀 Quick Start (5 Minutes)

This is the fastest way to see the tagger working end-to-end.

### 1) Prepare environment
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) Create `.env`
Minimal example (adjust URLs and API keys):

```env
RUN_MODE=webhook
LOG_LEVEL=INFO

QBIT_URL=https://qbittorrent.example.org
QBIT_USERNAME=admin
QBIT_PASSWORD=supersecret

RADARR_URL=https://radarr.example.org
RADARR_API_KEY=xxxx

SONARR_URL=https://sonarr.example.org
SONARR_API_KEY=xxxx
```

### 3) Define private trackers
```yaml
private_trackers:
  tracker.org: pt-tracker
```

### 4) Start the service
```bash
python main.py
```

You should see:
```
[INFO] Starting webhook server on 0.0.0.0:8787
```

### 5) Send a test payload
```bash
curl -X POST http://localhost:8787/tag   -H "Content-Type: application/json"   -d '{
    "arr": "radarr",
    "item_id": 123,
    "download_id": "<TORRENT_HASH>",
    "is_upgrade": false
  }'
```

### 6) Verify in Radarr / Sonarr
The movie or series now has:
- `pt-<tracker>` if it came from a private tracker
- `public` otherwise

That’s it — the rest of the README explains *why* this works and how to run it in production.

---

## ✨ Features

- ✅ **One script for Sonarr and Radarr**
- 🔒 **Private vs public tracker detection**
- 🏷️ Automatic tagging:
  - `pt-<tracker>` for private trackers
  - `public` for everything else
- 🔁 **Re-tags on upgrades** (source is recalculated every import)
- 📦 Works with **qBittorrent only** (by design, for reliability)
- 🐳 **Docker ready**
- 🧪 Fully testable locally via **venv + webhook payloads**
- 🧠 Human-readable, verbose logging

---

## 🧠 Core Concept

- **qBittorrent** is the *runtime source of truth*
- **Tracker domain ≈ indexer identity** (especially for private trackers)

Torrent files always retain their tracker announce URLs.
Those domains are stable, deterministic, and observable at import time.

That makes them perfect for **source tagging**.

---

## 🏗 Architecture Overview

```
Sonarr / Radarr
      |
      |  (On Import / Webhook)
      v
Arr Source Tagger
      |
      |  (Torrent Hash)
      v
qBittorrent API
      |
      |  (Tracker URLs)
      v
Domain → Tag Mapping
      |
      v
Sonarr / Radarr API (apply tag)
```

---

## 🏷 Tagging Rules

- First matching **private tracker domain** wins
- If no private tracker matches → `public`
- Existing source tags (`pt-*`, `public`) are removed before applying the new one
- All **other tags are preserved**

---

## 📁 Files

```
.
├── main.py                 # main application
├── private_trackers.yml    # domain → tag mapping
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env                    # local / Coolify config
└── data/state.json         # optional state cache
```

---

## 🛠 Configuration

### 🔐 Webhook Authentication (WEBHOOK_SECRET)

Webhook mode can be protected with a shared secret.

### Configure

Set the secret via environment variable:

```env
RUN_MODE=webhook
WEBHOOK_SECRET=change-me-please
```

If set, `/tag`, `/health` and `/backfill/history` require the secret

Supports either:

- Header: X-Webhook-Secret: <secret>
- Header: Authorization: Bearer <secret>
- Query param: ?secret=<secret> (handy for quick manual tests)  

> If WEBHOOK_SECRET is not set, behavior stays as-is (no auth).

### `.env` (example)

```env
LOG_LEVEL=DEBUG

# Mode
RUN_MODE=webhook
WEBHOOK_SECRET=change-me-please #or leave empty for no secret 

# qBittorrent
QBIT_URL=https://qbittorrent.example.org
QBIT_USERNAME=admin
QBIT_PASSWORD=supersecret
QBIT_VERIFY_TLS=true

# Sonarr / Radarr
SONARR_URL=https://sonarr.example.org
SONARR_API_KEY=xxxx
RADARR_URL=https://radarr.example.org
RADARR_API_KEY=xxxx

# Tagging
PUBLIC_TAG=public
PRIVATE_TRACKERS_FILE=./private_trackers.yml
SOURCE_TAG_PREFIXES=pt-,public
STATE_FILE=./data/state.json

WEBHOOK_BIND=0.0.0.0
WEBHOOK_PORT=8787

# History backfill
# If true, and a grabbed record is missing, the service will try qBittorrent trackers lookup
# (safe: 404/timeouts won't abort)
HISTORY_FALLBACK_QBIT=true
```

---

## 🔐 `private_trackers.yml`

Only **private trackers** belong here.

```yaml
private_trackers:
  awesome.tracker.org: pt-awesome
  tracker.stellar.club: pt-stellar
  the.one.and.only.com: pt-oao

### for history backfill feature ###
private_indexers:
  awesome.tracker.org: pt-awesome
  tracker.stellar.club: pt-stellar
  the.one.and.only.com: pt-oao 
```

If a torrent contains *any* of these domains, it will be tagged accordingly.

---

## 🚀 Running Locally (venv)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Expected output:

```
[INFO] Starting webhook server on 0.0.0.0:8787
```

Health check:

```bash
curl http://localhost:8787/health
```

---

## 🧪 Testing with Webhook Payloads

### Radarr example

```bash
curl -X POST http://localhost:8787/tag   -H "Content-Type: application/json"   -d '{
    "arr": "radarr",
    "item_id": 123,
    "download_id": "47ea34837a369d2e37ae74832adc1595e9a26aff",
    "is_upgrade": false,
    "title_hint": "Some.Movie.2024.1080p.BluRay.x264-GROUP"
  }'
```

### Sonarr example

```bash
curl -X POST http://localhost:8787/tag   -H "Content-Type: application/json"   -d '{
    "arr": "sonarr",
    "item_id": 456,
    "download_id": "0123456789abcdef0123456789abcdef01234567",
    "is_upgrade": true,
    "title_hint": "Some.Show.S01E01.2160p.WEB-DL.x265-GROUP"
  }'
```

---

## 🔗 Sonarr / Radarr Integration (Recommended)

Use **Connect → Custom Script → On Import**

- This ensures tagging happens exactly once per import
- Upgrades automatically trigger re-tagging
- No polling, no cron jobs

---

## 🧾 History backfill (tag existing library using Arr history)

If you deployed this tool after you already had content in Sonarr/Radarr, you can tag your existing library using Arr’s History.

This mode uses:

- `grabbed` events to get the indexer (and sometimes source URLs)
- successful `*Imported*` events to ensure we only tag items that actually imported
- a join on `downloadId`

### Endpoint

`POST /backfill/history`

Requires the same webhook authentication as `/tag` (see `WEBHOOK_SECRET`).

### Request payload fields

- `arr` *(required)*: `"radarr" | "sonarr" | "both"`
- `dry_run` *(optional, default: true)*  
  If `true`, the service will only log what it would do and return a summary — **no tags are changed**.
- `limit` *(optional, default: 0)*  
  Max number of items to process. `0` means “no limit”.
- `only_missing` *(optional, default: true)*  
  If `true`, only items that **do not already have a source tag** (e.g. `pt-*` or `public`) are processed.
- `reapply` *(optional, default: false)*  
  If `true`, re-tag items even if they already have source tags (useful if you changed mappings).
- `page_size` *(optional, default: 1000)*  
  How many history records to request from Arr in a single call. Increase if your history is large and you need older entries.

### Tag decision rules (in order)

1. Prefer `private_indexers` mapping from `PRIVATE_TRACKERS_FILE` (matches the normalized grabbed `data.indexer` value)
2. Fallback: extract domains from grabbed URLs (`nzbInfoUrl`, `guid`, `downloadUrl`) and match against `private_trackers`
3. If no grabbed record is found for the `downloadId`, optionally try qBittorrent trackers lookup (controlled by `HISTORY_FALLBACK_QBIT`)
4. Otherwise fallback to `PUBLIC_TAG`

### Sonarr “less noise” behavior

Sonarr tags are applied at **Series** level. The backfill uses the **latest successful import per series** to decide the series’ source tag.

### Example: dry-run both

```bash
curl -X POST http://localhost:8787/backfill/history \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: YOURSECRET" \
  -d '{
    "arr": "both",
    "dry_run": true,
    "only_missing": true,
    "limit": 200,
    "page_size": 1000
  }'
```

### Example: apply changes (Radarr only)

```bash
curl -X POST http://localhost:8787/backfill/history \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: YOURSECRET" \
  -d '{
    "arr": "radarr",
    "dry_run": false,
    "only_missing": false,
    "reapply": true,
    "page_size": 2000
  }'
```

---

## 📋 Logging

Example log flow:

```
Resolved torrent hash: ...
Tracker domains found: seedpool.org
Chosen source tag: pt-sp
Applied source tag 'pt-sp' to 'Movie Title'
```

Logs are intentionally human-readable and suitable for production use.

---
## 🚦 Upload-aware tagging (stateful)

This feature allows you to **protect content that is currently being uploaded** to trackers by applying a dedicated tag
(default: `uploading`) in Sonarr/Radarr.

It is designed to work with Maintainerr or similar cleanup tools to avoid accidental deletion of active uploads.

### How it works

- Reads torrents from a qBittorrent **category** (default: `tracker_own_uploads`)
- Matches torrents to:
  - **Radarr movies**, or
  - **Sonarr full-season packs only**
- Applies the `uploading` tag while the torrent is present
- **Removes the tag automatically** once the torrent disappears from that category
- Uses a **separate state file** to avoid re-processing the same torrents repeatedly

### Endpoint

`POST /backfill/uploading`

### Request payload

- `arr` *(optional, default: both)*  
  `"radarr" | "sonarr" | "both"`
- `dry_run` *(optional, default: true)*  
  If `true`, no tags are changed and **no state is written**
- `limit` *(optional, default: 0)*  
  Limit number of torrents processed (`0` = unlimited)

### Example (dry-run)

```bash
curl -X POST http://localhost:8787/backfill/uploading \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: YOURSECRET" \
  -d '{
    "arr": "both",
    "dry_run": true
  }'
```

### State file behavior

- State is stored in `UPLOADING_STATE_FILE`
- **Dry-run never modifies the state**
- Only new or changed torrents trigger Arr API calls
- Torrents removed from the upload category automatically trigger tag removal

### Required `.env` additions

```env
UPLOADING_TAG=uploading
UPLOAD_QBIT_CATEGORY=tracker_own_uploads
UPLOADING_STATE_FILE=./data/uploading_state.json
UPLOADING_SYNC_REMOVE_TAG=true
UPLOADING_UNMATCHED_TTL_HOURS=24
QBIT_UPLOAD_URL=
QBIT_UPLOAD_USERNAME=
QBIT_UPLOAD_PASSWORD=
QBIT_UPLOAD_VERIFY_TLS=true
```

---

## 🔒 Uploading tag ↔ Quality Profile enforcement (CQP)

If you are actively uploading content, you often want to **freeze upgrades/re-downloads** for that item until the upload is finished.
This feature integrates with an existing **Custom Quality Profile (CQP)** (e.g. created via Profilarr) to enforce a “do not touch” policy
while the `uploading` tag is present.

### Prerequisites

- You must already have a CQP in **Sonarr and Radarr** named (by default): `No Upgrades`  
- That profile should be configured to **prevent upgrades** (e.g. *Upgrade Allowed = false*).  
- Sync the profile to both Arrs (e.g. via Profilarr) so it exists in **both** systems.

### Behavior

While a torrent is considered “uploading” (present in your configured qBittorrent category):

- When `uploading` is added, the service switches the item to the CQP `No Upgrades` and stores the previous profile ID in `uploading_state.json`.
- If `uploading` is present but the item is on a different profile (drift/manual changes), the service enforces `No Upgrades` again.
- When `uploading` is removed (torrent left the category), the service restores the previously stored profile ID.

All profile changes are logged and included in the `/backfill/uploading` JSON response.

### New `.env` variables

```env
# CQP enforcement while uploading
UPLOADING_CQP_NAME=No Upgrades
UPLOADING_CQP_ENFORCE=true
UPLOADING_CQP_RESTORE=true
```

### Notes

- If the configured CQP cannot be found in an Arr instance, profile switching is disabled for that Arr (tagging still works).
- `dry_run=true` remains side-effect free (no tags, no profile changes, no state writes).

---

## ❓ FAQ

### Why not read Prowlarr indexer names?
Because that information **does not survive the handoff** to the download client.
Trackers do.

### Why qBittorrent only?
Because it exposes stable, queryable tracker metadata via API.
This is a feature, not a limitation.

### Can this break existing tags?
No — only tags matching `SOURCE_TAG_PREFIXES` are managed.

### What is “History backfill”?
It allows tagging **existing library items retroactively** by inspecting Sonarr/Radarr history.
The service correlates successful imports with prior `grabbed` events to infer the source
indexer, even if the torrent no longer exists in the download client.

This is intentionally conservative: only completed imports are considered, and no
re-downloads or searches are triggered.

### What does “upload-aware tagging (stateful)” mean?
The service can track torrents in a dedicated qBittorrent category (e.g. `tracker_own_uploads`)
and tag the corresponding Sonarr/Radarr items while they are actively being uploaded.

State is persisted per torrent hash to avoid repeated parsing and unnecessary Arr API calls.
When the torrent disappears from the category, the tag is automatically removed.

### Why is state needed for upload-aware tagging?
Without state, every run would require re-parsing titles and re-checking the entire library.
State allows the service to:
- only process new or changed torrents
- detect when uploads have finished
- cleanly revert changes made during the upload phase

### What is “Uploading tag ↔ Quality Profile enforcement”?
While an item carries the `uploading` tag, the service can enforce a specific
Custom Quality Profile (e.g. `No Upgrades`) to prevent upgrades or re-downloads.

The previously active quality profile is stored and restored automatically once
the upload finishes. This avoids accidental changes while content is being seeded.

### Does this interfere with other tools (Maintainerr, Profilarr, manual edits)?
No by default.
The service only acts while the `uploading` tag is present and only on the configured
Custom Quality Profile. Once the tag is removed, the original state is restored.

If the required profile does not exist, the feature disables itself gracefully.

---

## ❤️ Philosophy

> **Make the implicit explicit.**
>
> Once you know *where* media comes from,
> you can automate *everything else*.

Happy tagging.
