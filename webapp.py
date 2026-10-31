"""
4D — YouTube Downloader Web UI (single file, stdlib + yt-dlp).

Run:  python webapp.py   → open the printed URL.
Env:  PORT (default 8000), HOST (default 127.0.0.1; 0.0.0.0 inside Codespaces).
"""
from __future__ import annotations

import http.server
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zipfile
from itertools import islice
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


def _auto_install(pkgs: list[str]) -> None:
    print(f"[setup] Installing: {', '.join(pkgs)} …")
    base = [sys.executable, "-m", "pip", "install", "-U", "--quiet", *pkgs]
    for cmd in (base, base + ["--break-system-packages"]):
        try:
            subprocess.check_call(cmd)
            return
        except subprocess.CalledProcessError:
            continue
    print(f"[setup] pip failed. Run manually:\n  {' '.join(base)}")
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
    except (Exception, SystemExit):
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
_HAS_DENO = bool(shutil.which("deno"))
_HAS_NODE = bool(shutil.which("node"))
if not (_HAS_DENO or _HAS_NODE):
    print("[setup] Warning: no 'deno'/'node' on PATH (needed for YouTube n-challenge).")

ROOT = Path.cwd()
DOWNLOADS_DIR = ROOT / "downloads"
COOKIES_DIR = ROOT / ".webapp_cookies"
DOWNLOADS_DIR.mkdir(exist_ok=True)
COOKIES_DIR.mkdir(exist_ok=True)
for _f in COOKIES_DIR.glob("*.txt"):  # housekeeping: temp copies always, uploads after 7 days
    try:
        if _f.name.startswith("job_") or time.time() - _f.stat().st_mtime > 7 * 86400:
            _f.unlink()
    except OSError:
        pass

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
MAX_LOG_LINES = 250
AUDIO_EXT = {".mp3", ".m4a", ".opus", ".wav"}
SKIP_EXT = (".part", ".ytdl", ".temp")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
DEFAULT_RES = [144, 240, 360, 480, 720, 1080]


# ---------------- helpers ----------------
def _dl_url(path: Path) -> str:
    return "/downloads/" + quote(path.relative_to(DOWNLOADS_DIR).as_posix())


def _list_downloaded_files() -> list[dict]:
    out: list[dict] = []
    files = []
    for p in DOWNLOADS_DIR.rglob("*"):
        try:
            if p.is_file() and not p.name.endswith(SKIP_EXT):
                files.append((p.stat().st_mtime, p))
        except OSError:
            continue
    files.sort(key=lambda t: t[0], reverse=True)
    for mt, fp in files:
        try:
            ext = fp.suffix.lower()
            out.append({
                "name": fp.name, "path": fp.relative_to(DOWNLOADS_DIR).as_posix(), "url": _dl_url(fp),
                "size_mb": round(fp.stat().st_size / 1048576, 2), "mtime": int(mt),
                "is_zip": ext == ".zip",
                "kind": "audio" if ext in AUDIO_EXT else ("zip" if ext == ".zip" else "video"),
            })
        except OSError:
            continue
    return out


