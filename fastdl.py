"""
fastdl.py: fastest possible server -> user-device file delivery for webapp.py (stdlib only).

SERVER side (serve):
  * os.sendfile() zero-copy (kernel -> socket, no Python buffer); falls back to 1 MB read/send (Windows)
  * HTTP Range (206) + If-Range + ETag  -> resume, and parallel chunk downloads
  * Content-Length + Content-Disposition (UTF-8 names), TCP_NODELAY, HEAD support
  * path-traversal safe, BrokenPipe/ConnectionReset ignored quietly

CLIENT side (TURBO_JS, injected into the page by html_tag()):
  * Intercepts clicks on <a download href="/downloads/..."> links (many files at once is fine)
  * ONE shared pool of 6 connections serves every file: pieces of 2-16 MB (auto-sized), fair-share
    between files, up to 4 files in flight, the rest queued; per-file retry (3x) and fallback
  * Streams straight to disk via File System Access API (Chrome/Edge desktop); "SAVE ALL" asks for a
    folder once and writes every file into it; other browsers assemble Blobs (1.5 GB memory budget)
  * Animated, DRAGGABLE panel (position remembered, double-click header to reset): overall bar,
    speed graph, ETA, connections, and one row per file with step dots, piece map and cancel
"""
from __future__ import annotations

import errno
import mimetypes
import os
import re
import socket
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote, unquote

SEND_MAX = 8 << 20          # bytes per sendfile() call
BUF = 1 << 20               # fallback read buffer (1 MB)
_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")
_QUIET = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)
_NO_SENDFILE = {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV}


def _parse_range(value: str, size: int):
    """None = ignore header (serve full) | False = unsatisfiable (416) | (start, end)."""
    m = _RANGE.match(value.strip())
    if not m or (not m[1] and not m[2]):
        return None
    if m[1]:
        s = int(m[1])
        e = int(m[2]) if m[2] else size - 1
    else:  # suffix range: last N bytes
        s, e = max(0, size - int(m[2])), size - 1
    e = min(e, size - 1)
    return False if (s >= size or s > e) else (s, e)


def _send_body(h, f, off: int, left: int) -> None:
    sock = h.connection
    try:
        h.wfile.flush()
        if hasattr(os, "sendfile"):
            try:
                out, fd = sock.fileno(), f.fileno()
                while left > 0:
                    n = os.sendfile(out, fd, off, min(left, SEND_MAX))
                    if n == 0:
                        return
                    off += n
                    left -= n
                return
            except OSError as e:
                if e.errno not in _NO_SENDFILE:
                    return  # client went away / real error: just stop
        f.seek(off)
        buf = bytearray(min(BUF, left))
        mv = memoryview(buf)
        while left > 0:
            n = f.readinto(mv[:min(len(buf), left)])
            if not n:
                return
            sock.sendall(mv[:n])
            left -= n
    except _QUIET:
        pass


def serve(h, root: Path, rel: str, head: bool = False) -> None:
    """Serve root/rel through handler `h` (a BaseHTTPRequestHandler)."""
    try:
        root_r = root.resolve()
        target = (root_r / unquote(rel)).resolve()
        target.relative_to(root_r)
    except (ValueError, OSError):
        return h.send_error(400, "Invalid path")
    try:
        f = open(target, "rb")
    except OSError:
        return h.send_error(404)
    with f:
        try:
            st = os.fstat(f.fileno())
            if not os.path.isfile(target):
                return h.send_error(404)
            size = st.st_size
            etag = f'"{size:x}-{int(st.st_mtime):x}"'
            rng = h.headers.get("Range")
            ifr = h.headers.get("If-Range")
            if rng and ifr and ifr.strip() != etag:
                rng = None  # file changed -> send whole thing
            r = _parse_range(rng, size) if (rng and size) else None
            if r is False:
                h.send_response(416)
                h.send_header("Content-Range", f"bytes */{size}")
                h.send_header("Content-Length", "0")
                return h.end_headers()
            start, end = r if r else (0, size - 1)
            length = end - start + 1 if size else 0

            name = target.name
            ascii_name = re.sub(r'[^\x20-\x7e]|["\\]', "_", name)
            h.send_response(206 if r else 200)
            h.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
            h.send_header("Content-Length", str(length))
            if r:
                h.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            h.send_header("Accept-Ranges", "bytes")
            h.send_header("Content-Disposition",
                          f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}")
            h.send_header("ETag", etag)
            h.send_header("Last-Modified", formatdate(st.st_mtime, usegmt=True))
            h.send_header("Cache-Control", "public, max-age=3600")
            h.send_header("X-Accel-Buffering", "no")  # nginx: don't buffer the stream
            h.end_headers()
            if head or length == 0:
                return
            try:
                h.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            _send_body(h, f, start, length)
        except _QUIET:
            pass


