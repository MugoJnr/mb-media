import os
import re
import time
import uuid
import shutil
import sqlite3
import threading
from urllib.parse import quote, unquote, urlparse, parse_qs, urlencode, urlunparse

import requests
from flask import Flask, request, jsonify, render_template, send_file, abort, session, redirect, url_for

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import yt_dlp

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me-in-production")

limiter = Limiter(app=app, key_func=get_remote_address, default_limits=["120 per hour"])

# Prefer persistent disk when mounted (Render Disk at /var/data).
DATA_DIR = os.environ.get("DATA_DIR") or ("/var/data" if os.path.isdir("/var/data") else os.path.dirname(__file__))

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR") or os.path.join(DATA_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FILE_TTL = 15 * 60  # seconds a finished file is kept before auto-delete

SUPPORTED_DOMAINS = {
    "youtube.com": "YouTube", "youtu.be": "YouTube", "music.youtube.com": "YouTube",
    "tiktok.com": "TikTok", "vm.tiktok.com": "TikTok", "vt.tiktok.com": "TikTok",
    "instagram.com": "Instagram",
    "twitter.com": "X (Twitter)", "x.com": "X (Twitter)", "mobile.twitter.com": "X (Twitter)",
    "facebook.com": "Facebook", "fb.watch": "Facebook", "fb.gg": "Facebook",
    "vimeo.com": "Vimeo",
    "dailymotion.com": "Dailymotion", "dai.ly": "Dailymotion",
}

# Tracking / share noise that often breaks or slows extraction.
# Note: do not include YouTube start-time "t" here; YouTube handling keeps it separately.
_TRACKING_PARAMS = {
    "si", "feature", "pp", "bpctr", "spfreload", "rc", "source", "src",
    "igshid", "igsh", "img_index",
    "s", "ref_src", "ref_url",
    "fbclid", "gclid", "mc_cid", "mc_eid",
    "is_from_webapp", "sender_device", "sender_web_id", "share_app_id",
    "share_item_type", "share_link_id", "share_author_id",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
}

# In-memory job tracking. Fine for a single free instance; would need
# Redis/a DB if this ever runs across multiple workers or dynos.
JOBS = {}
JOBS_LOCK = threading.Lock()

# Concurrency control: only this many downloads actually run at once.
# Everything else sits in QUEUE_ORDER showing a visible position.
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", 2))
DOWNLOAD_SEMAPHORE = threading.Semaphore(MAX_CONCURRENT_DOWNLOADS)
QUEUE_ORDER = []
QUEUE_LOCK = threading.Lock()

# Reject anything above these before spending time/bandwidth on it.
MAX_DURATION_SECONDS = int(os.environ.get("MAX_DURATION_SECONDS", 3 * 3600))  # 3 hours
MAX_FILESIZE_MB = int(os.environ.get("MAX_FILESIZE_MB", 2000))  # 2 GB

COOKIES_DIR = os.path.join(DATA_DIR, "cookies")
os.makedirs(COOKIES_DIR, exist_ok=True)
SERVER_COOKIES_PATH = os.path.join(COOKIES_DIR, "server.txt")

DB_PATH = os.path.join(DATA_DIR, "analytics.db")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
CONTACT_TO_EMAIL = os.environ.get("CONTACT_TO_EMAIL")
YTDLP_PROXY = (os.environ.get("YTDLP_PROXY") or "").strip() or None
YTDLP_COOKIES_FILE = (os.environ.get("YTDLP_COOKIES_FILE") or "").strip() or None


def _load_proxy_config():
    """Validate proxy env wiring so bad values are visible in health checks."""
    raw = (os.environ.get("YTDLP_PROXY") or "").strip()
    if not raw:
        return {
            "configured": False,
            "valid": False,
            "value": None,
            "reason": "not_configured",
        }

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        return {
            "configured": True,
            "valid": False,
            "value": None,
            "reason": "invalid_format",
        }

    return {
        "configured": True,
        "valid": True,
        "value": raw,
        "reason": "ok",
    }