def _split_urls(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for p in re.split(r"[\s,]+", (text or "").strip()):
        if p.startswith(("http://", "https://")):
            seen.setdefault(p)
    return list(seen)


def _is_playlist_url(url: str) -> bool:
    try:
        q = urlparse(url).query
        m = re.search(r"(?:^|&)list=([^&]+)", q)
        if not m:
            return False
        # Auto-generated "Mix" lists (RD…) are endless; treat a video link carrying one as a single video.
        return not (m.group(1).startswith("RD") and "v=" in q)
    except Exception:
        return False


def _cookie_name(name: str) -> str:
    """Only accept names of files that live inside COOKIES_DIR."""
    if not name:
        return ""
    n = Path(str(name)).name
    return n if (COOKIES_DIR / n).is_file() else ""


def _cookie_copy(name: str, job_id: str) -> str:
    """yt-dlp rewrites its cookie jar; give every job a private copy so jobs never collide."""
    n = _cookie_name(name)
    if not n:
        return ""
    dst = COOKIES_DIR / f"job_{job_id}.txt"
    try:
        shutil.copyfile(COOKIES_DIR / n, dst)
        return str(dst)
    except OSError:
        return ""


def _cookie_cleanup(job_id: str) -> None:
    try:
        (COOKIES_DIR / f"job_{job_id}.txt").unlink(missing_ok=True)
    except OSError:
        pass


def _new_job(kind: str, **fields) -> str:
    jid = uuid.uuid4().hex[:12]
    now = time.time()
    with JOBS_LOCK:
        for k in [k for k, j in JOBS.items()
                  if j.get("status") in ("done", "error", "probe_done") and now - j.get("ts", now) > 7200]:
            del JOBS[k]
        JOBS[jid] = {"id": jid, "kind": kind, "status": "queued", "stage": "Queued", "progress": 0,
                     "ts": now, "log": [], **fields}
    return jid


def _job_log(job_id: str, message: str, level: str = "info") -> None:
    text = _ANSI.sub("", str(message)).strip()
    if not text:
        return
    line = {"time": time.strftime("%H:%M:%S"), "level": level, "message": text}
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        logs = job["log"]
        logs.append(line)
        del logs[:-MAX_LOG_LINES]
        job["last_message"] = text
        if level == "error":
            job["last_error"] = text


def _job_update(job_id: str, **values) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(values)


class YtdlpJobLogger:
    def __init__(self, job_id: str):
        self.job_id = job_id

    def debug(self, msg):
        t = str(msg)
        if t.startswith("[debug]") or ("[download]" in t and "% of" in t):
            return
        _job_log(self.job_id, t, "debug")

    def info(self, msg):
        self.debug(msg)

    def warning(self, msg):
        if "no longer valid" in str(msg) or "cookies are invalid" in str(msg).lower():
            _job_update(self.job_id, cookie_bad=True)
        _job_log(self.job_id, msg, "warn")

    def error(self, msg):
        _job_log(self.job_id, msg, "error")


def _friendly_error(error) -> str:
    msg = _ANSI.sub("", str(error))
    missing = []
    if not _HAS_EJS:
        missing.append("pip install -U yt-dlp-ejs")
    if not (_HAS_DENO or _HAS_NODE):
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


_COOKIE_RE = re.compile(r"sign in|not a bot|confirm your age|age-restricted|cookies|login required|"
                        r"members-only|private video|429|too many requests|HTTP Error 403", re.I)


def _cookie_flags(err, has_cookie: bool, job_id: str) -> dict:
    """Tell the UI whether to open the Cookies panel, and whether cookies are missing or rejected."""
    with JOBS_LOCK:
        bad = bool(JOBS.get(job_id, {}).get("cookie_bad"))
    need = bool(_COOKIE_RE.search(_ANSI.sub("", str(err)))) or bad
    return {"needs_cookies": need, "cookie_state": "invalid" if (has_cookie or bad) else "missing"}


def _base_opts() -> dict:
    opts: dict = {"quiet": True, "no_warnings": True, "noprogress": True, "socket_timeout": 30}
    if _FFMPEG:
        opts["ffmpeg_location"] = _FFMPEG
    if not _HAS_DENO and _HAS_NODE:  # yt-dlp only enables deno by default
        opts["js_runtimes"] = {"node": {}}
    return opts


# ---------------- probing ----------------
def probe_url(url: str, cookies_file: str, playlist: bool, limit: int = 200, job_id: str | None = None) -> dict:
    opts = _base_opts()
    opts.update(skip_download=True, extract_flat="in_playlist", retries=3, extractor_retries=3,
                ignoreerrors=False, noplaylist=not playlist)
    if cookies_file:
        opts["cookiefile"] = cookies_file
    if job_id:
        opts["logger"] = YtdlpJobLogger(job_id)
        _job_log(job_id, "Connecting to YouTube and reading metadata…")
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("Could not read this link.")

    videos, is_playlist = [], False
    title = info.get("title") or "video"
    if info.get("_type") == "playlist" or info.get("entries") is not None:
        is_playlist = True
        for e in islice(info.get("entries") or [], limit):
            if not e:
                continue
            vu = e.get("url") or e.get("webpage_url") or ""
            if vu and not vu.startswith("http"):
                vu = f"https://www.youtube.com/watch?v={vu}"
            th = e.get("thumbnail") or ((e.get("thumbnails") or [{}])[-1].get("url"))
            videos.append({"title": e.get("title") or "(untitled)", "url": vu,
                           "duration": e.get("duration"), "thumbnail": th})
    else:
        videos.append({"title": title, "url": info.get("webpage_url") or url,
                       "duration": info.get("duration"), "thumbnail": info.get("thumbnail")})

    res = sorted({f["height"] for f in (info.get("formats") or [])
                  if f.get("height") and f.get("vcodec") != "none"}) or DEFAULT_RES
    if job_id:
        _job_log(job_id, f"Found {len(videos)} item(s).", "ok")
    return {"title": title, "is_playlist": is_playlist, "videos": videos, "resolutions": res}


def run_probe(job_id: str) -> None:
    with JOBS_LOCK:
        job = dict(JOBS[job_id])
    cookies = ""
    try:
        _job_update(job_id, status="fetching", stage="Parsing links", progress=8)
        urls = _split_urls(job.get("url_text", ""))
        if not urls:
            raise ValueError("Please paste at least one YouTube URL.")
        cookies = _cookie_copy(job.get("cookie", ""), job_id)
        _job_log(job_id, f"Cookies: {'loaded' if cookies else 'not uploaded'}.")
        _job_update(job_id, progress=25, stage="Detecting mode")
        if len(urls) > 1:
            _job_log(job_id, f"Multiple mode: {len(urls)} links (skipping slow probing).", "ok")
            info = {"title": "Multiple videos", "is_playlist": False, "mode": "multiple", "urls": urls,
                    "videos": [{"title": u, "url": u, "duration": None, "thumbnail": None} for u in urls],
                    "resolutions": DEFAULT_RES}
        else:
            playlist = _is_playlist_url(urls[0])
            _job_update(job_id, progress=40, stage="Reading playlist" if playlist else "Reading video")
            info = probe_url(urls[0], cookies, playlist, job_id=job_id)
            info["mode"] = "playlist" if playlist and info["is_playlist"] else "single"
            info["urls"] = urls
        _job_update(job_id, status="probe_done", stage="Ready", progress=100, probe=info)
        _job_log(job_id, "Fetch complete.", "ok")
    except Exception as e:
        traceback.print_exc()
        m = _friendly_error(e)
        _job_update(job_id, status="error", stage="Fetch failed", error=m, progress=100,
                    **_cookie_flags(e, bool(cookies), job_id))
        _job_log(job_id, m, "error")
    finally:
        _cookie_cleanup(job_id)


# ---------------- download worker ----------------
def _make_progress_hook(job_id: str):
    st = {"vid": None, "fin": 0}

    def hook(d: dict) -> None:
        info = d.get("info_dict") or {}
        vid = info.get("id")
        n = max(1, len(info.get("requested_formats") or []))  # video+audio = 2 streams → one smooth 0-100
        status = d.get("status")
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if not j:
                return
            if vid != st["vid"]:
                st["vid"], st["fin"] = vid, 0
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                frac = min(1.0, (d.get("downloaded_bytes") or 0) / total) if total else 0.0
                j.update(stage="Downloading", current_file=os.path.basename(d.get("filename") or ""),
                         speed=d.get("speed") or 0, eta=d.get("eta") or 0,
                         current_pct=min(99.0, (st["fin"] + frac) / n * 100))
            elif status == "finished":
                st["fin"] += 1
                j["current_pct"] = min(99.0, st["fin"] / n * 100)
    return hook


def _make_pp_hook(job_id: str):
    def hook(d: dict) -> None:
        pp, st = d.get("postprocessor") or "postprocess", d.get("status")
        if st == "started":
            _job_update(job_id, stage=f"Running {pp}")
        elif st == "finished":
            _job_log(job_id, f"Finished {pp}.", "ok")
            if pp == "MoveFiles":
                with JOBS_LOCK:
                    j = JOBS.get(job_id)
                    if j:
                        j["completed_items"] = int(j.get("completed_items") or 0) + 1
                        j["current_pct"] = 0
    return hook


def _fix_unmerged(out_dir: Path, job_id: str) -> None:
    """Safety net: if yt-dlp left separate streams (name.f137.mp4 + name.f140.m4a), merge them."""
    if not _FFMPEG:
        return
    groups: dict = {}
    for fp in out_dir.rglob("*"):
        m = re.match(r"(.+)\.f\d+(?:-\w+)*\.(\w+)$", fp.name)
        if m and fp.is_file():
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
            try:
                ok = subprocess.run(cmd, capture_output=True, timeout=1800).returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                ok = False
            if ok and out.exists() and out.stat().st_size > 0:
                for x in parts:
                    x.unlink(missing_ok=True)
                _job_log(job_id, "Merged successfully.", "ok")
                break
            out.unlink(missing_ok=True)
        else:
            _job_log(job_id, f"Could not merge '{base}'; leaving the separate streams.", "error")


def run_download(job_id: str) -> None:
    with JOBS_LOCK:
        job = dict(JOBS[job_id])
        JOBS[job_id].update(status="running", stage="Preparing download", progress=0)
    cookies = ""
    try:
        out_dir = DOWNLOADS_DIR / job["folder_name"]
        out_dir.mkdir(parents=True, exist_ok=True)
        mode, media = job["mode"], job["media_type"]
        try:
            chosen = max(0, int(job.get("resolution") or 0))
        except (TypeError, ValueError):
            chosen = 0
        cookies = _cookie_copy(job.get("cookie", ""), job_id)
        if media == "audio":
            fmt = "bestaudio/best"
        else:
            h = f"[height<={chosen}]" if chosen else ""
            fmt = f"bv*[ext=mp4]{h}+ba[ext=m4a]/bv*{h}+ba/b{h}/b"
        _job_log(job_id, f"Mode: {mode} · {media} · format: {fmt}")

        opts = _base_opts()
        opts.update({
            "format": fmt, "ignoreerrors": True, "concurrent_fragment_downloads": 8,
            "outtmpl": str(out_dir / ("%(playlist_index)s - %(title).120B.%(ext)s" if mode == "playlist"
                                      else "%(title).120B.%(ext)s")),
            "retries": 20, "fragment_retries": 20, "extractor_retries": 5,
            "progress_hooks": [_make_progress_hook(job_id)],
            "postprocessor_hooks": [_make_pp_hook(job_id)],
            "logger": YtdlpJobLogger(job_id),
        })
        if cookies:
            opts["cookiefile"] = cookies
        if not _FFMPEG:
            _job_log(job_id, "ffmpeg missing: video and audio CANNOT be merged!", "error")
        if media == "audio":
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                       "preferredquality": "192"}]
        else:
            opts["merge_output_format"] = "mp4"

        targets = job["urls"]
        if not targets:
            raise ValueError("Nothing selected.")
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

        _job_update(job_id, stage="Downloading", total_items=planned)
        with YoutubeDL(opts) as ydl:
            ydl.download(targets)

        _fix_unmerged(out_dir, job_id)
        _job_update(job_id, stage="Collecting files")
        files = sorted(p for p in out_dir.rglob("*") if p.is_file() and not p.name.endswith(SKIP_EXT))
        if not files:
            shutil.rmtree(out_dir, ignore_errors=True)
            with JOBS_LOCK:
                last = JOBS[job_id].get("last_error")
            raise RuntimeError(last or "Nothing was downloaded.")

        links: list[dict] = []
        if mode == "single" or len(files) == 1:
            for fp in files:
                links.append({"name": fp.name, "size_mb": round(fp.stat().st_size / 1048576, 1),
                              "url": _dl_url(fp)})
        else:
            _job_update(job_id, stage="Creating ZIP")
            _job_log(job_id, f"Creating ZIP with {len(files)} file(s)…")
            zip_path = DOWNLOADS_DIR / f"{job['folder_name']}.zip"
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
                for fp in files:
                    zf.write(fp, arcname=fp.relative_to(DOWNLOADS_DIR).as_posix())
            shutil.rmtree(out_dir, ignore_errors=True)
            links.append({"name": zip_path.name, "size_mb": round(zip_path.stat().st_size / 1048576, 1),
                          "url": _dl_url(zip_path), "is_zip": True, "file_count": len(files)})
        _job_update(job_id, status="done", stage="Complete", current_pct=100, completed_items=planned,
                    links=links)
        _job_log(job_id, "All done. Download link is ready.", "ok")
    except Exception as e:
        traceback.print_exc()
        m = _friendly_error(e)
        _job_update(job_id, status="error", stage="Failed", error=m, **_cookie_flags(e, bool(cookies), job_id))
        _job_log(job_id, m, "error")
    finally:
        _cookie_cleanup(job_id)


