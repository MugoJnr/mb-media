import json
import mimetypes
import os
import re
import subprocess
import time
import uuid
import shutil
import sqlite3
import threading
from collections import deque
from urllib.parse import quote, unquote, urlparse, parse_qs, urlencode, urlunparse

import requests
from flask import Flask, request, jsonify, render_template, send_file, send_from_directory, abort, session, redirect, url_for

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import yt_dlp

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me-in-production")

# Bust browser/CDN cache for static assets after deploy (override via STATIC_VERSION env).
def _static_version():
    env = (os.environ.get("STATIC_VERSION") or "").strip()
    if env:
        return env
    try:
        js_path = os.path.join(app.static_folder, "js", "app.js")
        if os.path.isfile(js_path):
            return str(int(os.path.getmtime(js_path)))
    except OSError:
        pass
    return "1"


STATIC_VERSION = _static_version()


@app.context_processor
def inject_static_version():
    return {"static_version": STATIC_VERSION}

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
# Cap ffmpeg post-process so free-tier hosts do not spin forever on HEVC→H.264.
TRANSCODE_TIMEOUT = int(os.environ.get("TRANSCODE_TIMEOUT", 600))  # 10 minutes
# Scale down during phone transcode — 540p ultrafast on free CPU beats long 1080p waits.
TRANSCODE_MAX_HEIGHT = int(os.environ.get("TRANSCODE_MAX_HEIGHT", 540))

COOKIES_DIR = os.path.join(DATA_DIR, "cookies")
os.makedirs(COOKIES_DIR, exist_ok=True)
SERVER_COOKIES_PATH = os.path.join(COOKIES_DIR, "server.txt")