PROXY_STATUS = _load_proxy_config()
YTDLP_PROXY = PROXY_STATUS["value"]
if PROXY_STATUS["configured"] and not PROXY_STATUS["valid"]:
    app.logger.warning("YTDLP_PROXY is set but invalid. Expected http(s):// or socks5(h)://host:port")


def _bootstrap_server_cookies():
    """Allow operators to inject base64 Netscape cookies via env at boot."""
    b64 = (os.environ.get("YTDLP_COOKIES_B64") or "").strip()
    if not b64:
        return
    try:
        import base64
        raw = base64.b64decode(b64)
        with open(SERVER_COOKIES_PATH, "wb") as fh:
            fh.write(raw)
    except Exception as e:
        app.logger.warning(f"Failed to decode YTDLP_COOKIES_B64: {e}")


_bootstrap_server_cookies()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT,
            kind TEXT,
            created_at INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT,
            message TEXT,
            created_at INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT,
            name TEXT,
            email TEXT,
            subject TEXT,
            message TEXT,
            created_at INTEGER
        )
    """)
    conn.commit()
    conn.close()


init_db()


def record_event(platform, kind):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO downloads (platform, kind, created_at) VALUES (?, ?, ?)",
            (platform or "unknown", kind, int(time.time())),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # analytics must never break a real download


def record_error(platform, message):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO errors (platform, message, created_at) VALUES (?, ?, ?)",
            (platform or "unknown", message[:300], int(time.time())),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def detect_platform(url: str):
    low = url.lower()
    for domain, name in SUPPORTED_DOMAINS.items():
        if domain in low:
            return name
    return None


def cleanup_path_later(path: str, delay: int = FILE_TTL):
    def _cleanup():
        time.sleep(delay)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
    threading.Thread(target=_cleanup, daemon=True).start()


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' https: data:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self'; "
        "connect-src 'self';"
    )
    return response


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


@app.errorhandler(429)
def rate_limited(e):
    return jsonify({"error": "Too many requests. Please slow down and try again shortly."}), 429


# ---------- Static pages ----------
CURRENT_YEAR = time.strftime("%Y")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/donate")
def donate():
    return render_template("donate.html")


@app.route("/contact")
def contact_page():
    return render_template("contact.html")


@app.route("/history")
def history_page():
    return render_template("history.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html", current_year=CURRENT_YEAR)


@app.route("/terms")
def terms():
    return render_template("terms.html", current_year=CURRENT_YEAR)


@app.route("/cookies")
def cookies():
    return render_template("cookies.html", current_year=CURRENT_YEAR)


@app.route("/dmca")
def dmca():
    return render_template("dmca.html", current_year=CURRENT_YEAR)


# ---------- API: contact ----------
@app.route("/api/contact", methods=["POST"])
@limiter.limit("10 per hour")
def api_contact():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    subject = (data.get("subject") or "").strip()
    message = (data.get("message") or "").strip()
    kind = (data.get("type") or "other").strip().lower()[:40]

    if not name or not email or not subject or not message:
        return jsonify({"error": "All fields are required."}), 400
    if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
        return jsonify({"error": "Enter a valid email address."}), 400
    if len(name) > 120 or len(subject) > 200 or len(message) > 5000:
        return jsonify({"error": "One or more fields are too long."}), 400

    # Persist locally so nothing is lost even if outbound email is unset.
    app.logger.info(f"[CONTACT] ({kind}) {name} <{email}> — {subject}: {message[:200]}")
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO contacts (kind, name, email, subject, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (kind, name, email, subject, message, int(time.time())),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        app.logger.warning(f"Contact DB save failed: {e}")

    emailed = False
    if RESEND_API_KEY and CONTACT_TO_EMAIL:
        try:
            resp = requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json={
                    "from": "MB MEDIA <onboarding@resend.dev>",
                    "to": [CONTACT_TO_EMAIL],
                    "reply_to": email,
                    "subject": f"[MB MEDIA] {subject}",
                    "text": f"Type: {kind}\nFrom: {name} <{email}>\n\n{message}",
                },
                timeout=8,
            )
            emailed = resp.status_code < 300
            if not emailed:
                app.logger.warning(f"Contact email provider status: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            app.logger.warning(f"Contact email failed to send: {e}")

    return jsonify({"ok": True, "emailed": emailed})


# ---------- API: info ----------
@app.route("/api/cookies", methods=["POST"])
@limiter.limit("10 per hour")
def api_cookies_upload():
    """Accept a cookies.txt (Netscape format) for sites like Instagram/TikTok
    that require a logged-in session for some content. Stored per-token,
    never tied to an account — the token just lives in the user's browser."""
    file = request.files.get("cookies")
    if not file:
        return jsonify({"error": "No file provided."}), 400
    if file.filename and not file.filename.endswith(".txt"):
        return jsonify({"error": "Expected a cookies.txt file."}), 400

    token = str(uuid.uuid4())
    path = os.path.join(COOKIES_DIR, f"{token}.txt")
    file.save(path)
    cleanup_path_later(path, delay=60 * 60 * 6)  # expire after 6 hours

    return jsonify({"token": token})


