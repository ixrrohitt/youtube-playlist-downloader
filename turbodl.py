"""
turbodl.py: multi-connection ranged downloader (the server-side twin of fastdl's browser "turbo" method).

ONE shared pool of keep-alive connections downloads many files at once:
  * every file is sliced into 2-8 MB pieces that are written in place (os.pwrite) into a pre-allocated file
  * fair-share between a few active files, so files finish one after another instead of all at the end
  * persistent connections per worker (no new TCP/TLS handshake + slow-start for every piece)
  * per-piece retry with backoff on a fresh connection; 401/403/404/410 => Expired (caller falls back)
  * redirects followed, bytes counted live, cancel-aware, never leaves half-written files behind
Standard library only.
"""
from __future__ import annotations

import http.client
import os
import re
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit

PIECE_MIN = 2 << 20
PIECE_MAX = 8 << 20
READ = 512 * 1024
_CR = re.compile(r"/(\d+)\s*$")
_REDIRECT = (301, 302, 303, 307, 308)
_EXPIRED = (401, 403, 404, 410)
_SSL = ssl.create_default_context()
_PWRITE = hasattr(os, "pwrite")
_DROP_HEADERS = {"range", "accept-encoding", "host", "content-length", "connection"}


class TurboError(Exception):
    pass


class Expired(TurboError):
    """The direct URL is no longer valid (or needs auth): ask the caller for a fresh one."""


class Cancelled(TurboError):
    pass


class Item:
    """One file to fetch: url -> dest (dest is created/pre-allocated by TurboPool.add)."""

    def __init__(self, url: str, dest: str, headers: dict | None = None, size_hint: int = 0,
                 piece_cap: int = 0, tag=None, label: str = ""):
        self.url = url
        self.dest = dest
        self.headers = {k: v for k, v in (headers or {}).items() if k.lower() not in _DROP_HEADERS}
        self.size_hint = size_hint
        self.piece_cap = piece_cap
        self.tag = tag
        self.label = label or os.path.basename(dest)
        self.size = self.piece = self.n = 0
        self.next = self.live = self.done_pieces = 0
        self.bytes = 0
        self.error: Exception | None = None
        self.settled = False
        self.fd: int | None = None
        self.pst = bytearray()   # per piece: 0 waiting, 1 downloading, 2 done (for live progress maps)
        self.pgot: list = []     # per piece: bytes received so far
        self._wlock = threading.Lock()

    def write(self, off: int, buf: bytes) -> None:
        if _PWRITE:
            mv = memoryview(buf)
            while len(mv):
                n = os.pwrite(self.fd, mv, off)
                mv = mv[n:]
                off += n
        else:  # Windows: no pwrite
            with self._wlock:
                os.lseek(self.fd, off, os.SEEK_SET)
                mv = memoryview(buf)
                while len(mv):
                    mv = mv[os.write(self.fd, mv):]


