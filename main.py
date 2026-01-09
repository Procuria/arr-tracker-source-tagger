#!/usr/bin/env python3
"""
Arr Source Tagger (Sonarr + Radarr)
- Reads torrent trackers from qBittorrent
- Maps tracker domain to a private-tracker tag, otherwise "public"
- Applies tag via Sonarr/Radarr API
- Re-tags on upgrade (every import event recalculates source tag)

Modes:
1) "arr-script" (default): invoked by Sonarr/Radarr Custom Script on import
2) "webhook": run as a small HTTP service (Coolify-friendly)

Environment variables (common):
  LOG_LEVEL=DEBUG|INFO|WARNING|ERROR   (default: INFO)
  PUBLIC_TAG=public                   (default: public)
  PRIVATE_TRACKERS_FILE=/config/private_trackers.yml (default: ./private_trackers.yml)
  SOURCE_TAG_PREFIXES=pt-,public      (default: pt-,public)
  STATE_FILE=/data/state.json         (default: ./state.json)

qBittorrent:
  QBIT_URL=https://qbittorrent.example/
  QBIT_USERNAME=...
  QBIT_PASSWORD=...
  QBIT_VERIFY_TLS=true|false (default: true)

Sonarr:
  SONARR_URL=http://sonarr:8989
  SONARR_API_KEY=...
Radarr:
  RADARR_URL=http://radarr:7878
  RADARR_API_KEY=...

Webhook mode:
  RUN_MODE=webhook
  WEBHOOK_BIND=0.0.0.0
  WEBHOOK_PORT=8787
  WEBHOOK_SECRET=...    # if set, /tag and /health require auth

Webhook Secret accepted via:
  - Header: X-Webhook-Secret: <secret>
  - Header: Authorization: Bearer <secret>
  - Query:  ?secret=<secret>

History backfill:
  POST /backfill/history
    - Tags existing library items using Arr history:
        - "grabbed" provides indexer / URLs
        - "downloadFolderImported" (or other "*Imported*") provides success
        - Join is downloadId
    - Sonarr: tags per SERIES (latest successful import per series) -> less noise
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, quote

import requests

# Optional .env loading (recommended for local dev)
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(override=True)
except Exception:
    pass

try:
    import yaml  # type: ignore
except Exception:
    yaml = None  # handled gracefully


# -----------------------------
# Logging (human readable)
# -----------------------------
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


def log(level: str, msg: str) -> None:
    current = LEVELS.get(os.getenv("LOG_LEVEL", "INFO").upper(), 20)
    if LEVELS.get(level, 20) >= current:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"{ts} [{level}] {msg}", flush=True)


def die(msg: str, code: int = 1) -> None:
    log("ERROR", msg)
    sys.exit(code)


# -----------------------------
# Helpers
# -----------------------------
TORRENT_HASH_RE = re.compile(r"^[a-fA-F0-9]{40}$")


def is_torrent_hash(s: str) -> bool:
    return bool(s and TORRENT_HASH_RE.match(s.strip()))


def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def load_state(path: str) -> Dict[str, str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log("WARNING", f"State file '{path}' could not be read: {e}. Continuing without it.")
        return {}


def save_state(path: str, state: Dict[str, str]) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception as e:
        log("WARNING", f"State file '{path}' could not be written: {e}. Continuing.")


def load_uploading_state(path: str) -> dict:
    """Load uploading sync state (separate file)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log("WARNING", f"Uploading state file '{path}' could not be read: {e}. Continuing without it.")
        return {}


def save_uploading_state(path: str, state: dict) -> None:
    """Atomic write for uploading sync state."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception as e:
        log("WARNING", f"Uploading state file '{path}' could not be written: {e}. Continuing.")


def parse_domain(url: str) -> Optional[str]:
    try:
        p = urlparse(url)
        host = (p.hostname or "").strip().lower()
        return host or None
    except Exception:
        return None


def summarize_domains(domains: List[str]) -> str:
    if not domains:
        return "(none)"
    shown = domains[:6]
    rest = len(domains) - len(shown)
    if rest > 0:
        return ", ".join(shown) + f" (+{rest} more)"
    return ", ".join(shown)


def parse_iso_utc(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Handles "2025-12-29T20:48:41Z"
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def normalize_indexer_name(name: str) -> str:
    n = (name or "").strip().lower()
    if not n:
        return ""
    # common suffix from Prowlarr integration
    n = n.replace("(prowlarr)", "").strip()
    # collapse whitespace
    n = re.sub(r"\s+", " ", n)
    return n


def has_source_tag(labels: List[str], source_prefixes: List[str]) -> bool:
    low = [l.strip().lower() for l in labels if l]
    for lbl in low:
        for p in source_prefixes:
            p2 = p.strip().lower()
            if not p2:
                continue
            if p2.endswith("-") and lbl.startswith(p2):
                return True
            if lbl == p2:
                return True
    return False


# -----------------------------
# YAML loading (trackers + indexers)
# -----------------------------
def load_private_config(path: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Expected YAML (backwards compatible):
      private_trackers:
        fearnopeer.com: pt-fnp
      private_indexers:
        fearnopeer: pt-fnp
    """
    if not os.path.exists(path):
        log("WARNING", f"Private mapping file not found: {path}. All will be tagged as public.")
        return {}, {}

    if yaml is None:
        die(
            "PyYAML is not installed but a YAML mapping file is used. "
            "Install dependencies or switch mapping file to JSON.",
            2,
        )

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        trackers_raw = data.get("private_trackers", {}) or {}
        indexers_raw = data.get("private_indexers", {}) or {}

        trackers: Dict[str, str] = {}
        for k, v in trackers_raw.items():
            if not k or not v:
                continue
            trackers[str(k).strip().lower()] = str(v).strip()

        indexers: Dict[str, str] = {}
        for k, v in indexers_raw.items():
            if not k or not v:
                continue
            indexers[normalize_indexer_name(str(k))] = str(v).strip()

        return trackers, indexers
    except Exception as e:
        die(f"Failed to load private mapping file from {path}: {e}", 2)


def load_private_tracker_map(path: str) -> Dict[str, str]:
    # kept for backward compatibility with existing code paths
    trackers, _ = load_private_config(path)
    return trackers


# -----------------------------
# Webhook Secret helpers
# -----------------------------
def mask(s: str, keep: int = 4) -> str:
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * (len(s) - keep)