def cookiefile_for_token(token):
    if not token:
        return None
    path = os.path.join(COOKIES_DIR, f"{token}.txt")
    return path if os.path.isfile(path) else None


def resolve_cookiefile(token=None):
    """Prefer per-user cookie token, then explicit env file, then server cookies."""
    user_path = cookiefile_for_token(token)
    if user_path:
        return user_path
    if YTDLP_COOKIES_FILE and os.path.isfile(YTDLP_COOKIES_FILE):
        return YTDLP_COOKIES_FILE
    if os.path.isfile(SERVER_COOKIES_PATH):
        return SERVER_COOKIES_PATH
    return None


# Ordered strategies for YouTube on cloud IPs. First success wins.
# Prefer clients that currently need less / no PO-token attestation, then cookie-friendly ones.
YOUTUBE_CLIENT_STRATEGIES = [
    ["android_vr"],          # No PO token required for many streams
    ["tv", "tv_simply"],     # TV clients often skip bot-gate with guest/cookies
    ["web_safari"],          # HLS formats can succeed without full PO flow
    ["web_embedded"],        # Works for embeddable videos without PO token
    ["android"],             # May need cookies / may fail on account cookies
    ["mweb", "web"],
]


def extract_url_candidate(text: str) -> tuple[str, list[str]]:
    """Pull the first http(s) URL out of pasted chat/share text."""
    notes: list[str] = []
    text = (text or "").strip().strip('"').strip("'")
    if not text:
        return "", notes
    match = re.search(r"https?://[^\s<>\"']+", text, flags=re.I)
    if match:
        extracted = match.group(0).rstrip(").,]}>\"'")
        if extracted != text:
            notes.append("extracted link from pasted text")
        return extracted, notes
    # Bare domain paste without scheme.
    if re.match(r"^(www\.|[a-z0-9-]+\.)?[a-z0-9-]+\.[a-z]{2,}(/|\?|#|$)", text, re.I):
        notes.append("added https://")
        return "https://" + text.lstrip("/"), notes
    return text, notes


def _strip_tracking_params(qs: dict) -> tuple[dict, bool]:
    cleaned = {}
    removed = False
    for key, values in qs.items():
        low = key.lower()
        if low in _TRACKING_PARAMS or low.startswith("utm_"):
            removed = True
            continue
        cleaned[key] = values[0] if isinstance(values, list) and values else values
    return cleaned, removed


def resolve_short_redirect(url: str, timeout: float = 6.0) -> tuple[str, bool]:
    """Follow one hop for known short/share hosts (TikTok vm/vt, dai.ly, fb.watch)."""
    low = url.lower()
    if not any(h in low for h in ("vm.tiktok.com", "vt.tiktok.com", "dai.ly", "fb.watch", "t.co/")):
        return url, False
    try:
        resp = requests.head(url, allow_redirects=True, timeout=timeout)
        final = (resp.url or "").strip()
        if final and final != url:
            return final, True
    except Exception:
        try:
            resp = requests.get(url, allow_redirects=True, timeout=timeout, stream=True)
            final = (resp.url or "").strip()
            resp.close()
            if final and final != url:
                return final, True
        except Exception:
            pass
    return url, False