TURBO_JS = r"""
(()=>{"use strict";
const PAR=6,MINSZ=16<<20,MAXMEM=1.5*1024**3,MAXACT=4,MAXCELLS=120,MAXWQ=6;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const $=id=>document.getElementById(id);
const fb=b=>b>=1073741824?(b/1073741824).toFixed(2)+" GB":b>=1048576?(b/1048576).toFixed(b>=1e8?0:1)+" MB":Math.max(0,b/1024|0)+" KB";
const fsp=b=>b>=1048576?(b/1048576).toFixed(1)+" MB/s":(Math.max(0,b)/1024|0)+" KB/s";
const ft=s=>!isFinite(s)||s<=0?"\u2014":s<60?Math.ceil(s)+"s":s<3600?(s/60|0)+"m "+(Math.ceil(s)%60)+"s":(s/3600|0)+"h "+((s/60|0)%60)+"m";
const clean=n=>String(n||"").replace(/[\\\/:*?"<>|\x00-\x1f]+/g,"_").trim()||"download";
const M={tasks:[],conn:0,mem:0,seq:0,hist:[],sp:0,lb:0,ls:0,pc:0,ticking:false,hideT:0,armT:0,armed:false,
 pk:Promise.resolve(),dk:Promise.resolve(),nat:Promise.resolve(),oldTitle:"",warn:false,lastMsg:"",lastClamp:0};
const bu=e=>{e.preventDefault();e.returnValue=""};

/* ================= panel: styles + markup ================= */
const css=`
#tb-p,#tb-p *{box-sizing:border-box}
#tb-p{position:fixed;left:calc(50% - min(280px,47vw));bottom:calc(18px + env(safe-area-inset-bottom,0px));width:min(560px,94vw);z-index:60;transform:translate3d(0,160%,0);opacity:0;pointer-events:none;transition:transform .8s var(--e,cubic-bezier(.16,1,.3,1)),opacity .5s;padding:14px 16px 14px;border-radius:26px;background:var(--sf,#0b0b10);box-shadow:8px 8px 22px rgba(0,0,0,.95),-6px -6px 18px rgba(255,255,255,.055),0 0 0 1px rgba(124,92,255,.25);color:var(--tx,#eceeff);font:500 13px ui-sans-serif,system-ui,sans-serif;overflow:hidden}
#tb-p.on{transform:none;opacity:1;pointer-events:auto}
#tb-p.drag{transition:opacity .5s;box-shadow:14px 18px 40px rgba(0,0,0,.95),-6px -6px 18px rgba(255,255,255,.055),0 0 0 1.5px rgba(0,229,255,.55)}
#tb-p .glow{position:absolute;left:0;right:0;top:0;height:2px;background:linear-gradient(90deg,transparent,#7c5cff,#00e5ff,#ff3d9a,transparent);background-size:200% 100%;animation:tbg 2.2s linear infinite}
@keyframes tbg{to{background-position:-200% 0}}
#tb-p[data-s=done] .glow{background:linear-gradient(90deg,transparent,#2bff9a,transparent);animation:none}
#tb-p[data-s=err] .glow,#tb-p[data-s=cancel] .glow{background:linear-gradient(90deg,transparent,#ffb020,transparent);animation:none}
#tb-p .tbh{display:flex;align-items:center;gap:10px;cursor:grab;user-select:none;-webkit-user-select:none;touch-action:none;margin:-4px -4px 0;padding:4px}
#tb-p.drag .tbh{cursor:grabbing}
#tb-p .grip{color:#3b3d63;font:900 15px/1 ui-sans-serif;letter-spacing:-.15em;flex:0 0 auto;writing-mode:horizontal-tb}
#tb-p .ic{display:flex;gap:3px;align-items:flex-end;height:24px;width:26px;flex:0 0 26px}
#tb-p .ic i{flex:1;border-radius:2px;background:linear-gradient(180deg,#00e5ff,#7c5cff);height:30%;animation:tbe 1s ease-in-out infinite}
#tb-p .ic i:nth-child(2){animation-delay:.18s}#tb-p .ic i:nth-child(3){animation-delay:.36s}
@keyframes tbe{50%{height:100%}}
#tb-p .ic svg{display:none;width:26px;height:26px;stroke:#2bff9a;fill:none;stroke-width:3;stroke-linecap:round;stroke-linejoin:round;stroke-dasharray:30;stroke-dashoffset:30}
#tb-p[data-s=done] .ic i{display:none}
#tb-p[data-s=done] .ic svg{display:block;animation:tbd .6s .1s forwards}
@keyframes tbd{to{stroke-dashoffset:0}}
#tb-p[data-s=err] .ic i,#tb-p[data-s=cancel] .ic i{animation:none;height:50%;background:#ffb020}
#tb-p .tbn{flex:1;min-width:0;display:flex;flex-direction:column;line-height:1.3}
#tb-p .tbn b{font:700 13.5px ui-sans-serif,system-ui;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#tb-p .tbn small{color:var(--mu,#6b6d92);font:500 11.5px ui-sans-serif,system-ui;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#tb-p .tbn small.in{animation:tbm .5s var(--e,ease) both}
@keyframes tbm{from{opacity:0;transform:translate3d(0,6px,0)}}
#tb-p .tbpc{font:900 28px/1 ui-sans-serif,system-ui;letter-spacing:-.04em;font-variant-numeric:tabular-nums;background:linear-gradient(120deg,#7c5cff,#00e5ff);-webkit-background-clip:text;background-clip:text;color:transparent}
#tb-p .tbpc span{font-size:13px;margin-left:1px}
#tb-p .hb{width:30px;height:30px;flex:0 0 30px;border-radius:50%;border:0;background:var(--sf,#0b0b10);box-shadow:4px 4px 10px rgba(0,0,0,.9),-3px -3px 8px rgba(255,255,255,.05);color:var(--mu,#6b6d92);cursor:pointer;font:700 13px ui-sans-serif}
#tb-p .hb:hover{color:var(--tx,#eceeff)}#tb-p .hb:active{box-shadow:inset 3px 3px 7px #000,inset -2px -2px 6px rgba(255,255,255,.05)}
#tb-p .hb.arm{background:#3a0f1c;color:#fff}
#tb-p .tbbar{height:12px;border-radius:99px;background:#050508;box-shadow:inset 3px 3px 7px #000,inset -2px -2px 6px rgba(255,255,255,.05);overflow:hidden;margin:12px 0 2px}
#tb-p .tbfill{height:100%;width:0;border-radius:99px;background:linear-gradient(90deg,#7c5cff,#00e5ff,#7c5cff);background-size:200% 100%;animation:tbf 1.6s linear infinite;position:relative}
@keyframes tbf{to{background-position:-200% 0}}
#tb-p .tbfill::after{content:"";position:absolute;top:0;bottom:0;right:0;width:42px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.6));filter:blur(2px)}
#tb-p[data-s=done] .tbfill{background:linear-gradient(90deg,#16c784,#2bff9a);animation:none}
#tb-p[data-s=done] .tbfill::after{display:none}
#tb-p[data-s=err] .tbfill,#tb-p[data-s=cancel] .tbfill{background:#ffb020;animation:none}
#tb-p .spark{width:100%;height:34px;display:block;margin:8px 0 6px}
#tb-p .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;text-align:center}
#tb-p .stats small{display:block;color:var(--mu,#6b6d92);font:600 9.5px ui-sans-serif,system-ui;letter-spacing:.12em;text-transform:uppercase;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#tb-p .stats b{font:800 13px ui-sans-serif,system-ui;font-variant-numeric:tabular-nums}
#tb-p .tbl{display:flex;flex-direction:column;gap:8px;max-height:min(300px,34vh);overflow-y:auto;margin-top:12px;padding-right:2px;scrollbar-width:thin;scrollbar-color:#2c2e52 transparent}
#tb-p .tbl:empty{display:none}
#tb-p .tr{flex-shrink:0;padding:9px 12px 10px;border-radius:16px;background:#08080c;box-shadow:inset 3px 3px 8px #000,inset -2px -2px 6px rgba(255,255,255,.04);animation:tbin .6s var(--e,ease) backwards}
@keyframes tbin{from{opacity:0;transform:translate3d(0,14px,0) scale(.97)}}
#tb-p .tr.dup{animation:tbdup .7s}
@keyframes tbdup{30%{box-shadow:inset 3px 3px 8px #000,0 0 0 2px rgba(0,229,255,.8)}}
#tb-p .r1{display:flex;align-items:center;gap:8px}
#tb-p .rn{flex:1;min-width:0;font:700 12.5px ui-sans-serif,system-ui;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#tb-p .rp{font:900 13px ui-sans-serif,system-ui;font-variant-numeric:tabular-nums;color:#7fe3ff}
#tb-p .rx{width:22px;height:22px;border-radius:50%;border:0;background:none;color:var(--mu,#6b6d92);cursor:pointer;font:700 11px ui-sans-serif}
#tb-p .rx:hover{color:#ff6b8a}
#tb-p .r2{color:var(--mu,#6b6d92);font:500 11px ui-sans-serif,system-ui;margin:2px 0 7px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#tb-p .r3{display:flex;align-items:center;gap:10px}
#tb-p .dts{display:flex;gap:4px;flex:0 0 auto}
#tb-p .dts i{width:7px;height:7px;border-radius:50%;background:rgba(255,255,255,.12);transition:background .3s}
#tb-p .dts i.ok{background:#16c784}
#tb-p .dts i.cur{background:#00e5ff;animation:tbp 1.2s ease-in-out infinite}
@keyframes tbp{0%{box-shadow:0 0 0 0 rgba(0,229,255,.6)}100%{box-shadow:0 0 0 5px rgba(0,229,255,0)}}
#tb-p .rc{flex:1;min-width:0;height:10px;display:block}
#tb-p .tr[data-s=done] .rp{color:#2bff9a}
#tb-p .tr[data-s=err] .rp,#tb-p .tr[data-s=cancel] .rp{color:#ffb020}
#tb-p.min .spark,#tb-p.min .stats,#tb-p.min .tbl{display:none}
.tb-all{display:block;margin:0 auto 14px;padding:12px 24px;border:0;border-radius:99px;cursor:pointer;color:#fff;font:800 12px ui-sans-serif,system-ui;letter-spacing:.14em;background:linear-gradient(135deg,#7c5cff,#ff3d9a);box-shadow:0 12px 30px -12px rgba(255,61,154,.8),5px 5px 14px rgba(0,0,0,.9),-3px -3px 10px rgba(255,255,255,.05)}
.tb-all:active{filter:brightness(.88)}
@media(prefers-reduced-motion:reduce){#tb-p *{animation:none!important}}
`;
const stl=document.createElement("style");stl.textContent=css;document.head.appendChild(stl);
const P=document.createElement("div");P.id="tb-p";P.dataset.s="run";
P.innerHTML=`<i class="glow"></i>
<div class="tbh" id="tb-h" title="drag to move \u00b7 double-click to reset"><span class="grip">\u22ee\u22ee</span>
<span class="ic"><i></i><i></i><i></i><svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg></span>
<div class="tbn"><b id="tb-n">Turbo downloads</b><small id="tb-m"></small></div>
<div class="tbpc"><b id="tb-pc">0</b><span>%</span></div>
<button class="hb" id="tb-mn" title="minimize">\u2013</button><button class="hb" id="tb-x" title="cancel all / close">\u2715</button></div>
<div class="tbbar"><div class="tbfill" id="tb-f"></div></div>
<canvas class="spark" id="tb-sp"></canvas>
<div class="stats"><div><small>speed</small><b id="tb-sv">\u2014</b></div><div><small>time left</small><b id="tb-et">\u2014</b></div><div><small id="tb-dnl">received</small><b id="tb-dn">\u2014</b></div><div><small>connections</small><b id="tb-cn">\u2014</b></div></div>
<div class="tbl" id="tb-l"></div>`;
document.body.appendChild(P);
const L=$("tb-l");

/* ---------- drag (mouse, touch, pen) ---------- */
const POSK="tb_pos";let dg=null;
function place(x,y){
 const w=P.offsetWidth,h=P.offsetHeight;
 x=Math.max(0,Math.min(innerWidth-w,x));y=Math.max(0,Math.min(innerHeight-h,y));
 P.style.left=x+"px";P.style.top=y+"px";P.style.bottom="auto";P.dataset.moved="1";return[x,y]}
function clampPos(){if(P.dataset.moved!=="1")return;const r=P.getBoundingClientRect();place(r.left,r.top)}
function savePos(){try{const r=P.getBoundingClientRect();localStorage.setItem(POSK,JSON.stringify([Math.round(r.left),Math.round(r.top)]))}catch(_){}}
function restorePos(){try{const v=JSON.parse(localStorage.getItem(POSK)||"null");if(v&&isFinite(v[0])&&isFinite(v[1]))place(v[0],v[1])}catch(_){}}
function resetPos(){P.style.left=P.style.top=P.style.bottom="";delete P.dataset.moved;try{localStorage.removeItem(POSK)}catch(_){}}
const HD=$("tb-h");
HD.addEventListener("pointerdown",e=>{
 if(e.button||e.target.closest("button"))return;
 const r=P.getBoundingClientRect();dg={dx:e.clientX-r.left,dy:e.clientY-r.top,id:e.pointerId};
 try{HD.setPointerCapture(e.pointerId)}catch(_){}
 P.classList.add("drag");e.preventDefault()});
HD.addEventListener("pointermove",e=>{if(dg&&e.pointerId===dg.id)place(e.clientX-dg.dx,e.clientY-dg.dy)});
const dEnd=e=>{if(!dg)return;dg=null;P.classList.remove("drag");savePos()};
HD.addEventListener("pointerup",dEnd);HD.addEventListener("pointercancel",dEnd);
HD.addEventListener("dblclick",e=>{if(!e.target.closest("button"))resetPos()});
addEventListener("resize",clampPos);

/* ---------- panel helpers ---------- */
function msg(t){if(t===M.lastMsg)return;M.lastMsg=t;const m=$("tb-m");m.classList.remove("in");void m.offsetWidth;m.textContent=t;m.classList.add("in")}
function drawSpark(h){
 const c=$("tb-sp"),w=c.clientWidth,hh=34;if(!w)return;
 const dpr=Math.min(devicePixelRatio||1,2),cw=Math.round(w*dpr);
 if(c.width!==cw){c.width=cw;c.height=Math.round(hh*dpr)}
 const g=c.getContext("2d");g.setTransform(dpr,0,0,dpr,0,0);g.clearRect(0,0,w,hh);
 if(h.length<2)return;
 const mx=Math.max(...h,1)*1.15,st=w/79,x0=w-(h.length-1)*st;
 g.beginPath();
 h.forEach((v,i)=>{const x=x0+i*st,y=hh-2-(v/mx)*(hh-6);i?g.lineTo(x,y):g.moveTo(x,y)});
 g.strokeStyle="#00e5ff";g.lineWidth=1.6;g.lineJoin="round";g.stroke();
 g.lineTo(w,hh);g.lineTo(x0,hh);g.closePath();
 const gr=g.createLinearGradient(0,0,0,hh);gr.addColorStop(0,"rgba(124,92,255,.45)");gr.addColorStop(1,"rgba(124,92,255,0)");
 g.fillStyle=gr;g.fill()}
function openPanel(){
 clearTimeout(M.hideT);
 if(!P.classList.contains("on")){
  P.dataset.s="run";M.hist=[];M.sp=0;M.pc=0;M.lastMsg="";
  $("tb-pc").textContent="0";$("tb-f").style.width="0%";
  if(P.dataset.moved!=="1")restorePos();
  void P.offsetWidth;P.classList.add("on")}
 else P.dataset.s="run";
 if(!M.warn){M.warn=true;M.oldTitle=document.title;addEventListener("beforeunload",bu)}}
function closePanel(){
 clearTimeout(M.hideT);P.classList.remove("on");
 setTimeout(()=>{if(!M.tasks.some(t=>!t.fin)){M.tasks=[];L.innerHTML=""}},900)}
$("tb-mn").onclick=()=>P.classList.toggle("min");
const xb=$("tb-x");
function disarm(){M.armed=false;clearTimeout(M.armT);xb.classList.remove("arm");xb.textContent="\u2715"}
xb.onclick=()=>{
 if(!M.tasks.some(t=>!t.fin)){disarm();closePanel();return}
 if(!M.armed){M.armed=true;xb.classList.add("arm");xb.textContent="?";msg("Tap \u2715 again to cancel everything");M.armT=setTimeout(disarm,2600);return}
 disarm();for(const t of M.tasks.slice())if(!t.fin)cancelTask(t,"Cancelled")};

/* ================= rows ================= */
function mkRow(t){
 const d=document.createElement("div");d.className="tr";d.dataset.s=t.state;
 d.innerHTML=`<div class="r1"><span class="rn"></span><b class="rp">0%</b><button class="rx" title="cancel / remove">\u2715</button></div><div class="r2"></div><div class="r3"><span class="dts" title="check \u2192 destination \u2192 download \u2192 save"><i></i><i></i><i></i><i></i></span><canvas class="rc"></canvas></div>`;
 const e={root:d,rn:d.querySelector(".rn"),rp:d.querySelector(".rp"),r2:d.querySelector(".r2"),dots:[...d.querySelectorAll(".dts i")],cv:d.querySelector(".rc"),x:d.querySelector(".rx"),s:"",tx:"",pp:"",dk:-9};
 e.rn.textContent=t.name;e.rn.title=t.name;
 e.x.onclick=()=>{if(!t.fin)cancelTask(t,"Cancelled");else{d.remove();M.tasks=M.tasks.filter(z=>z!==t);if(!M.tasks.length)closePanel()}};
 L.appendChild(d);t.el=e;return e}
function dotIdx(t){return t.state==="probe"||t.state==="queued"?-1:t.state==="dest"?1:t.state==="run"?2:t.state==="save"?3:t.state==="done"?4:-1}
function rowText(t,now){
 switch(t.state){
  case "queued":return t.wait?"Queued \u00b7 waiting for memory":"Queued \u00b7 "+fb(t.size)+" \u00b7 waiting for a free slot";
  case "dest":return t.dir?"Creating the file in your folder\u2026":(window.showSaveFilePicker?"Choose where to save\u2026":"Getting ready\u2026");
  case "run":return now<t.noteT?t.note:fb(t.bytes)+" / "+fb(t.size)+" \u00b7 "+fsp(t.sp)+" \u00b7 "+t.live+" conn \u00b7 "+(t.w?"to disk":"in memory");
  case "save":return t.w?"Finishing the file on your disk\u2026":"Assembling the final file\u2026";
  case "done":return "Saved \u00b7 "+fb(t.size)+" in "+ft(t.dur)+" \u00b7 avg "+fsp(t.size/Math.max(t.dur,.001));
  default:return t.note||(t.state==="err"?"Problem \u00b7 switching to normal download":"Cancelled")}}
function drawMap(t,now){
 const cv=t.el.cv,w=cv.clientWidth,h=10;if(!w)return;
 const dpr=Math.min(devicePixelRatio||1,2),cw=Math.round(w*dpr);
 if(cv.width!==cw){cv.width=cw;cv.height=Math.round(h*dpr)}
 const g=cv.getContext("2d");g.setTransform(dpr,0,0,dpr,0,0);g.clearRect(0,0,w,h);
 const cn=t.cn,gap=cn>60?1:2,cwid=Math.max(1,(w-gap*(cn-1))/cn),pulse=.5+.5*Math.sin(now/120);
 const done=t.state==="done";
 for(let c=0;c<cn;c++){
  const lo=c*t.g,hi=Math.min(t.n,lo+t.g);let l=0,tt=0,a=false;
  for(let i=lo;i<hi;i++){l+=t.cl[i];tt+=t.len(i);if(t.st[i]===1)a=true}
  const x=c*(cwid+gap),f=done?1:(tt?l/tt:0);
  g.fillStyle="rgba(255,255,255,.07)";g.fillRect(x,0,cwid,h);
  if(f>0){g.fillStyle=done?"#16c784":"hsl("+(258-(c/cn)*70)+",95%,62%)";g.fillRect(x,0,cwid*f,h)}
  if(a&&!done){g.strokeStyle="rgba(0,229,255,"+(.35+.5*pulse)+")";g.lineWidth=1;g.strokeRect(x+.5,.5,Math.max(1,cwid-1),h-1)}}}
function renderRow(t,now){
 const e=t.el;if(!e)return;
 if(e.s!==t.state){e.s=t.state;e.root.dataset.s=t.state}
 const k=dotIdx(t);if(e.dk!==k){e.dk=k;e.dots.forEach((d,i)=>{d.className=i<k?"ok":i===k?"cur":""})}
 const tx=rowText(t,now);if(tx!==e.tx){e.tx=tx;e.r2.textContent=tx}
 t.pc+=((t.size?t.bytes/t.size*100:0)-t.pc)*.25;
 const p=t.state==="done"?100:Math.floor(Math.min(99.9,t.pc)),ps=p+"%";if(ps!==e.pp){e.pp=ps;e.rp.textContent=ps}
 if(t.state!=="cancel")drawMap(t,now)}

/* ================= overall tick ================= */
function tick(now){
 const T=M.tasks;let tot=0,all=0,left=0,nr=0,nq=0,nd=0,ne=0;
 for(const t of T){
  if(t.state==="cancel")continue;
  all+=t.size;tot+=t.state==="done"?t.size:t.bytes;
  if(!t.fin)left+=t.size-t.bytes;
  if(t.state==="dest"||t.state==="run"||t.state==="save")nr++;else if(t.state==="queued")nq++;else if(t.state==="done")nd++;else if(t.state==="err")ne++}
 if(now-M.ls>=250){
  const dt=(now-M.ls)/1000,inst=Math.max(0,(tot-M.lb)/dt);
  M.sp=M.sp?M.sp*.7+inst*.3:inst;M.lb=tot;M.ls=now;M.hist.push(M.sp);if(M.hist.length>80)M.hist.shift();
  for(const t of T)if(!t.fin){const ti=Math.max(0,(t.bytes-t.lb)/dt);t.sp=t.sp?t.sp*.7+ti*.3:ti;t.lb=t.bytes}
  drawSpark(M.hist);
  $("tb-sv").textContent=fsp(M.sp);
  $("tb-et").textContent=M.sp>1024&&left>0?ft(left/M.sp):"\u2014";
  $("tb-dn").textContent=fb(tot);$("tb-dnl").textContent="of "+fb(all)}
 const live=T.some(t=>!t.fin);
 const target=all?tot/all*100:0;M.pc+=(target-M.pc)*.2;
 const p=live?Math.min(M.pc,99.9):(nd?100:Math.min(M.pc,100));
 $("tb-f").style.width=p.toFixed(2)+"%";$("tb-pc").textContent=Math.floor(p);
 $("tb-cn").textContent=M.conn+"/"+PAR;
 if(live){
  document.title=Math.floor(p)+"% \u00b7 saving";
  const parts=[];if(nr)parts.push(nr+" downloading");if(nq)parts.push(nq+" queued");if(nd)parts.push(nd+" done");
  if(!M.armed)msg((T.length>1?T.length+" files \u00b7 ":"")+parts.join(" \u00b7 "))}
 for(const t of T)renderRow(t,now);
 if(now-M.lastClamp>400){M.lastClamp=now;clampPos()}
 if(live)requestAnimationFrame(tick);else M.ticking=false}
function ensureTick(){if(M.ticking)return;M.ticking=true;M.ls=performance.now();M.lb=0;for(const t of M.tasks)M.lb+=t.state==="done"?t.size:t.bytes;requestAnimationFrame(tick)}
function checkIdle(){
 if(M.tasks.some(t=>!t.fin))return;
 if(M.warn){M.warn=false;removeEventListener("beforeunload",bu);document.title=M.oldTitle}
 disarm();
 const nd=M.tasks.filter(t=>t.state==="done").length,ne=M.tasks.filter(t=>t.state==="err").length;
 P.dataset.s=ne?"err":nd?"done":"cancel";
 const total=M.tasks.filter(t=>t.state==="done").reduce((a,t)=>a+t.size,0);
 msg(nd?(nd>1?nd+" files saved":"Saved")+" \u00b7 "+fb(total)+(ne?" \u00b7 "+ne+" via normal download":""):ne?"Switched to normal download":"Nothing saved");
 requestAnimationFrame(now=>{ if(!M.ticking)tick(now) });
 clearTimeout(M.hideT);M.hideT=setTimeout(closePanel,7000)}

/* ================= transfer engine (shared connection pool) ================= */
function native(url,name){const a=document.createElement("a");a._native=1;a.href=url;a.download=name||"";document.body.appendChild(a);a.click();a.remove()}
function nat(url,name){M.nat=M.nat.then(()=>{native(url,name);return sleep(400)})}
async function probe(url){
 const c=new AbortController();
 try{
  const r=await fetch(url,{headers:{Range:"bytes=0-0"},signal:c.signal,cache:"no-store"});
  if(r.status!==206)return 0;
  const m=/\/(\d+)\s*$/.exec(r.headers.get("Content-Range")||"");return m?+m[1]:0}
 catch(e){return 0}finally{c.abort()}}
function note(t,s){t.note=s;t.noteT=performance.now()+2800}
function releaseMem(t){if(t.memHeld){M.mem-=t.memHeld;t.memHeld=0}}
function killWriter(t,drop){
 const w=t.w;t.w=null;t.parts=null;
 if(!w)return;
 let p;try{p=Promise.resolve(w.abort())}catch(_){p=Promise.resolve()}
 p.catch(()=>{}).then(()=>{if(drop&&t.dir&&t.created)return t.dir.removeEntry(t.name)}).catch(()=>{})}
function endTask(t,state,text){
 if(t.fin)return;
 t.fin=true;t.state=state;if(text)t.note=text;t.ctl.abort();killWriter(t,true);releaseMem(t);
 activate();checkIdle();pump()}
function cancelTask(t,text){t.cancelled=true;endTask(t,"cancel",text)}
function failTask(t,text){if(t.fin)return;t.failed=true;endTask(t,"err",text||"Problem \u00b7 switching to normal download");nat(t.url,t.name)}
async function uniq(dir,name){
 const m=/^(.*?)(\.[^.]*)?$/.exec(name),b=m[1],x=m[2]||"";
 for(let k=0;k<500;k++){
  const nm=k?b+" ("+k+")"+x:name;
  try{await dir.getFileHandle(nm)}catch(e){if(e&&e.name==="NotFoundError")return nm;if(e&&e.name==="TypeMismatchError")continue;throw e}}
 return name}
function dirFile(t){
 const run=async()=>{const nm=await uniq(t.dir,t.name),h=await t.dir.getFileHandle(nm,{create:true});t.name=nm;t.created=true;if(t.el){t.el.rn.textContent=nm;t.el.rn.title=nm}return h.createWritable()};
 const p=M.dk.then(run,run);M.dk=p.catch(()=>{});return p}
function pickFile(t){
 const run=()=>{if(t.fin){const e=new Error("cancelled");e.name="AbortError";throw e}return showSaveFilePicker({suggestedName:t.name})};
 const p=M.pk.then(run,run);M.pk=p.catch(()=>{});return p}
async function begin(t){
 t.state="dest";
 try{
  if(t.dir)t.w=await dirFile(t);
  else if(window.showSaveFilePicker){const h=await pickFile(t);if(t.fin)return;t.w=await h.createWritable()}}
 catch(e){
  if(t.fin)return;
  if(e&&e.name==="AbortError"&&!t.dir){cancelTask(t,"Save cancelled");return}
  t.w=null}
 if(t.fin){killWriter(t);return}
 if(!t.w){
  if(!t.memHeld){
   if(t.size>MAXMEM||(M.mem>0&&M.mem+t.size>MAXMEM)){failTask(t,"Too big for this browser \u00b7 normal download");return}
   M.mem+=t.size;t.memHeld=t.size}
  t.parts=new Array(t.n)}
 t.state="run";t.t0=performance.now();pump()}
function activate(){
 let act=M.tasks.filter(t=>t.state==="dest"||t.state==="run"||t.state==="save").length;
 for(const t of M.tasks){
  if(act>=MAXACT)break;
  if(t.state!=="queued")continue;
  const needMem=!(t.dir||window.showSaveFilePicker);
  if(needMem){
   if(t.size>MAXMEM){failTask(t,"Too big for this browser \u00b7 normal download");continue}
   if(M.mem>0&&M.mem+t.size>MAXMEM){t.wait=true;continue}
   M.mem+=t.size;t.memHeld=t.size}
  t.wait=false;act++;begin(t)}}
function pump(){
 while(M.conn<PAR){
  let best=null;
  for(const t of M.tasks){
   if(t.state!=="run"||t.fin||t.next>=t.n||t.wq>=MAXWQ)continue;
   if(!best||t.live<best.live)best=t}
  if(!best)break;
  runChunk(best,best.next++)}}
async function getChunk(t,i){
 const s=i*t.CH,len=t.len(i),e=s+len-1;
 for(let k=0;;k++){
  let got=0;t.cl[i]=0;t.st[i]=1;
  try{
   const res=await fetch(t.url,{headers:{Range:`bytes=${s}-${e}`},signal:t.ctl.signal,cache:"no-store"});
   if(res.status!==206)throw new Error("HTTP "+res.status);
   const buf=new Uint8Array(len);
   if(res.body&&res.body.getReader){
    const rd=res.body.getReader();
    for(;;){
     const {done,value}=await rd.read();if(done)break;
     if(got+value.length>len)throw new Error("overflow");
     buf.set(value,got);got+=value.length;t.cl[i]=got;t.bytes+=value.length}}
   else{
    const ab=new Uint8Array(await res.arrayBuffer());
    if(ab.length!==len)throw new Error("short read");
    buf.set(ab);got=len;t.cl[i]=len;t.bytes+=len}
   if(got!==len)throw new Error("short read");
   t.st[i]=2;return buf}
  catch(x){
   t.bytes-=got;t.cl[i]=0;t.st[i]=0;
   if(t.ctl.signal.aborted||k>=3)throw x;
   note(t,"Piece "+(i+1)+" hiccup \u00b7 retrying ("+(k+1)+"/3)\u2026");
   await sleep(500*(k+1))}}}
async function runChunk(t,i){
 M.conn++;t.live++;let buf;
 try{buf=await getChunk(t,i)}
 catch(e){M.conn--;t.live--;if(!t.fin)failTask(t);pump();return}
 M.conn--;t.live--;t.wq++;pump();
 try{if(t.w)await t.w.write({type:"write",position:i*t.CH,data:buf});else if(t.parts)t.parts[i]=buf}
 catch(e){t.wq--;if(!t.fin)failTask(t);return}
 t.wq--;t.doneN++;
 if(t.doneN===t.n&&!t.fin)finalize(t);else pump()}
async function finalize(t){
 t.state="save";t.bytes=t.size;
 await sleep(450);if(t.fin)return;
 try{
  if(t.w){const w=t.w;await w.close();t.w=null}
  else{const u=URL.createObjectURL(new Blob(t.parts,{type:"application/octet-stream"}));t.parts=null;nat(u,t.name);setTimeout(()=>URL.revokeObjectURL(u),180000)}}
 catch(e){failTask(t);return}
 t.dur=(performance.now()-t.t0)/1000;releaseMem(t);
 t.fin=true;t.state="done";t.ctl.abort();
 activate();checkIdle();pump()}
async function add(url,name,o){
 o=o||{};name=clean(name);
 const dup=M.tasks.find(t=>!t.fin&&t.url===url);
 if(dup){if(dup.el){dup.el.root.classList.remove("dup");void dup.el.root.offsetWidth;dup.el.root.classList.add("dup")}return}
 const size=await probe(url);
 if(!size)return native(url,name);
 if(size<MINSZ&&!o.force&&!M.tasks.some(t=>!t.fin))return native(url,name);
 const CH=Math.max(2<<20,Math.min(16<<20,Math.ceil(size/(PAR*4)/1048576)*1048576)),n=Math.ceil(size/CH),cn=Math.min(n,MAXCELLS);
 const t={id:++M.seq,url,name,size,n,CH,len:i=>Math.min(size,(i+1)*CH)-i*CH,g:Math.ceil(n/cn),cn,
  state:"queued",fin:false,wait:false,ctl:new AbortController(),dir:o.dir||null,w:null,parts:null,memHeld:0,
  bytes:0,sp:0,lb:0,pc:0,live:0,wq:0,doneN:0,next:0,cl:new Float64Array(n),st:new Uint8Array(n),note:"",noteT:0,t0:0,dur:0,el:null,failed:false,cancelled:false};
 M.tasks.push(t);mkRow(t);openPanel();ensureTick();activate()}

/* ================= wiring ================= */
const isDl=a=>{try{const u=new URL(a.href,location.href);return u.origin===location.origin&&u.pathname.startsWith("/downloads/")}catch(_){return false}};
const nameOf=p=>{try{return decodeURIComponent(p.split("/").pop())}catch(_){return p.split("/").pop()}};
document.addEventListener("click",e=>{
 const a=e.target.closest&&e.target.closest("a[download]");
 if(!a||a._native||e.button||e.ctrlKey||e.metaKey||e.shiftKey||!isDl(a))return;
 e.preventDefault();const u=new URL(a.href,location.href);add(u.pathname,nameOf(u.pathname))});
async function saveAll(host){
 const list=[...host.querySelectorAll("a[download]")].filter(isDl).map(a=>{const u=new URL(a.href,location.href);return{url:u.pathname,name:nameOf(u.pathname)}});
 if(!list.length)return;
 let dir=null;
 if(window.showDirectoryPicker){
  try{dir=await showDirectoryPicker({id:"tbsave",mode:"readwrite"})}
  catch(e){if(e&&e.name==="AbortError")return;dir=null}}
 for(const f of list)add(f.url,f.name,{dir,force:true})}
function syncAll(host){
 const n=[...host.querySelectorAll("a[download]")].filter(isDl).length,old=host.querySelector(".tb-all");
 if(n<2){if(old)old.remove();return}
 const label="\u26a1 TURBO \u00b7 SAVE ALL ("+n+")";
 if(old){if(old.textContent!==label)old.textContent=label;return}
 const b=document.createElement("button");b.className="tb-all";b.textContent=label;b.onclick=()=>saveAll(host);host.insertBefore(b,host.firstChild)}
for(const id of["lib","links"]){
 const h=$(id);if(!h)continue;
 new MutationObserver(()=>syncAll(h)).observe(h,{childList:true});syncAll(h)}
window.TurboDL={add:(u,n,o)=>add(u,n,o),cancelAll:()=>M.tasks.forEach(t=>!t.fin&&cancelTask(t,"Cancelled")),pool:PAR};
})();
"""


def html_tag() -> str:
    return "<script>" + TURBO_JS + "</script>"