def webhook_secret_ok(req) -> bool:
    """
    Accept secret via:
      - X-Webhook-Secret header (and common variants)
      - Authorization: Bearer <secret>
      - query param ?secret=<secret>
    If WEBHOOK_SECRET is unset/empty => allow.
    """
    expected = (os.getenv("WEBHOOK_SECRET") or "").strip()
    if not expected:
        return True

    header_candidates = [
        "X-Webhook-Secret",
        "X-WebhookSecret",
        "X_WEBHOOK_SECRET",
    ]

    for hn in header_candidates:
        got = (req.headers.get(hn) or "").strip()
        if got and got == expected:
            return True

    auth = (req.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token == expected:
            return True

    q = (req.args.get("secret") or "").strip()
    if q and q == expected:
        return True

    return False


# -----------------------------
# qBittorrent client
# -----------------------------
@dataclass
class QbitConfig:
    base_url: str
    username: str
    password: str
    verify_tls: bool


class QbitClient:
    def __init__(self, cfg: QbitConfig):
        self.cfg = cfg
        self.sess = requests.Session()
        self.sess.verify = cfg.verify_tls
        self.sess.headers.update({"User-Agent": "arr-source-tagger/1.0"})

    def login(self) -> None:
        url = self.cfg.base_url.rstrip("/") + "/api/v2/auth/login"
        log("DEBUG", f"qBittorrent login: POST {url}")
        r = self.sess.post(url, data={"username": self.cfg.username, "password": self.cfg.password}, timeout=15)
        if r.status_code != 200 or r.text.strip() != "Ok.":
            die(f"qBittorrent login failed (HTTP {r.status_code}): {r.text.strip()}", 3)
        log("DEBUG", "qBittorrent login OK")

    def trackers(self, torrent_hash: str) -> List[dict]:
        url = self.cfg.base_url.rstrip("/") + "/api/v2/torrents/trackers"
        log("DEBUG", f"qBittorrent trackers: GET {url}?hash={torrent_hash}")
        r = self.sess.get(url, params={"hash": torrent_hash}, timeout=20)

        # If torrent is no longer in qBittorrent, trackers endpoint returns 404.
        # This is expected during history-backfill; treat as "no trackers available".
        if r.status_code == 404:
            log("INFO", f"qBittorrent: torrent hash not found (404) for trackers lookup: {torrent_hash} (likely removed).")
            return []

        if r.status_code != 200:
           die(f"qBittorrent trackers request failed (HTTP {r.status_code}): {r.text.strip()}", 3)

        return r.json() if r.text.strip() else []


    def torrents_info(self, category: Optional[str] = None) -> List[dict]:
        url = self.cfg.base_url.rstrip("/") + "/api/v2/torrents/info"
        params: Dict[str, str] = {}
        if category:
            params["category"] = category
        log("DEBUG", f"qBittorrent torrents/info: GET {url} params={params if params else '{}'}")
        r = self.sess.get(url, params=params if params else None, timeout=30)
        if r.status_code != 200:
            die(f"qBittorrent torrents/info failed (HTTP {r.status_code}): {r.text.strip()}", 3)
        return r.json() if r.text.strip() else []

    def resolve_hash(self, download_id: str, fallback_name: Optional[str] = None) -> Optional[str]:
        if download_id and is_torrent_hash(download_id):
            return download_id.lower()

        if not fallback_name:
            return None

        name = fallback_name.strip().lower()
        if not name:
            return None

        try:
            torrents = self.torrents_info()
        except Exception as e:
            log("WARNING", f"qBittorrent fallback resolve failed: {e}")
            return None

        for t in torrents:
            tname = str(t.get("name", "")).strip().lower()
            thash = str(t.get("hash", "")).strip().lower()
            if thash and tname and (name == tname or name in tname or tname in name):
                return thash

        return None


# -----------------------------
# Arr client (Sonarr/Radarr)
# -----------------------------
@dataclass
class ArrConfig:
    name: str  # "sonarr" or "radarr"
    base_url: str
    api_key: str


class ArrClient:
    def __init__(self, cfg: ArrConfig):
        self.cfg = cfg
        self.sess = requests.Session()
        self.sess.headers.update(
            {
                "User-Agent": "arr-source-tagger/1.0",
                "X-Api-Key": cfg.api_key,
                "Content-Type": "application/json",
            }
        )

    def _url(self, path: str) -> str:
        return self.cfg.base_url.rstrip("/") + path

    def get_tags(self) -> List[dict]:
        r = self.sess.get(self._url("/api/v3/tag"), timeout=20)
        if r.status_code != 200:
            die(f"{self.cfg.name}: GET /api/v3/tag failed (HTTP {r.status_code}): {r.text.strip()}", 4)
        return r.json() if r.text.strip() else []

    def ensure_tag(self, label: str) -> int:
        label_norm = label.strip()
        tags = self.get_tags()
        for t in tags:
            if str(t.get("label", "")).strip().lower() == label_norm.lower():
                return int(t["id"])

        log("INFO", f"{self.cfg.name}: Tag '{label_norm}' does not exist yet. Creating it.")
        r = self.sess.post(self._url("/api/v3/tag"), data=json.dumps({"label": label_norm}), timeout=20)
        if r.status_code not in (200, 201):
            die(f"{self.cfg.name}: Failed to create tag '{label_norm}' (HTTP {r.status_code}): {r.text.strip()}", 4)
        data = r.json()
        return int(data["id"])

    def get_item(self, item_id: int) -> dict:
        if self.cfg.name == "sonarr":
            path = f"/api/v3/series/{item_id}"
        else:
            path = f"/api/v3/movie/{item_id}"
        r = self.sess.get(self._url(path), timeout=30)
        if r.status_code != 200:
            die(f"{self.cfg.name}: GET {path} failed (HTTP {r.status_code}): {r.text.strip()}", 4)
        return r.json()

    def update_item(self, item: dict) -> None:
        if self.cfg.name == "sonarr":
            path = "/api/v3/series"
        else:
            path = "/api/v3/movie"
        r = self.sess.put(self._url(path), data=json.dumps(item), timeout=60)
        if r.status_code not in (200, 202):
            die(f"{self.cfg.name}: PUT {path} failed (HTTP {r.status_code}): {r.text.strip()}", 4)


def parse_title(self, title: str) -> Optional[dict]:
    """Use Arr's parser to identify movie/series and season/episode info from a release title."""
    t = (title or "").strip()
    if not t:
        return None
    url = self._url("/api/v3/parse") + f"?title={quote(t)}"
    log("DEBUG", f"{self.cfg.name}: GET {url}")
    r = self.sess.get(url, timeout=30)
    if r.status_code != 200:
        log("DEBUG", f"{self.cfg.name}: parse failed (HTTP {r.status_code}) for title='{t}': {r.text.strip()}")
        return None
    try:
        return r.json()
    except Exception:
        return None

def add_tag(self, item_id: int, label: str) -> bool:
    """Adds a tag without removing any other tags. Returns True if a change was made."""
    item = self.get_item(item_id)
    title = item.get("title") or item.get("titleSlug") or f"ID:{item_id}"

    existing: List[int] = list(item.get("tags") or [])
    tag_id = self.ensure_tag(label)

    if tag_id in existing:
        log("DEBUG", f"{self.cfg.name}: Tag '{label}' already present on '{title}'.")
        return False

    existing.append(tag_id)
    item["tags"] = existing
    self.update_item(item)
    log("INFO", f"{self.cfg.name}: Added tag '{label}' to '{title}'.")
    return True

def remove_tag(self, item_id: int, label: str) -> bool:
    """Removes a tag without touching any other tags. Returns True if a change was made."""
    label_norm = (label or "").strip()
    if not label_norm:
        return False

    item = self.get_item(item_id)
    title = item.get("title") or item.get("titleSlug") or f"ID:{item_id}"

    tag_objects = self.get_tags()
    tag_id = None
    for t in tag_objects:
        if str(t.get("label", "")).strip().lower() == label_norm.lower():
            tag_id = int(t.get("id"))
            break

    if tag_id is None:
        log("DEBUG", f"{self.cfg.name}: Tag '{label_norm}' does not exist; nothing to remove on '{title}'.")
        return False

    existing: List[int] = list(item.get("tags") or [])
    if tag_id not in existing:
        log("DEBUG", f"{self.cfg.name}: Tag '{label_norm}' not present on '{title}'.")
        return False

    item["tags"] = [tid for tid in existing if int(tid) != int(tag_id)]
    self.update_item(item)
    log("INFO", f"{self.cfg.name}: Removed tag '{label_norm}' from '{title}'.")
    return True

    # def apply_source_tag(self, item_id: int, chosen_tag: str, source_prefixes: List[str]) -> None:
    #     item = self.get_item(item_id)
    #     title = item.get("title") or item.get("titleSlug") or f"ID:{item_id}"
    #     existing_tag_ids: List[int] = list(item.get("tags") or [])

    #     tag_objects = self.get_tags()
    #     id_to_label = {int(t["id"]): str(t.get("label", "")) for t in tag_objects if "id" in t}

    #     def is_source_label(lbl: str) -> bool:
    #         l = lbl.strip().lower()
    #         for p in source_prefixes:
    #             p2 = p.strip().lower()
    #             if not p2:
    #                 continue
    #             if p2.endswith("-") and l.startswith(p2):
    #                 return True
    #             if l == p2:
    #                 return True
    #         return False

    #     removed: List[str] = []
    #     kept_ids: List[int] = []
    #     for tid in existing_tag_ids:
    #         lbl = id_to_label.get(int(tid), "")
    #         if lbl and is_source_label(lbl):
    #             removed.append(lbl)
    #         else:
    #             kept_ids.append(int(tid))

    #     chosen_id = self.ensure_tag(chosen_tag)

    #     new_ids = kept_ids[:]
    #     if chosen_id not in new_ids:
    #         new_ids.append(chosen_id)

    #     item["tags"] = new_ids
    #     self.update_item(item)

    #     log(
    #         "INFO",
    #         f"{self.cfg.name}: Applied source tag '{chosen_tag}' to '{title}'. "
    #         f"Removed source tags: {removed if removed else '(none)'}; kept other tags: {len(kept_ids)}.",
    #     )

    # def fetch_history(self, page_size: int = 1000) -> List[dict]:
    #     url = self._url(f"/api/v3/history?page=1&pageSize={page_size}&sortKey=date&sortDirection=descending")
    #     log("DEBUG", f"{self.cfg.name}: GET {url}")
    #     r = self.sess.get(url, timeout=120)
    #     if r.status_code != 200:
    #         die(f"{self.cfg.name}: GET /api/v3/history failed (HTTP {r.status_code}): {r.text.strip()}", 4)
    #     data = r.json() if r.text.strip() else {}
    #     # Arr returns {page, pageSize, records, totalRecords}
    #     recs = data.get("records")
    #     if isinstance(recs, list):
    #         return recs
    #     # Some versions may return list directly
    #     if isinstance(data, list):
    #         return data
    #     return []




# -----------------------------
# ArrClient: optional helpers (v0.4)
# Added as monkeypatched methods to avoid indentation/scope regressions.
# -----------------------------
def _arr_parse_title(self, title: str) -> Optional[dict]:
    """Use Arr's /api/v3/parse endpoint to interpret a release title."""
    t = (title or "").strip()
    if not t:
        return None
    url = self._url("/api/v3/parse") + f"?title={quote(t)}"
    log("DEBUG", f"{self.cfg.name}: GET {url}")
    r = self.sess.get(url, timeout=30)
    if r.status_code != 200:
        log("DEBUG", f"{self.cfg.name}: parse failed (HTTP {r.status_code}) for title='{t}': {r.text.strip()}")
        return None
    try:
        return r.json()
    except Exception:
        return None


def _arr_add_tag(self, item_id: int, label: str) -> bool:
    """Adds a tag without removing any other tags. Returns True if a change was made."""
    item = self.get_item(item_id)
    title = item.get("title") or item.get("titleSlug") or f"ID:{item_id}"

    existing: List[int] = list(item.get("tags") or [])
    tag_id = self.ensure_tag(label)

    if tag_id in existing:
        log("DEBUG", f"{self.cfg.name}: Tag '{label}' already present on '{title}'.")
        return False

    existing.append(tag_id)
    item["tags"] = existing
    self.update_item(item)
    log("INFO", f"{self.cfg.name}: Added tag '{label}' to '{title}'.")
    return True


def _arr_remove_tag(self, item_id: int, label: str) -> bool:
    """Removes a tag without affecting others. Returns True if a change was made."""
    item = self.get_item(item_id)
    title = item.get("title") or item.get("titleSlug") or f"ID:{item_id}"

    label_norm = (label or "").strip()
    if not label_norm:
        return False

    tag_id: Optional[int] = None
    for t in self.get_tags():
        if str(t.get("label", "")).strip().lower() == label_norm.lower():
            tag_id = int(t.get("id"))
            break

    if tag_id is None:
        log("DEBUG", f"{self.cfg.name}: Tag '{label_norm}' does not exist; nothing to remove on '{title}'.")
        return False

    existing: List[int] = list(item.get("tags") or [])
    if tag_id not in existing:
        log("DEBUG", f"{self.cfg.name}: Tag '{label_norm}' not present on '{title}'.")
        return False

    item["tags"] = [tid for tid in existing if int(tid) != int(tag_id)]
    self.update_item(item)
    log("INFO", f"{self.cfg.name}: Removed tag '{label_norm}' from '{title}'.")
    return True

def _arr_apply_source_tag(self, item_id: int, chosen_tag: str, source_prefixes: List[str]) -> None:
    """
    Remove existing source tags (pt-* and/or 'public' etc. via source_prefixes) and apply chosen_tag.
    Keeps all other tags intact.
    """
    chosen_tag = (chosen_tag or "").strip()
    if not chosen_tag:
        raise ValueError("chosen_tag is empty")

    item = self.get_item(item_id)
    title = item.get("title") or item.get("titleSlug") or f"ID:{item_id}"
    existing_tag_ids: List[int] = list(item.get("tags") or [])

    tag_objects = self.get_tags()
    id_to_label = {int(t["id"]): str(t.get("label", "")) for t in tag_objects if "id" in t}

    def is_source_label(lbl: str) -> bool:
        l = (lbl or "").strip().lower()
        for p in source_prefixes:
            p2 = (p or "").strip().lower()
            if not p2:
                continue
            if p2.endswith("-") and l.startswith(p2):
                return True
            if l == p2:
                return True
        return False

    removed: List[str] = []
    kept_ids: List[int] = []
    for tid in existing_tag_ids:
        lbl = id_to_label.get(int(tid), "")
        if lbl and is_source_label(lbl):
            removed.append(lbl)
        else:
            kept_ids.append(int(tid))

    chosen_id = self.ensure_tag(chosen_tag)
    if chosen_id not in kept_ids:
        kept_ids.append(chosen_id)

    item["tags"] = kept_ids
    self.update_item(item)

    log(
        "INFO",
        f"{self.cfg.name}: Applied source tag '{chosen_tag}' to '{title}'. "
        f"Removed source tags: {removed if removed else '(none)'}; kept other tags: {len(kept_ids)}.",
    )
    
def _arr_fetch_history(self, page_size: int = 1000) -> List[dict]:
    """
    Fetch Arr history (Radarr/Sonarr) in descending date order.

    Returns a list of history records (the "records" array).
    """
    url = self._url(f"/api/v3/history?page=1&pageSize={page_size}&sortKey=date&sortDirection=descending")
    log("DEBUG", f"{self.cfg.name}: GET {url}")
    r = self.sess.get(url, timeout=120)
    if r.status_code != 200:
        die(f"{self.cfg.name}: GET /api/v3/history failed (HTTP {r.status_code}): {r.text.strip()}", 4)

    data = r.json() if r.text.strip() else {}
    # Typical Arr response: { page, pageSize, sortKey, sortDirection, totalRecords, records: [...] }
    recs = data.get("records")
    if isinstance(recs, list):
        return recs

    # Some builds may return list directly
    if isinstance(data, list):
        return data

    return []



# Monkeypatch only if missing (keeps compatibility with older/newer versions of this file)
if not hasattr(ArrClient, "parse_title"):
    ArrClient.parse_title = _arr_parse_title  # type: ignore[attr-defined]
if not hasattr(ArrClient, "add_tag"):
    ArrClient.add_tag = _arr_add_tag  # type: ignore[attr-defined]
if not hasattr(ArrClient, "remove_tag"):
    ArrClient.remove_tag = _arr_remove_tag  # type: ignore[attr-defined]
if not hasattr(ArrClient, "apply_source_tag"):
    ArrClient.apply_source_tag = _arr_apply_source_tag  # type: ignore[attr-defined]
if not hasattr(ArrClient, "fetch_history"):
    ArrClient.fetch_history = _arr_fetch_history  # type: ignore[attr-defined]
    
    

# -----------------------------
# Core tagging logic (qBittorrent tracker domains)
# -----------------------------
@dataclass
class ArrEvent:
    arr: str  # "sonarr" or "radarr"
    item_id: int
    download_id: str
    is_upgrade: bool
    title_hint: Optional[str] = None


def detect_arr_event_from_env() -> Optional[ArrEvent]:
    # Sonarr
    if os.getenv("SONARR_EVENTTYPE"):
        eventtype = (os.getenv("SONARR_EVENTTYPE") or "").strip().lower()
        if eventtype not in ("download", "test"):
            log("INFO", f"Sonarr eventtype '{eventtype}' not handled. Exiting.")
            return None

        sid = os.getenv("SONARR_SERIES_ID") or os.getenv("SONARR_SERIESID")
        did = os.getenv("SONARR_DOWNLOAD_ID") or os.getenv("SONARR_DOWNLOADID") or ""
        isup = (os.getenv("SONARR_ISUPGRADE") or "false").strip().lower() == "true"
        title_hint = os.getenv("SONARR_RELEASE_TITLE") or os.getenv("SONARR_SERIES_TITLE") or None

        if eventtype == "test":
            log("INFO", "Sonarr Test event received. Configuration looks reachable from Sonarr.")
            return None

        if not sid or not sid.isdigit():
            die("SONARR_SERIES_ID is missing or not numeric.", 2)
        return ArrEvent(arr="sonarr", item_id=int(sid), download_id=did, is_upgrade=isup, title_hint=title_hint)

    # Radarr
    if os.getenv("RADARR_EVENTTYPE"):
        eventtype = (os.getenv("RADARR_EVENTTYPE") or "").strip().lower()
        if eventtype not in ("download", "test"):
            log("INFO", f"Radarr eventtype '{eventtype}' not handled. Exiting.")
            return None

        mid = os.getenv("RADARR_MOVIE_ID") or os.getenv("RADARR_MOVIEID")
        did = os.getenv("RADARR_DOWNLOAD_ID") or os.getenv("RADARR_DOWNLOADID") or ""
        isup = (os.getenv("RADARR_ISUPGRADE") or "false").strip().lower() == "true"
        title_hint = os.getenv("RADARR_RELEASE_TITLE") or os.getenv("RADARR_MOVIE_TITLE") or None

        if eventtype == "test":
            log("INFO", "Radarr Test event received. Configuration looks reachable from Radarr.")
            return None

        if not mid or not mid.isdigit():
            die("RADARR_MOVIE_ID is missing or not numeric.", 2)
        return ArrEvent(arr="radarr", item_id=int(mid), download_id=did, is_upgrade=isup, title_hint=title_hint)

    log("INFO", "No SONARR_EVENTTYPE or RADARR_EVENTTYPE found. Nothing to do.")
    return None


def choose_source_tag(
    qbit: QbitClient,
    torrent_hash: str,
    private_trackers: Dict[str, str],
    public_tag: str,
) -> Tuple[str, List[str]]:
    trackers = qbit.trackers(torrent_hash)

    domains: List[str] = []
    candidates: List[Tuple[int, str]] = []
    for tr in trackers:
        url = str(tr.get("url", "")).strip()
        tier = int(tr.get("tier", 0))
        dom = parse_domain(url)
        if not dom:
            continue
        candidates.append((tier, dom))

    candidates.sort(key=lambda x: x[0])

    for _, dom in candidates:
        if dom not in domains:
            domains.append(dom)

    for dom in domains:
        if dom.lower() in private_trackers:
            return private_trackers[dom.lower()], domains

    return public_tag, domains


def run_arr_script_mode() -> None:
    public_tag = os.getenv("PUBLIC_TAG", "public").strip() or "public"
    mapping_file = os.getenv("PRIVATE_TRACKERS_FILE", "./private_trackers.yml")
    state_file = os.getenv("STATE_FILE", "./state.json")
    source_prefixes = [p.strip() for p in os.getenv("SOURCE_TAG_PREFIXES", "pt-,public").split(",") if p.strip()]

    private_trackers, _private_indexers = load_private_config(mapping_file)

    ev = detect_arr_event_from_env()
    if ev is None:
        return

    log("INFO", f"Arr event received: arr={ev.arr}, item_id={ev.item_id}, is_upgrade={ev.is_upgrade}")

    qcfg = QbitConfig(
        base_url=os.getenv("QBIT_URL", "").strip(),
        username=os.getenv("QBIT_USERNAME", "").strip(),
        password=os.getenv("QBIT_PASSWORD", "").strip(),
        verify_tls=env_bool("QBIT_VERIFY_TLS", True),
    )
    if not qcfg.base_url or not qcfg.username or not qcfg.password:
        die("Missing qBittorrent config. Set QBIT_URL, QBIT_USERNAME, QBIT_PASSWORD.", 2)

    qbit = QbitClient(qcfg)
    qbit.login()

    torrent_hash = qbit.resolve_hash(ev.download_id, fallback_name=ev.title_hint)
    if not torrent_hash:
        die(
            "Could not resolve torrent hash. Ensure Arr passes *_DOWNLOAD_ID (torrent hash). "
            "If not, ensure *_RELEASE_TITLE is available for fallback matching.",
            5,
        )

    log("INFO", f"Resolved torrent hash: {torrent_hash}")

    chosen_tag, domains = choose_source_tag(qbit, torrent_hash, private_trackers, public_tag)
    log("INFO", f"Tracker domains found: {summarize_domains(domains)}")
    log("INFO", f"Chosen source tag: {chosen_tag}")

    state = load_state(state_file)
    prev = state.get(torrent_hash)
    if prev and prev == chosen_tag:
        log("DEBUG", f"State: torrent hash already mapped to '{prev}' previously. Continuing (idempotent).")
    state[torrent_hash] = chosen_tag
    save_state(state_file, state)

    if ev.arr == "sonarr":
        url = os.getenv("SONARR_URL", "").strip()
        key = os.getenv("SONARR_API_KEY", "").strip()
        if not url or not key:
            die("Missing Sonarr config. Set SONARR_URL and SONARR_API_KEY.", 2)
        arr = ArrClient(ArrConfig(name="sonarr", base_url=url, api_key=key))
        arr.apply_source_tag(ev.item_id, chosen_tag, source_prefixes)
    else:
        url = os.getenv("RADARR_URL", "").strip()
        key = os.getenv("RADARR_API_KEY", "").strip()
        if not url or not key:
            die("Missing Radarr config. Set RADARR_URL and RADARR_API_KEY.", 2)
        arr = ArrClient(ArrConfig(name="radarr", base_url=url, api_key=key))
        arr.apply_source_tag(ev.item_id, chosen_tag, source_prefixes)

    log("INFO", "Done.")


# -----------------------------
# History backfill logic
# -----------------------------
def history_event_is_grabbed(evt: dict) -> bool:
    return str(evt.get("eventType") or "").strip().lower() == "grabbed"


def history_event_is_success_import(evt: dict) -> bool:
    # Works for both Radarr and Sonarr variants:
    # - downloadFolderImported
    # - movieFileImported / episodeFileImported
    # - ...Imported
    et = str(evt.get("eventType") or "").strip().lower()
    return "imported" in et and "failed" not in et


def extract_history_indexer_name(evt: dict) -> str:
    # Your examples show indexer is under evt.data.indexer for grabbed
    data = evt.get("data") or {}
    if isinstance(data, dict):
        idx = data.get("indexer")
        if idx:
            return str(idx)
    # fallback (some versions use top-level fields)
    if evt.get("indexer"):
        return str(evt.get("indexer"))
    return ""


def extract_history_domains(evt: dict) -> List[str]:
    """
    Use URLs that often contain the tracker domain:
      - nzbInfoUrl
      - guid
      - downloadUrl
    We treat them generically as "source URL(s)".
    """
    out: List[str] = []
    data = evt.get("data") or {}
    if not isinstance(data, dict):
        return out

    for key in ("nzbInfoUrl", "guid", "downloadUrl"):
        v = data.get(key)
        if not v:
            continue
        s = str(v).strip()
        # sometimes Sonarr prefixes "PUSH-"
        if s.lower().startswith("push-"):
            s = s[5:]
        dom = parse_domain(s)
        if dom and dom not in out:
            out.append(dom)
    return out


def choose_tag_from_history_grabbed(
    grabbed_evt: dict,
    private_trackers: Dict[str, str],
    private_indexers: Dict[str, str],
    public_tag: str,
) -> Tuple[str, str]:
    """
    Returns (tag, reason).
    Prefer explicit private_indexers mapping; fallback to domain mapping via URLs in grabbed data.
    """
    idx_raw = extract_history_indexer_name(grabbed_evt)
    idx_norm = normalize_indexer_name(idx_raw)
    if idx_norm and idx_norm in private_indexers:
        return private_indexers[idx_norm], f"private_indexers:{idx_norm}"

    domains = extract_history_domains(grabbed_evt)
    for dom in domains:
        if dom in private_trackers:
            return private_trackers[dom], f"private_trackers:{dom}"

    # still public
    if idx_norm:
        return public_tag, f"public:indexer={idx_norm}"
    if domains:
        return public_tag, f"public:domains={','.join(domains[:2])}"
    return public_tag, "public:no-hints"


def build_grabbed_index(records: List[dict]) -> Dict[str, List[dict]]:
    """
    downloadId -> [grabbed events (with date)]
    """
    out: Dict[str, List[dict]] = {}
    for r in records:
        if not history_event_is_grabbed(r):
            continue
        did = str(r.get("downloadId") or "").strip()
        if not did:
            continue
        out.setdefault(did, []).append(r)

    # sort each list by date desc
    for did, lst in out.items():
        lst.sort(
            key=lambda e: parse_iso_utc(str(e.get("date") or "")) or datetime(1970, 1, 1, tzinfo=timezone.utc),
            reverse=True,
        )
    return out


def pick_best_grabbed_for_import(grabbed_list: List[dict], import_dt: Optional[datetime]) -> Optional[dict]:
    if not grabbed_list:
        return None
    if not import_dt:
        return grabbed_list[0]

    # choose newest grabbed that is <= import date; else newest grabbed overall
    for g in grabbed_list:
        gdt = parse_iso_utc(str(g.get("date") or ""))
        if gdt and gdt <= import_dt:
            return g
    return grabbed_list[0]


def backfill_history_radarr(
    radarr: ArrClient,
    qbit: Optional[QbitClient],
    private_trackers: Dict[str, str],
    private_indexers: Dict[str, str],
    public_tag: str,
    source_prefixes: List[str],
    dry_run: bool,
    limit: int,
    only_missing: bool,
    reapply: bool,
    page_size: int,
) -> dict:
    records = radarr.fetch_history(page_size=page_size)
    grabbed_idx = build_grabbed_index(records)

    # Build list of latest successful imports per movieId
    latest_import_by_movie: Dict[int, dict] = {}
    for r in records:
        if not history_event_is_success_import(r):
            continue
        mid = r.get("movieId")
        if not isinstance(mid, int):
            continue
        did = str(r.get("downloadId") or "").strip()
        if not did:
            continue
        dt = parse_iso_utc(str(r.get("date") or ""))
        prev = latest_import_by_movie.get(mid)
        if not prev:
            latest_import_by_movie[mid] = r
            continue
        prev_dt = parse_iso_utc(str(prev.get("date") or "")) or datetime(1970, 1, 1, tzinfo=timezone.utc)
        if dt and dt > prev_dt:
            latest_import_by_movie[mid] = r

    processed = 0
    tagged = 0
    skipped = 0
    errors = 0
    no_grab = 0

    # cache tag labels map for "only_missing"
    tag_objs = radarr.get_tags()
    id_to_label = {int(t["id"]): str(t.get("label", "")) for t in tag_objs if "id" in t}

    # newest first
    items = list(latest_import_by_movie.items())
    items.sort(
        key=lambda kv: parse_iso_utc(str(kv[1].get("date") or "")) or datetime(1970, 1, 1, tzinfo=timezone.utc),
        reverse=True,
    )

    for mid, imp in items:
        if limit and processed >= limit:
            break
        processed += 1

        # skip if already tagged and only_missing
        try:
            movie = radarr.get_item(mid)
        except SystemExit:
            errors += 1
            continue

        labels = [id_to_label.get(int(tid), "") for tid in (movie.get("tags") or [])]
        already_has_source = has_source_tag(labels, source_prefixes)

        title = str(movie.get("title") or f"ID:{mid}")

        if only_missing and already_has_source and not reapply:
            skipped += 1
            log("DEBUG", f"radarr history backfill: skip '{title}' (already has source tag)")
            continue

        did = str(imp.get("downloadId") or "").strip()
        imp_dt = parse_iso_utc(str(imp.get("date") or ""))

        grabbed_list = grabbed_idx.get(did, [])
        grabbed = pick_best_grabbed_for_import(grabbed_list, imp_dt)

        chosen_tag = public_tag
        reason = "public:default"

        if grabbed:
            chosen_tag, reason = choose_tag_from_history_grabbed(grabbed, private_trackers, private_indexers, public_tag)
        else:
            no_grab += 1
            # Optional fallback: if we still have qbit and downloadId looks like hash, try qbit trackers
            if qbit is not None and is_torrent_hash(did):
               try:
                  chosen_tag, domains = choose_source_tag(qbit, did.lower(), private_trackers, public_tag)
                  reason = f"qbit-fallback:{summarize_domains(domains)}"
               except SystemExit:
                 # Never abort history backfill due to qBittorrent issues (404/timeouts/etc.)
                 chosen_tag = public_tag
                 reason = "qbit-fallback:unavailable"


        log("INFO", f"radarr history backfill: '{title}' -> tag={chosen_tag} ({reason})")

        if dry_run:
            continue

        try:
            radarr.apply_source_tag(mid, chosen_tag, source_prefixes)
            tagged += 1
        except SystemExit:
            errors += 1

    return {
        "arr": "radarr",
        "dry_run": dry_run,
        "processed": processed,
        "tagged": tagged,
        "skipped": skipped,
        "no_grab_match": no_grab,
        "errors": errors,
        "history_records": len(records),
        "imports_considered": len(latest_import_by_movie),
    }


def backfill_history_sonarr(
    sonarr: ArrClient,
    qbit: Optional[QbitClient],
    private_trackers: Dict[str, str],
    private_indexers: Dict[str, str],
    public_tag: str,
    source_prefixes: List[str],
    dry_run: bool,
    limit: int,
    only_missing: bool,
    reapply: bool,
    page_size: int,
) -> dict:
    records = sonarr.fetch_history(page_size=page_size)
    grabbed_idx = build_grabbed_index(records)

    # "less noise": latest successful import per SERIES (not per episode)
    latest_import_by_series: Dict[int, dict] = {}
    for r in records:
        if not history_event_is_success_import(r):
            continue
        sid = r.get("seriesId")
        if not isinstance(sid, int):
            continue
        did = str(r.get("downloadId") or "").strip()
        if not did:
            continue
        dt = parse_iso_utc(str(r.get("date") or ""))
        prev = latest_import_by_series.get(sid)
        if not prev:
            latest_import_by_series[sid] = r
            continue
        prev_dt = parse_iso_utc(str(prev.get("date") or "")) or datetime(1970, 1, 1, tzinfo=timezone.utc)
        if dt and dt > prev_dt:
            latest_import_by_series[sid] = r

    processed = 0
    tagged = 0
    skipped = 0
    errors = 0
    no_grab = 0

    tag_objs = sonarr.get_tags()
    id_to_label = {int(t["id"]): str(t.get("label", "")) for t in tag_objs if "id" in t}

    items = list(latest_import_by_series.items())
    items.sort(
        key=lambda kv: parse_iso_utc(str(kv[1].get("date") or "")) or datetime(1970, 1, 1, tzinfo=timezone.utc),
        reverse=True,
    )

    for sid, imp in items:
        if limit and processed >= limit:
            break
        processed += 1

        try:
            series = sonarr.get_item(sid)
        except SystemExit:
            errors += 1
            continue

        title = str(series.get("title") or f"ID:{sid}")

        labels = [id_to_label.get(int(tid), "") for tid in (series.get("tags") or [])]
        already_has_source = has_source_tag(labels, source_prefixes)

        if only_missing and already_has_source and not reapply:
            skipped += 1
            log("DEBUG", f"sonarr history backfill: skip '{title}' (already has source tag)")
            continue

        did = str(imp.get("downloadId") or "").strip()
        imp_dt = parse_iso_utc(str(imp.get("date") or ""))

        grabbed_list = grabbed_idx.get(did, [])
        grabbed = pick_best_grabbed_for_import(grabbed_list, imp_dt)

        chosen_tag = public_tag
        reason = "public:default"

        if grabbed:
            chosen_tag, reason = choose_tag_from_history_grabbed(grabbed, private_trackers, private_indexers, public_tag)
        else:
            no_grab += 1
            if qbit is not None and is_torrent_hash(did):
               try:
                  chosen_tag, domains = choose_source_tag(qbit, did.lower(), private_trackers, public_tag)
                  reason = f"qbit-fallback:{summarize_domains(domains)}"
               except SystemExit:
                 # Never abort history backfill due to qBittorrent issues (404/timeouts/etc.)
                 chosen_tag = public_tag
                 reason = "qbit-fallback:unavailable"

        log("INFO", f"sonarr history backfill: '{title}' -> tag={chosen_tag} ({reason})")

        if dry_run:
            continue

        try:
            sonarr.apply_source_tag(sid, chosen_tag, source_prefixes)
            tagged += 1
        except SystemExit:
            errors += 1

    return {
        "arr": "sonarr",
        "dry_run": dry_run,
        "processed": processed,
        "tagged": tagged,
        "skipped": skipped,
        "no_grab_match": no_grab,
        "errors": errors,
        "history_records": len(records),
        "imports_considered": len(latest_import_by_series),
    }


def run_backfill_history(
    arr_target: str,
    dry_run: bool,
    limit: int,
    only_missing: bool,
    reapply: bool,
    page_size: int,
) -> dict:
    public_tag = os.getenv("PUBLIC_TAG", "public").strip() or "public"
    mapping_file = os.getenv("PRIVATE_TRACKERS_FILE", "./private_trackers.yml")
    source_prefixes = [p.strip() for p in os.getenv("SOURCE_TAG_PREFIXES", "pt-,public").split(",") if p.strip()]

    private_trackers, private_indexers = load_private_config(mapping_file)

    # Optional qBittorrent fallback (only used when grabbed can't be found)
    qbit: Optional[QbitClient] = None
    if env_bool("HISTORY_FALLBACK_QBIT", True):
        qurl = os.getenv("QBIT_URL", "").strip()
        quser = os.getenv("QBIT_USERNAME", "").strip()
        qpass = os.getenv("QBIT_PASSWORD", "").strip()
        if qurl and quser and qpass:
            qcfg = QbitConfig(
                base_url=qurl,
                username=quser,
                password=qpass,
                verify_tls=env_bool("QBIT_VERIFY_TLS", True),
            )
            qbit = QbitClient(qcfg)
            qbit.login()
        else:
            log("DEBUG", "History fallback qBittorrent disabled (missing QBIT_* env vars).")

    results: List[dict] = []

    if arr_target in ("radarr", "both"):
        rurl = os.getenv("RADARR_URL", "").strip()
        rkey = os.getenv("RADARR_API_KEY", "").strip()
        if not rurl or not rkey:
            die("Missing Radarr config. Set RADARR_URL and RADARR_API_KEY.", 2)
        rad = ArrClient(ArrConfig(name="radarr", base_url=rurl, api_key=rkey))
        results.append(
            backfill_history_radarr(
                radarr=rad,
                qbit=qbit,
                private_trackers=private_trackers,
                private_indexers=private_indexers,
                public_tag=public_tag,
                source_prefixes=source_prefixes,
                dry_run=dry_run,
                limit=limit,
                only_missing=only_missing,
                reapply=reapply,
                page_size=page_size,
            )
        )

    if arr_target in ("sonarr", "both"):
        surl = os.getenv("SONARR_URL", "").strip()
        skey = os.getenv("SONARR_API_KEY", "").strip()
        if not surl or not skey:
            die("Missing Sonarr config. Set SONARR_URL and SONARR_API_KEY.", 2)
        son = ArrClient(ArrConfig(name="sonarr", base_url=surl, api_key=skey))
        results.append(
            backfill_history_sonarr(
                sonarr=son,
                qbit=qbit,
                private_trackers=private_trackers,
                private_indexers=private_indexers,
                public_tag=public_tag,
                source_prefixes=source_prefixes,
                dry_run=dry_run,
                limit=limit,
                only_missing=only_missing,
                reapply=reapply,
                page_size=page_size,
            )
        )

    return {"status": "ok", "dry_run": dry_run, "results": results}



# -----------------------------
# Uploading sync (protect active uploads) - stateful
# -----------------------------
def build_qbit_upload_client() -> QbitClient:
    """
    Uses QBIT_UPLOAD_* if set, otherwise falls back to primary QBIT_*.
    """
    base_url = (os.getenv("QBIT_UPLOAD_URL") or "").strip() or (os.getenv("QBIT_URL") or "").strip()
    username = (os.getenv("QBIT_UPLOAD_USERNAME") or "").strip() or (os.getenv("QBIT_USERNAME") or "").strip()
    password = (os.getenv("QBIT_UPLOAD_PASSWORD") or "").strip() or (os.getenv("QBIT_PASSWORD") or "").strip()

    if os.getenv("QBIT_UPLOAD_VERIFY_TLS") is not None:
        verify_tls = env_bool("QBIT_UPLOAD_VERIFY_TLS", True)
    else:
        verify_tls = env_bool("QBIT_VERIFY_TLS", True)

    if not base_url or not username or not password:
        die(
            "Missing upload qBittorrent config. "
            "Set QBIT_UPLOAD_URL/QBIT_UPLOAD_USERNAME/QBIT_UPLOAD_PASSWORD or ensure primary QBIT_* is configured.",
            2,
        )

    qc = QbitClient(QbitConfig(base_url=base_url, username=username, password=password, verify_tls=verify_tls))
    qc.login()
    return qc


def is_sonarr_full_season(parsed: dict) -> bool:
    """
    Determine if Sonarr /api/v3/parse result represents a whole-season pack.

    Sonarr versions differ: some include a populated top-level "episodes" list even for season packs.
    The most reliable signals are in parsedEpisodeInfo:
      - fullSeason == true
      - releaseType == "seasonPack"
      - seasonNumber present
      - episodeNumbers empty (common for packs)
    """
    if not isinstance(parsed, dict):
        return False

    pei = parsed.get("parsedEpisodeInfo")
    if isinstance(pei, dict):
        if pei.get("fullSeason") is True:
            return True

        rt = str(pei.get("releaseType") or "").strip().lower()
        if rt == "seasonpack":
            return True

        season = pei.get("seasonNumber")
        if season is None:
            return False

        eps = pei.get("episodeNumbers")
        if isinstance(eps, list) and len(eps) == 0:
            return True

        return False

    # Legacy fallback
    season = parsed.get("seasonNumber")
    if season is None:
        return False
    eps = parsed.get("episodeNumbers")
    if isinstance(eps, list) and len(eps) > 0:
        return False
    return True


def looks_like_series_title(name: str) -> bool:
    n = (name or "")
    return bool(
        re.search(r"\bS\d{1,2}\b", n, flags=re.I)
        or re.search(r"\bS\d{1,2}E\d{1,3}\b", n, flags=re.I)
        or re.search(r"\bSeason\s*\d{1,3}\b", n, flags=re.I)
    )


def looks_like_movie_title(name: str) -> bool:
    n = (name or "")
    has_year = bool(re.search(r"\b(19\d{2}|20\d{2})\b", n))
    has_series = looks_like_series_title(n)
    return has_year and not has_series


def run_backfill_uploading(arr_target: str, dry_run: bool, limit: int) -> dict:
    uploading_tag = (os.getenv("UPLOADING_TAG") or "uploading").strip() or "uploading"
    category = (os.getenv("UPLOAD_QBIT_CATEGORY") or "tracker_own_uploads").strip() or "tracker_own_uploads"

    state_file = (os.getenv("UPLOADING_STATE_FILE") or "./data/uploading_state.json").strip() or "./data/uploading_state.json"
    remove_when_gone = env_bool("UPLOADING_SYNC_REMOVE_TAG", True)
    try:
        unmatched_ttl_hours = int(os.getenv("UPLOADING_UNMATCHED_TTL_HOURS", "24") or "24")
    except Exception:
        unmatched_ttl_hours = 24

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    qbit = build_qbit_upload_client()
    torrents = qbit.torrents_info(category=category)

    st = load_uploading_state(state_file) or {}
    tracked = st.get("tracked") if isinstance(st.get("tracked"), dict) else {}
    unmatched = st.get("unmatched") if isinstance(st.get("unmatched"), dict) else {}

    current_by_hash: Dict[str, dict] = {}
    for t in torrents:
        th = str(t.get("hash") or "").strip().lower()
        if th:
            current_by_hash[th] = t

    log("INFO", f"uploading sync: found {len(current_by_hash)} torrents in category '{category}'")

    radarr: Optional[ArrClient] = None
    sonarr: Optional[ArrClient] = None

    if arr_target in ("radarr", "both"):
        rurl = os.getenv("RADARR_URL", "").strip()
        rkey = os.getenv("RADARR_API_KEY", "").strip()
        if rurl and rkey:
            radarr = ArrClient(ArrConfig(name="radarr", base_url=rurl, api_key=rkey))
        else:
            log("WARNING", "uploading sync: arr includes radarr but RADARR_URL/RADARR_API_KEY not configured.")

    if arr_target in ("sonarr", "both"):
        surl = os.getenv("SONARR_URL", "").strip()
        skey = os.getenv("SONARR_API_KEY", "").strip()
        if surl and skey:
            sonarr = ArrClient(ArrConfig(name="sonarr", base_url=surl, api_key=skey))
        else:
            log("WARNING", "uploading sync: arr includes sonarr but SONARR_URL/SONARR_API_KEY not configured.")

    processed = 0
    new_tracked = 0
    already_tracked = 0
    skipped_unmatched = 0
    parse_calls = 0
    tagged_radarr = 0
    tagged_sonarr = 0
    removed_tags = 0
    errors = 0

    def unmatched_fresh(entry: dict) -> bool:
        try:
            ls = parse_iso_utc(str(entry.get("last_seen") or "")) if isinstance(entry, dict) else None
            if not ls:
                return False
            age_h = (datetime.now(timezone.utc) - ls).total_seconds() / 3600.0
            return age_h <= float(unmatched_ttl_hours)
        except Exception:
            return False

    for th, t in current_by_hash.items():
        if limit and processed >= limit:
            break
        processed += 1

        name = str(t.get("name") or "").strip()
        if not name:
            continue

        if th in tracked and isinstance(tracked.get(th), dict):
            already_tracked += 1
            tracked[th]["last_seen"] = now
            if tracked[th].get("name") != name:
                tracked[th]["name"] = name
            continue

        if th in unmatched and isinstance(unmatched.get(th), dict) and unmatched_fresh(unmatched[th]) and unmatched[th].get("name") == name:
            skipped_unmatched += 1
            unmatched[th]["last_seen"] = now
            continue

        want_sonarr_first = looks_like_series_title(name) and sonarr is not None
        want_radarr_first = looks_like_movie_title(name) and radarr is not None

        matched = False

        if want_sonarr_first and sonarr is not None:
            parse_calls += 1
            p = sonarr.parse_title(name)
            if p and isinstance(p.get("series"), dict) and isinstance(p["series"].get("id"), int):
                sid = int(p["series"]["id"])
                if is_sonarr_full_season(p):
                    log("INFO", f"uploading sync: sonarr match seriesId={sid} (full season) from '{name}'")
                    if not dry_run:
                        try:
                            if sonarr.add_tag(sid, uploading_tag):
                                tagged_sonarr += 1
                        except SystemExit:
                            errors += 1
                    tracked[th] = {"name": name, "arr": "sonarr", "arr_id": sid, "tag": uploading_tag, "last_seen": now}
                    new_tracked += 1
                    matched = True
                else:
                    log("DEBUG", f"uploading sync: sonarr match but not full season, skipping '{name}'")
                    unmatched[th] = {"name": name, "reason": "sonarr:not_full_season", "last_seen": now}
                    skipped_unmatched += 1
                    matched = True

        if (not matched) and want_radarr_first and radarr is not None:
            parse_calls += 1
            p = radarr.parse_title(name)
            if p and isinstance(p.get("movie"), dict) and isinstance(p["movie"].get("id"), int):
                mid = int(p["movie"]["id"])
                log("INFO", f"uploading sync: radarr match movieId={mid} from '{name}'")
                if not dry_run:
                    try:
                        if radarr.add_tag(mid, uploading_tag):
                            tagged_radarr += 1
                    except SystemExit:
                        errors += 1
                tracked[th] = {"name": name, "arr": "radarr", "arr_id": mid, "tag": uploading_tag, "last_seen": now}
                new_tracked += 1
                matched = True

        if not matched and radarr is not None and sonarr is not None:
            parse_calls += 1
            pr = radarr.parse_title(name)
            if pr and isinstance(pr.get("movie"), dict) and isinstance(pr["movie"].get("id"), int):
                mid = int(pr["movie"]["id"])
                log("INFO", f"uploading sync: radarr match movieId={mid} from '{name}'")
                if not dry_run:
                    try:
                        if radarr.add_tag(mid, uploading_tag):
                            tagged_radarr += 1
                    except SystemExit:
                        errors += 1
                tracked[th] = {"name": name, "arr": "radarr", "arr_id": mid, "tag": uploading_tag, "last_seen": now}
                new_tracked += 1
                matched = True
            else:
                parse_calls += 1
                ps = sonarr.parse_title(name)
                if ps and isinstance(ps.get("series"), dict) and isinstance(ps["series"].get("id"), int):
                    sid = int(ps["series"]["id"])
                    if is_sonarr_full_season(ps):
                        log("INFO", f"uploading sync: sonarr match seriesId={sid} (full season) from '{name}'")
                        if not dry_run:
                            try:
                                if sonarr.add_tag(sid, uploading_tag):
                                    tagged_sonarr += 1
                            except SystemExit:
                                errors += 1
                        tracked[th] = {"name": name, "arr": "sonarr", "arr_id": sid, "tag": uploading_tag, "last_seen": now}
                        new_tracked += 1
                        matched = True
                    else:
                        log("DEBUG", f"uploading sync: sonarr match but not full season, skipping '{name}'")
                        unmatched[th] = {"name": name, "reason": "sonarr:not_full_season", "last_seen": now}
                        skipped_unmatched += 1
                        matched = True

        if not matched:
            unmatched[th] = {"name": name, "reason": "no_match", "last_seen": now}
            skipped_unmatched += 1
            log("DEBUG", f"uploading sync: no Arr match for '{name}'")

        if th in tracked and th in unmatched:
            unmatched.pop(th, None)

    gone_hashes = [h for h in list(tracked.keys()) if h not in current_by_hash]
    for h in gone_hashes:
        entry = tracked.get(h)
        if not isinstance(entry, dict):
            tracked.pop(h, None)
            continue

        arr_name = str(entry.get("arr") or "").strip().lower()
        arr_id = entry.get("arr_id")
        if isinstance(arr_id, str) and arr_id.isdigit():
            arr_id = int(arr_id)

        if remove_when_gone and isinstance(arr_id, int) and arr_id > 0 and not dry_run:
            try:
                if arr_name == "radarr" and radarr is not None:
                    if radarr.remove_tag(arr_id, uploading_tag):
                        removed_tags += 1
                elif arr_name == "sonarr" and sonarr is not None:
                    if sonarr.remove_tag(arr_id, uploading_tag):
                        removed_tags += 1
            except SystemExit:
                errors += 1

        tracked.pop(h, None)

    # Purge old unmatched entries that are not currently present
    for h in list(unmatched.keys()):
        if h in current_by_hash:
            continue
        entry = unmatched.get(h)
        if not isinstance(entry, dict):
            unmatched.pop(h, None)
            continue
        if not unmatched_fresh(entry):
            unmatched.pop(h, None)

    if not dry_run:
        save_uploading_state(state_file, {"tracked": tracked, "unmatched": unmatched})

    return {
        "status": "ok",
        "dry_run": dry_run,
        "category": category,
        "uploading_tag": uploading_tag,
        "state_file": state_file,
        "remove_when_gone": remove_when_gone,
        "unmatched_ttl_hours": unmatched_ttl_hours,
        "processed_torrents": processed,
        "already_tracked": already_tracked,
        "new_tracked": new_tracked,
        "skipped_unmatched": skipped_unmatched,
        "parse_calls": parse_calls,
        "tagged_radarr": tagged_radarr,
        "tagged_sonarr": tagged_sonarr,
        "removed_tags": removed_tags,
        "gone_tracked": len(gone_hashes),
        "errors": errors,
    }

# -----------------------------
# Webhook mode (Coolify friendly)
# -----------------------------
def run_webhook_mode() -> None:
    try:
        from flask import Flask, jsonify, request  # type: ignore
    except Exception:
        die("Webhook mode requires Flask. Add 'flask' to requirements.txt if you want this mode.", 2)

    app = Flask(__name__)

    configured = (os.getenv("WEBHOOK_SECRET") or "").strip()
    if configured:
        log("INFO", f"Webhook auth enabled (WEBHOOK_SECRET set: {mask(configured)})")
    else:
        log("WARNING", "Webhook auth disabled (WEBHOOK_SECRET not set).")

    @app.get("/")
    def root():
        return "ok", 200

    @app.get("/health")
    def health():
        if not webhook_secret_ok(request):
            return jsonify({"status": "unauthorized"}), 401
        return jsonify({"status": "ok"})

    @app.post("/tag")
    def tag():
        if not webhook_secret_ok(request):
            log("WARNING", f"Webhook unauthorized. Provided headers: {list(request.headers.keys())}")
            return jsonify({"status": "unauthorized"}), 401

        payload = request.get_json(silent=True) or {}
        keys = list(payload.keys())
        log("INFO", f"Webhook payload received: keys={keys}")

        # Determine arr (explicit field OR infer by payload shape)
        arr = str(payload.get("arr", "")).strip().lower()
        if not arr:
            if "movie" in payload or "remoteMovie" in payload:
                arr = "radarr"
            elif "series" in payload or "episodes" in payload:
                arr = "sonarr"

        event_type = str(payload.get("eventType", "")).strip().lower()
        log("INFO", f"Webhook eventType={event_type or '(none)'}, inferred_arr={arr or '(none)'}")

        # Handle Arr Connect Test messages
        if event_type == "test":
            if arr in ("radarr", "sonarr"):
                log("INFO", f"{arr}: Test webhook received. Authentication OK.")
                return jsonify({"status": "ok", "mode": "test", "arr": arr}), 200
            log("INFO", "Webhook test received (arr could not be inferred). Authentication OK.")
            return jsonify({"status": "ok", "mode": "test", "arr": "unknown"}), 200

        # Non-test must know arr
        if arr not in ("sonarr", "radarr"):
            return jsonify({"error": "arr must be 'sonarr' or 'radarr'"}), 400

        # Extract fields (generic payload + Arr-native payload)
        item_id = payload.get("item_id")
        download_id = str(payload.get("download_id", "")).strip()
        is_upgrade = bool(payload.get("is_upgrade", False))
        title_hint = payload.get("title_hint")

        # Arr-native top-level fields (camelCase)
        if not download_id:
            download_id = str(payload.get("downloadId") or "").strip()
        if not is_upgrade and "isUpgrade" in payload:
            is_upgrade = bool(payload.get("isUpgrade"))

        # Extract fields (Arr-native nested best effort)
        if arr == "radarr":
            if item_id is None:
                item_id = (payload.get("movie") or {}).get("id")
            if not download_id:
                download_id = str((payload.get("release") or {}).get("downloadId") or "").strip()
            if not title_hint:
                title_hint = (
                    (payload.get("release") or {}).get("releaseTitle")
                    or (payload.get("movieFile") or {}).get("relativePath")
                    or (payload.get("movie") or {}).get("title")
                )
        else:
            if item_id is None:
                item_id = (payload.get("series") or {}).get("id")
            if not download_id:
                download_id = str((payload.get("release") or {}).get("downloadId") or "").strip()
            if not title_hint:
                title_hint = (
                    (payload.get("release") or {}).get("releaseTitle")
                    or (payload.get("series") or {}).get("title")
                )

        # Validate
        if not isinstance(item_id, int):
            return jsonify({"error": "item_id must be an integer"}), 400
        if not download_id:
            return jsonify({"error": "download_id is required for non-test events"}), 400

        # Inject into env-like flow and run
        os.environ.pop("SONARR_EVENTTYPE", None)
        os.environ.pop("RADARR_EVENTTYPE", None)

        if arr == "sonarr":
            os.environ["SONARR_EVENTTYPE"] = "Download"
            os.environ["SONARR_SERIES_ID"] = str(item_id)
            os.environ["SONARR_DOWNLOAD_ID"] = download_id
            os.environ["SONARR_ISUPGRADE"] = "true" if is_upgrade else "false"
            if title_hint:
                os.environ["SONARR_RELEASE_TITLE"] = str(title_hint)
        else:
            os.environ["RADARR_EVENTTYPE"] = "Download"
            os.environ["RADARR_MOVIE_ID"] = str(item_id)
            os.environ["RADARR_DOWNLOAD_ID"] = download_id
            os.environ["RADARR_ISUPGRADE"] = "true" if is_upgrade else "false"
            if title_hint:
                os.environ["RADARR_RELEASE_TITLE"] = str(title_hint)

        try:
            run_arr_script_mode()
            return jsonify({"status": "ok", "arr": arr}), 200
        except SystemExit as e:
            return jsonify({"status": "error", "code": int(e.code)}), 500
        except Exception as e:
            log("ERROR", f"Unhandled exception in webhook mode: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.post("/backfill/history")
    def backfill_history():
        if not webhook_secret_ok(request):
            log("WARNING", f"Webhook unauthorized. Provided headers: {list(request.headers.keys())}")
            return jsonify({"status": "unauthorized"}), 401

        payload = request.get_json(silent=True) or {}
        log("INFO", f"Backfill(history) payload received: keys={list(payload.keys())}")

        arr = str(payload.get("arr", "")).strip().lower()
        if arr not in ("radarr", "sonarr", "both"):
            return jsonify({"error": "arr must be 'radarr', 'sonarr' or 'both'"}), 400

        dry_run = bool(payload.get("dry_run", True))
        limit = int(payload.get("limit", 0) or 0)  # 0 = no limit
        only_missing = bool(payload.get("only_missing", True))
        reapply = bool(payload.get("reapply", False))
        page_size = int(payload.get("page_size", 1000) or 1000)

        try:
            result = run_backfill_history(
                arr_target=arr,
                dry_run=dry_run,
                limit=limit,
                only_missing=only_missing,
                reapply=reapply,
                page_size=page_size,
            )
            return jsonify(result), 200
        except SystemExit as e:
            return jsonify({"status": "error", "code": int(e.code)}), 500
        except Exception as e:
            log("ERROR", f"Unhandled exception in backfill(history): {e}")
            return jsonify({"status": "error", "message": str(e)}), 500


    @app.post("/backfill/uploading")
    def backfill_uploading():
        if not webhook_secret_ok(request):
            log("WARNING", f"Webhook unauthorized. Provided headers: {list(request.headers.keys())}")
            return jsonify({"status": "unauthorized"}), 401

        payload = request.get_json(silent=True) or {}
        log("INFO", f"Backfill(uploading) payload received: keys={list(payload.keys())}")

        arr = str(payload.get("arr", "both")).strip().lower()
        if arr not in ("radarr", "sonarr", "both"):
            return jsonify({"error": "arr must be 'radarr', 'sonarr' or 'both'"}), 400

        dry_run = bool(payload.get("dry_run", True))
        limit = int(payload.get("limit", 0) or 0)

        try:
            result = run_backfill_uploading(arr_target=arr, dry_run=dry_run, limit=limit)
            return jsonify(result), 200
        except SystemExit as e:
            return jsonify({"status": "error", "code": int(e.code)}), 500
        except Exception as e:
            log("ERROR", f"Unhandled exception in backfill(uploading): {e}")
            return jsonify({"status": "error", "message": str(e)}), 500

    bind = os.getenv("WEBHOOK_BIND", "0.0.0.0")
    port = int(os.getenv("WEBHOOK_PORT", "8787"))
    log("INFO", f"Starting webhook server on {bind}:{port}")
    app.run(host=bind, port=port)

def main() -> None:
    mode = os.getenv("RUN_MODE", "arr-script").strip().lower()
    if mode == "webhook":
        run_webhook_mode()
    else:
        run_arr_script_mode()


if __name__ == "__main__":
    main()
