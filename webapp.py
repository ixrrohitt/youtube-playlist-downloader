"""
3·4·5D — YouTube Downloader Web UI (single file, stdlib + yt-dlp).

Run:  python webapp.py   → open the printed URL (localhost or Codespaces).
UI: 3D warp-speed starfield, rotating 3D status cube, video+audio MERGE fusion
animation, ZIP crush animation, confetti + shockwave on completion, tilt cards,
sound blips, auto-fetch on paste, Ctrl+Enter to fetch/download.
"""
from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import socketserver
import sys
import threading
import time
import traceback
import uuid
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse


def _auto_install(pkgs: list[str]) -> None:
    """pip install; retries with --break-system-packages for PEP 668 distros (Ubuntu/Mint/Debian)."""
    import subprocess
    print(f"[setup] Installing: {', '.join(pkgs)} …")
    base = [sys.executable, "-m", "pip", "install", "-U", "--quiet", *pkgs]
    for extra in ([], ["--break-system-packages"], ["--user", "--break-system-packages"]):
        try:
            subprocess.check_call(base + extra, stderr=subprocess.DEVNULL if extra else None)
            return
        except subprocess.CalledProcessError:
            continue
    print("[setup] pip install failed. Run manually (inside a venv is best):\n  "
          + " ".join(base) + "   (add --break-system-packages if pip says 'externally-managed')")
    sys.exit(1)


try:
    from yt_dlp import YoutubeDL
except ImportError:
    _auto_install(["yt-dlp[default]"])
    from yt_dlp import YoutubeDL  # type: ignore

try:
    import yt_dlp_ejs  # type: ignore  # noqa: F401
    _HAS_EJS = True
except ImportError:
    try:
        _auto_install(["yt-dlp-ejs"])
        import yt_dlp_ejs  # type: ignore  # noqa: F401
        _HAS_EJS = True
    except Exception:
        _HAS_EJS = False
        print("[setup] Warning: yt-dlp-ejs missing; YouTube may block downloads.")

def _find_ffmpeg():
    """System ffmpeg, else a bundled static one via imageio-ffmpeg (needed to MERGE video+audio)."""
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg  # type: ignore
    except ImportError:
        try:
            _auto_install(["imageio-ffmpeg"])
            import imageio_ffmpeg  # type: ignore
        except (Exception, SystemExit):
            return None
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


_FFMPEG = _find_ffmpeg()
if _FFMPEG:
    print(f"[setup] ffmpeg: {_FFMPEG}")
else:
    print("[setup] Warning: ffmpeg not found — merging / mp3 conversion will fail.")
    print("        apt: sudo apt-get install -y ffmpeg | brew install ffmpeg | winget install Gyan.FFmpeg")
_HAS_DENO = bool(shutil.which("deno") or shutil.which("node"))
if not _HAS_DENO:
    print("[setup] Warning: no 'deno'/'node' on PATH (needed for YouTube n-challenge).")

try:
    from yt_dlp.utils import DownloadCancelled
except ImportError:  # very old yt-dlp
    class DownloadCancelled(Exception):  # type: ignore
        pass


class _Cancel(DownloadCancelled):
    pass


ROOT = Path.cwd()
DOWNLOADS_DIR = ROOT / "downloads"
COOKIES_DIR = ROOT / ".webapp_cookies"
DOWNLOADS_DIR.mkdir(exist_ok=True)
COOKIES_DIR.mkdir(exist_ok=True)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
MAX_LOG_LINES = 250
AUDIO_EXT = {".mp3", ".m4a", ".opus", ".wav"}