class _Conns:
    """Per-worker keep-alive connections, one per (scheme, host)."""

    def __init__(self, timeout: float = 30):
        self.timeout = timeout
        self.c: dict = {}

    def _conn(self, scheme: str, netloc: str):
        key = (scheme, netloc)
        c = self.c.get(key)
        if c is None:
            c = (http.client.HTTPSConnection(netloc, timeout=self.timeout, context=_SSL) if scheme == "https"
                 else http.client.HTTPConnection(netloc, timeout=self.timeout))
            self.c[key] = c
        return c

    def drop(self, key) -> None:
        c = self.c.pop(key, None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def close(self) -> None:
        for k in list(self.c):
            self.drop(k)

    def get(self, item: Item, start: int, end: int):
        """GET a byte range, following redirects (the final URL is remembered on the item)."""
        url = item.url
        for _ in range(6):
            sp = urlsplit(url)
            key = (sp.scheme, sp.netloc)
            path = (sp.path or "/") + ("?" + sp.query if sp.query else "")
            hdr = {**item.headers, "Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}
            r = None
            for attempt in (0, 1):  # a stale keep-alive socket gets one transparent retry
                try:
                    c = self._conn(*key)
                    c.request("GET", path, headers=hdr)
                    r = c.getresponse()
                    break
                except (http.client.HTTPException, OSError):
                    self.drop(key)
                    if attempt:
                        raise
            r._tb_key = key  # type: ignore[attr-defined]
            loc = r.getheader("Location")
            if r.status in _REDIRECT and loc:
                try:
                    r.read()
                except Exception:
                    self.drop(key)
                url = item.url = urljoin(url, loc)
                continue
            return r
        raise TurboError("too many redirects")


def _probe(item: Item) -> int:
    """Total size via a 1-byte range request; also proves the server really supports ranges."""
    cs = _Conns()
    try:
        r = cs.get(item, 0, 0)
        st = r.status
        if st in _EXPIRED:
            raise Expired(f"HTTP {st}")
        if st != 206:
            raise TurboError(f"no range support (HTTP {st})")
        r.read()
        m = _CR.search(r.getheader("Content-Range") or "")
        if not m or int(m.group(1)) <= 0:
            raise TurboError("server did not report the file size")
        return int(m.group(1))
    finally:
        cs.close()


class TurboPool:
    def __init__(self, workers: int = 8, max_active: int = 3, cancelled=None, on_bytes=None, on_done=None):
        self.workers = max(1, workers)
        self.max_active = max(1, max_active)
        self.cancelled = cancelled or (lambda: False)
        self.on_bytes = on_bytes
        self.on_done = on_done
        self.items: list[Item] = []
        self.cond = threading.Condition()
        self._plock = threading.Lock()
        self.closed = False
        self.threads: list[threading.Thread] = []
        self.in_flight = 0   # items not settled yet

    # ---- public API ----
    def start(self) -> None:
        for i in range(self.workers):
            t = threading.Thread(target=self._worker, name=f"turbo-{i}", daemon=True)
            t.start()
            self.threads.append(t)

    def add(self, item: Item) -> None:
        """Probe + pre-allocate (blocking, run it from the caller's thread), then queue for download."""
        if self.cancelled():
            raise Cancelled()
        total = _probe(item)
        cap = min(PIECE_MAX, item.piece_cap) if item.piece_cap else PIECE_MAX
        lo = min(PIECE_MIN, cap)
        piece = -(-total // (self.workers * 3))
        piece = max(lo, min(cap, -(-piece // 1048576) * 1048576))
        item.size, item.piece = total, piece
        item.n = -(-total // piece)
        item.pst = bytearray(item.n)
        item.pgot = [0] * item.n
        item.fd = os.open(item.dest, os.O_RDWR | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o644)
        try:
            os.ftruncate(item.fd, total)
        except OSError:
            pass  # file just grows as pieces arrive
        with self.cond:
            self.items.append(item)
            self.in_flight += 1
            self.cond.notify_all()

    def abort(self, item: Item, exc: Exception | None = None) -> None:
        with self.cond:
            if item.error is None:
                item.error = exc or Cancelled()
            fin = self._try_settle(item)
            self.cond.notify_all()
        if fin:
            self._settled(item)

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def join(self) -> None:
        for t in self.threads:
            t.join()
        with self.cond:
            rest = list(self.items)
        for it in rest:  # cancelled mid-way: finish them off cleanly
            self.abort(it, Cancelled())

    def busy(self) -> int:
        with self.cond:
            return self.in_flight

    # ---- internals ----
    def _count(self, it: Item, n: int) -> None:
        with self._plock:
            it.bytes += n
        if self.on_bytes:
            self.on_bytes(it, n)

    def _pick(self):
        best, seen = None, 0
        for it in self.items:
            if it.error is not None or it.next >= it.n:
                continue
            seen += 1
            if best is None or it.live < best.live:
                best = it
            if seen >= self.max_active:
                break
        return best

    def _try_settle(self, it: Item) -> bool:
        """Call with self.cond held. True exactly once, when the item has nothing left in flight."""
        if it.settled or it.live:
            return False
        if it.error is not None or it.done_pieces >= it.n:
            it.settled = True
            if it in self.items:
                self.items.remove(it)
            self.in_flight -= 1
            return True
        return False

    def _settled(self, it: Item) -> None:
        try:
            if it.fd is not None:
                os.close(it.fd)
        except OSError:
            pass
        it.fd = None
        if it.error is not None:
            try:
                os.unlink(it.dest)
            except OSError:
                pass
        if self.on_done:
            try:
                self.on_done(it)
            except Exception:
                pass

    def _worker(self) -> None:
        conns = _Conns()
        try:
            while True:
                with self.cond:
                    while True:
                        if self.cancelled():
                            return
                        it = self._pick()
                        if it is not None:
                            idx = it.next
                            it.next += 1
                            it.live += 1
                            break
                        if self.closed and not any(x.next < x.n and x.error is None for x in self.items):
                            return
                        self.cond.wait(0.5)
                err: Exception | None = None
                try:
                    self._piece(it, idx, conns)
                except Exception as e:  # noqa: BLE001
                    err = e
                with self.cond:
                    it.live -= 1
                    if err is not None:
                        if it.error is None:
                            it.error = err
                    else:
                        it.done_pieces += 1
                    fin = self._try_settle(it)
                    self.cond.notify_all()
                if fin:
                    self._settled(it)
        finally:
            conns.close()

    def _piece(self, it: Item, idx: int, conns: _Conns) -> None:
        start = idx * it.piece
        end = min(it.size, start + it.piece) - 1
        for attempt in range(6):
            if self.cancelled() or it.error is not None:
                raise Cancelled()
            got, r, ok = 0, None, False
            it.pst[idx], it.pgot[idx] = 1, 0
            try:
                r = conns.get(it, start, end)
                if r.status in _EXPIRED:
                    raise Expired(f"HTTP {r.status}")
                if r.status != 206:
                    raise TurboError(f"HTTP {r.status}")
                off = start
                while off <= end:
                    buf = r.read(min(READ, end - off + 1))
                    if not buf:
                        raise TurboError("connection closed early")
                    it.write(off, buf)
                    off += len(buf)
                    got += len(buf)
                    it.pgot[idx] = got
                    self._count(it, len(buf))
                    if self.cancelled() or it.error is not None:
                        raise Cancelled()
                if r.will_close:
                    conns.drop(r._tb_key)
                it.pst[idx] = 2
                ok = True
                return
            except (Cancelled, Expired):
                raise
            except Exception as e:  # noqa: BLE001
                if attempt == 5:
                    raise TurboError(f"piece {idx + 1}/{it.n}: {e}") from e
                time.sleep(min(0.5 * 2 ** attempt, 5))
            finally:
                if not ok:
                    it.pst[idx], it.pgot[idx] = 0, 0
                    if r is not None:
                        conns.drop(r._tb_key)
                    if got:
                        try:
                            self._count(it, -got)
                        except Exception:
                            pass