# ---------------- HTTP handler ----------------
INDEX_HTML = ""  # filled below


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass

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
        if n <= 0 or n > 1_000_000:
            return {}
        try:
            d = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _serve_file(self, rel: str) -> None:
        root = DOWNLOADS_DIR.resolve()
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return self.send_error(403)
        if not target.is_file():
            return self.send_error(404)
        size = target.stat().st_size
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)$", (self.headers.get("Range") or "").strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                return self.end_headers()
            code = 206
        length = end - start + 1
        try:
            self.send_response(code)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(target.name)}")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(target, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html"):
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/api/job/"):
            with JOBS_LOCK:
                j = JOBS.get(path.rsplit("/", 1)[-1])
                out = None
                if j:
                    out = {k: v for k, v in j.items() if k not in ("cookie", "url_text")}
                    out["log"] = list(j.get("log", []))[-80:]
            if not out:
                return self._send_json(404, {"error": "unknown job"})
            self._send_json(200, out)
        elif path == "/api/files":
            self._send_json(200, {"files": _list_downloaded_files()})
        elif path.startswith("/downloads/") and not path.endswith("/"):
            self._serve_file(path[len("/downloads/"):])
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)

    def do_DELETE(self):
        if urlparse(self.path).path != "/api/files":
            return self.send_error(404)
        try:
            rel = (self._read_json().get("path") or "").strip().lstrip("/")
            if not rel:
                return self._send_json(400, {"error": "Missing path"})
            root = DOWNLOADS_DIR.resolve()
            target = (root / rel).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                return self._send_json(400, {"error": "Invalid path"})
            if target == root or not target.exists():
                return self._send_json(404, {"error": "Not found"})
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            p = target.parent
            if p != root and p.exists() and not any(p.iterdir()):
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
            if path not in ("/api/probe", "/api/download"):
                return self.send_error(404)
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._send_json(415, {"error": "JSON body required."})
            data = self._read_json()
            cookie = _cookie_name(data.get("cookies_file") or "")
            if path == "/api/probe":
                text = str(data.get("url", ""))
                if not _split_urls(text):
                    return self._send_json(400, {"error": "Please paste at least one YouTube URL."})
                jid = _new_job("probe", url_text=text, cookie=cookie)
                threading.Thread(target=run_probe, args=(jid,), daemon=True).start()
                return self._send_json(202, {"job_id": jid})
            urls = [u for u in (data.get("urls") or []) if isinstance(u, str) and u.startswith(("http://", "https://"))]
            if not urls:
                return self._send_json(400, {"error": "Nothing selected."})
            folder = re.sub(r"[^\w\-\. ]+", "_", str(data.get("title") or "download"))[:80].strip(" .") or "job"
            mode = data.get("mode") if data.get("mode") in ("single", "multiple", "playlist") else "single"
            try:
                total = max(1, int(data.get("total_items") or len(urls)))
            except (TypeError, ValueError):
                total = len(urls)
            pi = str(data.get("playlist_items") or "")
            if not re.fullmatch(r"[\d,\-\s]*", pi):
                return self._send_json(400, {"error": "Bad playlist selection."})
            jid = uuid.uuid4().hex[:4]
            jid = _new_job(
                "download", mode=mode, media_type="audio" if data.get("media_type") == "audio" else "video",
                resolution=data.get("resolution") or 0, urls=urls, playlist_items=pi,
                folder_name=f"{folder}_{jid}", cookie=cookie, current_file="", current_pct=0,
                completed_items=0, total_items=total)
            threading.Thread(target=run_download, args=(jid,), daemon=True).start()
            self._send_json(202, {"job_id": jid})
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
            boundary = ctype.split("boundary=", 1)[1].split(";")[0].strip().strip('"').encode()
            file_bytes = None
            for p in body.split(b"--" + boundary):
                if b"filename=" in p:
                    he = p.find(b"\r\n\r\n")
                    if he != -1:
                        file_bytes = p[he + 4:]
                        if file_bytes.endswith(b"\r\n"):
                            file_bytes = file_bytes[:-2]
                        break
            if not file_bytes:
                return self._send_json(400, {"error": "No file found in upload."})
        text = file_bytes.decode("utf-8", errors="ignore")
        if "Netscape HTTP Cookie File" not in "\n".join(text.splitlines()[:3]) and "\t" not in text:
            return self._send_json(400, {"error": "Not a Netscape cookies.txt file."})
        cp = COOKIES_DIR / f"c_{uuid.uuid4().hex[:10]}.txt"
        cp.write_bytes(file_bytes)
        self._send_json(200, {"cookies_file": cp.name, "name": cp.name})


INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="color-scheme" content="dark"/><meta name="theme-color" content="#000000"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Great+Vibes&display=swap"/>
<title>4D · downloader</title>
<style>
:root{--e:cubic-bezier(.16,1,.3,1);--bg:#000;--sf:#0b0b0f;--hi:rgba(255,255,255,.06);--lo:rgba(0,0,0,.9);--a:#8b7bff;--b:#38e1ff;--t:#f4f4f8;--m:#6f7287;--r:#ff6b8a;--cal:"Great Vibes","Snell Roundhand","Apple Chancery","Segoe Script","Lucida Handwriting","Brush Script MT",cursive}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}html,body{margin:0;height:100%;background:var(--bg)}
body{color:var(--t);font:300 16px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;overflow:hidden}
button,input{font:inherit;color:inherit}button{cursor:pointer}
:focus-visible{outline:2px solid var(--b);outline-offset:3px}
#c{position:fixed;inset:0;width:100%;height:100%;display:block}
/* neumorphism on OLED black */
.up{background:var(--sf);box-shadow:-5px -5px 12px var(--hi),7px 7px 16px var(--lo)}
.dn{background:var(--sf);box-shadow:inset -3px -3px 8px var(--hi),inset 5px 5px 10px var(--lo)}
.bar{position:fixed;top:0;left:0;right:0;display:flex;gap:14px;align-items:center;padding:18px 22px;z-index:20;pointer-events:none}
.bar>*{pointer-events:auto}.sp{flex:1;pointer-events:none!important}
.mark{font:400 40px/1 var(--cal);color:var(--t)}
.ib{width:44px;height:44px;border-radius:50%;border:0;font-size:17px;transition:box-shadow .35s var(--e),color .3s}
.ib:hover{color:var(--b)}.ib:active,.ib.off{box-shadow:inset -3px -3px 8px var(--hi),inset 5px 5px 10px var(--lo)}.ib.off{color:var(--m)}
.step{position:fixed;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:flex-start;gap:20px;padding:84px 20px 7vh;text-align:center;overflow-y:auto;opacity:0;visibility:hidden;pointer-events:none;transform:translate3d(0,36px,0) scale(.98);transition:opacity .7s var(--e),transform .9s var(--e),visibility 0s linear .7s;z-index:5;scrollbar-width:none}
.step::-webkit-scrollbar{display:none}.step>:first-child{margin-top:auto}
.step.on{opacity:1;visibility:visible;pointer-events:auto;transform:none;transition-delay:0s}
.step.gone{transform:translate3d(0,-36px,0) scale(1.02)}
#s-prog{justify-content:center}#s-prog>:first-child{margin-top:0}
h1{font:400 clamp(72px,17vw,230px)/1.1 var(--cal);letter-spacing:0;margin:0}
h2{font:400 clamp(36px,6vw,68px)/1.15 var(--cal);letter-spacing:0;margin:0;max-width:86vw}#cT{font:200 clamp(20px,3vw,30px)/1.3 ui-sans-serif,system-ui}
.hint{color:var(--m);font-size:13px;letter-spacing:.04em;margin:0}.small{max-width:80vw;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.field{display:flex;align-items:center;gap:10px;width:min(660px,92vw);padding:8px 8px 8px 28px;border-radius:99px}

input{flex:1;background:none;border:0;outline:0;font-weight:300;font-size:18px;min-width:0}input::placeholder{color:#4d5068}input:focus,input:focus-visible{outline:0;box-shadow:none}
.orb{width:54px;height:54px;border-radius:50%;border:0;font-size:22px;color:var(--b);transition:box-shadow .3s}.orb:active{box-shadow:inset -3px -3px 8px var(--hi),inset 5px 5px 10px var(--lo)}
.pill{padding:15px 34px;border-radius:99px;border:0;font:400 26px/1.2 var(--cal);letter-spacing:0;transition:box-shadow .3s,color .3s}
.pill:hover{color:var(--b)}.pill:active{box-shadow:inset -3px -3px 8px var(--hi),inset 5px 5px 10px var(--lo)}
.go{padding:14px 60px;font-size:32px;color:var(--b);box-shadow:-5px -5px 12px var(--hi),7px 7px 16px var(--lo)}
.err{color:var(--r);font-size:13px;min-height:18px;white-space:pre-wrap;max-width:80vw}
.cards{display:flex;gap:18px;overflow-x:auto;scroll-snap-type:x mandatory;padding:16px 24px 22px;max-width:100vw;width:100%;justify-content:safe center;scrollbar-width:none}
.cards::-webkit-scrollbar{display:none}
.cd{flex:0 0 172px;scroll-snap-align:center;border-radius:22px;overflow:hidden;cursor:pointer;text-align:left;opacity:.5;transition:opacity .35s,box-shadow .4s var(--e)}
.cd.sel{opacity:1;box-shadow:-5px -5px 12px var(--hi),7px 7px 16px var(--lo)}
.cd img,.cd .th{width:100%;height:96px;object-fit:cover;display:block;background:#15151c}
.cd b{display:-webkit-box;font:400 12px/1.35 ui-sans-serif;padding:10px 12px 12px;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.seg{position:relative;display:flex;padding:5px;border-radius:99px}
.seg button{position:relative;z-index:1;border:0;background:none;font-weight:400;font-size:14px;padding:11px 0;width:128px;letter-spacing:.06em;transition:color .4s}
.seg button.on{color:var(--b)}
.seg i{position:absolute;top:5px;bottom:5px;left:5px;width:128px;border-radius:99px;transition:transform .6s var(--e);box-shadow:-4px -4px 10px var(--hi),5px 5px 12px var(--lo)}
.chips{display:flex;gap:12px;flex-wrap:wrap;justify-content:center;max-width:90vw}
.ch{padding:8px 18px;border-radius:99px;color:var(--m);font-size:13px;cursor:pointer;transition:box-shadow .4s var(--e),color .3s;box-shadow:-3px -3px 8px var(--hi),4px 4px 10px var(--lo)}
.ch.on{color:var(--b);box-shadow:inset -3px -3px 7px var(--hi),inset 4px 4px 9px var(--lo)}
.lnk{color:var(--m);font-size:13px;background:none;border:0;letter-spacing:.06em;padding:6px 10px}.lnk:hover{color:var(--t)}
#pnum{font:100 clamp(120px,30vw,380px)/1 ui-sans-serif,system-ui;font-variant-numeric:tabular-nums;letter-spacing:-.06em}
.dl{display:flex;flex-direction:column;gap:16px;align-items:center;max-width:90vw}
.dl a{display:flex;gap:12px;align-items:center;padding:16px 30px;border-radius:99px;color:var(--b);text-decoration:none;font-weight:400;max-width:100%;word-break:break-all;box-shadow:-5px -5px 12px var(--hi),7px 7px 16px var(--lo);transition:box-shadow .3s}
.dl a:active{box-shadow:inset -3px -3px 8px var(--hi),inset 5px 5px 10px var(--lo)}
.dl small{color:var(--m);font-weight:300;white-space:nowrap}
.shade{position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:30;opacity:0;pointer-events:none;transition:opacity .5s var(--e)}.shade.on{opacity:1;pointer-events:auto}
.sheet{position:fixed;top:0;right:0;bottom:0;width:min(420px,92vw);z-index:31;background:var(--sf);box-shadow:-10px 0 30px var(--lo);padding:26px;transform:translate3d(105%,0,0);visibility:hidden;transition:transform .7s var(--e),visibility 0s linear .7s;overflow:auto}
.sheet.on{transform:none;visibility:visible;transition-delay:0s}.sheet h3{font:400 40px/1.1 var(--cal);margin:0 0 18px}
.li{padding:12px 0;border-bottom:1px solid rgba(255,255,255,.06);display:flex;gap:12px;align-items:center}.li .n{flex:1;font-size:13px;word-break:break-all}.li a,.li button{color:var(--b);font-size:13px;background:none;border:0;text-decoration:none}.li button{color:var(--r)}
.drop{border-radius:22px;padding:44px 16px;text-align:center;color:var(--m);cursor:pointer;display:block;transition:color .3s}.drop:hover,.drop.dz{color:var(--b)}
pre{font:11px/1.6 ui-monospace,monospace;color:#8fe;white-space:pre-wrap;margin:0}
.toast{position:fixed;left:50%;bottom:26px;transform:translate(-50%,30px);opacity:0;z-index:50;padding:11px 24px;border-radius:99px;font-size:14px;pointer-events:none;transition:opacity .5s var(--e),transform .6s var(--e);box-shadow:-4px -4px 10px var(--hi),6px 6px 14px var(--lo)}.toast.on{opacity:1;transform:translate(-50%,0)}
.ltr .w{display:inline-block;white-space:nowrap}
.ltr .w span{display:inline-block;clip-path:inset(-40% 100% -40% 0);opacity:.25;transform:translateY(.05em) skewX(-7deg);animation:write .6s cubic-bezier(.4,.1,.2,1) forwards,ink 6s ease-in-out infinite;animation-delay:calc(var(--i)*90ms),calc(1.4s + var(--i)*120ms)}
@keyframes write{60%{opacity:1}to{clip-path:inset(-40% -60% -40% -60%);opacity:1;transform:none}}
@keyframes ink{0%,60%,100%{text-shadow:0 0 0 transparent}30%{text-shadow:0 0 22px rgba(56,225,255,.5)}}
#pnum{animation:pulse 4s ease-in-out infinite}@keyframes pulse{50%{color:var(--b)}}
.note{color:var(--r);font-size:14px;margin:0 0 14px}.note:empty{display:none}
.sheet h4{font:300 17px ui-sans-serif;margin:26px 0 8px}
.how{text-align:left;color:var(--m);font-size:13px;line-height:1.75;padding-left:18px;margin:0}.how li{margin-bottom:6px}.how a{color:var(--b)}.how b{color:var(--t);font-weight:400}
#cState{margin-top:12px}
@media (prefers-reduced-motion:reduce){.step,.sheet,.toast{transition-duration:.01s}.ltr .w span{animation:none;opacity:1;transform:none;clip-path:none}#pnum{animation:none}}
</style></head><body>
<canvas id="c"></canvas>
<div class="bar"><div class="mark">4D</div><div class="sp"></div>
 <button class="ib up" id="bV" title="Files" aria-label="Files">⌂</button><button class="ib up" id="bC" title="Cookies" aria-label="Cookies">◍</button><button class="ib up" id="bL" title="Log" aria-label="Log">≡</button><button class="ib up" id="bS" title="Sound" aria-label="Sound">♪</button></div>

<section class="step on" id="s-paste"><h1 class="ltr" data-t="Drop a link."></h1>
 <div class="field dn"><input id="url" placeholder="Paste a YouTube link" autocomplete="off" spellcheck="false" aria-label="YouTube link"><button class="orb up mag" id="goF" aria-label="Fetch">→</button></div>
 <p class="hint">Videos, playlists, or several links at once.</p><div class="err" id="fErr" role="alert"></div></section>
<section class="step" id="s-fetch"><h2 id="fT" class="ltr" data-t="Reading the link"></h2><p class="hint" id="fS">Connecting</p></section>
<section class="step" id="s-choose"><h2 id="cT"></h2><p class="hint" id="cM"></p>
 <div class="cards" id="cards"></div>
 <div class="seg dn" id="seg"><i class="up"></i><button data-v="video" class="on">Video</button><button data-v="audio">Audio</button></div>
 <div class="chips" id="chips"></div>
 <div class="err" id="cErr" role="alert"></div>
 <button class="pill go up mag" id="goD">Download</button><button class="lnk" id="back">New link</button></section>
<section class="step" id="s-prog"><div id="pnum">0%</div><h2 id="pT" class="ltr" data-t="Downloading"></h2><p class="hint" id="pS"></p><p class="hint small" id="pF"></p></section>
<section class="step" id="s-done"><h1 class="ltr" data-t="Ready."></h1><div class="dl" id="links"></div><button class="pill up mag" id="again">Another link</button></section>

<div class="shade" id="shade"></div>
<aside class="sheet" id="sh-V"><h3>Files</h3><div id="lib"></div></aside>
<aside class="sheet" id="sh-C"><h3>Cookies</h3>
 <p class="note" id="cNote" role="alert"></p>
 <label class="drop dn" id="dz" for="cf">Drop cookies.txt here<br><small>or tap to choose the file</small><input type="file" id="cf" accept=".txt" hidden></label>
 <p class="hint" id="cState"></p>
 <h4>How to get cookies.txt</h4>
 <ol class="how">
  <li>Install <b>Get cookies.txt LOCALLY</b>: <a href="https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc" target="_blank" rel="noopener">Chrome, Edge, Brave</a>. On Firefox use <a href="https://addons.mozilla.org/firefox/addon/cookies-txt/" target="_blank" rel="noopener">cookies.txt</a>.</li>
  <li>Open a private (incognito) window and sign in to YouTube. In the extension settings, allow it in private windows.</li>
  <li>In that same tab, open <b>youtube.com/robots.txt</b>.</li>
  <li>Click the extension and export cookies for youtube.com. A cookies.txt file downloads.</li>
  <li>Close the private window so YouTube doesn’t rotate the cookies.</li>
  <li>Drop the file in the box above, then try your link again.</li>
 </ol>
 <p class="hint" style="margin-top:16px;text-align:left">Cookies give access to your account. Use a spare account, and never share the file. It stays on this machine.</p>
</aside>
<aside class="sheet" id="sh-L"><h3>Log</h3><pre id="log"></pre></aside>
<div class="toast up" id="toast"></div>
<script>
(()=>{"use strict";
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const RM=matchMedia("(prefers-reduced-motion: reduce)").matches;
const S={cookie:"",probe:null,media:"video",res:0,ph:"idle",snd:true,lock:false,disp:0,target:0,speed:0,step:"paste",tok:0};
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const split=(el,t)=>{el.setAttribute("aria-label",t);let i=0;el.innerHTML=t.split(" ").map(w=>`<span class="w" aria-hidden="true">${[...w].map(c=>`<span style="--i:${i++}">${esc(c)}</span>`).join("")}</span>`).join(" ")};
const fmtS=b=>!b?"—":b>1e6?(b/1e6).toFixed(1)+" MB/s":b>1e3?(b/1e3|0)+" KB/s":(b|0)+" B/s";
const fmtE=s=>!s?"—":s<60?s+"s":(s/60|0)+"m "+s%60+"s";
async function api(p,o={}){const ac=new AbortController(),t=setTimeout(()=>ac.abort(),20000);try{const r=await fetch(p,{...o,signal:ac.signal});const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||"HTTP "+r.status);return j}finally{clearTimeout(t)}}
const JSONH={"Content-Type":"application/json"};
/* ---------- sound (one scheduler, cleared on every phase change so nothing overlaps) ---------- */
let AC;function tone(f,d=.2,t="sine",v=.05,at=0,f2){if(!S.snd)return;try{AC=AC||new(window.AudioContext||window.webkitAudioContext)();if(AC.state==="suspended")AC.resume();const o=AC.createOscillator(),g=AC.createGain(),n=AC.currentTime+at;o.type=t;o.frequency.setValueAtTime(f,n);if(f2)o.frequency.exponentialRampToValueAtTime(f2,n+d);g.gain.setValueAtTime(.0001,n);g.gain.exponentialRampToValueAtTime(v,n+.02);g.gain.exponentialRampToValueAtTime(.0001,n+d);o.connect(g).connect(AC.destination);o.start(n);o.stop(n+d+.05)}catch(_){}}
const chord=a=>a.forEach((f,i)=>tone(f,.9,"sine",.05,i*.08));
const timers=new Set();let iv=0;const later=(f,ms)=>{const id=setTimeout(()=>{timers.delete(id);f()},ms);timers.add(id)};
let tt=0;function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("on");clearTimeout(tt);tt=setTimeout(()=>t.classList.remove("on"),2800)}
$("#bS").onclick=()=>{S.snd=!S.snd;$("#bS").classList.toggle("off",!S.snd);tone(660)};
/* ---------- steps & sheets ---------- */
const LB={fetch:"Reading the link",download:"Downloading",merge:"Merging video + audio",convert:"Making MP3",zip:"Packing ZIP",done:"Done",error:"Something broke"};
function go(n){S.step=n;$$(".step").forEach(e=>{const me=e.id==="s-"+n;if(me){e.classList.remove("gone");e.classList.add("on")}else if(e.classList.contains("on")){e.classList.remove("on");e.classList.add("gone")}else e.classList.remove("gone")});
 if(n==="prog")$("#pT").dataset.t=LB[S.ph]||"Downloading";$$("#s-"+n+" .ltr").forEach(e=>split(e,e.dataset.t));if(n==="paste")setTimeout(()=>$("#url").focus({preventScroll:true}),350);tone(520,.35,"sine",.03,0,780);refreshMag()}
function sheet(id){const open=$(".sheet.on");if(open)open.classList.remove("on");const s=id&&$("#sh-"+id);if(s&&s!==open){s.classList.add("on");$("#shade").classList.add("on");tone(400,.3,"sine",.03,0,600)}else $("#shade").classList.remove("on")}
$("#shade").onclick=()=>sheet(null);$("#bV").onclick=()=>{refreshLib();sheet("V")};$("#bC").onclick=()=>sheet("C");$("#bL").onclick=()=>sheet("L");
addEventListener("keydown",e=>{if(e.key==="Escape")sheet(null);if(e.ctrlKey&&e.key==="Enter"){if(S.step==="choose")$("#goD").click();else if(S.step==="paste")$("#goF").click()}});
/* ---------- N-dimensional hypercubes: 4D tesseract (working) · 5D penteract (finished) ---------- */
function mk(n,sc){const V=[],E=[],pl=[];for(let i=0;i<1<<n;i++)V.push(Array.from({length:n},(_,b)=>(i>>b&1?1:-1)*sc));for(let i=0;i<1<<n;i++)for(let b=0;b<n;b++){const j=i^1<<b;if(j>i)E.push([i,j,b])}for(let i=0;i<n;i++)for(let j=i+1;j<n;j++)pl.push([i,j]);return{n,V,E,pl}}
const P4=mk(4,1),P5=mk(5,.7);
function proj(P,A){return P.V.map(p=>{const v=p.slice();for(let k=0;k<P.pl.length;k++){const[i,j]=P.pl[k],c=Math.cos(A[k]),s=Math.sin(A[k]),x=v[i],y=v[j];v[i]=x*c-y*s;v[j]=x*s+y*c}
 for(let d=P.n-1;d>=3;d--){const k=d===3?1/Math.max(.6,2.6-v[3]):4.2/Math.max(.8,4.2-v[d]);for(let i=0;i<d;i++)v[i]*=k}
 const s=1/Math.max(1.2,3.6-v[2]*1.6);return[v[0]*s*3.4,v[1]*s*3.4,s]})}
const cv=$("#c"),g=cv.getContext("2d",{alpha:false});let W,H,stars=[];
function rz(){const d=Math.min(devicePixelRatio||1,2);W=innerWidth;H=innerHeight;cv.width=W*d;cv.height=H*d;g.setTransform(d,0,0,d,0,0);g.fillStyle="#000";g.fillRect(0,0,W,H);stars=Array.from({length:RM?50:110},()=>({x:Math.random()*W,y:Math.random()*H,z:Math.random()*.9+.1,p:Math.random()*6}));refreshMag()}
addEventListener("resize",()=>{clearTimeout(rz.t);rz.t=setTimeout(rz,120)});
function drawT(P,pts,cx,cy,R,h,prog,al){const m=P.E.length;g.lineCap="round";
 for(let n=0;n<m;n++){const[a,b,k]=P.E[n],p=pts[a],q=pts[b],x1=cx+p[0]*R,y1=cy+p[1]*R,x2=cx+q[0]*R,y2=cy+q[1]*R,d=(p[2]+q[2])/2,col=`hsla(${h+k*20},85%,${62+d*28}%,`;g.lineWidth=1+d*3;
  g.strokeStyle=col+(prog<0?al*(.3+d*1.2):al*.12)+")";g.beginPath();g.moveTo(x1,y1);g.lineTo(x2,y2);g.stroke();
  if(prog>=0){const f=Math.max(0,Math.min(1,prog*m-n));if(f>0){const ex=x1+(x2-x1)*f,ey=y1+(y2-y1)*f;g.strokeStyle=col+al*.14+")";g.lineWidth+=5;g.beginPath();g.moveTo(x1,y1);g.lineTo(ex,ey);g.stroke();g.strokeStyle=col+al+")";g.lineWidth-=5;g.beginPath();g.moveTo(x1,y1);g.lineTo(ex,ey);g.stroke()}}}
 g.fillStyle=`hsla(${h},90%,85%,${al*.85})`;for(const p of pts){g.beginPath();g.arc(cx+p[0]*R,cy+p[1]*R,1.4+p[2]*4,0,6.283);g.fill()}}
const K={hue:{idle:255,fetch:195,download:190,merge:320,convert:150,zip:40,done:150,error:350},sc:{idle:1,fetch:1.08,download:1,merge:.9,convert:1.05,zip:.6,done:1.15,error:.95},sp:{idle:1,fetch:4,download:2.5,merge:2,convert:3,zip:6,done:.8,error:.3},
 st:{paste:[.34,1],fetch:[.4,1.1],choose:[.17,.5],prog:[.5,1.1],done:[.34,.9]}};
const A=new Array(10).fill(0),base=[.31,.23,.41,.27,.17,.33,.21,.14,.26,.19];let hue=255,sc=1,sp=1,mx=0,my=0,tx=0,ty=0,cyk=.34,stk=1,T=0,last=0,parts=[],rings=[],mxp=-999,myp=-999,shown=-1;
const pnum=$("#pnum");
function boom(){for(let i=0;i<110;i++){const a=Math.random()*6.283,v=2+Math.random()*9;parts.push({x:W/2,y:H*cyk,vx:Math.cos(a)*v,vy:Math.sin(a)*v,l:1,h:hue+Math.random()*70-35})}rings.push({r:0,a:1},{r:-90,a:1})}
let MAG=[];function refreshMag(){MAG=$$(".mag").map(e=>{const st=e.closest(".step"),r=e.getBoundingClientRect(),m=e._m||(e._m={x:0,y:0});return{e,m,on:st.classList.contains("on"),cx:r.left+r.width/2-m.x,cy:r.top+r.height/2-m.y}})}
setInterval(refreshMag,400);
function frame(now){const dt=Math.min(.05,(now-last)/1000||.016);last=now;T+=dt;const ph=S.ph,q=k=>Math.min(1,dt*k);
 hue+=(K.hue[ph]-hue)*q(2.5);sc+=(K.sc[ph]-sc)*q(3);const tsp=K.sp[ph]+(ph==="download"?Math.min(S.speed/1.5e6,5):0);sp+=(tsp-sp)*q(2);
 tx+=(mx-tx)*q(4);ty+=(my-ty)*q(4);const[st,ss]=K.st[S.step];cyk+=(st-cyk)*q(3);stk+=(ss-stk)*q(3);
 for(let i=0;i<10;i++)A[i]+=base[i]*sp*dt;A[1]+=tx*.02;A[2]+=ty*.02;
 S.disp+=(S.target-S.disp)*q(5);
 g.globalCompositeOperation="source-over";g.fillStyle=RM?"rgba(0,0,0,.6)":"rgba(0,0,0,.26)";g.fillRect(0,0,W,H);
 for(const s of stars){s.y+=s.z*dt*(10+sp*14);if(s.y>H){s.y=0;s.x=Math.random()*W}g.fillStyle=`rgba(190,200,255,${.2+.45*Math.abs(Math.sin(T*s.z+s.p))*s.z})`;g.fillRect(s.x+tx*s.z*40,s.y+ty*s.z*40,s.z*1.8,s.z*1.8)}
 g.globalCompositeOperation="lighter";
 const cx=W/2,cy=H*cyk,R=Math.min(W,H)*.3*sc*stk*(1+Math.sin(T*3)*(ph==="download"?.025:.008)),P=ph==="done"?P5:P4,jit=ph==="error"?Math.sin(T*60)*6:0;
 const pr=ph==="download"?S.disp/100:(ph==="convert"||ph==="zip"||ph==="done"||ph==="merge")?1:-1;
 if(ph==="merge"){const pts=proj(P4,A),m=(T%2.4)/2.4,e=Math.min(1,m/.45),off=(1-e*e*(3-2*e))*R*1.5,fl=Math.max(0,1-Math.abs(m-.5)*5);drawT(P4,pts,cx-off,cy,R,330,1,.9);drawT(P4,pts,cx+off,cy,R,190,1,.9);
  if(fl>0){const gr=g.createRadialGradient(cx,cy,0,cx,cy,R*(.6+fl));gr.addColorStop(0,`rgba(255,255,255,${fl*.7})`);gr.addColorStop(1,"rgba(255,255,255,0)");g.fillStyle=gr;g.fillRect(0,0,W,H)}}
 else drawT(P,proj(P,A),cx+jit,cy,R,hue,pr,1);
 for(let i=parts.length-1;i>=0;i--){const p=parts[i];p.x+=p.vx;p.y+=p.vy;p.vx*=.975;p.vy=p.vy*.975+.12;p.l-=dt*.45;if(p.l<=0){parts.splice(i,1);continue}g.fillStyle=`hsla(${p.h},100%,65%,${p.l})`;g.beginPath();g.arc(p.x,p.y,2+p.l*3,0,6.283);g.fill()}
 for(let i=rings.length-1;i>=0;i--){const r=rings[i];r.r+=dt*900;r.a-=dt*.8;if(r.a<=0){rings.splice(i,1);continue}if(r.r<0)continue;g.strokeStyle=`hsla(${hue},100%,70%,${r.a})`;g.lineWidth=3;g.beginPath();g.arc(cx,cy,r.r,0,6.283);g.stroke()}
 const pc=Math.round(S.disp);if(pc!==shown){shown=pc;pnum.textContent=pc+"%";if(S.lock&&S.step==="prog")document.title=pc+"% · 4D"}
 for(const o of MAG){if(!o.on){if(o.m.x||o.m.y){o.m.x=o.m.y=0;o.e.style.transform=""}continue}const dx=mxp-o.cx,dy=myp-o.cy,tg=Math.hypot(dx,dy)<130?[dx*.3,dy*.3]:[0,0];o.m.x+=(tg[0]-o.m.x)*q(8);o.m.y+=(tg[1]-o.m.y)*q(8);o.e.style.transform=`translate3d(${o.m.x.toFixed(1)}px,${o.m.y.toFixed(1)}px,0)`}
 requestAnimationFrame(frame)}
addEventListener("pointermove",e=>{mxp=e.clientX;myp=e.clientY;mx=e.clientX/W-.5;my=e.clientY/H-.5},{passive:true});
rz();requestAnimationFrame(frame);
/* ---------- phases ---------- */
function phase(p){if(S.ph===p)return;S.ph=p;clearInterval(iv);timers.forEach(clearTimeout);timers.clear();if(LB[p]&&S.step==="prog"){const t=$("#pT");t.dataset.t=LB[p];split(t,LB[p])}
 if(p==="download")iv=setInterval(()=>tone(300+S.disp*6,.25,"sine",.025),700);
 else if(p==="merge"){const t=()=>{tone(200,.9,"sawtooth",.025,0,700);later(()=>chord([523,784]),1100)};t();iv=setInterval(t,2400)}
 else if(p==="convert")iv=setInterval(()=>tone(500+Math.random()*500,.25,"triangle",.035),300);
 else if(p==="zip")iv=setInterval(()=>tone(500,.4,"sine",.045,0,160),650);
 else if(p==="done"){boom();chord([523,659,784,1047,1319])}else if(p==="error")tone(220,.8,"sawtooth",.05,0,60)}
const phaseOf=j=>{const s=j.stage||"";return /Merg/i.test(s)?"merge":/Extract|Convert|Audio/i.test(s)?"convert":/ZIP/i.test(s)?"zip":"download"};
/* ---------- flow ---------- */
const logs=l=>{$("#log").textContent=(l||[]).slice(-80).map(x=>`[${x.time}] ${x.message}`).join("\n")};
const selIdx=()=>$$("#cards .cd").map((c,i)=>c.classList.contains("sel")?i:-1).filter(i=>i>=0);
const selTxt=()=>$("#cM").textContent=(S.probe.mode==="single"?"1 video":selIdx().length+" of "+S.probe.videos.length+" selected")+". Tap cards to toggle.";
function rng(ix){const n=ix.map(i=>i+1).sort((a,b)=>a-b),o=[];let s=n[0],p=n[0];for(let i=1;i<n.length;i++){if(n[i]===p+1){p=n[i];continue}o.push(s===p?""+s:s+"-"+p);s=p=n[i]}o.push(s===p?""+s:s+"-"+p);return o.join(",")}
function setMedia(v){S.media=v;$("#seg i").style.transform=v==="audio"?"translateX(100%)":"none";$$("#seg button").forEach(b=>b.classList.toggle("on",b.dataset.v===v));$("#chips").style.display=v==="audio"?"none":"";tone(v==="audio"?700:500,.25)}
$("#seg").onclick=e=>{const b=e.target.closest("[data-v]");if(b)setMedia(b.dataset.v)};
function showProbe(j){$("#cT").textContent=j.title;$("#cErr").textContent="";
 $("#cards").innerHTML=j.videos.map(v=>`<div class="cd up sel">${v.thumbnail?`<img loading="lazy" decoding="async" src="${esc(v.thumbnail)}" alt="" onerror="this.outerHTML='<div class=th></div>'">`:'<div class="th"></div>'}<b>${esc(v.title)}</b></div>`).join("");
 $$("#cards .cd").forEach(c=>c.onclick=()=>{c.classList.toggle("sel");selTxt();tone(c.classList.contains("sel")?740:420,.15)});selTxt();
 const rs=(j.resolutions||[]).filter(r=>r<=4320).sort((a,b)=>a-b);S.res=rs.includes(720)?720:0;
 $("#chips").innerHTML=[0,...rs].map(r=>`<span class="ch ${r===S.res?"on":""}" data-r="${r}">${r?r+"p":"Best"}</span>`).join("");setMedia("video");go("choose")}
$("#chips").onclick=e=>{const c=e.target.closest("[data-r]");if(!c)return;S.res=+c.dataset.r;$$("#chips .ch").forEach(x=>x.classList.toggle("on",x===c));tone(600,.15)};
function fail(msg,step,j){S.lock=false;phase("error");(step==="paste"?$("#fErr"):$("#cErr")).textContent=msg||"Something went wrong.";toast("Failed — see log");go(step);later(()=>phase("idle"),1600);if(j&&j.needs_cookies)setTimeout(()=>cookieHelp(j.cookie_state),450)}
async function poll(id,kind,tok){let miss=0;
 while(tok===S.tok){await sleep(kind==="probe"?400:600);if(tok!==S.tok)return;let j;
  try{j=await api("/api/job/"+id);miss=0}catch(e){if(++miss>8){fail("Lost connection to the server.",kind==="probe"?"paste":"choose");return}continue}
  logs(j.log);S.speed=j.speed||0;
  if(kind==="probe"){S.target=Math.max(S.target,j.progress||0);$("#fS").textContent=j.stage||j.status;
   if(j.status==="probe_done"){S.lock=false;S.target=0;phase("idle");S.probe=j.probe;showProbe(j.probe);if(j.cookie_bad){toast("Cookies expired");setTimeout(()=>cookieHelp("invalid"),600)}return}
   if(j.status==="error"){fail(j.error,"paste",j);return}phase("fetch")}
  else{const t=Math.max(1,j.total_items||1),d=Math.min(j.completed_items||0,t);
   if(j.status==="error"){fail(j.error,"choose",j);return}
   if(j.status==="done"){S.target=100;await sleep(800);if(tok!==S.tok)return;finish(j.links||[]);return}
   S.target=Math.max(S.target,Math.min(99,(d+(j.current_pct||0)/100)/t*100));
   $("#pS").textContent=`${fmtS(j.speed)}  ·  eta ${fmtE(j.eta)}  ·  ${d}/${t} files`;$("#pF").textContent=j.current_file||"";phase(phaseOf(j))}}}
function finish(l){S.lock=false;$("#links").innerHTML=l.length?l.map(x=>`<a href="${esc(x.url)}" download>${esc(x.name)} <small>${x.size_mb} MB${x.is_zip?" · zip":""}</small></a>`).join(""):'<p class="hint">Nothing came out. Check the log.</p>';
 phase("done");go("done");document.title="Ready · 4D";refreshLib()}
async function doFetch(){if(S.lock)return;const url=$("#url").value.trim();if(!/https?:\/\//.test(url)){$("#fErr").textContent="Paste a link first.";return}
 S.lock=true;const tok=++S.tok;$("#fErr").textContent="";S.disp=0;S.target=2;phase("fetch");go("fetch");
 try{const{job_id}=await api("/api/probe",{method:"POST",headers:JSONH,body:JSON.stringify({url,cookies_file:S.cookie})});poll(job_id,"probe",tok)}
 catch(e){fail(e.message,"paste")}}
const feed=t=>{if(!/https?:\/\//.test(t||""))return false;$("#url").value=t.trim();doFetch();return true};
$("#goF").onclick=doFetch;$("#url").addEventListener("keydown",e=>{if(e.key==="Enter")doFetch()});$("#url").addEventListener("paste",()=>setTimeout(()=>feed($("#url").value),60));
document.addEventListener("paste",e=>{const t=(e.clipboardData||window.clipboardData).getData("text");if(S.step==="paste"&&e.target.id!=="url"&&/https?:\/\//.test(t)){e.preventDefault();feed(t)}});
document.addEventListener("dragover",e=>e.preventDefault());
document.addEventListener("drop",e=>{e.preventDefault();const f=e.dataTransfer.files[0];if(f&&/\.txt$/i.test(f.name))return upCookie(f);if(S.step==="paste")feed(e.dataTransfer.getData("text/uri-list")||e.dataTransfer.getData("text/plain"))});
const reset=()=>{S.tok++;S.lock=false;$("#url").value="";S.probe=null;S.target=S.disp=0;$("#fErr").textContent="";phase("idle");document.title="4D · downloader";go("paste")};
$("#back").onclick=reset;$("#again").onclick=reset;
$("#goD").onclick=async()=>{if(!S.probe||S.lock)return;const p=S.probe,ix=selIdx();if(!ix.length){toast("Pick at least one");return}
 let urls,pi="";if(p.mode==="playlist"){urls=[p.urls[0]];pi=rng(ix)}else urls=ix.map(i=>p.videos[i].url).filter(Boolean);
 S.lock=true;const tok=++S.tok;S.disp=0;S.target=1;$("#cErr").textContent="";$("#pS").textContent="";$("#pF").textContent="";phase("download");go("prog");
 try{const{job_id}=await api("/api/download",{method:"POST",headers:JSONH,body:JSON.stringify({mode:p.mode,media_type:S.media,resolution:S.res,urls,playlist_items:pi,total_items:ix.length,title:p.title,cookies_file:S.cookie})});poll(job_id,"dl",tok)}
 catch(e){fail(e.message,"choose")}};
/* cookies + files */
async function upCookie(f){if(!f)return;const fd=new FormData();fd.append("file",f);try{const j=await api("/api/cookies",{method:"POST",body:fd});S.cookie=j.cookies_file;$("#cNote").textContent="";$("#fErr").textContent="";$("#cErr").textContent="";$("#cState").textContent="Cookies loaded. Try your link again.";toast("Cookies loaded");chord([600,900]);sheet(null)}catch(e){$("#cNote").textContent=e.message+" Export it again with the extension below.";$("#cState").textContent="";if(!$("#sh-C").classList.contains("on"))sheet("C");toast("Invalid cookies file")}}
function cookieHelp(state){$("#cNote").textContent=state==="invalid"?"YouTube rejected your cookies, or they have expired. Export a fresh cookies.txt and upload it.":"YouTube wants verification and no cookies are loaded. Upload a cookies.txt to continue.";$("#cState").textContent="";if(!$("#sh-C").classList.contains("on"))sheet("C")}
$("#cf").onchange=e=>upCookie(e.target.files[0]);
$("#lib").addEventListener("click",async e=>{const b=e.target.closest("button[data-p]");if(!b||!confirm("Delete "+b.dataset.p+"?"))return;try{await api("/api/files",{method:"DELETE",headers:JSONH,body:JSON.stringify({path:b.dataset.p})});refreshLib()}catch(er){toast(er.message)}});
async function refreshLib(){try{const f=(await api("/api/files")).files||[];$("#lib").innerHTML=f.length?f.map(x=>`<div class="li"><div class="n">${esc(x.name)}<br><small style="color:var(--m)">${x.size_mb} MB</small></div><a href="${esc(x.url)}" download>Save</a><button data-p="${esc(x.path)}">Delete</button></div>`).join(""):'<p class="hint">No files yet.</p>'}catch(e){}}
$$(".ltr").forEach(e=>split(e,e.dataset.t));$("#url").focus();
})();
</script></body></html>
"""


def main():
    port = int(os.environ.get("PORT") or 8000)
    cs_domain, cs_name = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN"), os.environ.get("CODESPACE_NAME")
    host = os.environ.get("HOST") or ("0.0.0.0" if (cs_domain and cs_name) else "127.0.0.1")
    server = http.server.ThreadingHTTPServer((host, port), Handler)
    if cs_domain and cs_name:
        print(f"\n  🌐 Open in browser:  https://{cs_name}-{port}.{cs_domain}\n")
    else:
        print(f"\n  🌐 Open in browser:  http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}\n")
    print("  (Ctrl+C to stop)\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