DB_PATH = os.path.join(DATA_DIR, "analytics.db")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
CONTACT_TO_EMAIL = os.environ.get("CONTACT_TO_EMAIL")
YTDLP_PROXY = (os.environ.get("YTDLP_PROXY") or "").strip() or None
YTDLP_COOKIES_FILE = (os.environ.get("YTDLP_COOKIES_FILE") or "").strip() or None
POT_ENABLED = (os.environ.get("POT_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")
POT_PROVIDER_URL = (os.environ.get("POT_PROVIDER_URL") or "http://127.0.0.1:4416").strip().rstrip("/")
POT_SERVER_HOME = (os.environ.get("POT_SERVER_HOME") or "/opt/bgutil-ytdlp-pot-provider/server").strip()


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
        "connect-src 'self'; "
        "media-src 'self' blob:;"
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
SITE_ORIGIN = os.environ.get("PUBLIC_SITE_URL", "https://media.mugobyte.com").rstrip("/")


@app.route("/")
def index():
    return render_template("index.html")




@app.route("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "favicon.ico", mimetype="image/vnd.microsoft.icon")
@app.route("/robots.txt")
def robots_txt():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "\n"
        "Disallow: /admin\n"
        "Disallow: /admin/\n"
        "Disallow: /api/\n"
        "\n"
        f"Sitemap: {SITE_ORIGIN}/sitemap.xml\n"
    )
    return body, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/sitemap.xml")
def sitemap_xml():
    today = time.strftime("%Y-%m-%d")
    pages = [
        ("/", "1.0", "weekly"),
        ("/about", "0.7", "monthly"),
        ("/contact", "0.8", "monthly"),
        ("/donate", "0.5", "monthly"),
        ("/history", "0.4", "monthly"),
        ("/privacy", "0.3", "yearly"),
        ("/terms", "0.3", "yearly"),
        ("/cookies", "0.3", "yearly"),
        ("/dmca", "0.3", "yearly"),
    ]
    urls = []
    for path, priority, changefreq in pages:
        loc = SITE_ORIGIN + path
        urls.append(
            "  <url>\n"
            f"    <loc>{loc}</loc>\n"
            f"    <lastmod>{today}</lastmod>\n"
            f"    <changefreq>{changefreq}</changefreq>\n"
            f"    <priority>{priority}</priority>\n"
            "  </url>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>\n"
    )
    return xml, 200, {"Content-Type": "application/xml; charset=utf-8"}


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
# With bgutil PO tokens, prefer mweb/web; keep non-POT clients as fallbacks.
YOUTUBE_CLIENT_STRATEGIES = [
    ["mweb"],                # Needs PO token — supplied by bgutil when available
    ["web"],
    ["web_safari"],          # HLS formats can succeed without full PO flow
    ["android_vr"],          # Often no PO token required
    ["tv", "tv_simply"],     # TV clients often skip bot-gate with guest/cookies
    ["web_embedded"],        # Works for embeddable videos without PO token
    ["android"],
]


def pot_extractor_args():
    """yt-dlp extractor_args for the bgutil PO Token plugin (HTTP preferred)."""
    if not POT_ENABLED:
        return {}
    args = {
        # HTTP server started by scripts/start.sh (default port 4416).
        # yt-dlp expects `base_url` as a string, not a list.
        "youtubepot-bgutilhttp": {"base_url": POT_PROVIDER_URL},
    }
    if POT_SERVER_HOME and os.path.isdir(POT_SERVER_HOME):
        # Script fallback if the HTTP provider is down (plugin prefers HTTP when up).
        # yt-dlp expects `server_home` as a string, not a list.
        args["youtubepot-bgutilscript"] = {"server_home": POT_SERVER_HOME}
    return args


def pot_reachable():
    """True when the local bgutil HTTP provider answers /ping."""
    if not POT_ENABLED:
        return False
    try:
        import urllib.request

        with urllib.request.urlopen(f"{POT_PROVIDER_URL}/ping", timeout=1.5) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        return False


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
    clients = player_clients or (["mweb"] if POT_ENABLED else ["android_vr"])
    youtube_args = {"player_client": clients}
    # Skipping webpage helps on bare datacenter IPs, but hurts authenticated cookie sessions.
    if not cookiefile:
        youtube_args["player_skip"] = ["webpage"]
    extractor_args = {"youtube": youtube_args}
    extractor_args.update(pot_extractor_args())
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
        "extractor_args": extractor_args,
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


def _brief_technical(msg, limit=180):
    """One-line tail of a subprocess/yt-dlp error for the UI."""
    cleaned = re.sub(r"\s+", " ", (msg or "")).strip()
    if len(cleaned) <= limit:
        return cleaned
    return "…" + cleaned[-(limit - 1):]


def friendly_extractor_error(exc, *, for_download=False):
    msg = str(exc or "")
    low = msg.lower()
    tail = _brief_technical(msg)
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
    if "instagram" in low and any(tok in low for tok in ("login", "cookie", "csrf", "rate", "429", "403")):
        hint = "Try uploading cookies with the 🍪 button, then retry."
        return f"Instagram blocked this request. {hint} {tail}".strip()
    if "tiktok" in low and any(tok in low for tok in ("login", "cookie", "403", "429")):
        return f"TikTok blocked this request. Try cookies (🍪) or another link. {tail}".strip()
    if "drm" in low:
        return "This video is DRM-protected and cannot be downloaded."
    if "private" in low or "unavailable" in low:
        return "This video is private, removed, or unavailable."
    if "geo" in low or "not available in your country" in low:
        return "This video is geo-restricted and cannot be fetched from this server."
    if "timed out" in low or "timeout" in low:
        return f"Server timed out — try phone-friendly quality or a shorter clip. {tail}".strip()
    if for_download:
        return f"Download failed. {tail}".strip() if tail else "Download failed. The link may be restricted or the format unavailable."
    return f"Could not fetch video info. {tail}".strip() if tail else "Could not fetch video info. The link may be private, geo-restricted, or invalid."


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

    formats = _pick_info_formats(info.get("formats", []) or [], platform)

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
        "formats": formats,
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


def run_download_job(job_id, url, kind, format_id=None, audio_quality=None, audio_format=None, cookiefile=None, convert_after=False):
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

        _do_download(job_id, url, kind, format_id, audio_quality, audio_format, cookiefile, platform, convert_after=convert_after)
    finally:
        DOWNLOAD_SEMAPHORE.release()


def _vcodec_is_h264(vcodec):
    v = (vcodec or "").lower()
    return v not in ("", "none") and ("avc" in v or "h264" in v)


def _acodec_is_aac(acodec):
    a = (acodec or "").lower()
    return a not in ("", "none") and ("mp4a" in a or "aac" in a)


def _update_job_processing(job_id, *, stage=None, percent=None):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["status"] = "processing"
        if stage is not None:
            job["processing_stage"] = stage
        if percent is not None:
            job["processing_percent"] = round(min(99.0, max(0.0, percent)), 1)


def _ffmpeg_input_path(cmd):
    try:
        idx = cmd.index("-i")
        return cmd[idx + 1]
    except (ValueError, IndexError):
        return None


def _ffmpeg_cmd_with_progress(cmd):
    """Insert -progress pipe:1 so ffmpeg emits structured out_time_ms lines."""
    if not cmd or cmd[0] != "ffmpeg":
        return cmd
    flags = ["-hide_banner", "-nostats", "-progress", "pipe:1"]
    return [cmd[0], *flags, *cmd[1:]]


def _parse_ffmpeg_progress_line(line, duration_sec):
    """Return 0–99 percent from an ffmpeg progress or stderr status line."""
    if not duration_sec or duration_sec <= 0:
        return None
    m = re.search(r"out_time_ms=(\d+)", line)
    if m:
        elapsed = int(m.group(1)) / 1_000_000.0
        return (elapsed / duration_sec) * 100
    m = re.search(r"out_time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
    if m:
        elapsed = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        return (elapsed / duration_sec) * 100
    m = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
    if m:
        elapsed = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        return (elapsed / duration_sec) * 100
    return None


def _run_ffmpeg(cmd, *, timeout=1800, job_id=None, stage="Processing…", duration_sec=None):
    """Run ffmpeg; when job_id is set, stream -progress pipe:1 into the job poll API."""
    if not job_id:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "ffmpeg failed")[-500:]
            raise RuntimeError(tail)
        return result

    if not duration_sec or duration_sec <= 0:
        src = _ffmpeg_input_path(cmd)
        if src and os.path.isfile(src):
            probe = _probe_media_file(src)
            duration_sec = (probe or {}).get("duration")

    progress_cmd = _ffmpeg_cmd_with_progress(cmd)
    _update_job_processing(job_id, stage=stage, percent=0)
    proc = subprocess.Popen(
        progress_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    err_lines = deque(maxlen=40)
    last_pct = [-1.0]

    def _emit_progress(line):
        pct = _parse_ffmpeg_progress_line(line, duration_sec)
        if pct is None:
            return
        if pct - last_pct[0] >= 0.4 or pct >= 99.0:
            last_pct[0] = pct
            _update_job_processing(job_id, stage=stage, percent=pct)

    def _stdout_reader():
        try:
            for line in proc.stdout:
                if "progress=end" in line:
                    _update_job_processing(job_id, stage=stage, percent=99)
                _emit_progress(line)
        except Exception:
            pass

    def _stderr_reader():
        try:
            for line in proc.stderr:
                err_lines.append(line)
                _emit_progress(line)
        except Exception:
            pass

    readers = [
        threading.Thread(target=_stdout_reader, daemon=True),
        threading.Thread(target=_stderr_reader, daemon=True),
    ]
    for t in readers:
        t.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        raise RuntimeError(f"{stage} timed out after {timeout // 60} min — try 720p or phone-friendly quality.")
    for t in readers:
        t.join(timeout=2)
    if proc.returncode != 0:
        tail = "".join(err_lines)[-500:]
        raise RuntimeError(tail or "ffmpeg failed")
    _update_job_processing(job_id, stage=stage, percent=99)
    return proc


def _probe_media_file(path):
    """Return {vcodec, acodec, has_audio, duration} from ffprobe, or None on failure."""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", "-select_streams", "v:0,a:0",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        duration = None
        try:
            duration = float((data.get("format") or {}).get("duration") or 0) or None
        except (TypeError, ValueError):
            duration = None
        if not duration and video:
            try:
                duration = float(video.get("duration") or 0) or None
            except (TypeError, ValueError):
                duration = None
        height = None
        if video:
            try:
                height = int(video.get("height") or 0) or None
            except (TypeError, ValueError):
                height = None
        return {
            "vcodec": (video or {}).get("codec_name"),
            "acodec": (audio or {}).get("codec_name"),
            "has_audio": audio is not None,
            "duration": duration,
            "height": height,
        }
    except Exception:
        return None


def _is_ios_playable(info, *, require_audio=False):
    """iPhone needs H.264 video and AAC audio (when audio exists) in MP4."""
    if not info or not info.get("vcodec"):
        return False
    if not _vcodec_is_h264(info["vcodec"]):
        return False
    if require_audio and not info.get("has_audio"):
        return False
    if info.get("has_audio") and not _acodec_is_aac(info["acodec"]):
        return False
    return True


def _mp4_faststart(path, job_id=None):
    """Move moov atom to file start — helps iOS Files / Photos playback."""
    tmp = path + ".faststart.tmp.mp4"
    try:
        _run_ffmpeg([
            "ffmpeg", "-y", "-i", path,
            "-c", "copy", "-movflags", "+faststart",
            tmp,
        ], timeout=120, job_id=job_id, stage="Optimizing for iPhone…")
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _ffmpeg_thread_args():
    """Limit ffmpeg CPU/RAM on small Render instances."""
    return ["-threads", "2"]


def _transcode_to_ios_mp4(src_path, job_id=None, *, max_height=None, crf=28):
    """Re-encode to H.264 + AAC MP4 when remux left incompatible codecs."""
    info = _probe_media_file(src_path)
    duration = (info or {}).get("duration")
    base, _ = os.path.splitext(src_path)
    dest = base + "_ios.mp4"
    cap = max_height if max_height is not None else TRANSCODE_MAX_HEIGHT
    vf = None
    height = (info or {}).get("height")
    if height and height > cap:
        vf = f"scale=-2:{cap}"
    h264 = info and _vcodec_is_h264(info.get("vcodec"))
    has_audio = info and info.get("has_audio")
    aac_ok = info and _acodec_is_aac(info.get("acodec"))
    stage = "Converting for phones…"
    if vf:
        stage = f"Converting for phones ({cap}p)…"
    cmd = ["ffmpeg", "-y", "-i", src_path, "-map", "0:v:0", *_ffmpeg_thread_args()]
    if h264 and not vf:
        cmd.extend(["-c:v", "copy"])
    else:
        if vf:
            cmd.extend(["-vf", vf])
        cmd.extend([
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", str(crf),
            "-pix_fmt", "yuv420p",
        ])
    if has_audio:
        cmd.extend(["-map", "0:a:0?"])
        if aac_ok:
            cmd.extend(["-c:a", "copy"])
        else:
            cmd.extend(["-c:a", "aac", "-b:a", "128k"])
    else:
        cmd.append("-an")
    cmd.extend(["-movflags", "+faststart", dest])
    try:
        _run_ffmpeg(
            cmd,
            timeout=TRANSCODE_TIMEOUT,
            job_id=job_id,
            stage=stage,
            duration_sec=duration,
        )
    except Exception:
        if os.path.isfile(dest):
            os.remove(dest)
        raise
    os.remove(src_path)
    return dest


def _simple_remux_mp4(src_path, job_id=None):
    """Cheap fallback: copy streams into MP4 + faststart."""
    base, _ = os.path.splitext(src_path)
    dest = base + "_ios.mp4"
    try:
        _run_ffmpeg([
            "ffmpeg", "-y", "-i", src_path,
            *_ffmpeg_thread_args(),
            "-c", "copy", "-movflags", "+faststart",
            dest,
        ], timeout=120, job_id=job_id, stage="Remuxing for phones…")
    except Exception:
        if os.path.isfile(dest):
            os.remove(dest)
        raise
    os.remove(src_path)
    return dest


def _ensure_ios_playable_mp4(path, job_id=None):
    """Ensure downloaded video plays in iOS Files / Photos / Chrome.

    Returns (filepath, convert_warning). Never raises — on failure serves the original.
    """
    if not os.path.isfile(path):
        return path, None

    ext = os.path.splitext(path)[1].lower()
    if ext not in (".mp4", ".m4v", ".mov", ".webm", ".mkv"):
        return path, None

    info = _probe_media_file(path)
    if info and _is_ios_playable(info):
        try:
            _mp4_faststart(path, job_id=job_id)
        except Exception as e:
            app.logger.warning(f"faststart remux skipped: {e}")
        if not path.lower().endswith(".mp4"):
            mp4_path = os.path.splitext(path)[0] + ".mp4"
            os.replace(path, mp4_path)
            return mp4_path, None
        return path, None

    app.logger.info(
        "Transcoding for iOS playback: v=%s a=%s has_audio=%s",
        (info or {}).get("vcodec"), (info or {}).get("acodec"),
        (info or {}).get("has_audio"),
    )
    last_err = None
    for attempt, fn in (
        ("full", lambda: _transcode_to_ios_mp4(path, job_id=job_id)),
        ("remux", lambda: _simple_remux_mp4(path, job_id=job_id)),
        ("lite", lambda: _transcode_to_ios_mp4(path, job_id=job_id, max_height=480, crf=32)),
    ):
        try:
            return fn(), None
        except Exception as e:
            last_err = e
            app.logger.warning("iOS convert attempt %s failed: %s", attempt, e)
            if not os.path.isfile(path):
                # A successful attempt removes src_path; only continue if original still exists.
                break

    brief = _brief_technical(str(last_err or "convert failed"))
    return path, (
        f"Phone convert skipped ({brief}). Original file saved — "
        "may not play in Photos; try VLC or Save file again."
    )


def _prepare_video_fast_path(path):
    """Finish immediately after download — quick faststart only when already iOS-playable."""
    if not os.path.isfile(path):
        return path, False, None

    ext = os.path.splitext(path)[1].lower()
    if ext not in (".mp4", ".m4v", ".mov", ".webm", ".mkv"):
        return path, True, None

    info = _probe_media_file(path)
    phone_compatible = bool(info and _is_ios_playable(info, require_audio=True))

    if phone_compatible:
        try:
            _mp4_faststart(path, job_id=None)
        except Exception as e:
            app.logger.warning(f"faststart remux skipped: {e}")
        if not path.lower().endswith(".mp4"):
            mp4_path = os.path.splitext(path)[0] + ".mp4"
            os.replace(path, mp4_path)
            return mp4_path, True, None
        return path, True, None

    return path, False, (
        "May not play on some iPhones — tap Make phone-friendly to convert."
    )


def _finish_job(job_id, job_dir, filename, kind, platform, *, convert_warning=None, phone_compatible=None):
    cleanup_path_later(job_dir)
    finished = {
        "status": "finished",
        "percent": 100,
        "filename": filename,
        "download_url": f"/api/file/{job_id}",
        "kind": kind,
    }
    if phone_compatible is not None:
        finished["phone_compatible"] = phone_compatible
    if convert_warning:
        finished["convert_warning"] = convert_warning
    with JOBS_LOCK:
        JOBS[job_id] = finished
    record_event(platform, kind)


def run_convert_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job or job.get("status") != "finished" or job.get("kind") != "video":
            return
        if job.get("phone_compatible") and not job.get("convert_warning"):
            return
        filename = job.get("filename")
        kind = job.get("kind", "video")
        job["status"] = "processing"
        job["processing_stage"] = "Converting for phones…"
        job["processing_percent"] = 0

    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    filepath = os.path.join(job_dir, filename)
    if not os.path.isfile(filepath):
        with JOBS_LOCK:
            JOBS[job_id] = {"status": "error", "error": "File no longer available for convert."}
        return

    try:
        new_path, convert_warning = _ensure_ios_playable_mp4(filepath, job_id=job_id)
        filename = os.path.basename(new_path)
        if not filename.lower().endswith(".mp4"):
            mp4_name = os.path.splitext(filename)[0] + ".mp4"
            mp4_path = os.path.join(job_dir, mp4_name)
            if not os.path.exists(mp4_path):
                os.replace(new_path, mp4_path)
            filename = mp4_name

        finished = {
            "status": "finished",
            "percent": 100,
            "filename": filename,
            "download_url": f"/api/file/{job_id}",
            "kind": kind,
            "phone_compatible": convert_warning is None,
        }
        if convert_warning:
            finished["convert_warning"] = convert_warning
        with JOBS_LOCK:
            JOBS[job_id] = finished
    except Exception as e:
        app.logger.warning(f"convert job failed: {e}")
        with JOBS_LOCK:
            prev = JOBS.get(job_id) or {}
            JOBS[job_id] = {
                "status": "finished",
                "percent": 100,
                "filename": prev.get("filename", filename),
                "download_url": f"/api/file/{job_id}",
                "kind": kind,
                "phone_compatible": False,
                "convert_warning": f"Phone convert failed — original file still available. {_brief_technical(str(e))}",
            }


def _format_has_audio(f):
    return f.get("acodec") not in (None, "none")


def _formats_have_audio(formats):
    return any(_format_has_audio(f) for f in (formats or []))


def _video_merge_postprocessors():
    """Merge split DASH/HLS streams, then remux to MP4."""
    return [
        {"key": "FFmpegMerger"},
        {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"},
    ]


def _is_hls_format(f):
    ext = (f.get("ext") or "").lower()
    proto = (f.get("protocol") or "").lower()
    return ext in ("m3u8", "m3u8_native") or "m3u8" in proto


def _platform_key(platform):
    s = (platform or "").lower()
    if "youtube" in s:
        return "youtube"
    if "tiktok" in s:
        return "tiktok"
    if "instagram" in s:
        return "instagram"
    if s.startswith("x") or "twitter" in s:
        return "x"
    if "facebook" in s:
        return "facebook"
    if "vimeo" in s:
        return "vimeo"
    if "dailymotion" in s:
        return "dailymotion"
    return "default"


def _mobile_video_format_string(format_id=None, platform=None):
    """Prefer phone-playable H.264 + audio in MP4 across all platforms."""
    pkey = _platform_key(platform)

    if pkey == "tiktok":
        auto = (
            "download/"
            "best[ext=mp4][vcodec^=avc1][acodec!=none][height<=1080]/"
            "best[ext=mp4][vcodec*=avc1][acodec!=none][height<=1080]/"
            "best[ext=mp4][acodec!=none][height<=1080]/"
            "bestvideo[vcodec^=avc1][height<=1080]+bestaudio/"
            "best[height<=1080]/best"
        )
    elif pkey == "instagram":
        auto = (
            "best[ext=mp4][vcodec^=avc1][acodec!=none][height<=720]/"
            "best[ext=mp4][vcodec*=avc1][acodec!=none][height<=720]/"
            "best[ext=mp4][vcodec^=avc1][acodec!=none][height<=1080]/"
            "best[ext=mp4][vcodec*=avc1][acodec!=none][height<=1080]/"
            "best[ext=mp4][acodec!=none][height<=720]/"
            "bestvideo[vcodec^=avc1][height<=720]+bestaudio[ext=m4a]/"
            "bestvideo[ext=mp4][height<=720]+bestaudio/"
            "best[height<=720]/best"
        )
    elif pkey in ("x", "facebook", "vimeo", "dailymotion", "default"):
        auto = (
            "best[ext=mp4][vcodec^=avc1][acodec!=none][height<=1080]/"
            "best[ext=mp4][acodec!=none][height<=1080]/"
            "bestvideo[vcodec^=avc1][height<=1080]+bestaudio[acodec^=mp4a]/"
            "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/"
            "best[ext=mp4][height<=1080]/best[height<=1080]/best"
        )
    else:
        # YouTube (DASH-first)
        auto = (
            "bestvideo[vcodec^=avc1][height<=1080]+bestaudio[acodec^=mp4a]/"
            "bestvideo[vcodec*=avc1][height<=1080]+bestaudio[acodec*=mp4a]/"
            "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/"
            "best[ext=mp4][height<=1080]/best[height<=1080]/best"
        )

    if not format_id:
        return auto

    # Always merge audio before bare format_id — video-only DASH/HLS ids are silent.
    return (
        f"{format_id}+bestaudio[acodec^=mp4a]/"
        f"{format_id}+bestaudio[ext=m4a]/"
        f"{format_id}+bestaudio/"
        f"{format_id}[acodec!=none]/"
        f"{auto}"
    )


def _pick_info_formats(raw_formats, platform=None):
    """Build a short quality list biased toward mobile-playable MP4 with audio."""
    pkey = _platform_key(platform)
    candidates = []
    for f in raw_formats or []:
        if f.get("vcodec") in (None, "none"):
            continue
        height = f.get("height")
        if not height:
            res = f.get("resolution") or ""
            m = re.search(r"(\d{3,4})", str(res))
            height = int(m.group(1)) if m else None
        if not height:
            continue
        entry = dict(f)
        entry["height"] = height
        candidates.append(entry)

    def quality_score(f):
        h264 = 3 if _vcodec_is_h264(f.get("vcodec")) else 0
        aac = 2 if _acodec_is_aac(f.get("acodec")) else 0
        has_aud = 4 if _format_has_audio(f) else -6
        mp4 = 1 if (f.get("ext") or "").lower() == "mp4" else 0
        hls_penalty = -3 if _is_hls_format(f) else 0
        tiktok_dl = 4 if pkey == "tiktok" and str(f.get("format_id") or "") == "download" else 0
        ig_progressive = 0
        if pkey == "instagram":
            proto = (f.get("protocol") or "").lower()
            if proto in ("https", "http") and not _is_hls_format(f) and _format_has_audio(f):
                ig_progressive = 4
        return h264 + aac + has_aud + mp4 + tiktok_dl + ig_progressive + hls_penalty

    def pick_score(f):
        h = f.get("height") or 0
        return (quality_score(f), h)

    candidates.sort(key=pick_score, reverse=True)

    formats = []
    seen_heights = set()
    for f in candidates:
        height = f["height"]
        if height in seen_heights:
            continue
        same_h = [x for x in candidates if x.get("height") == height]
        pick = max(same_h, key=pick_score)
        seen_heights.add(height)
        ext = pick.get("ext") or "mp4"
        has_audio = _format_has_audio(pick)
        compatible = (
            _vcodec_is_h264(pick.get("vcodec"))
            and _acodec_is_aac(pick.get("acodec"))
            and ext == "mp4"
            and has_audio
        )
        formats.append({
            "format_id": pick["format_id"],
            "height": height,
            "ext": ext,
            "compatible": compatible,
            "has_audio": has_audio,
            "filesize_approx": pick.get("filesize") or pick.get("filesize_approx"),
        })
        if len(formats) >= 8:
            break

    # Surface phone-ready options first so the default picker avoids slow transcodes.
    formats.sort(key=lambda x: (x["compatible"], x["height"]), reverse=True)
    return formats


_SKIP_OUTPUT_SUFFIXES = (".part", ".tmp", ".ytdl", ".frag")


def _pick_job_output_file(job_dir, kind):
    """Pick the largest real media file — never a sidecar/thumbnail by listdir order."""
    entries = []
    for name in os.listdir(job_dir):
        if name.startswith("."):
            continue
        low = name.lower()
        if any(low.endswith(s) for s in _SKIP_OUTPUT_SUFFIXES):
            continue
        path = os.path.join(job_dir, name)
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(name)[1].lower()
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size <= 0:
            continue
        entries.append((size, name, ext))

    if not entries:
        raise RuntimeError("No output file was produced.")

    if kind == "video":
        pool = [e for e in entries if e[2] in (".mp4", ".m4v", ".mov", ".webm", ".mkv")] or entries
    elif kind == "audio":
        pool = [e for e in entries if e[2] in (".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac", ".aac")] or entries
    elif kind == "thumbnail":
        pool = [e for e in entries if e[2] in (".jpg", ".jpeg", ".png", ".webp")] or entries
    elif kind == "subtitles":
        pool = [e for e in entries if e[2] in (".vtt", ".srt", ".ass")] or entries
    else:
        pool = entries
    return max(pool, key=lambda e: e[0])[1]


def _ensure_video_has_audio(filepath, job_dir, url, cookiefile, platform, format_id, job_id):
    """If output is silent but the source has audio, mux in bestaudio."""
    probe = _probe_media_file(filepath)
    if not probe or probe.get("has_audio"):
        return filepath

    try:
        info = extract_with_fallback(url, cookiefile, skip_download=True, noplaylist=True)
    except Exception:
        return filepath

    if not _formats_have_audio(info.get("formats", [])):
        return filepath

    app.logger.warning("Output missing audio — muxing bestaudio for %s", url)
    _update_job_processing(job_id, stage="Adding audio…", percent=0)

    audio_dir = os.path.join(job_dir, "_audio_tmp")
    os.makedirs(audio_dir, exist_ok=True)
    audio_tmpl = os.path.join(audio_dir, "%(id)s.%(ext)s")

    def build_audio_opts(clients):
        opts = base_ydl_opts(cookiefile, noplaylist=True, player_clients=clients)
        opts["outtmpl"] = audio_tmpl
        opts["format"] = "bestaudio[ext=m4a]/bestaudio[acodec!=none]/bestaudio/best[acodec!=none]"
        return opts

    try:
        download_with_fallback(url, build_audio_opts)
        audio_name = _pick_job_output_file(audio_dir, "audio")
        audio_path = os.path.join(audio_dir, audio_name)
        if not os.path.isfile(audio_path):
            return filepath

        base, _ = os.path.splitext(filepath)
        merged = base + "_merged.mp4"
        duration = (probe or {}).get("duration")
        _run_ffmpeg([
            "ffmpeg", "-y", "-i", filepath, "-i", audio_path,
            *_ffmpeg_thread_args(),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-movflags", "+faststart",
            merged,
        ], timeout=TRANSCODE_TIMEOUT, job_id=job_id, stage="Adding audio…", duration_sec=duration)

        merged_probe = _probe_media_file(merged)
        if not merged_probe or not merged_probe.get("has_audio"):
            if os.path.isfile(merged):
                os.remove(merged)
            return filepath

        os.remove(filepath)
        return merged
    except Exception as e:
        app.logger.warning("Audio mux fallback failed: %s", e)
        return filepath
    finally:
        shutil.rmtree(audio_dir, ignore_errors=True)


def _do_download(job_id, url, kind, format_id, audio_quality, audio_format, cookiefile, platform, convert_after=False):
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    outtmpl = os.path.join(job_dir, "%(title).80s.%(ext)s")

    def build_opts(clients):
        opts = base_ydl_opts(cookiefile, noplaylist=True, player_clients=clients)
        opts["outtmpl"] = outtmpl
        opts["progress_hooks"] = [make_progress_hook(job_id)]

        if kind == "video":
            opts["format"] = _mobile_video_format_string(format_id, platform)
            opts["merge_output_format"] = "mp4"
            opts["format_sort"] = [
                "hasvid", "hasaud", "vcodec:h264", "acodec:mp4a",
                "ext:mp4:m4a", "proto:https", "res:1080", "size",
            ]
            opts["postprocessors"] = _video_merge_postprocessors()
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

        filename = _pick_job_output_file(job_dir, kind)
        # Keep a short readable name for the user's save dialog.
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", filename).strip(" .") or f"{kind}.bin"
        if safe_name != filename:
            dest = os.path.join(job_dir, safe_name)
            if not os.path.exists(dest):
                os.replace(os.path.join(job_dir, filename), dest)
                filename = safe_name

        convert_warning = None
        phone_compatible = None
        if kind == "video":
            filepath = os.path.join(job_dir, filename)
            filepath = _ensure_video_has_audio(
                filepath, job_dir, url, cookiefile, platform, format_id, job_id,
            )
            filename = os.path.basename(filepath)
            if convert_after:
                with JOBS_LOCK:
                    if JOBS.get(job_id):
                        JOBS[job_id]["status"] = "processing"
                        JOBS[job_id]["processing_stage"] = "Converting for phones…"
                        JOBS[job_id]["processing_percent"] = 0
                filepath, convert_warning = _ensure_ios_playable_mp4(filepath, job_id=job_id)
                filename = os.path.basename(filepath)
                phone_compatible = convert_warning is None
            else:
                filepath, phone_compatible, convert_warning = _prepare_video_fast_path(filepath)
                filename = os.path.basename(filepath)

        _finish_job(
            job_id, job_dir, filename, kind, platform,
            convert_warning=convert_warning,
            phone_compatible=phone_compatible,
        )

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
    format_id = (data.get("format_id") or "").strip() or None
    audio_quality = data.get("audio_quality")
    audio_format = data.get("audio_format")
    cookie_token = data.get("cookie_token")
    convert_after = bool(data.get("convert")) or request.args.get("convert") in ("1", "true", "yes")

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
        JOBS[job_id] = {"status": "queued", "percent": 0, "kind": kind}
    with QUEUE_LOCK:
        QUEUE_ORDER.append(job_id)

    thread = threading.Thread(
        target=run_download_job,
        args=(job_id, url, kind, format_id, audio_quality, audio_format, cookiefile, convert_after),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id, "normalized_url": url, "url_fixes": url_fixes})


@app.route("/api/convert/<job_id>", methods=["POST"])
@limiter.limit("20 per hour")
def api_convert(job_id):
    if not job_id or not re.fullmatch(r"[0-9a-fA-F-]{36}", job_id):
        return jsonify({"error": "Invalid job id."}), 400
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job."}), 404
        if job.get("kind") != "video":
            return jsonify({"error": "Only video jobs can be converted."}), 400
        if job.get("status") == "processing":
            return jsonify({"status": "processing", "message": "Convert already in progress."}), 409
        if job.get("status") != "finished":
            return jsonify({"error": "Job is not ready for convert."}), 400
        if job.get("phone_compatible") and not job.get("convert_warning"):
            return jsonify({"status": "finished", "message": "Already phone-friendly.", "phone_compatible": True})

    thread = threading.Thread(target=run_convert_job, args=(job_id,), daemon=True)
    thread.start()
    return jsonify({"status": "processing", "job_id": job_id})


@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "Unknown job."}), 404
    return jsonify(job)