def sanitize_media_url(url: str) -> tuple[str, list[str]]:
    """Detect and repair common broken/share/mix URLs before fetch."""
    changes: list[str] = []
    raw, extract_notes = extract_url_candidate(url)
    changes.extend(extract_notes)
    if not raw:
        return "", changes

    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw.lstrip("/")
        changes.append("added https://")

    raw, redirected = resolve_short_redirect(raw)
    if redirected:
        changes.append("followed short/share redirect")

    try:
        parsed = urlparse(raw)
    except Exception:
        return raw, changes

    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path or ""
    qs = parse_qs(parsed.query or "")
    fragment = parsed.fragment or ""

    # ---- YouTube family ----
    if any(h in host for h in ("youtube.com", "youtu.be", "youtube-nocookie.com")):
        if host.startswith("music.") or host.startswith("m."):
            host = "youtube.com"
            changes.append("converted mobile/music YouTube host")

        m = re.search(r"/(shorts|embed|live|v)/([A-Za-z0-9_-]{6,})", path)
        if m:
            cleaned = {"v": m.group(2)}
            if "t" in qs:
                cleaned["t"] = qs["t"][0]
            elif "start" in qs:
                cleaned["t"] = qs["start"][0]
            changes.append(f"converted /{m.group(1)}/ link to watch URL")
            return urlunparse(("https", "www.youtube.com", "/watch", "", urlencode(cleaned), "")), changes

        if "youtu.be" in host:
            vid = path.strip("/").split("/")[0]
            if vid:
                cleaned = {"v": vid}
                if "t" in qs:
                    cleaned["t"] = qs["t"][0]
                changes.append("expanded youtu.be short link")
                return urlunparse(("https", "www.youtube.com", "/watch", "", urlencode(cleaned), "")), changes

        if "/watch" in path and "v" in qs:
            cleaned = {"v": qs["v"][0]}
            if "t" in qs:
                cleaned["t"] = qs["t"][0]
            elif fragment.startswith("t="):
                cleaned["t"] = fragment[2:]
            list_id = (qs.get("list") or [""])[0]
            if list_id:
                if list_id.startswith("RD"):
                    changes.append("removed YouTube Mix (list=RD...) to avoid stall")
                else:
                    changes.append("removed playlist list= param; using this video only")
            if set(qs.keys()) - {"v", "t", "list"}:
                changes.append("removed tracking parameters")
            return urlunparse(("https", "www.youtube.com", "/watch", "", urlencode(cleaned), "")), changes

        if path.rstrip("/") == "/playlist" and (qs.get("list") or [""])[0].startswith("RD"):
            changes.append("YouTube Mix playlists need a specific video link")
            return raw, changes

    # ---- TikTok ----
    elif "tiktok.com" in host:
        cleaned_qs, removed = _strip_tracking_params(qs)
        m = re.search(r"(?:/@[^/]+)?/video/(\d+)", path)
        if m:
            video_id = m.group(1)
            um = re.search(r"/@([^/]+)/video/", path)
            user = um.group(1) if um else ""
            new_path = f"/@{user}/video/{video_id}" if user else f"/video/{video_id}"
            new_url = urlunparse(("https", "www.tiktok.com", new_path, "", "", ""))
            if removed:
                changes.append("removed TikTok share/tracking params")
            if new_url != raw:
                changes.append("normalized TikTok video path")
            return new_url, changes
        if removed:
            changes.append("removed TikTok share/tracking params")
            return urlunparse((parsed.scheme or "https", parsed.netloc, path, "", urlencode(cleaned_qs), "")), changes

    # ---- Instagram ----
    elif "instagram.com" in host:
        cleaned_qs, removed = _strip_tracking_params(qs)
        m = re.search(r"/(reel|reels|p|tv)/([^/?#]+)", path)
        if m:
            kind, code = m.group(1), m.group(2)
            kind = "reel" if kind == "reels" else kind
            new_url = urlunparse(("https", "www.instagram.com", f"/{kind}/{code}/", "", "", ""))
            if removed:
                changes.append("removed Instagram tracking params")
            if new_url != raw:
                changes.append("normalized Instagram media path")
            return new_url, changes
        if removed:
            changes.append("removed Instagram tracking params")
            return urlunparse((parsed.scheme or "https", parsed.netloc, path, "", urlencode(cleaned_qs), "")), changes

    # ---- X / Twitter ----
    elif host in ("x.com", "twitter.com", "mobile.twitter.com"):
        cleaned_qs, removed = _strip_tracking_params(qs)
        if "t" in cleaned_qs:
            cleaned_qs.pop("t", None)
            removed = True
        m = re.search(r"/([^/]+)/status/(\d+)", path)
        if m:
            user, sid = m.group(1), m.group(2)
            new_url = f"https://x.com/{user}/status/{sid}"
            if removed:
                changes.append("removed X/Twitter tracking params")
            if new_url != raw:
                changes.append("normalized X status URL")
            return new_url, changes
        if removed:
            changes.append("removed X/Twitter tracking params")
            return urlunparse(("https", "x.com", path, "", urlencode(cleaned_qs), "")), changes

    # ---- Facebook / Vimeo / Dailymotion generic tracking strip ----
    else:
        cleaned_qs, removed = _strip_tracking_params(qs)
        if removed:
            changes.append("removed tracking parameters")
            return urlunparse((parsed.scheme or "https", parsed.netloc, path, "", urlencode(cleaned_qs), "")), changes

    return raw, changes