# ---------------- helpers ----------------
def _list_downloaded_files() -> list[dict]:
    out: list[dict] = []
    files = [p for p in DOWNLOADS_DIR.rglob("*") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for fp in files:
        try:
            st = fp.stat()
            rel = fp.relative_to(DOWNLOADS_DIR).as_posix()
            ext = fp.suffix.lower()
            out.append({
                "name": fp.name, "path": rel, "url": "/downloads/" + quote(rel),
                "size_mb": round(st.st_size / 1048576, 2), "mtime": int(st.st_mtime),
                "is_zip": ext == ".zip",
                "kind": "audio" if ext in AUDIO_EXT else ("zip" if ext == ".zip" else "video"),
            })
        except OSError:
            continue
    return out


def _split_urls(text: str) -> list[str]:
    return [p for p in re.split(r"[\s,]+", text.strip()) if p.startswith("http")]


def _is_playlist_url(url: str) -> bool:
    try:
        return "list=" in urlparse(url).query
    except Exception:
        return False


def _safe_cookie(name: str) -> str:
    """Only allow files that live inside COOKIES_DIR (blocks arbitrary path access)."""
    if not name:
        return ""
    p = COOKIES_DIR / Path(name).name
    return str(p) if p.exists() else ""


def _job_log(job_id: str, message: str, level: str = "info") -> None:
    line = {"time": time.strftime("%H:%M:%S"), "level": level, "message": str(message).strip()}
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        logs = job.setdefault("log", [])
        logs.append(line)
        del logs[:-MAX_LOG_LINES]
        job["last_message"] = line["message"]


def _job_update(job_id: str, **values) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(values)


class YtdlpJobLogger:
    def __init__(self, job_id: str):
        self.job_id = job_id

    def debug(self, msg):
        t = str(msg)
        if t and not t.startswith("[debug]"):
            _job_log(self.job_id, t, "debug")

    def warning(self, msg):
        _job_log(self.job_id, msg, "warn")

    def error(self, msg):
        _job_log(self.job_id, msg, "error")


def _friendly_error(error: Exception) -> str:
    msg = str(error)
    low = msg.lower()
    if any(k in low for k in ("login required", "rate-limit reached", "need to log in", "log in to",
                              "requested content is not available", "nsfw", "restricted video")):
        return ("This site wants a logged-in session. Export cookies.txt from your browser while "
                "logged in (Cookies button), upload it and retry.")
    if "unsupported url" in low:
        return "This link isn't supported (or it's a profile/page, not a post). Paste a direct post/video link."
    if "no video could be found" in low or "there is no video" in low:
        return "No video in this post (image-only?). Photos are tried automatically; if it still fails, upload cookies.txt."
    missing = []
    if not _HAS_EJS:
        missing.append("pip install -U yt-dlp-ejs")
    if not _HAS_DENO:
        missing.append("install deno or node")
    hint = ("\nMissing: " + "; ".join(missing)) if missing else ""
    if any(k in msg for k in ("n challenge", "JavaScript runtime", "challenge solver",
                              "Requested format is not available", "Only images are available")):
        return "YouTube's bot-check solver failed. Install yt-dlp-ejs + deno/node, upgrade yt-dlp, retry." + hint
    if "Sign in to confirm" in msg or "not a bot" in msg:
        return "YouTube wants verification. Upload a fresh cookies.txt and make sure the JS solver is installed." + hint
    if "429" in msg or "Too Many Requests" in msg:
        return "Rate-limited by YouTube. Wait a few minutes or use fresh cookies."
    if "HTTP Error 403" in msg:
        return "YouTube blocked this request. Refresh cookies.txt and retry."
    if "HTTP Error 404" in msg:
        return "Video not found or private."
    return msg or "Something went wrong."


# ---------------- probing ----------------
_EST_BPS = {144: 19e3, 240: 37e3, 360: 62e3, 480: 112e3, 720: 225e3, 1080: 437e3}  # rough bytes/sec incl. audio
_DEF_RES = sorted(_EST_BPS)


def _fsize(f: dict, dur) -> int:
    s = f.get("filesize") or f.get("filesize_approx")
    if not s and f.get("tbr") and dur:
        s = f["tbr"] * 125 * dur  # kbit/s -> bytes
    return int(s) if s else 0


def _sizes_from_formats(fm: list, dur) -> dict:
    """{height: bytes of the video(+best audio) that would be downloaded}."""
    aud = [f for f in fm if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")]
    best_a = max(aud, key=lambda f: (f.get("ext") == "m4a", f.get("abr") or 0), default=None)
    asz = _fsize(best_a, dur) if best_a else 0
    by: dict = {}
    for f in fm:
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none"):
            continue
        sz = _fsize(f, dur)
        if not sz:
            continue
        key = (f.get("ext") == "mp4", sz)  # mp4 is what the downloader prefers
        has_audio = f.get("acodec") not in (None, "none")
        if h not in by or key > by[h][0]:
            by[h] = (key, sz + (0 if has_audio else asz))
    return {str(h): v[1] for h, v in by.items()}


def _entry(info: dict, url: str = "") -> dict:
    dur = info.get("duration")
    thumbs = info.get("thumbnails") or []
    th = info.get("thumbnail") or (thumbs[-1].get("url") if thumbs else None)
    sizes = _sizes_from_formats(info.get("formats") or [], dur)
    est = False
    if not sizes and dur:  # flat playlist entries: no formats, estimate from duration
        sizes, est = {str(h): int(dur * bps) for h, bps in _EST_BPS.items()}, True
    aud = [f for f in (info.get("formats") or []) if f.get("vcodec") in (None, "none")
           and f.get("acodec") not in (None, "none") and f.get("abr")]
    ba = max(aud, key=lambda f: f["abr"], default=None)
    return {"title": info.get("title") or url or "(untitled)", "url": info.get("webpage_url") or url,
            "duration": dur, "thumbnail": th, "sizes": sizes, "est": est,
            "abr": int(ba["abr"]) if ba else None,
            "acodec": (ba.get("acodec") or "").split(".")[0] if ba else None,
            "audio": int(dur * 40000) if dur else None}  # mp3 320 kbps = 40 KB/s


_SPOTIFY_RE = re.compile(r"open\.spotify\.com/(?:intl-[\w-]+/)?(track|album|playlist)/([A-Za-z0-9]+)")


def _spotify_ref(url: str):
    m = _SPOTIFY_RE.search(url or "")
    return (m.group(1), m.group(2)) if m else None


def _dig(obj, *keys):
    for k in keys:
        if not isinstance(obj, dict):
            return {}
        obj = obj.get(k) or {}
    return obj


SPOTIFY_AUTH: dict = {}
_SP_SCOPE = "playlist-read-private playlist-read-collaborative"


def _spotify_redirect() -> str:
    env = os.environ.get("SPOTIFY_REDIRECT_URI", "").strip()
    if env:
        return env
    port = int(os.environ.get("PORT") or 8000)
    dom, name = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN"), os.environ.get("CODESPACE_NAME")
    if dom and name:
        return f"https://{name}-{port}.{dom}/api/spotify/callback"
    return f"http://127.0.0.1:{port}/api/spotify/callback"


def _spotify_token_request(form: dict) -> None:
    import urllib.error
    import urllib.request
    req = urllib.request.Request("https://accounts.spotify.com/api/token", data=urlencode(form).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            js = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode("utf-8", "ignore")[:200])
    SPOTIFY_AUTH["access"] = js["access_token"]
    SPOTIFY_AUTH["exp"] = time.time() + int(js.get("expires_in", 3600)) - 30
    if js.get("refresh_token"):
        SPOTIFY_AUTH["refresh"] = js["refresh_token"]
    try:
        (COOKIES_DIR / "spotify_token.json").write_text(json.dumps({"refresh": SPOTIFY_AUTH.get("refresh", "")}))
    except OSError:
        pass


def _spotify_token() -> str | None:
    cid = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    if not cid:
        return None
    if not SPOTIFY_AUTH.get("refresh"):
        try:
            SPOTIFY_AUTH["refresh"] = json.loads((COOKIES_DIR / "spotify_token.json").read_text()).get("refresh", "")
        except Exception:
            pass
    if SPOTIFY_AUTH.get("access") and SPOTIFY_AUTH.get("exp", 0) > time.time():
        return SPOTIFY_AUTH["access"]
    if SPOTIFY_AUTH.get("refresh"):
        try:
            _spotify_token_request({"grant_type": "refresh_token", "refresh_token": SPOTIFY_AUTH["refresh"],
                                    "client_id": cid})
            return SPOTIFY_AUTH["access"]
        except Exception:
            return None
    return None


def _spotapi_rows(sid: str, job_id: str | None) -> list | None:
    """Optional, no login/Premium: full playlist via the `spotapi` package (Spotify web-player route).
    Unofficial, so it can break whenever Spotify changes things. None on any problem."""
    def log(m, lv="warn"):
        if job_id:
            _job_log(job_id, m, lv)
    try:
        from spotapi import PublicPlaylist  # type: ignore
    except Exception as ex:
        log(f"For full playlists without Premium: pip install spotapi pymongo redis  ({type(ex).__name__})")
        return None
    try:
        log("Reading the full list via the web-player route (spotapi)…", "info")
        rows = []
        for chunk in PublicPlaylist(sid).paginate_playlist():
            for it in (chunk or {}).get("items") or []:
                d = ((it or {}).get("itemV2") or {}).get("data") or {}
                if not d.get("name") or d.get("__typename") not in (None, "Track"):
                    continue
                arts = ", ".join(((a or {}).get("profile") or {}).get("name", "")
                                 for a in ((d.get("artists") or {}).get("items") or []))
                rows.append({"title": d["name"], "subtitle": arts,
                             "duration": (d.get("trackDuration") or {}).get("totalMilliseconds") or 0})
        return rows or None
    except Exception as ex:
        log(f"spotapi failed: {type(ex).__name__}: {str(ex)[:140]}")
        return None


def _spotify_api_rows(kind: str, sid: str, job_id: str | None) -> list | None:
    """Full track list through the official Spotify Web API (needs a one-time login). None on any problem."""
    import urllib.error
    import urllib.request

    def log(m, lv="warn"):
        if job_id:
            _job_log(job_id, m, lv)

    tok = _spotify_token()
    if not tok:
        if os.environ.get("SPOTIFY_CLIENT_ID", "").strip():
            log("Not logged in to Spotify yet: open /api/spotify/login in this app's address once, then retry.")
        else:
            log("Official Spotify API login is not set up (it needs a Premium developer app).")
        return None
    base = (f"https://api.spotify.com/v1/playlists/{sid}/items?limit=100" if kind == "playlist"
            else f"https://api.spotify.com/v1/albums/{sid}/tracks?limit=50")
    url, rows, retries, tried_old = base, [], 0, False
    log("Reading the full list through the Spotify API…", "info")
    while url:
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                js = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and retries < 5:
                retries += 1
                time.sleep(min(int(e.headers.get("Retry-After") or 2), 10))
                continue
            if e.code in (400, 404) and kind == "playlist" and not rows and not tried_old:
                tried_old, url = True, base.replace("/items?", "/tracks?")
                continue
            log(f"Spotify API {e.code}: {e.read().decode('utf-8', 'ignore')[:160]}")
            return None
        except Exception as ex:
            log(f"Spotify API failed: {str(ex)[:120]}")
            return None
        for it in js.get("items") or []:
            tr = (it.get("item") or it.get("track")) if kind == "playlist" else it
            if not tr or tr.get("type") not in (None, "track") or not tr.get("name"):
                continue
            rows.append({"title": tr["name"],
                         "subtitle": ", ".join(a.get("name", "") for a in (tr.get("artists") or [])),
                         "duration": tr.get("duration_ms") or 0})
        url = js.get("next")
    return rows


def _spotify_probe(url: str, job_id: str | None = None, limit: int = 200) -> dict:
    # Spotify streams are DRM-protected, so we read track names from the public embed page
    # and download each song's audio from YouTube (search "Artist - Title").
    import urllib.request
    kind, sid = _spotify_ref(url)
    if job_id:
        _job_log(job_id, f"Spotify {kind} detected: reading track list…")
    req = urllib.request.Request(
        f"https://open.spotify.com/embed/{kind}/{sid}",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                 "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=20) as r:
        html = r.read().decode("utf-8", "ignore")
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("Could not read this Spotify link (private, or Spotify changed its page format).")
    ent = _dig(json.loads(m.group(1)), "props", "pageProps", "state", "data", "entity")
    cover = ""
    srcs = (ent.get("coverArt") or {}).get("sources") or (ent.get("visualIdentity") or {}).get("image") or []
    if srcs:
        cover = srcs[-1].get("url") or ""
    rows = ent.get("trackList") or []
    capped = kind != "track" and len(rows) >= 100
    if capped:
        more = _spotapi_rows(sid, job_id) if kind == "playlist" else None
        if not more or len(more) <= len(rows):
            more = _spotify_api_rows(kind, sid, job_id) if _spotify_token() else more
        if more and len(more) > len(rows):
            rows, capped = more, False
            if job_id:
                _job_log(job_id, f"Full list received: {len(rows)} tracks.", "ok")
    if not rows and kind == "track":
        rows = [{"title": ent.get("title") or ent.get("name"),
                 "subtitle": ", ".join(a.get("name", "") for a in (ent.get("artists") or [])),
                 "duration": ent.get("duration")}]
    videos = []
    for t in rows[:max(limit, 1000)]:
        title = (t.get("title") or "").strip()
        artist = (t.get("subtitle") or "").replace("\xa0", " ").strip()
        if not title:
            continue
        dur = (t.get("duration") or 0) / 1000 or None
        q = f"{artist} - {title}".strip(" -")
        videos.append({"title": q, "url": f"ytsearch1:{q} audio", "duration": dur, "thumbnail": cover or None,
                       "sizes": {}, "est": False, "audio": int(dur * 40000) if dur else None})
    if not videos:
        raise ValueError("No tracks found in this Spotify link.")
    if capped and job_id:
        _job_log(job_id, "Only the first ~100 tracks could be read. Split the playlist into parts of <=100 "
                         "songs and paste all the links together.", "warn")
    if job_id:
        _job_log(job_id, f"Found {len(videos)} track(s).", "ok")
    return {"title": ent.get("name") or ent.get("title") or f"Spotify {kind}", "is_playlist": len(videos) > 1,
            "mode": "single" if len(videos) == 1 else "multiple", "urls": [v["url"] for v in videos],
            "videos": videos, "resolutions": _DEF_RES}


def _spotify_many(urls: list, cookies_file: str | None, job_id: str) -> dict:
    """Several links, at least one Spotify: expand Spotify links into tracks, read the rest normally."""
    videos, seen = [], set()
    titles = []
    others = [u for u in urls if not _spotify_ref(u)]
    for u in urls:
        if not _spotify_ref(u):
            continue
        try:
            info = _spotify_probe(u, job_id)
        except Exception as ex:
            _job_log(job_id, f"Could not read {u[:60]}: {str(ex)[:80]}", "warn")
            continue
        titles.append(info["title"])
        for v in info["videos"]:
            if v["url"] not in seen:
                seen.add(v["url"])
                videos.append(v)
    if others:
        for v in _probe_many(others, cookies_file, job_id)["videos"]:
            if v["url"] not in seen:
                seen.add(v["url"])
                videos.append(v)
    if not videos:
        raise ValueError("Nothing could be read from these links.")
    title = titles[0] if len(titles) == 1 and not others else f"{len(videos)} items"
    res = sorted({int(h) for v in videos for h in v["sizes"]}) or _DEF_RES
    return {"title": title, "is_playlist": len(videos) > 1, "mode": "single" if len(videos) == 1 else "multiple",
            "urls": [v["url"] for v in videos], "videos": videos, "resolutions": res}


def probe_url(url: str, cookies_file: str | None, limit: int = 200, job_id: str | None = None) -> dict:
    opts = {"quiet": True, "skip_download": True, "extract_flat": "in_playlist",
            "socket_timeout": 20, "retries": 3, "extractor_retries": 3, "ignoreerrors": False}
    if cookies_file:
        opts["cookiefile"] = cookies_file
    if job_id:
        opts["logger"] = YtdlpJobLogger(job_id)
        _job_log(job_id, "Connecting to YouTube and reading metadata…")
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    title = info.get("title") or "video"
    is_playlist = bool(info.get("_type") == "playlist" or info.get("entries"))
    videos = []
    if is_playlist:
        for e in (info.get("entries") or [])[:limit]:
            if not e:
                continue
            vu = e.get("url") or e.get("webpage_url") or ""
            if vu and not vu.startswith("http"):
                vu = f"https://www.youtube.com/watch?v={vu}"
            videos.append(_entry(e, vu))
    else:
        videos.append(_entry(info, url))
    res = sorted({f["height"] for f in (info.get("formats") or [])
                  if f.get("height") and f.get("vcodec") != "none"}) or _DEF_RES
    if job_id:
        _job_log(job_id, f"Found {len(videos)} item(s). Sizes ready.", "ok")
    return {"title": title, "is_playlist": is_playlist, "videos": videos, "resolutions": res}


def _probe_many(urls: list, cookies_file: str | None, job_id: str) -> dict:
    """Read several links in parallel so we can show real sizes for each."""
    from concurrent.futures import ThreadPoolExecutor
    count = [0]

    def one(u: str) -> dict:
        try:
            opts = {"quiet": True, "skip_download": True, "noplaylist": True, "socket_timeout": 20,
                    "retries": 2, "extractor_retries": 2, "logger": YtdlpJobLogger(job_id)}
            if cookies_file:
                opts["cookiefile"] = cookies_file
            with YoutubeDL(opts) as ydl:
                e = _entry(ydl.extract_info(u, download=False), u)
        except Exception as ex:
            _job_log(job_id, f"Could not read {u[:60]}: {str(ex)[:80]}", "warn")
            e = {"title": u, "url": u, "duration": None, "thumbnail": None, "sizes": {}, "est": False, "audio": None}
        count[0] += 1
        _job_update(job_id, progress=25 + int(70 * count[0] / len(urls)), stage=f"Reading {count[0]}/{len(urls)}")
        _job_log(job_id, f"Read {count[0]}/{len(urls)}: {e['title'][:60]}", "ok")
        return e

    with ThreadPoolExecutor(max_workers=4) as ex:
        videos = list(ex.map(one, urls))
    res = sorted({int(h) for v in videos for h in v["sizes"]}) or _DEF_RES
    return {"title": f"{len(videos)} videos", "is_playlist": False, "mode": "multiple", "urls": urls,
            "videos": videos, "resolutions": res}


def run_probe(job_id: str) -> None:
    with JOBS_LOCK:
        job = JOBS[job_id]
        url_text, cookies_file = job.get("url_text", ""), job.get("cookies_file") or None
    try:
        _job_update(job_id, status="fetching", stage="Parsing links", progress=8)
        urls = _split_urls(url_text)[:20]
        if not urls:
            raise ValueError("Please paste at least one YouTube URL.")
        _job_log(job_id, f"Cookies: {'loaded' if cookies_file else 'not uploaded'}.")
        _job_update(job_id, progress=25, stage="Detecting mode")
        if any(_spotify_ref(u) for u in urls):
            _job_update(job_id, progress=40, stage="Reading Spotify")
            info = _spotify_many(urls, cookies_file, job_id)
        elif len(urls) > 1:
            _job_log(job_id, f"Multiple mode: reading {len(urls)} links in parallel…")
            info = _probe_many(urls, cookies_file, job_id)
        else:
            playlist = _is_playlist_url(urls[0])
            _job_update(job_id, progress=40, stage="Reading playlist" if playlist else "Reading video")
            info = probe_url(urls[0], cookies_file, job_id=job_id)
            info["mode"] = "playlist" if (playlist or info.get("is_playlist")) else "single"
            info["urls"] = urls
        _job_update(job_id, status="probe_done", stage="Ready", progress=100, probe=info)
        _job_log(job_id, "Fetch complete.", "ok")
    except Exception as e:
        traceback.print_exc()
        m = _friendly_error(e)
        _job_update(job_id, status="error", stage="Fetch failed", error=m, progress=100)
        _job_log(job_id, m, "error")


# ---------------- download worker ----------------
def _make_progress_hook(job_id: str):
    def hook(d: dict) -> None:
        fin = d.get("status") == "finished"
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if not j:
                return
            if j.get("cancel"):
                raise _Cancel("Cancelled by user")
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                j["stage"] = "Downloading"
                j["current_file"] = os.path.basename(d.get("filename", ""))
                j["speed"] = d.get("speed") or 0
                j["eta"] = d.get("eta") or 0
                j["current_pct"] = ((d.get("downloaded_bytes") or 0) / total * 100) if total else 0
            elif fin:
                j["current_pct"] = 0
        if fin:
            ai = d.get("info_dict") or {}
            if ai.get("abr") and ai.get("vcodec") in (None, "none"):
                _job_log(job_id, f"Source audio: {(ai.get('acodec') or '?').split('.')[0]} "
                                 f"~{int(ai['abr'])} kbps. Converting to MP3 320k.", "ok")
            _job_log(job_id, "File finished. Merging/converting if needed…", "ok")
    return hook


def _make_pp_hook(job_id: str):
    def hook(d: dict) -> None:
        pp, st = d.get("postprocessor") or "postprocess", d.get("status")
        if st == "started":
            _job_update(job_id, stage=f"Running {pp}")
            _job_log(job_id, f"Started {pp}…")
        elif st == "finished":
            _job_log(job_id, f"Finished {pp}.", "ok")
            if pp == "MoveFiles":
                with JOBS_LOCK:
                    j = JOBS.get(job_id)
                    if j:
                        j["completed_items"] = int(j.get("completed_items") or 0) + 1
    return hook


def _fix_unmerged(out_dir: Path, job_id: str) -> None:
    """Safety net: if yt-dlp left separate video/audio streams (name.f137.mp4 + name.f140.m4a), merge them."""
    if not _FFMPEG:
        return
    import subprocess
    groups: dict = {}
    for fp in out_dir.rglob("*"):
        m = re.match(r"(.+)\.f[\w-]+\.(\w+)$", fp.name)
        if fp.is_file() and m:
            groups.setdefault((fp.parent, m.group(1)), []).append(fp)
    for (parent, base), parts in groups.items():
        if len(parts) != 2:
            continue
        out = parent / f"{base}.mp4"
        _job_update(job_id, stage="Running Merger")
        _job_log(job_id, f"Merge fallback: joining video+audio for '{base}'", "warn")
        for a, b in (parts, parts[::-1]):
            cmd = [_FFMPEG, "-y", "-loglevel", "error", "-i", str(a), "-i", str(b),
                   "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", str(out)]
            if subprocess.run(cmd).returncode == 0 and out.exists() and out.stat().st_size > 0:
                for x in parts:
                    x.unlink(missing_ok=True)
                _job_log(job_id, "Merged successfully.", "ok")
                break
            out.unlink(missing_ok=True)
        else:
            _job_log(job_id, f"Could not merge '{base}'; leaving the separate streams.", "error")


def _gallery_dl_fallback(job_id: str, url: str, out_dir: Path, cookies: str) -> None:
    # yt-dlp only does video/audio. For photo posts (Instagram/X images) try gallery-dl.
    import subprocess
    try:
        import gallery_dl  # type: ignore  # noqa: F401
    except ImportError:
        try:
            _auto_install(["gallery-dl"])
        except SystemExit:
            _job_log(job_id, "Could not install gallery-dl (needed for image posts).", "warn")
            return
    _job_update(job_id, stage="Fetching images")
    _job_log(job_id, "No video found; trying image download (gallery-dl)…", "warn")
    cmd = [sys.executable, "-m", "gallery_dl", "-D", str(out_dir)]
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
        for ln in tail:
            _job_log(job_id, ln, "ok" if r.returncode == 0 else "warn")
    except Exception as ex:
        _job_log(job_id, f"gallery-dl failed: {ex}", "error")


def run_download(job_id: str) -> None:
    with JOBS_LOCK:
        job = JOBS[job_id]
        job.update(status="running", stage="Preparing download", progress=0)
    try:
        out_dir = DOWNLOADS_DIR / job["folder_name"]
        out_dir.mkdir(parents=True, exist_ok=True)
        mode, media = job["mode"], job["media_type"]
        chosen = int(job.get("resolution") or 0)
        cookies = _safe_cookie(job.get("cookies_file") or "")
        if media == "audio":
            fmt = "bestaudio/best"
        else:
            h = f"[height<={chosen}]" if chosen else ""
            fmt = f"bv*[ext=mp4]{h}+ba[ext=m4a]/bv*{h}+ba/b{h}/b"
        _job_log(job_id, f"Mode: {mode} · {media} · format: {fmt}")

        outtmpl = str(out_dir / ("%(playlist_index)s - %(title)s.%(ext)s" if mode == "playlist"
                                 else "%(title)s.%(ext)s"))
        opts = {
            "format": fmt, "outtmpl": outtmpl, "ignoreerrors": True, "noprogress": True,
            "quiet": True, "no_warnings": True, "concurrent_fragment_downloads": 8, "trim_file_name": 150,
            "retries": 20, "fragment_retries": 20, "extractor_retries": 5, "socket_timeout": 30,
            "progress_hooks": [_make_progress_hook(job_id)],
            "postprocessor_hooks": [_make_pp_hook(job_id)],
            "logger": YtdlpJobLogger(job_id),
        }
        if cookies:
            opts["cookiefile"] = cookies
        if _FFMPEG:
            opts["ffmpeg_location"] = _FFMPEG
        else:
            _job_log(job_id, "ffmpeg missing: video and audio CANNOT be merged!", "error")
        if media == "audio":
            opts["format_sort"] = ["abr", "asr"]  # highest bitrate stream first
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                       "preferredquality": "320"}]
        else:
            opts["merge_output_format"] = "mp4"

        targets = job["urls"]
        planned = len(targets)
        if mode == "playlist":
            items = (job.get("playlist_items") or "").strip()
            if items:
                opts["playlist_items"] = items
                n = 0
                for part in items.split(","):
                    a, _, b = part.strip().partition("-")
                    n += max(1, int(b) - int(a) + 1) if a.isdigit() and b.isdigit() else 1
                planned = max(1, n)
            else:
                planned = job.get("total_items") or 1
        else:
            opts["noplaylist"] = True

        if shutil.which("aria2c") and planned > 1:
            opts["external_downloader"] = "aria2c"
            opts["external_downloader_args"] = ["-x", "4", "-s", "4", "-k", "1M", "--max-tries=10",
                                                "--retry-wait=3", "--console-log-level=warn",
                                                "--summary-interval=0", "--allow-overwrite=true"]
            _job_log(job_id, "aria2c detected: turbo mode for multi-file job.")

        _job_update(job_id, stage="Downloading", total_items=planned)
        with YoutubeDL(opts) as ydl:
            ydl.download(targets)
        with JOBS_LOCK:
            if JOBS[job_id].get("cancel"):
                raise _Cancel("Cancelled by user")

        if media == "video" and not any(p.is_file() and not p.name.endswith((".part", ".ytdl"))
                                        for p in out_dir.rglob("*")):
            for t in targets:
                if not t.startswith("ytsearch"):
                    _gallery_dl_fallback(job_id, t, out_dir, cookies)

        _fix_unmerged(out_dir, job_id)
        _job_update(job_id, stage="Collecting files")
        files = sorted(os.path.join(r, f) for r, _, fs in os.walk(out_dir) for f in fs
                       if not f.endswith((".part", ".ytdl")))
        if not files:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise RuntimeError("Nothing could be downloaded. Check the log; Instagram/X often need "
                               "a cookies.txt from a logged-in browser, and yt-dlp may need an update "
                               "(pip install -U yt-dlp).")
        links: list[dict] = []
        if mode == "single" or len(files) <= 1:
            for fp in files:
                rel = os.path.relpath(fp, ROOT).replace(os.sep, "/")
                links.append({"name": os.path.basename(fp),
                              "size_mb": round(os.path.getsize(fp) / 1048576, 1), "url": "/" + quote(rel)})
        else:
            _job_update(job_id, stage="Creating ZIP")
            _job_log(job_id, f"Creating ZIP with {len(files)} file(s)…")
            zip_name = f"{job['folder_name']}.zip"
            zip_path = DOWNLOADS_DIR / zip_name
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
                for fp in files:
                    zf.write(fp, arcname=os.path.relpath(fp, DOWNLOADS_DIR))
            shutil.rmtree(out_dir, ignore_errors=True)
            rel = os.path.relpath(zip_path, ROOT).replace(os.sep, "/")
            links.append({"name": zip_name, "size_mb": round(zip_path.stat().st_size / 1048576, 1),
                          "url": "/" + quote(rel), "is_zip": True, "file_count": len(files)})
        with JOBS_LOCK:
            JOBS[job_id].update(status="done", stage="Complete", current_pct=100, links=links)
        _job_log(job_id, "All done. Download link is ready.", "ok")
    except Exception as e:
        with JOBS_LOCK:
            cancelled = isinstance(e, DownloadCancelled) or bool(JOBS[job_id].get("cancel"))
        if cancelled:
            shutil.rmtree(DOWNLOADS_DIR / job["folder_name"], ignore_errors=True)
            _job_update(job_id, status="cancelled", stage="Cancelled")
            _job_log(job_id, "Cancelled. Partial files removed.", "warn")
            return
        traceback.print_exc()
        m = _friendly_error(e)
        _job_update(job_id, status="error", stage="Failed", error=m)
        _job_log(job_id, m, "error")


# ---------------- HTTP handler ----------------
INDEX_HTML = ""


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads((self.rfile.read(n) if n else b"{}").decode("utf-8") or "{}")
        except Exception:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/api/job/"):
            with JOBS_LOCK:
                j = JOBS.get(path.rsplit("/", 1)[-1])
                out = dict(j) if j else None
                if out:
                    out["log"] = list(j.get("log") or [])
                    out["links"] = list(j.get("links") or [])
            if not out:
                return self._send_json(404, {"error": "unknown job"})
            out.pop("cookies_file", None)
            self._send_json(200, out)
        elif path == "/api/files":
            self._send_json(200, {"files": _list_downloaded_files()})
        elif path.startswith("/api/spotify/"):
            self._spotify_route(path)
        elif path.startswith("/downloads/") and not path.endswith("/"):
            super().do_GET()
        else:
            self.send_error(404)

    def _spotify_route(self, path: str) -> None:
        import base64
        import hashlib
        import secrets
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        cid = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()

        def page(msg: str, code: int = 200) -> None:
            body = ("<!doctype html><meta charset=utf-8><body style='background:#000;color:#eceeff;"
                    "font:18px system-ui;padding:12vh 8vw'>" + msg.replace("<", "&lt;") + "</body>").encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        if not cid:
            return page("Set the SPOTIFY_CLIENT_ID environment variable first, then restart the app.", 400)
        if path.endswith("/login"):
            ver, st = secrets.token_urlsafe(64), secrets.token_urlsafe(16)
            SPOTIFY_AUTH.update(verifier=ver, state=st)
            ch = base64.urlsafe_b64encode(hashlib.sha256(ver.encode()).digest()).rstrip(b"=").decode()
            loc = "https://accounts.spotify.com/authorize?" + urlencode({
                "client_id": cid, "response_type": "code", "redirect_uri": _spotify_redirect(),
                "scope": _SP_SCOPE, "state": st, "code_challenge_method": "S256", "code_challenge": ch})
            self.send_response(302)
            self.send_header("Location", loc)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.endswith("/callback"):
            if q.get("error") or not q.get("code") or q.get("state") != SPOTIFY_AUTH.get("state"):
                return page("Spotify login was cancelled or the state did not match. Open /api/spotify/login again.", 400)
            try:
                _spotify_token_request({"grant_type": "authorization_code", "code": q["code"],
                                        "redirect_uri": _spotify_redirect(), "client_id": cid,
                                        "code_verifier": SPOTIFY_AUTH.get("verifier", "")})
            except Exception as ex:
                return page(f"Login failed: {ex}", 400)
            return page("Spotify connected. You can close this tab and fetch your playlist again.")
        self.send_error(404)

    def do_DELETE(self):
        if urlparse(self.path).path != "/api/files":
            return self.send_error(404)
        try:
            rel = (self._read_json().get("path") or "").strip().lstrip("/")
            if not rel:
                return self._send_json(400, {"error": "Missing path"})
            target = (DOWNLOADS_DIR / rel).resolve()
            try:
                target.relative_to(DOWNLOADS_DIR.resolve())
            except ValueError:
                return self._send_json(400, {"error": "Invalid path"})
            if not target.exists():
                return self._send_json(404, {"error": "Not found"})
            shutil.rmtree(target) if target.is_dir() else target.unlink()
            p = target.parent
            if p != DOWNLOADS_DIR.resolve() and p.exists() and not any(p.iterdir()):
                p.rmdir()
            self._send_json(200, {"ok": True, "files": _list_downloaded_files()})
        except Exception as e:
            traceback.print_exc()
            self._send_json(500, {"error": str(e)})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/cookies":
                return self._upload_cookies()
            data = self._read_json()
            jid = uuid.uuid4().hex[:12]
            if path == "/api/cancel":
                with JOBS_LOCK:
                    j = JOBS.get(data.get("job_id"))
                    if j is not None:
                        j["cancel"] = True
                return self._send_json(200, {"ok": j is not None})
            if path == "/api/probe":
                if not _split_urls(data.get("url", "")):
                    return self._send_json(400, {"error": "Please paste at least one YouTube URL."})
                with JOBS_LOCK:
                    JOBS[jid] = {"id": jid, "kind": "probe", "status": "queued", "stage": "Queued",
                                 "progress": 0, "url_text": data.get("url", ""),
                                 "cookies_file": _safe_cookie(data.get("cookies_file") or ""), "log": []}
                threading.Thread(target=run_probe, args=(jid,), daemon=True).start()
                return self._send_json(202, {"job_id": jid})
            if path == "/api/download":
                folder = re.sub(r"[^\w\-\. ]+", "_", data.get("title") or "download")[:80].strip() or "job"
                with JOBS_LOCK:
                    JOBS[jid] = {
                        "id": jid, "kind": "download", "status": "queued", "stage": "Queued",
                        "mode": data.get("mode") or "single", "media_type": data.get("media_type") or "video",
                        "resolution": data.get("resolution") or 0, "urls": data.get("urls") or [],
                        "playlist_items": data.get("playlist_items") or "",
                        "folder_name": f"{folder}_{jid[:4]}",
                        "cookies_file": _safe_cookie(data.get("cookies_file") or ""),
                        "current_file": "", "current_pct": 0, "completed_items": 0,
                        "total_items": data.get("total_items") or len(data.get("urls") or []) or 1, "log": []}
                threading.Thread(target=run_download, args=(jid,), daemon=True).start()
                return self._send_json(202, {"job_id": jid})
            self.send_error(404)
        except Exception as e:
            traceback.print_exc()
            self._send_json(500, {"error": str(e)})

    def _upload_cookies(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 5 * 1024 * 1024:
            return self._send_json(400, {"error": "Cookie file must be under 5 MB."})
        body = self.rfile.read(n)
        ctype = self.headers.get("Content-Type", "")
        file_bytes = body
        if "multipart/form-data" in ctype and "boundary=" in ctype:
            boundary = ctype.split("boundary=", 1)[1].encode()
            file_bytes = None
            for p in body.split(b"--" + boundary):
                if b"filename=" in p:
                    he = p.find(b"\r\n\r\n")
                    if he != -1:
                        file_bytes = p[he + 4:].rstrip(b"\r\n-")
                        break
            if not file_bytes:
                return self._send_json(400, {"error": "No file found in upload."})
        text = file_bytes.decode("utf-8", errors="ignore")
        if "Netscape HTTP Cookie File" not in "\n".join(text.splitlines()[:3]) and "\t" not in text:
            return self._send_json(400, {"error": "Not a Netscape cookies.txt file."})
        cp = COOKIES_DIR / f"c_{uuid.uuid4().hex[:10]}.txt"
        cp.write_bytes(file_bytes)
        self._send_json(200, {"cookies_file": cp.name, "name": cp.name})


class ReuseServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        # Browser cancelled / refreshed mid-transfer: harmless, don't spam tracebacks.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<meta name="color-scheme" content="dark"/>
<title>5D downloader</title>
<style>
:root{--bg:#000;--sf:#0b0b10;--lo:rgba(0,0,0,.95);--hi:rgba(255,255,255,.055);--ac:#7c5cff;--ac2:#00e5ff;--hot:#ff3d9a;--tx:#eceeff;--mu:#6b6d92;--e:cubic-bezier(.16,1,.3,1);
--out:8px 8px 20px var(--lo),-6px -6px 16px var(--hi);--in:inset 6px 6px 14px var(--lo),inset -5px -5px 12px var(--hi)}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}html,body{margin:0;height:100%;background:#000}
body{color:var(--tx);font:400 16px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;overflow:hidden;-webkit-font-smoothing:antialiased}
#c{position:fixed;inset:0;width:100%;height:100%;display:block}
.grad{background:linear-gradient(120deg,var(--ac),var(--ac2));-webkit-background-clip:text;background-clip:text;color:transparent}
.bar{position:fixed;top:0;left:0;right:0;display:flex;gap:14px;align-items:center;padding:18px 22px;z-index:20}
.mark{font:800 15px ui-sans-serif;letter-spacing:.5em}.sp{flex:1}
.ib{width:46px;height:46px;border-radius:50%;border:0;background:var(--sf);box-shadow:var(--out);color:var(--mu);font-size:17px;cursor:pointer;transition:transform .5s var(--e),box-shadow .35s,color .3s}
.ib:hover{color:var(--tx);transform:translate3d(0,-2px,0)}.ib:active{box-shadow:var(--in);transform:scale(.94)}
.step{position:fixed;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:18px;padding:0 20px 7vh;text-align:center;opacity:0;visibility:hidden;pointer-events:none;transform:translate3d(0,44px,0) scale(.96);transition:opacity .55s var(--e),transform .8s var(--e),visibility 0s .8s;z-index:5}
.step.on{opacity:1;visibility:visible;pointer-events:auto;transform:none;transition:opacity .6s var(--e) .1s,transform .9s var(--e) .1s,visibility 0s}
.step.gone{transform:translate3d(0,-44px,0) scale(1.04)}
.big{position:absolute;top:13vh;left:0;right:0;font:900 clamp(60px,18vw,270px)/.9 ui-sans-serif,system-ui;letter-spacing:-.07em;color:transparent;-webkit-text-stroke:1.5px rgba(255,255,255,.17);pointer-events:none;will-change:transform;user-select:none}
.hint{color:var(--mu);font-size:12px;letter-spacing:.22em;text-transform:uppercase;margin:0}
.small{letter-spacing:.04em;text-transform:none;max-width:84vw;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.t2{font:300 clamp(18px,2.6vw,28px);letter-spacing:.03em;margin:0;max-width:80vw;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.field{display:flex;align-items:center;gap:10px;width:min(660px,92vw);padding:9px 9px 9px 28px;border-radius:99px;background:var(--sf);box-shadow:var(--in);transition:box-shadow .5s var(--e)}
.field:focus-within{box-shadow:var(--in),0 0 0 2px rgba(124,92,255,.7),0 0 46px -8px var(--ac)}
input{flex:1;background:none;border:0;outline:0;color:var(--tx);font:300 19px ui-sans-serif,system-ui;min-width:0}input::placeholder{color:#44466a}
.orb{width:56px;height:56px;border-radius:50%;border:0;background:var(--sf);box-shadow:var(--out);color:var(--ac2);font-size:22px;cursor:pointer;will-change:transform;transition:box-shadow .3s,color .3s}
.orb:active,.pill:active{box-shadow:var(--in)}
.pill{padding:15px 34px;border-radius:99px;border:0;background:var(--sf);box-shadow:var(--out);color:var(--tx);font:600 14px ui-sans-serif;letter-spacing:.14em;cursor:pointer;will-change:transform;transition:box-shadow .3s}
.launch{position:relative;display:flex;align-items:center;justify-content:space-between;gap:26px;min-width:min(330px,88vw);padding:13px 14px 13px 34px;border:0;border-radius:28px;color:#fff;cursor:pointer;background:linear-gradient(135deg,#7c5cff,#ff3d9a);box-shadow:0 18px 44px -16px rgba(255,61,154,.75),6px 6px 18px rgba(0,0,0,.9),-4px -4px 14px rgba(255,255,255,.05);will-change:transform;transition:box-shadow .3s,filter .3s}
.launch .lt{display:flex;flex-direction:column;align-items:flex-start;text-align:left;line-height:1.15}.launch .lt b{font:900 22px ui-sans-serif;letter-spacing:.01em}.launch .lt small{font:600 12px ui-sans-serif;opacity:.88;letter-spacing:.04em;margin-top:3px}
.launch .la{width:54px;height:54px;border-radius:50%;background:rgba(0,0,0,.3);display:grid;place-items:center;font-style:normal;font-size:24px;transition:transform .6s var(--e);box-shadow:inset 2px 2px 6px rgba(0,0,0,.5),inset -2px -2px 6px rgba(255,255,255,.14)}
.launch:hover .la{transform:translate3d(6px,0,0)}.launch:active{filter:brightness(.88);box-shadow:inset 6px 6px 16px rgba(0,0,0,.5)}
.cancel{color:#ff6b8a;padding:13px 30px;font-size:12px;transition:box-shadow .3s,color .3s,background .3s}.cancel.arm{color:#fff;background:#3a0f1c}.cancel:disabled{opacity:.6;cursor:wait}
.err{color:#ff6b8a;font-size:13px;min-height:19px;max-width:84vw;white-space:pre-wrap}
.cards{display:flex;gap:18px;overflow-x:auto;scroll-snap-type:x proximity;padding:16px 26px 22px;width:100%;justify-content:safe center;scrollbar-width:none}
.cards::-webkit-scrollbar{display:none}
.cd{flex:0 0 172px;scroll-snap-align:center;border-radius:22px;overflow:hidden;background:var(--sf);box-shadow:var(--out);cursor:pointer;text-align:left;transition:transform .5s var(--e),box-shadow .35s,opacity .35s;opacity:.5;contain:layout paint}
.cd.a{animation:cin .8s var(--e) backwards;animation-delay:calc(var(--i)*55ms)}@keyframes cin{from{opacity:0;transform:translate3d(0,40px,0) scale(.85)}}
.cd.sel{opacity:1;box-shadow:var(--in),0 0 0 2px rgba(0,229,255,.55);transform:scale(.96)}
.cd img,.cd .th{width:100%;height:96px;object-fit:cover;display:block;background:linear-gradient(135deg,var(--ac),var(--hot))}
.cd b{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;font:500 12px/1.35 ui-sans-serif;padding:10px 12px 12px}
.tiles{display:flex;gap:18px}
.tile{width:min(172px,42vw);padding:18px 12px 16px;border:0;border-radius:26px;background:var(--sf);box-shadow:var(--out);color:var(--mu);cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:5px;transition:box-shadow .45s var(--e),color .4s,transform .6s var(--e)}
.tile svg{width:34px;height:34px;stroke:currentColor;fill:none;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round;transition:transform .7s var(--e),stroke .4s}
.tile b{font:800 15px ui-sans-serif;letter-spacing:.16em;text-transform:uppercase;color:inherit}.tile small{font:600 11.5px ui-sans-serif;letter-spacing:.05em;opacity:.8}
.tile.on{box-shadow:var(--in),0 0 0 1.5px rgba(124,92,255,.65),0 0 34px -10px var(--ac);color:var(--tx);transform:scale(.97)}
.tile.on svg{stroke:var(--ac2);transform:translate3d(0,-2px,0) scale(1.12)}.tile:active{transform:scale(.94)}
.chips{display:flex;gap:12px;flex-wrap:wrap;justify-content:center;max-width:94vw}
.ch{display:flex;flex-direction:column;align-items:center;gap:1px;min-width:88px;padding:9px 16px;border-radius:18px;background:var(--sf);box-shadow:var(--out);color:var(--mu);cursor:pointer;transition:box-shadow .3s,color .3s,transform .5s var(--e)}
.ch b{font:800 14px ui-sans-serif;color:inherit}.ch small{font:600 11px ui-sans-serif;opacity:.78}
.ch.on{box-shadow:var(--in);color:var(--ac2);transform:scale(1.05)}
.usage{font:500 14px ui-sans-serif;color:var(--mu);letter-spacing:.02em}.usage b{font:900 28px ui-sans-serif;letter-spacing:-.02em;margin-right:5px}
.lnk{color:var(--mu);font:600 12px ui-sans-serif;cursor:pointer;background:none;border:0;letter-spacing:.2em;text-transform:uppercase;padding:6px}.lnk:hover{color:var(--tx)}
#pnum{position:absolute;top:12vh;left:0;right:0;font:900 clamp(90px,26vw,380px)/.9 ui-sans-serif,system-ui;letter-spacing:-.08em;font-variant-numeric:tabular-nums;background:linear-gradient(180deg,#fff,rgba(255,255,255,.25));-webkit-background-clip:text;background-clip:text;color:transparent;will-change:transform}
.dl{display:flex;flex-direction:column;gap:14px;align-items:center;max-width:92vw}
.dl a{display:flex;gap:12px;align-items:center;padding:18px 34px;border-radius:99px;background:var(--sf);box-shadow:var(--out);color:var(--tx);text-decoration:none;font:600 15px ui-sans-serif;word-break:break-all;animation:pin .9s var(--e) backwards;transition:box-shadow .3s}
.dl a:hover{color:var(--ac2)}.dl a:active{box-shadow:var(--in)}@keyframes pin{from{opacity:0;transform:translate3d(0,40px,0) scale(.8)}}
.dl small{color:var(--mu);font-weight:500;white-space:nowrap}
.shade{position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:30;opacity:0;visibility:hidden;transition:opacity .5s var(--e),visibility 0s .5s}.shade.on{opacity:1;visibility:visible;transition:opacity .5s var(--e)}
.sheet{position:fixed;top:0;right:0;bottom:0;width:min(420px,92vw);z-index:31;background:var(--sf);box-shadow:-14px 0 40px #000;padding:28px 24px;transform:translate3d(105%,0,0);transition:transform .8s var(--e);overflow:auto}
.sheet.on{transform:none}.sheet h3{font:900 34px ui-sans-serif;letter-spacing:-.04em;margin:0 0 18px}
.li{padding:14px 0;display:flex;gap:12px;align-items:center;border-bottom:1px solid rgba(255,255,255,.05)}.li .n{flex:1;font-size:13px;word-break:break-all}.li a,.li button{color:var(--ac2);font:600 12px ui-sans-serif;background:none;border:0;cursor:pointer;text-decoration:none;letter-spacing:.1em}.li button{color:#ff6b8a}
.drop{display:block;border-radius:24px;padding:46px 16px;text-align:center;color:var(--mu);cursor:pointer;background:var(--sf);box-shadow:var(--in);transition:color .3s}.drop:hover{color:var(--ac2)}
pre{font:11px/1.65 ui-monospace,monospace;color:#8fe;white-space:pre-wrap;margin:0}
.toast{position:fixed;left:50%;bottom:26px;transform:translate3d(-50%,40px,0);opacity:0;z-index:50;padding:12px 26px;border-radius:99px;background:var(--sf);box-shadow:var(--out);transition:transform .7s var(--e),opacity .5s;font-size:14px;pointer-events:none}.toast.on{opacity:1;transform:translate3d(-50%,0,0)}
#bglog{position:fixed;left:50%;bottom:31vh;width:min(780px,90vw);height:21vh;transform:translate3d(-50%,0,0);display:flex;flex-direction:column;justify-content:flex-end;gap:2px;font:11.5px/1.6 ui-monospace,"SF Mono",Menlo,Consolas,monospace;color:#8fe;opacity:0;transition:opacity 1.8s var(--e);pointer-events:none;z-index:3;overflow:hidden;-webkit-mask-image:linear-gradient(to top,#000 20%,transparent 100%);mask-image:linear-gradient(to top,#000 20%,transparent 100%)}
#bglog.on{opacity:.4;transition:opacity .8s var(--e)}
#bglog div{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;animation:lg .7s var(--e) backwards}@keyframes lg{from{opacity:0;transform:translate3d(0,8px,0)}}
#bglog .warn{color:#fd6}#bglog .error{color:#f77}#bglog .ok{color:#7f9}
@media(prefers-reduced-motion:reduce){*{animation:none!important}}
</style></head><body>
<canvas id="c"></canvas><div id="bglog" aria-hidden="true"></div>
<div class="bar"><div class="mark grad">5D</div><div class="sp"></div>
 <button class="ib" id="bV" aria-label="vault">⌂</button><button class="ib" id="bC" aria-label="cookies">◍</button><button class="ib" id="bL" aria-label="log">≡</button><button class="ib" id="bS" aria-label="sound">♪</button></div>

<section class="step on" id="s-paste"><div class="big" data-big>PASTE</div>
 <div class="field"><input id="url" placeholder="paste a youtube link…" autocomplete="off" spellcheck="false"><button class="orb mag" id="goF" aria-label="go">→</button></div>
 <p class="hint">playlists · many links · drag &amp; drop</p><div class="err" id="fErr"></div></section>
<section class="step" id="s-fetch"><div class="big" data-big>READ</div><p class="hint" id="fS">connecting</p><button class="lnk" id="cancelF">✕ cancel</button></section>
<section class="step" id="s-choose"><div class="big" data-big>PICK</div><h2 class="t2" id="cT"></h2><p class="hint" id="cM"></p>
 <div class="cards" id="cards"></div>
 <div class="tiles" id="tiles">
  <button class="tile on" data-v="video"><svg viewBox="0 0 24 24"><rect x="2.5" y="4.5" width="19" height="15" rx="3.5"/><path d="M10 9.2v5.6l4.6-2.8z"/></svg><b>Video</b><small id="tV">MP4</small></button>
  <button class="tile" data-v="audio"><svg viewBox="0 0 24 24"><path d="M4 14v-2a8 8 0 0 1 16 0v2"/><rect x="3" y="14" width="4.5" height="6.5" rx="2"/><rect x="16.5" y="14" width="4.5" height="6.5" rx="2"/></svg><b>Audio</b><small id="tA">MP3</small></button></div>
 <div class="chips" id="chips"></div>
 <div class="usage" id="usage"></div>
 <button class="launch mag" id="goD"><span class="lt"><b>Download</b><small id="lsz">—</small></span><i class="la">→</i></button><button class="lnk" id="back">← new link</button></section>
<section class="step" id="s-prog"><div id="pnum" data-big>0%</div><h2 class="t2" id="pT">Downloading</h2><p class="hint" id="pS"></p><p class="hint small" id="pF"></p><button class="pill cancel mag" id="cancel">✕ CANCEL</button></section>
<section class="step" id="s-done"><div class="big" data-big>DONE</div><div class="dl" id="links"></div><button class="pill mag" id="again">+ ANOTHER</button></section>

<div class="shade" id="shade"></div>
<aside class="sheet" id="sh-V"><h3>Vault</h3><div id="lib"></div></aside>
<aside class="sheet" id="sh-C"><h3>Cookies</h3><label class="drop" for="cf">drop cookies.txt here<br><small>optional · fixes “confirm you’re not a bot”</small><input type="file" id="cf" accept=".txt" hidden></label></aside>
<aside class="sheet" id="sh-L"><h3>Log</h3><pre id="log"></pre></aside>
<div class="toast" id="toast"></div>
<script>
(()=>{"use strict";
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const S={cookie:"",probe:null,media:"video",res:0,ph:"idle",snd:true,busy:false,disp:0,target:0,speed:0,step:"paste"};
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtS=b=>!b?"—":b>1e6?(b/1e6).toFixed(1)+" MB/s":b>1e3?(b/1e3|0)+" KB/s":b+" B/s";
const fmtE=s=>!s?"—":s<60?s+"s":(s/60|0)+"m "+s%60+"s";
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function api(p,o={}){const r=await fetch(p,o);const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||"HTTP "+r.status);return j}
/* ================= sound (lazy, gesture-unlocked) ================= */
let AC;const unlock=()=>{try{AC=AC||new(window.AudioContext||window.webkitAudioContext)();if(AC.state==="suspended")AC.resume()}catch(_){}};
addEventListener("pointerdown",unlock,{passive:true});addEventListener("keydown",unlock,{passive:true});
function tone(f,d=.2,t="sine",v=.04,at=0,f2){if(!S.snd||!AC||AC.state!=="running")return;try{const o=AC.createOscillator(),g=AC.createGain(),n=AC.currentTime+at;o.type=t;o.frequency.setValueAtTime(f,n);if(f2)o.frequency.exponentialRampToValueAtTime(f2,n+d);g.gain.setValueAtTime(.0001,n);g.gain.exponentialRampToValueAtTime(v,n+.02);g.gain.exponentialRampToValueAtTime(.0001,n+d);o.connect(g);g.connect(AC.destination);o.start(n);o.stop(n+d+.05)}catch(_){}}
const chord=a=>a.forEach((f,i)=>tone(f,.9,"sine",.04,i*.08));
let tt=0;function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("on");clearTimeout(tt);tt=setTimeout(()=>t.classList.remove("on"),2800)}
$("#bS").onclick=()=>{S.snd=!S.snd;$("#bS").style.opacity=S.snd?1:.35;tone(660)};
/* ================= director: steps never overlap ================= */
let goBusy=false,goQ=null,bigEl=$("#s-paste [data-big]");
function doGo(n){goBusy=true;S.step=n;$$(".step").forEach(e=>{if(e.id==="s-"+n){e.classList.remove("gone");e.classList.add("on")}else if(e.classList.contains("on")){e.classList.remove("on");e.classList.add("gone")}else e.classList.remove("gone")});
 bigEl=$("#s-"+n+" [data-big]");tone(480,.5,"sine",.03,0,760);
 setTimeout(()=>{goBusy=false;cacheMag();if(n==="paste")$("#url").focus({preventScroll:true});if(goQ&&goQ!==S.step){const q=goQ;goQ=null;doGo(q)}else goQ=null},700)}
function go(n){if(n===S.step&&!goBusy)return;if(goBusy){goQ=n;return}doGo(n)}
function sheet(id){const open=$(".sheet.on");if(open)open.classList.remove("on");const s=id&&$("#sh-"+id);if(s&&s!==open){s.classList.add("on");$("#shade").classList.add("on")}else $("#shade").classList.remove("on")}
$("#shade").onclick=()=>sheet(null);$("#bV").onclick=()=>{refreshLib();sheet("V")};$("#bC").onclick=()=>sheet("C");$("#bL").onclick=()=>sheet("L");
addEventListener("keydown",e=>{if(e.key==="Escape")sheet(null);if(e.ctrlKey&&e.key==="Enter")(S.step==="choose"?$("#goD"):$("#goF")).click()});
/* phases: minimum dwell so no animation is cut off by the next */
const DW={merge:1900,done:2600,error:1600,zip:1100,convert:1100};let hold=0,pend=null,pt=0,iv=0;
function phase(p){if(!p)return;if(p===S.ph){pend=null;return}const n=performance.now();if(n<hold){pend=p;clearTimeout(pt);pt=setTimeout(()=>{const q=pend;pend=null;if(q&&q!==S.ph)applyPhase(q)},hold-n+30);return}applyPhase(p)}
function applyPhase(p){S.ph=p;hold=performance.now()+(DW[p]||0);clearInterval(iv);
 if(p==="download")iv=setInterval(()=>tone(300+S.disp*6,.3,"sine",.025),800);
 if(p==="merge"){const t=()=>{tone(180,1,"sawtooth",.02,0,640);setTimeout(()=>chord([523,784]),1150)};t();iv=setInterval(t,2400)}
 if(p==="convert")iv=setInterval(()=>tone(520+Math.random()*420,.28,"triangle",.03),340);
 if(p==="zip")iv=setInterval(()=>tone(520,.45,"sine",.04,0,170),700);
 if(p==="done"){boom();chord([523,659,784,1047,1319])}if(p==="error")tone(220,.9,"sawtooth",.04,0,60);
 if(S.step==="prog")$("#pT").textContent=LB[p]||"Working"}
const LB={download:"Downloading",merge:"Merging video + audio",convert:"Making MP3",zip:"Packing ZIP",done:"Done",error:"Something broke"};
/* ================= n-cube engine: 3D → 4D → 5D ================= */
const N=5,NV=32,V=[],E=[],PL=[];
for(let i=0;i<NV;i++)V.push([0,1,2,3,4].map(b=>(i>>b&1)?1:-1));
for(let i=0;i<NV;i++)for(let b=0;b<N;b++){const j=i^(1<<b);if(j>i)E.push([i,j,b])}
for(let i=0;i<N;i++)for(let j=i+1;j<N;j++)PL.push([i,j]);
const RT=[.31,.23,.41,.27,.17,.33,.21,.37,.13,.29];
const PT=Array.from({length:NV},()=>[0,0,0]),vt=[0,0,0,0,0];
function proj(A,f4,f5){const cs=A.map(Math.cos),sn=A.map(Math.sin);for(let n=0;n<NV;n++){const o=V[n];vt[0]=o[0];vt[1]=o[1];vt[2]=o[2];vt[3]=o[3]*f4;vt[4]=o[4]*f5;
 for(let p=0;p<10;p++){const i=PL[p][0],j=PL[p][1],x=vt[i],y=vt[j];vt[i]=x*cs[p]-y*sn[p];vt[j]=x*sn[p]+y*cs[p]}
 let k=3.4/(3.4-vt[4]);vt[0]*=k;vt[1]*=k;vt[2]*=k;vt[3]*=k;k=3.2/(3.2-vt[3]);vt[0]*=k;vt[1]*=k;vt[2]*=k;
 const s=1/(3.6-vt[2]*1.4),r=PT[n];r[0]=vt[0]*s*2.2;r[1]=vt[1]*s*2.2;r[2]=s}return PT}
const cv=$("#c"),g=cv.getContext("2d",{alpha:false});let W,H,Q=0,stars=[];
function size(){const d=Q?1:Math.min(devicePixelRatio||1,1.5);W=innerWidth;H=innerHeight;cv.width=W*d;cv.height=H*d;g.setTransform(d,0,0,d,0,0);g.fillStyle="#000";g.fillRect(0,0,W,H);
 stars=Array.from({length:Q===0?110:Q===1?50:0},()=>({x:Math.random()*W,y:Math.random()*H,z:Math.random()*.9+.1,p:Math.random()*6}))}
size();let rzT=0;addEventListener("resize",()=>{clearTimeout(rzT);rzT=setTimeout(()=>{size();cacheMag()},150)});
function drawN(P,cx,cy,R,h,prog,al,f4,f5){
 for(let n=0;n<80;n++){const e=E[n],a=P[e[0]],b=P[e[1]],j=e[1],fac=((j&8)?f4:1)*((j&16)?f5:1);if(fac<.03)continue;
  const x1=cx+a[0]*R,y1=cy+a[1]*R,x2=cx+b[0]*R,y2=cy+b[1]*R,d=(a[2]+b[2])*.5,hh=h+e[2]*26,A=al*fac;
  const lw=.8+d*2.4,lit=prog<0?1:Math.max(0,Math.min(1,prog*80-n));
  g.lineWidth=lw;g.strokeStyle="hsla("+hh+",95%,"+(58+d*34)+"%,"+(A*(prog<0?.3+d*.9:.13))+")";g.beginPath();g.moveTo(x1,y1);g.lineTo(x2,y2);g.stroke();
  if(prog>=0&&lit>0){const xe=x1+(x2-x1)*lit,ye=y1+(y2-y1)*lit;if(Q===0){g.lineWidth=lw+5;g.strokeStyle="hsla("+hh+",100%,60%,"+A*.16+")";g.beginPath();g.moveTo(x1,y1);g.lineTo(xe,ye);g.stroke()}g.lineWidth=lw+.8;g.strokeStyle="hsla("+hh+",100%,"+(62+d*28)+"%,"+A+")";g.beginPath();g.moveTo(x1,y1);g.lineTo(xe,ye);g.stroke()}
  else if(prog<0&&Q===0&&d>.34){g.lineWidth=lw+4;g.strokeStyle="hsla("+hh+",100%,60%,"+A*.07+")";g.beginPath();g.moveTo(x1,y1);g.lineTo(x2,y2);g.stroke()}}
 if(Q<2)for(let n=0;n<NV;n++){const p=P[n],f=((n&8)?f4:1)*((n&16)?f5:1);if(f<.05)continue;g.fillStyle="hsla("+h+",100%,84%,"+al*f*.9+")";g.beginPath();g.arc(cx+p[0]*R,cy+p[1]*R,1.3+p[2]*3.6,0,6.283);g.fill()}}
const K={hue:{idle:262,fetch:200,download:190,merge:320,convert:150,zip:40,done:140,error:350},sc:{idle:1,fetch:1.06,download:1,merge:.85,convert:1.04,zip:.62,done:1.12,error:.95},sp:{idle:1,fetch:4,download:2.4,merge:2,convert:3,zip:6,done:.8,error:.3},
 st:{paste:[.46,1,0],fetch:[.46,1.08,1],choose:[.25,.5,1],prog:[.47,1.1,1],done:[.4,.85,1]}};
const A=new Array(10).fill(0);let hue=262,sc=1,sp=1,mx=0,my=0,tx=0,ty=0,cyk=.46,stk=1,f4=0,f5=0,T=0,last=0,ema=16,lastQ=0,parts=[],rings=[],lastPct=-1;
function boom(){for(let i=0;i<90;i++){const a=Math.random()*6.283,v=2+Math.random()*9;parts.push({x:W/2,y:H*cyk,vx:Math.cos(a)*v,vy:Math.sin(a)*v,l:1,h:hue+Math.random()*90-45})}rings.push({r:0,a:1},{r:-90,a:1})}
const wrap=a=>{a%=6.2832;return a>3.1416?a-6.2832:a<-3.1416?a+6.2832:a};
function frame(now){requestAnimationFrame(frame);if(document.hidden){last=now;return}
 const dt=Math.min(.05,(now-last)/1000||.016);last=now;T+=dt;const q=k=>Math.min(1,dt*k);
 ema+=(dt*1000-ema)*.04;if(T-lastQ>2.5){if(ema>27&&Q<2){Q++;lastQ=T;size()}}
 const ph=S.ph,[st,ss,d4]=K.st[S.step];
 hue+=(K.hue[ph]-hue)*q(2.2);sc+=(K.sc[ph]-sc)*q(2.5);sp+=(K.sp[ph]+(ph==="download"?Math.min(S.speed/1.6e6,5):0)-sp)*q(1.8);
 tx+=(mx-tx)*q(3.5);ty+=(my-ty)*q(3.5);cyk+=(st-cyk)*q(2.6);stk+=(ss-stk)*q(2.6);
 const t5=S.step==="prog"?Math.max(S.disp/100,(ph==="merge"||ph==="convert"||ph==="zip")?1:0):S.step==="done"?1:0;
 f4+=(d4-f4)*q(1.6);f5+=(t5-f5)*q(1.6);
 for(let p=0;p<10;p++){const i=PL[p][0],j=PL[p][1],gp=((i===3||j===3)?f4:1)*((i===4||j===4)?f5:1);if(gp<.04){A[p]=wrap(A[p]);A[p]*=1-q(2)}else A[p]+=RT[p]*sp*dt*gp}
 S.disp+=(S.target-S.disp)*q(4.5);
 const rA=A.slice();rA[1]+=tx*.6;rA[3]+=ty*.6;
 g.globalCompositeOperation="source-over";g.globalAlpha=1;g.fillStyle="rgba(0,0,0,"+(Q?.5:.28)+")";g.fillRect(0,0,W,H);
 for(const s of stars){s.y+=s.z*dt*(8+sp*14);if(s.y>H){s.y=0;s.x=Math.random()*W}g.globalAlpha=(.2+.5*Math.abs(Math.sin(T*s.z+s.p)))*s.z;g.fillStyle="#bcc8ff";g.fillRect(s.x+tx*s.z*36,s.y+ty*s.z*36,s.z*1.8,s.z*1.8)}
 g.globalAlpha=1;g.globalCompositeOperation="lighter";
 const cx=W/2,cy=H*cyk,R=Math.min(W,H)*.2*sc*stk*(1+Math.sin(T*3)*(ph==="download"?.022:.008)),P=proj(rA,f4,f5),jit=ph==="error"?Math.sin(T*55)*6:0;
 const pr=S.step==="prog"&&ph==="download"?S.disp/100:(ph==="convert"||ph==="zip"||ph==="done"||ph==="merge")?1:-1;
 if(ph==="merge"&&S.step==="prog"){const m=(T%2.4)/2.4,e=Math.min(1,m/.45),off=(1-e*e*(3-2*e))*R*1.6,fl=Math.max(0,1-Math.abs(m-.5)*5);drawN(P,cx-off,cy,R,330,1,.9,f4,f5);drawN(P,cx+off,cy,R,190,1,.9,f4,f5);
  if(fl>0){const gr=g.createRadialGradient(cx,cy,0,cx,cy,R*(.7+fl));gr.addColorStop(0,"rgba(255,255,255,"+fl*.75+")");gr.addColorStop(1,"rgba(255,255,255,0)");g.fillStyle=gr;g.fillRect(cx-R*2.2,cy-R*2.2,R*4.4,R*4.4)}}
 else drawN(P,cx+jit,cy,R,hue,pr,1,f4,f5);
 for(let i=parts.length-1;i>=0;i--){const p=parts[i];p.x+=p.vx;p.y+=p.vy;p.vx*=.975;p.vy=p.vy*.975+.1;p.l-=dt*.45;if(p.l<=0){parts.splice(i,1);continue}g.fillStyle="hsla("+p.h+",100%,66%,"+p.l+")";g.beginPath();g.arc(p.x,p.y,1.6+p.l*3,0,6.283);g.fill()}
 for(let i=rings.length-1;i>=0;i--){const r=rings[i];r.r+=dt*900;r.a-=dt*.8;if(r.a<=0){rings.splice(i,1);continue}if(r.r<0)continue;g.strokeStyle="hsla("+hue+",100%,70%,"+r.a+")";g.lineWidth=2.5;g.beginPath();g.arc(cx,cy,r.r,0,6.283);g.stroke()}
 const pc=Math.round(S.disp);if(pc!==lastPct){lastPct=pc;$("#pnum").textContent=pc+"%";if(S.busy)document.title=pc+"% · downloading"}
 if(bigEl)bigEl.style.transform="translate3d("+(-tx*34).toFixed(1)+"px,"+(-ty*22).toFixed(1)+"px,0)";
 for(const m of MAG){if(!m.on)continue;const dx=mxp-m.cx,dy=myp-m.cy,d=Math.hypot(dx,dy),tg=d<140?[dx*.32,dy*.32]:[0,0];m.x+=(tg[0]-m.x)*q(9);m.y+=(tg[1]-m.y)*q(9);m.e.style.transform="translate3d("+m.x.toFixed(1)+"px,"+m.y.toFixed(1)+"px,0)"}}
let mxp=-999,myp=-999;const MAG=$$(".mag").map(e=>({e,x:0,y:0,cx:0,cy:0,on:false}));
function cacheMag(){for(const m of MAG){const st=m.e.closest(".step");m.on=!!st&&st.classList.contains("on");if(!m.on)continue;const r=m.e.getBoundingClientRect();m.cx=r.left+r.width/2-m.x;m.cy=r.top+r.height/2-m.y}}
addEventListener("pointermove",e=>{mxp=e.clientX;myp=e.clientY;mx=e.clientX/W-.5;my=e.clientY/H-.5},{passive:true});
requestAnimationFrame(frame);setTimeout(cacheMag,800);
/* ================= flow ================= */
let tok=0;
function logs(l=[]){if($("#sh-L").classList.contains("on"))$("#log").textContent=l.slice(-80).map(x=>`[${x.time}] ${x.message}`).join("\n")}
const selIdx=()=>$$("#cards .cd").map((c,i)=>c.classList.contains("sel")?i:-1).filter(i=>i>=0);
const selTxt=()=>{if(S.probe){$("#cM").textContent=(S.probe.videos.length===1?"1 video":selIdx().length+" of "+S.probe.videos.length+" selected")+" · tap cards to toggle";updateSizes()}};
function rng(ix){const n=ix.map(i=>i+1).sort((a,b)=>a-b),o=[];let s=n[0],p=n[0];for(let i=1;i<n.length;i++){if(n[i]===p+1){p=n[i];continue}o.push(s===p?""+s:s+"-"+p);s=p=n[i]}o.push(s===p?""+s:s+"-"+p);return o.join(",")}
function setMedia(v){S.media=v;$$("#tiles .tile").forEach(b=>b.classList.toggle("on",b.dataset.v===v));$("#chips").style.display=v==="audio"?"none":"";updateSizes()}
$("#tiles").onclick=e=>{const b=e.target.closest("[data-v]");if(b){setMedia(b.dataset.v);tone(b.dataset.v==="audio"?700:500,.3)}};
function showProbe(j){$("#cT").textContent=j.title;
 $("#cards").innerHTML=j.videos.map((v,i)=>`<div class="cd sel ${i<12?"a":""}" style="--i:${i}">${v.thumbnail?`<img loading="lazy" decoding="async" src="${esc(v.thumbnail)}" onerror="this.outerHTML='<div class=th></div>'">`:'<div class="th"></div>'}<b>${esc(v.title)}</b></div>`).join("");
 $$("#cards .cd").forEach(c=>c.onclick=()=>{c.classList.toggle("sel");selTxt();tone(c.classList.contains("sel")?740:420,.2)});selTxt();
 const rs=(j.resolutions||[]).filter(r=>r<=4320).sort((a,b)=>a-b);S.res=rs.includes(720)?720:0;
 $("#chips").innerHTML=[0,...rs].map(r=>`<span class="ch ${r===S.res?"on":""}" data-r="${r}"><b>${r?r+"p":"Best"}</b><small>—</small></span>`).join("");setMedia(j.videos.length&&j.videos.every(v=>/^ytsearch/.test(v.url))?"audio":"video");go("choose")}
$("#chips").onclick=e=>{const c=e.target.closest("[data-r]");if(!c)return;S.res=+c.dataset.r;$$("#chips .ch").forEach(x=>x.classList.toggle("on",x===c));updateSizes();tone(600,.2)};
function fail(msg,back){logOff();S.busy=false;try{localStorage.removeItem("ytdl_job")}catch(_){}$("#fErr").textContent=msg;toast(msg.slice(0,60));phase("error");go(back);setTimeout(()=>phase("idle"),50)}
function phaseOf(j,cur){const s=j.stage||"";if(j.status==="done")return"done";if(j.status==="error")return"error";if(/Merg/i.test(s))return"merge";if(/Extract|Convert|Audio/i.test(s))return"convert";if(/ZIP/i.test(s))return"zip";if(j.status==="fetching")return"fetch";if(/^Downloading/i.test(s))return"download";return cur}
async function poll(id,kind){const my=++tok;let fails=0;
 for(;;){await sleep(kind==="probe"?500:650);if(my!==tok)return;let j;
  try{j=await api("/api/job/"+id);fails=0}catch(e){if(++fails>=6||/unknown job/.test(e.message)){if(my===tok)fail(/unknown job/.test(e.message)?"Job lost — server restarted?":"Lost connection to the server",kind==="probe"?"paste":"choose");return}continue}
  if(my!==tok)return;logs(j.log);logBg(j.log);S.speed=j.speed||0;
  if(j.status==="cancelled"){S.busy=false;try{localStorage.removeItem("ytdl_job")}catch(_){}logOff();toast("Download cancelled");S.target=0;phase("idle");cancelReset();go("choose");return}
  if(kind==="probe"){S.target=j.progress||0;$("#fS").textContent=j.stage||j.status;
   if(j.status==="probe_done"){logOff();S.probe=j.probe;S.target=0;phase("idle");showProbe(j.probe);return}
   if(j.status==="error"){fail(j.error||"Fetch failed","paste");return}}
  else{phase(phaseOf(j,S.ph));const t=Math.max(1,j.total_items||1),d=j.completed_items||0;S.target=j.status==="done"?100:Math.min(99,(d+(j.current_pct||0)/100)/t*100);
   $("#pS").textContent=fmtS(j.speed)+"  ·  eta "+fmtE(j.eta)+"  ·  "+Math.min(d,t)+"/"+t+" files";$("#pF").textContent=j.current_file||"";
   if(j.status==="done"){S.target=100;S.busy=false;try{localStorage.removeItem("ytdl_job")}catch(_){}done(j.links||[]);return}
   if(j.status==="error"){fail(j.error||"Download failed","choose");return}}}}
function done(l){logOff();$("#links").innerHTML=l.length?l.map(x=>`<a href="${x.url}" download>⬇ ${esc(x.name)} <small>${x.size_mb} MB${x.is_zip?" · zip":""}</small></a>`).join(""):'<p class="hint">nothing came out — check the log</p>';
 setTimeout(()=>{go("done");phase("done");document.title="Ready";cacheMag()},900);refreshLib()}
let fetching=false;
async function doFetch(){if(fetching)return;const url=$("#url").value.trim();if(!url){$("#fErr").textContent="paste a link first";return}
 fetching=true;$("#fErr").textContent="";go("fetch");S.target=3;phase("fetch");$("#fS").textContent="connecting";
 try{const{job_id}=await api("/api/probe",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url,cookies_file:S.cookie})});await poll(job_id,"probe")}
 catch(e){fail(e.message,"paste")}finally{fetching=false}}
const feed=t=>{if(!/https?:\/\//.test(t||""))return false;$("#url").value=t.trim();doFetch();return true};
$("#goF").onclick=doFetch;$("#url").addEventListener("keydown",e=>{if(e.key==="Enter")doFetch()});$("#url").addEventListener("paste",()=>setTimeout(()=>feed($("#url").value),60));
document.addEventListener("paste",e=>{const t=(e.clipboardData||window.clipboardData).getData("text");if(S.step==="paste"&&e.target.id!=="url"&&/https?:\/\//.test(t)){e.preventDefault();feed(t)}});
document.addEventListener("dragover",e=>e.preventDefault());
document.addEventListener("drop",e=>{e.preventDefault();const f=e.dataTransfer.files[0];if(f&&/\.txt$/i.test(f.name))return upCookie(f);if(S.step==="paste")feed(e.dataTransfer.getData("text/uri-list")||e.dataTransfer.getData("text/plain"))});
function reset(){tok++;logOff();S.probe=null;S.busy=false;S.target=0;S.disp=0;$("#url").value="";$("#fErr").textContent="";phase("idle");document.title="3·4·5D downloader";go("paste")}
$("#back").onclick=reset;$("#again").onclick=reset;
$("#goD").onclick=async()=>{if(!S.probe||S.busy)return;const p=S.probe,ix=selIdx();if(!ix.length){toast("pick at least one");return}
 let urls,pi="";if(p.mode==="playlist"){urls=[p.urls[0]];pi=rng(ix)}else urls=ix.map(i=>p.videos[i].url).filter(Boolean);
 S.busy=true;S.disp=0;S.target=1;cancelReset();$("#pT").textContent="Downloading";phase("download");go("prog");
 try{const{job_id}=await api("/api/download",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:p.mode,media_type:S.media,resolution:S.res,urls,playlist_items:pi,total_items:ix.length,title:p.title,cookies_file:S.cookie})});
  S.job=job_id;try{localStorage.setItem("ytdl_job",job_id)}catch(_){}poll(job_id,"dl")}
 catch(e){fail(e.message,"choose")}};
async function upCookie(f){if(!f)return;const fd=new FormData();fd.append("file",f);try{const j=await api("/api/cookies",{method:"POST",body:fd});S.cookie=j.cookies_file;toast("cookies loaded");chord([600,900]);sheet(null)}catch(e){toast(e.message)}}
$("#cf").onchange=e=>upCookie(e.target.files[0]);
async function refreshLib(){try{const f=(await api("/api/files")).files||[];$("#lib").innerHTML=f.length?f.map(x=>`<div class="li"><div class="n">${esc(x.name)}<br><small style="color:var(--mu)">${x.size_mb} MB</small></div><a href="${x.url}" download>SAVE</a><button data-p="${esc(x.path)}">RM</button></div>`).join(""):'<p class="hint">empty</p>';
 $$("#lib button").forEach(b=>b.onclick=async()=>{if(!confirm("Delete "+b.dataset.p+"?"))return;try{await fetch("/api/files",{method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({path:b.dataset.p})});refreshLib()}catch(e){toast(e.message)}})}catch(e){}}
/* ===== data-usage estimates ===== */
const fb=b=>!b?"—":b>=1073741824?(b/1073741824).toFixed(2)+" GB":b>=1048576?(b/1048576).toFixed(b>=1e8?0:1)+" MB":Math.max(1,b/1024|0)+" KB";
function vSize(v,res){const s=v.sizes||{},hs=Object.keys(s).map(Number).sort((a,b)=>a-b);if(!hs.length)return null;const k=!res?hs[hs.length-1]:(hs.filter(x=>x<=res).pop()||hs[0]);return s[k]||null}
function total(res,media){let sum=0,unk=0,est=false;for(const i of selIdx()){const v=S.probe.videos[i],z=media==="audio"?v.audio:vSize(v,res);if(z){sum+=z;if(v.est||media==="audio")est=true}else unk++}return{sum,unk,est}}
function aq(){let b=0,c="";for(const i of selIdx()){const v=S.probe.videos[i];if(v.abr&&v.abr>b){b=v.abr;c=v.acodec||""}}return b?"source "+c+" ~"+b+" kbps":"best source available"}
function updateSizes(){if(!S.probe)return;const n=selIdx().length;
 $$("#chips .ch").forEach(c=>{const t=total(+c.dataset.r,"video");c.querySelector("small").textContent=t.sum?(t.est?"~":"")+fb(t.sum):"—"});
 const tv=total(S.res,"video"),ta=total(0,"audio"),t=S.media==="audio"?ta:tv;
 $("#tV").textContent="MP4 · "+(tv.sum?(tv.est?"~":"")+fb(tv.sum):"size n/a");$("#tA").textContent="MP3 320k · "+(ta.sum?"~"+fb(ta.sum):"size n/a");
 $("#usage").innerHTML=n?`<b class="grad">${t.sum?(t.est?"≈ ":"")+fb(t.sum):"size unknown"}</b>of data<span> · ${n} ${n>1?"files":"file"} · ${S.media==="video"?(S.res?S.res+"p":"best quality"):"MP3 320 kbps · "+aq()}${t.unk?" · "+t.unk+" unknown":""}</span>`:"nothing selected";
 $("#lsz").textContent=n&&t.sum?(t.est?"≈ ":"")+fb(t.sum)+" · "+(S.media==="video"?"video":"audio"):n?"size unknown":"pick something"}
/* ===== ambient log: low-opacity background, fades away when the process ends ===== */
const bg=$("#bglog");let bgLast="",bgOff=0;
function logBg(l){if(!l||!l.length)return;clearTimeout(bgOff);bg.classList.add("on");
 const sig=x=>x.time+x.message;let from=-1;if(bgLast){for(let i=l.length-1;i>=0;i--)if(sig(l[i])===bgLast){from=i;break}}
 if(from<0)bg.innerHTML="";const fresh=(from<0?l.slice(-9):l.slice(from+1)).slice(-9);
 for(const x of fresh){const d=document.createElement("div");d.className=x.level||"";d.textContent=String(x.message).replace(/\s+/g," ").slice(0,150);bg.appendChild(d)}
 while(bg.children.length>9)bg.firstChild.remove();bgLast=sig(l[l.length-1])}
function logOff(){clearTimeout(bgOff);bg.classList.remove("on");bgOff=setTimeout(()=>{bg.innerHTML="";bgLast=""},2400)}
/* ===== cancel (double-tap to confirm) ===== */
let cArm=0,cT=0;const cb=$("#cancel");
function cancelReset(){cArm=0;clearTimeout(cT);cb.disabled=false;cb.textContent="✕ CANCEL";cb.classList.remove("arm")}
cb.onclick=async()=>{if(!cArm){cArm=1;cb.textContent="TAP AGAIN TO CANCEL";cb.classList.add("arm");cT=setTimeout(cancelReset,2600);return}
 clearTimeout(cT);cb.disabled=true;cb.textContent="CANCELLING…";try{await api("/api/cancel",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({job_id:S.job})})}catch(e){toast(e.message);cancelReset()}};
$("#cancelF").onclick=()=>reset();
/* resume a running download after a page refresh */
(async()=>{try{const id=localStorage.getItem("ytdl_job");if(!id)return;const j=await api("/api/job/"+id);if(j.kind==="download"&&(j.status==="running"||j.status==="queued")){S.busy=true;S.target=1;S.job=id;$("#pT").textContent="Resuming…";go("prog");poll(id,"dl")}else localStorage.removeItem("ytdl_job")}catch(_){try{localStorage.removeItem("ytdl_job")}catch(__){}}})();
$("#url").focus({preventScroll:true});
})();
</script></body></html>
"""


def main():
    port = int(os.environ.get("PORT") or 8000)
    os.chdir(ROOT)
    server = ReuseServer(("0.0.0.0", port), Handler)
    host = f"localhost:{port}"
    cs_domain, cs_name = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN"), os.environ.get("CODESPACE_NAME")
    if cs_domain and cs_name:
        print(f"\n  🌐 Open in browser:  https://{cs_name}-{port}.{cs_domain}\n")
    else:
        print(f"\n  🌐 Open in browser:  http://{host}\n")
    if os.environ.get("SPOTIFY_CLIENT_ID", "").strip():
        print(f"  🎵 Spotify: add this Redirect URI in your Spotify app:  {_spotify_redirect()}")
        print("     then open  /api/spotify/login  once (same address as above) to unlock full playlists.\n")
    print("  (Ctrl+C to stop)\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…")
        server.shutdown()


if __name__ == "__main__":
    main()
