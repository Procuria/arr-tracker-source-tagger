
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
  seedpool.org: pt-sp
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
- 🐳 **Docker & Coolify ready**
- 🧪 Fully testable locally via **venv + webhook payloads**
- 🧠 Human-readable, verbose logging

---

## 🧠 Core Concept

- **Prowlarr** is your *control plane* (which trackers exist)
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

### `.env` (example)

```env
LOG_LEVEL=DEBUG

# Mode
RUN_MODE=webhook

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
```

---

## 🔐 `private_trackers.yml`

Only **private trackers** belong here.

```yaml
private_trackers:
  seedpool.org: pt-sp
  tracker.digitalcore.club: pt-digitalcore
  fearnopeer.com: pt-fnp
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

## ❓ FAQ

### Why not read Prowlarr indexer names?
Because that information **does not survive the handoff** to the download client.
Trackers do.

### Why qBittorrent only?
Because it exposes stable, queryable tracker metadata via API.
This is a feature, not a limitation.

### Can this break existing tags?
No — only tags matching `SOURCE_TAG_PREFIXES` are managed.

---

## 🧭 Roadmap (optional ideas)

- History-based auto item_id resolution (webhook mode)
- Multiple private tracker priority rules
- Export Prowlarr → private_trackers.yml helper
- Prometheus metrics

---

## ❤️ Philosophy

> **Make the implicit explicit.**
>
> Once you know *where* media comes from,
> you can automate *everything else*.

Happy tagging.

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