def _resolve_job_filepath(job_id, filename=None):
    """Resolve a download path safely under DOWNLOAD_DIR/<job_id>/."""
    if not job_id or not re.fullmatch(r"[0-9a-fA-F-]{36}", job_id):
        return None, None
    job_dir = os.path.realpath(os.path.join(DOWNLOAD_DIR, job_id))
    root = os.path.realpath(DOWNLOAD_DIR)
    if job_dir != root and not job_dir.startswith(root + os.sep):
        return None, None
    if not os.path.isdir(job_dir):
        return None, None

    if filename:
        filename = unquote(filename or "").replace("\\", "/").split("/")[-1]
        candidate = os.path.realpath(os.path.join(job_dir, filename))
        if candidate.startswith(job_dir + os.sep) and os.path.isfile(candidate):
            return candidate, os.path.basename(candidate)

    with JOBS_LOCK:
        stored = (JOBS.get(job_id) or {}).get("filename")
    if stored:
        candidate = os.path.realpath(os.path.join(job_dir, stored))
        if candidate.startswith(job_dir + os.sep) and os.path.isfile(candidate):
            return candidate, os.path.basename(candidate)

    candidates = [f for f in os.listdir(job_dir) if not f.startswith(".")]
    if len(candidates) == 1:
        name = candidates[0]
        return os.path.join(job_dir, name), name
    return None, None