def normalize_media_url(url: str) -> str:
    cleaned, _ = sanitize_media_url(url)
    return cleaned or (url or "").strip()


def base_ydl_opts(cookiefile=None, *, skip_download=False, noplaylist=True, player_clients=None):
    """Shared yt-dlp options tuned for cloud hosts (bot / datacenter IP blocks)."""
    clients = player_clients or ["android_vr"]
    youtube_args = {"player_client": clients}
    # Skipping webpage helps on bare datacenter IPs, but hurts authenticated cookie sessions.
    if not cookiefile:
        youtube_args["player_skip"] = ["webpage"]
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": noplaylist,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 20,
        "force_ipv4": True,
        # Gentle pacing — rapid bursts on free-tier IPs trigger YouTube bot gates.
        "sleep_interval_requests": 0.8,
        "sleep_interval": 1,
        "max_sleep_interval": 3,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_args": {"youtube": youtube_args},
    }
    if skip_download:
        opts["skip_download"] = True
    if cookiefile:
        opts["cookiefile"] = cookiefile
    if YTDLP_PROXY:
        opts["proxy"] = YTDLP_PROXY
    return opts


def extract_with_fallback(url, cookiefile=None, *, skip_download=False, noplaylist=True):
    """Try multiple YouTube player clients until one returns usable info."""
    last_error = None
    strategies = YOUTUBE_CLIENT_STRATEGIES if "youtu" in url.lower() else [None]
    for clients in strategies:
        opts = base_ydl_opts(
            cookiefile,
            skip_download=skip_download,
            noplaylist=noplaylist,
            player_clients=clients,
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            # If this was info-only, just return. Download path uses download() separately.
            if skip_download:
                formats = info.get("formats") or []
                if not formats and info.get("_type") != "playlist" and "entries" not in info:
                    last_error = RuntimeError("No video formats found for this link.")
                    continue
            return info
        except Exception as e:
            last_error = e
            continue
    if last_error:
        raise last_error
    raise RuntimeError("Could not extract media info.")


def download_with_fallback(url, ydl_opts_builder):
    """Run yt-dlp download across client strategies until one succeeds."""
    last_error = None
    strategies = YOUTUBE_CLIENT_STRATEGIES if "youtu" in url.lower() else [None]
    for clients in strategies:
        opts = ydl_opts_builder(clients)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            return
        except Exception as e:
            last_error = e
            continue
    if last_error:
        raise last_error
    raise RuntimeError("Download failed for all client strategies.")


def friendly_extractor_error(exc, *, for_download=False):
    msg = str(exc or "")
    low = msg.lower()
    if any(tok in low for tok in ("sign in to confirm", "not a bot", "confirm you're not a bot", "login required")):
        action = "download" if for_download else "preview"
        has_cookies = bool(resolve_cookiefile())
        if has_cookies:
            return (
                f"YouTube is rate-limiting this server right now. Wait a minute and retry the {action}, "
                "or try another video / quality. TikTok and Instagram usually still work."
            )
        return (
            f"YouTube blocked this request. An admin must keep server cookies fresh, "
            f"then retry the {action}."
        )
    if "drm" in low:
        return "This video is DRM-protected and cannot be downloaded."
    if "private" in low or "unavailable" in low:
        return "This video is private, removed, or unavailable."
    if "geo" in low or "not available in your country" in low:
        return "This video is geo-restricted and cannot be fetched from this server."
    if for_download:
        return "Download failed. The link may be restricted or the format unavailable."
    return "Could not fetch video info. The link may be private, geo-restricted, or invalid."


@app.route("/api/info", methods=["POST"])
@limiter.limit("30 per hour")
def api_info():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    cookie_token = data.get("cookie_token")

    if not url:
        return jsonify({"error": "No URL provided."}), 400
    url, url_fixes = sanitize_media_url(url)
    if not url:
        return jsonify({"error": "No URL provided."}), 400
    platform = detect_platform(url)
    if not platform:
        return jsonify({"error": "Unsupported or unrecognized platform."}), 400

    cookiefile = resolve_cookiefile(cookie_token)

    try:
        # Prefer single-video extraction; playlists can hang for minutes on free hosts.
        info = extract_with_fallback(url, cookiefile, skip_download=True, noplaylist=True)
    except Exception as e:
        record_error(platform, str(e))
        return jsonify({"error": friendly_extractor_error(e, for_download=False)}), 422

    is_playlist = info.get("_type") == "playlist" or "entries" in info
    entry_count = None
    playlist_entries = []
    if is_playlist:
        entries = list(info.get("entries") or [])
        entry_count = len(entries)
        for e in entries[:30]:
            playlist_entries.append({
                "title": e.get("title"),
                "url": e.get("webpage_url") or e.get("url"),
                "thumbnail": e.get("thumbnail"),
                "duration": e.get("duration"),
            })
        if entries:
            info = entries[0]

    formats = []
    seen = set()
    for f in info.get("formats", []) or []:
        if f.get("vcodec") in (None, "none"):
            continue
        height = f.get("height")
        ext = f.get("ext")
        if not height or (height, ext) in seen:
            continue
        seen.add((height, ext))
        formats.append({
            "format_id": f["format_id"],
            "height": height,
            "ext": ext,
            "filesize_approx": f.get("filesize") or f.get("filesize_approx"),
        })
    formats.sort(key=lambda x: x["height"], reverse=True)

    subtitles_available = bool(info.get("subtitles") or info.get("automatic_captions"))

    return jsonify({
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "platform": platform,
        "is_playlist": is_playlist,
        "entry_count": entry_count,
        "playlist_entries": playlist_entries,
        "subtitles_available": subtitles_available,
        "formats": formats[:8],
        "normalized_url": url,
        "url_fixes": url_fixes,
    })


# ---------- Progress hook ----------
def make_progress_hook(job_id):
    def hook(d):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is None:
                return
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes", 0)
                percent = (downloaded / total * 100) if total else 0
                job["status"] = "downloading"
                job["percent"] = round(percent, 1)
                job["speed"] = _human_speed(d.get("speed"))
                job["eta"] = _human_eta(d.get("eta"))
            elif d["status"] == "finished":
                job["percent"] = 100
                job["speed"] = ""
                job["eta"] = ""
    return hook


def _human_speed(speed):
    if not speed:
        return ""
    kb = speed / 1024
    return f"{kb/1024:.1f} MB/s" if kb > 1024 else f"{kb:.0f} KB/s"


def _human_eta(eta):
    if not eta:
        return ""
    m, s = divmod(int(eta), 60)
    return f"{m}m {s}s" if m else f"{s}s"


def run_download_job(job_id, url, kind, format_id=None, audio_quality=None, audio_format=None, cookiefile=None):
    platform = detect_platform(url)

    # Wait our turn in the queue, updating position as others finish.
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "queued"
    try:
        while True:
            with QUEUE_LOCK:
                if job_id in QUEUE_ORDER:
                    position = QUEUE_ORDER.index(job_id) + 1
                else:
                    position = 1
            with JOBS_LOCK:
                if JOBS.get(job_id, {}).get("status") == "queued":
                    JOBS[job_id]["queue_position"] = position
            if DOWNLOAD_SEMAPHORE.acquire(timeout=2):
                break
    finally:
        with QUEUE_LOCK:
            if job_id in QUEUE_ORDER:
                QUEUE_ORDER.remove(job_id)

    try:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "checking"
            JOBS[job_id].pop("queue_position", None)

        # Pre-check duration/size before committing bandwidth to a full download.
        try:
            check_info = extract_with_fallback(url, cookiefile, skip_download=True, noplaylist=True)

            duration = check_info.get("duration")
            if duration and duration > MAX_DURATION_SECONDS:
                hours = MAX_DURATION_SECONDS / 3600
                with JOBS_LOCK:
                    JOBS[job_id] = {"status": "error", "error": f"Video is longer than the {hours:.0f}-hour limit."}
                return

            if kind == "video":
                est_size = None
                for f in check_info.get("formats", []) or []:
                    if format_id and f.get("format_id") == format_id:
                        est_size = f.get("filesize") or f.get("filesize_approx")
                        break
                if est_size is None:
                    est_size = check_info.get("filesize") or check_info.get("filesize_approx")
                if est_size and est_size > MAX_FILESIZE_MB * 1024 * 1024:
                    with JOBS_LOCK:
                        JOBS[job_id] = {"status": "error", "error": f"File exceeds the {MAX_FILESIZE_MB}MB size limit."}
                    return
        except Exception:
            pass  # if the pre-check itself fails, fall through and let the real download attempt surface the error

        _do_download(job_id, url, kind, format_id, audio_quality, audio_format, cookiefile, platform)
    finally:
        DOWNLOAD_SEMAPHORE.release()


def _do_download(job_id, url, kind, format_id, audio_quality, audio_format, cookiefile, platform):
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    outtmpl = os.path.join(job_dir, "%(title).80s.%(ext)s")

    def build_opts(clients):
        opts = base_ydl_opts(cookiefile, noplaylist=True, player_clients=clients)
        opts["outtmpl"] = outtmpl
        opts["progress_hooks"] = [make_progress_hook(job_id)]

        if kind == "video":
            opts["format"] = (
                format_id
                or "bestvideo*+bestaudio/best[ext=mp4]/best"
            )
            opts["merge_output_format"] = "mp4"
            opts["format_sort"] = ["res:1080", "ext:mp4:m4a"]
        elif kind == "audio":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format or "mp3",
                "preferredquality": str(audio_quality or "192"),
            }]
        elif kind == "thumbnail":
            opts["skip_download"] = True
            opts["writethumbnail"] = True
        elif kind == "subtitles":
            opts["skip_download"] = True
            opts["writesubtitles"] = True
            opts["writeautomaticsub"] = True
            opts["subtitleslangs"] = ["en"]
        return opts

    try:
        if kind not in ("video", "audio", "thumbnail", "subtitles"):
            with JOBS_LOCK:
                JOBS[job_id] = {"status": "error", "error": "Unknown download type."}
            return

        download_with_fallback(url, build_opts)

        files = [f for f in os.listdir(job_dir) if not f.startswith(".")]
        if not files:
            raise RuntimeError("No output file was produced.")

        filename = files[0]
        # Keep a short readable name for the user's save dialog.
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", filename).strip(" .") or f"{kind}.bin"
        if safe_name != filename:
            dest = os.path.join(job_dir, safe_name)
            if not os.path.exists(dest):
                os.replace(os.path.join(job_dir, filename), dest)
                filename = safe_name
        cleanup_path_later(job_dir)

        with JOBS_LOCK:
            JOBS[job_id] = {
                "status": "finished",
                "percent": 100,
                "filename": filename,
                "download_url": f"/api/file/{job_id}/{quote(filename)}",
            }
        record_event(platform, kind)

    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        record_error(platform, str(e))
        with JOBS_LOCK:
            JOBS[job_id] = {"status": "error", "error": friendly_extractor_error(e, for_download=True)}


