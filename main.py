#!/usr/bin/env python3
"""
Arr Source Tagger (Sonarr + Radarr)
- Reads torrent trackers from qBittorrent
- Maps tracker domain to a private-tracker tag, otherwise "public"
- Applies tag to the imported Movie/Series in Radarr/Sonarr
- Re-tags on upgrade (every import event recalculates source tag)

Modes:
1) "arr-script" (default): invoked by Sonarr/Radarr Custom Script on import
2) "webhook": run as a small HTTP service (optional; Coolify-friendly)

Environment variables (common):
  LOG_LEVEL=DEBUG|INFO|WARNING|ERROR   (default: INFO)
  PUBLIC_TAG=public                   (default: public)
  PRIVATE_TRACKERS_FILE=/config/private_trackers.yml (default: ./private_trackers.yml)
  SOURCE_TAG_PREFIXES=pt-,public      (default: pt-,public)
  STATE_FILE=/data/state.json         (default: ./state.json)  # best-effort; optional

qBittorrent:
  QBIT_URL=http://qbittorrent:8080
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

Arr custom script mode:
  Sonarr provides (typical):
    SONARR_EVENTTYPE=Download
    SONARR_SERIES_ID=123
    SONARR_DOWNLOAD_ID=<torrent hash>
    SONARR_ISUPGRADE=True|False
  Radarr provides (typical):
    RADARR_EVENTTYPE=Download
    RADARR_MOVIE_ID=456
    RADARR_DOWNLOAD_ID=<torrent hash>
    RADARR_ISUPGRADE=True|False
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

try:
    import yaml  # type: ignore
except Exception:
    yaml = None  # Will handle gracefully


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


def load_private_tracker_map(path: str) -> Dict[str, str]:
    if not os.path.exists(path):
        log("WARNING", f"Private tracker mapping file not found: {path}. All will be tagged as public.")
        return {}

    if yaml is None:
        die(
            "PyYAML is not installed but a YAML mapping file is used. "
            "Install dependencies from requirements.txt or set PRIVATE_TRACKERS_FILE to a JSON file.",
            2,
        )

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        m = data.get("private_trackers", {}) or {}
        # Normalize keys to lowercase
        out: Dict[str, str] = {}
        for k, v in m.items():
            if not k or not v:
                continue
            out[str(k).strip().lower()] = str(v).strip()
        return out
    except Exception as e:
        die(f"Failed to load private tracker map from {path}: {e}", 2)


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
    # Show up to 6 domains, then summarize
    shown = domains[:6]
    rest = len(domains) - len(shown)
    if rest > 0:
        return ", ".join(shown) + f" (+{rest} more)"
    return ", ".join(shown)

def mask(s: str, keep: int = 4) -> str:
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * (len(s) - keep)


def webhook_secret_ok(req) -> bool:
    """
    Accept secret via:
      - X-Webhook-Secret header
      - Authorization: Bearer <secret>
      - query param ?secret=<secret>
    If WEBHOOK_SECRET is unset/empty => allow.
    """
    def webhook_secret_ok(req) -> bool:
        expected = (os.getenv("WEBHOOK_SECRET") or "").strip()
        if not expected:
            return True

    # Common header variants used by Arr / proxies
    header_candidates = [
        "X-Webhook-Secret",
        "X-WebhookSecret",
        "X_WEBHOOK_SECRET",
    ]

    for hn in header_candidates:
        got = (req.headers.get(hn) or "").strip()
        if got and got == expected:
            return True

    # Authorization: Bearer <secret>
    auth = (req.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token == expected:
            return True

    # Query param fallback
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
        if r.status_code != 200:
            die(f"qBittorrent trackers request failed (HTTP {r.status_code}): {r.text.strip()}", 3)
        return r.json() if r.text.strip() else []

    def torrents_info(self) -> List[dict]:
        url = self.cfg.base_url.rstrip("/") + "/api/v2/torrents/info"
        log("DEBUG", f"qBittorrent torrents/info: GET {url}")
        r = self.sess.get(url, timeout=30)
        if r.status_code != 200:
            die(f"qBittorrent torrents/info failed (HTTP {r.status_code}): {r.text.strip()}", 3)
        return r.json() if r.text.strip() else []

    def resolve_hash(self, download_id: str, fallback_name: Optional[str] = None) -> Optional[str]:
        """
        Best case: Arr download_id already is the torrent hash.
        Fallback: search qBittorrent by name match.
        """
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

        # Heuristic: exact contains match
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
        """
        Ensure a tag exists; return its ID.
        """
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

    def apply_source_tag(self, item_id: int, chosen_tag: str, source_prefixes: List[str]) -> None:
        """
        Remove previous source tags (pt-* and public by default), then add chosen_tag.
        Keeps all non-source tags intact.
        """
        item = self.get_item(item_id)
        title = item.get("title") or item.get("titleSlug") or f"ID:{item_id}"
        existing_tag_ids: List[int] = list(item.get("tags") or [])

        # Resolve labels for existing tag IDs
        tag_objects = self.get_tags()
        id_to_label = {int(t["id"]): str(t.get("label", "")) for t in tag_objects if "id" in t}

        def is_source_label(lbl: str) -> bool:
            l = lbl.strip().lower()
            for p in source_prefixes:
                p2 = p.strip().lower()
                if not p2:
                    continue
                # exact match prefix or exact match label
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

        new_ids = kept_ids[:]
        if chosen_id not in new_ids:
            new_ids.append(chosen_id)

        # Write back
        item["tags"] = new_ids
        self.update_item(item)

        log(
            "INFO",
            f"{self.cfg.name}: Applied source tag '{chosen_tag}' to '{title}'. "
            f"Removed source tags: {removed if removed else '(none)'}; kept other tags: {len(kept_ids)}.",
        )


# -----------------------------
# Core tagging logic
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
    private_map: Dict[str, str],
    public_tag: str,
) -> Tuple[str, List[str]]:
    trackers = qbit.trackers(torrent_hash)

    # Collect domains, ordered by tier then by appearance
    # qBittorrent tracker entries contain 'tier' and 'url'
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

    # Decide private vs public
    for dom in domains:
        if dom.lower() in private_map:
            return private_map[dom.lower()], domains

    return public_tag, domains


def run_arr_script_mode() -> None:
    public_tag = os.getenv("PUBLIC_TAG", "public").strip() or "public"
    mapping_file = os.getenv("PRIVATE_TRACKERS_FILE", "./private_trackers.yml")
    state_file = os.getenv("STATE_FILE", "./state.json")
    source_prefixes = [p.strip() for p in os.getenv("SOURCE_TAG_PREFIXES", "pt-,public").split(",") if p.strip()]

    private_map = load_private_tracker_map(mapping_file)

    # Arr event
    ev = detect_arr_event_from_env()
    if ev is None:
        return

    log("INFO", f"Arr event received: arr={ev.arr}, item_id={ev.item_id}, is_upgrade={ev.is_upgrade}")

    # qBittorrent
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

    # Resolve torrent hash
    torrent_hash = qbit.resolve_hash(ev.download_id, fallback_name=ev.title_hint)
    if not torrent_hash:
        die(
            "Could not resolve torrent hash. Ensure Arr passes *_DOWNLOAD_ID (torrent hash). "
            "If not, ensure *_RELEASE_TITLE is available for fallback matching.",
            5,
        )

    log("INFO", f"Resolved torrent hash: {torrent_hash}")

    # Determine tag from trackers
    chosen_tag, domains = choose_source_tag(qbit, torrent_hash, private_map, public_tag)
    log("INFO", f"Tracker domains found: {summarize_domains(domains)}")
    log("INFO", f"Chosen source tag: {chosen_tag}")

    # Optional state (best-effort). Even though we re-tag on upgrade, state helps avoid noise.
    state = load_state(state_file)
    prev = state.get(torrent_hash)
    if prev and prev == chosen_tag:
        log("DEBUG", f"State: torrent hash already mapped to '{prev}' previously. Continuing (idempotent).")
    state[torrent_hash] = chosen_tag
    save_state(state_file, state)

    # Arr config + apply
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
# Optional Webhook mode (Coolify friendly)
# -----------------------------
def run_webhook_mode() -> None:
    """
    Lightweight webhook server (optional).
    This does NOT depend on exact Sonarr/Radarr JSON schema, but expects at least:
      {
        "arr": "sonarr"|"radarr",
        "item_id": 123,
        "download_id": "<torrent hash>",
        "is_upgrade": true|false,
        "title_hint": "optional"
      }

    Configure Arr to send a Webhook that you can transform upstream (or via a small middleware),
    OR call this endpoint from your own automation.

    Endpoints:
      POST /tag
      GET  /health
    """
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

        # 1) Determine arr type
        arr = str(payload.get("arr", "")).strip().lower()
        if not arr:
            # infer from schema
            if "movie" in payload or "remoteMovie" in payload:
                arr = "radarr"
            elif "series" in payload or "episodes" in payload:
                arr = "sonarr"

        # 2) Handle test events from Arr Connect
        event_type = str(payload.get("eventType", "")).strip().lower()
        if event_type == "test":
            # must return 200 so Arr UI accepts the webhook config
            if arr in ("radarr", "sonarr"):
                log("INFO", f"{arr}: Test webhook received. Authentication OK.")
                return jsonify({"status": "ok", "mode": "test", "arr": arr}), 200

            # Some connectors send minimal test payloads; still accept if auth was OK.
            log("INFO", "Webhook test received (arr could not be inferred). Authentication OK.")
            return jsonify({"status": "ok", "mode": "test", "arr": "unknown"}), 200

        # 3) Non-test events must know arr
        if arr not in ("sonarr", "radarr"):
            return jsonify({"error": "arr must be 'sonarr' or 'radarr'"}), 400

        # 4) Extract item_id + download hash
        # Your own generic payloads (what you used in curl tests)
        item_id = payload.get("item_id")
        download_id = str(payload.get("download_id", "")).strip()
        is_upgrade = bool(payload.get("is_upgrade", False))
        title_hint = payload.get("title_hint")

        # Arr-native payloads:
        # Radarr: payload.movie.id, payload.release.downloadId (sometimes)
        # Sonarr: payload.series.id, payload.release.downloadId (sometimes)
        if arr == "radarr":
            if item_id is None:
                item_id = (payload.get("movie") or {}).get("id")
            if not download_id:
                download_id = str((payload.get("release") or {}).get("downloadId") or "").strip()
            if not title_hint:
                title_hint = (payload.get("release") or {}).get("releaseTitle") or (payload.get("movie") or {}).get("title")
        else:
            if item_id is None:
                item_id = (payload.get("series") or {}).get("id")
            if not download_id:
                download_id = str((payload.get("release") or {}).get("downloadId") or "").strip()
            if not title_hint:
                title_hint = (payload.get("release") or {}).get("releaseTitle") or (payload.get("series") or {}).get("title")

        # validate
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