@app.route("/api/file/<job_id>")
@app.route("/api/file/<job_id>/<path:filename>")
def serve_file(job_id, filename=None):
    filepath, download_name = _resolve_job_filepath(job_id, filename)
    if not filepath:
        abort(404)
    # ASCII fallback name avoids Content-Disposition issues on some mobile browsers.
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", download_name).strip("._") or "download.mp4"
    safe_name = download_name if download_name.isascii() else ascii_name
    ext = os.path.splitext(safe_name)[1].lower()
    mime, _ = mimetypes.guess_type(safe_name)
    if ext == ".mp4":
        mime = "video/mp4"
    elif ext == ".webm":
        mime = "video/webm"
    elif ext in (".m4a", ".m4v"):
        mime = "audio/mp4" if ext == ".m4a" else "video/mp4"
    response = send_file(
        filepath,
        as_attachment=True,
        download_name=safe_name,
        mimetype=mime or "application/octet-stream",
        conditional=True,
    )
    # Explicit type helps iOS Web Share accept the fetched blob as a video file.
    if mime:
        response.headers["Content-Type"] = mime
    return response


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
        "static_version": STATIC_VERSION,
        "cookies": bool(resolve_cookiefile()),
        "proxy": bool(YTDLP_PROXY),
        "proxy_configured": PROXY_STATUS["configured"],
        "proxy_valid": PROXY_STATUS["valid"],
        "proxy_reason": PROXY_STATUS["reason"],
        "pot": POT_ENABLED,
        "pot_url": POT_PROVIDER_URL if POT_ENABLED else None,
        "pot_reachable": pot_reachable(),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