@app.route("/api/download", methods=["POST"])
@limiter.limit("20 per hour")
def api_download():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    kind = data.get("type", "video")
    format_id = data.get("format_id")
    audio_quality = data.get("audio_quality")
    audio_format = data.get("audio_format")
    cookie_token = data.get("cookie_token")

    if not url:
        return jsonify({"error": "No URL provided."}), 400
    url, url_fixes = sanitize_media_url(url)
    if not url or not detect_platform(url):
        return jsonify({"error": "Unsupported or unrecognized platform."}), 400
    if kind not in ("video", "audio", "thumbnail", "subtitles"):
        return jsonify({"error": "Unsupported download type."}), 400

    cookiefile = resolve_cookiefile(cookie_token)

    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "percent": 0}
    with QUEUE_LOCK:
        QUEUE_ORDER.append(job_id)

    thread = threading.Thread(
        target=run_download_job,
        args=(job_id, url, kind, format_id, audio_quality, audio_format, cookiefile),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id, "normalized_url": url, "url_fixes": url_fixes})


@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "Unknown job."}), 404
    return jsonify(job)


@app.route("/api/file/<job_id>/<path:filename>")
def serve_file(job_id, filename):
    filename = unquote(filename or "")
    if ".." in job_id or ".." in filename or not job_id or not filename:
        abort(400)
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    filepath = os.path.join(job_dir, filename)
    # Fallback: if exact name missing, serve the only file in the job dir.
    if not os.path.isfile(filepath) and os.path.isdir(job_dir):
        candidates = [f for f in os.listdir(job_dir) if not f.startswith(".")]
        if len(candidates) == 1:
            filename = candidates[0]
            filepath = os.path.join(job_dir, filename)
    if not os.path.isfile(filepath):
        abort(404)
    return send_file(filepath, as_attachment=True, download_name=filename)


# ---------- Admin analytics ----------
@app.route("/admin", methods=["GET", "POST"])
@limiter.limit("20 per hour")
def admin():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin"))
        return render_template("admin_login.html", error="Incorrect password.")

    if not session.get("is_admin"):
        return render_template("admin_login.html", error=None)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    total = cur.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
    day_ago = int(time.time()) - 86400
    month_ago = int(time.time()) - 30 * 86400
    today = cur.execute("SELECT COUNT(*) FROM downloads WHERE created_at > ?", (day_ago,)).fetchone()[0]
    this_month = cur.execute("SELECT COUNT(*) FROM downloads WHERE created_at > ?", (month_ago,)).fetchone()[0]

    by_platform = cur.execute(
        "SELECT platform, COUNT(*) c FROM downloads GROUP BY platform ORDER BY c DESC"
    ).fetchall()
    by_kind = cur.execute(
        "SELECT kind, COUNT(*) c FROM downloads GROUP BY kind ORDER BY c DESC"
    ).fetchall()
    recent_errors = cur.execute(
        "SELECT platform, message, created_at FROM errors ORDER BY created_at DESC LIMIT 15"
    ).fetchall()
    conn.close()

    return render_template(
        "admin.html",
        total=total, today=today, this_month=this_month,
        by_platform=by_platform, by_kind=by_kind, recent_errors=recent_errors,
        active_jobs=len(JOBS),
        has_server_cookies=bool(resolve_cookiefile()),
        has_proxy=bool(YTDLP_PROXY),
        cookies_message=None,
    )


@app.route("/admin/cookies", methods=["POST"])
@limiter.limit("10 per hour")
def admin_cookies():
    if not session.get("is_admin"):
        return redirect(url_for("admin"))

    file = request.files.get("cookies")
    if not file:
        return redirect(url_for("admin"))
    if file.filename and not file.filename.endswith(".txt"):
        return redirect(url_for("admin"))

    file.save(SERVER_COOKIES_PATH)
    return redirect(url_for("admin"))


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin"))


@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "cookies": bool(resolve_cookiefile()),
        "proxy": bool(YTDLP_PROXY),
        "proxy_configured": PROXY_STATUS["configured"],
        "proxy_valid": PROXY_STATUS["valid"],
        "proxy_reason": PROXY_STATUS["reason"],
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
