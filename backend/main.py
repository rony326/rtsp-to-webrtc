"""
Stream Manager Backend — Dual-Stream / Nahtlos-Umschalten
==========================================================

Prinzip:
  Pro Kamera laufen IMMER zwei FFmpeg-Prozesse parallel:
    • /hls/{id}/standby/index.m3u8   ← Loop des Standby-Videos
    • /hls/{id}/live/index.m3u8      ← Live-RTSP-Stream der Kamera

  Das Frontend hält beide <video>-Elemente geladen und blendet
  per CSS/JS zwischen ihnen um. Kein FFmpeg-Neustart → kein Schwarzbild.

TCP-Protokoll (Newline-delimited JSON oder Plaintext):
  {"action":"live",    "stream":"cam1"}
  {"action":"standby", "stream":"cam1"}
  {"action":"toggle",  "stream":"cam1"}
  {"action":"live",    "stream":"*"}
  {"action":"status"}
  oder: "live cam1", "standby cam1", "toggle cam1"
"""

import asyncio
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, Response, HTMLResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("stream-manager")

# ─── Config ───────────────────────────────────────────────────────────────────

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "config.json"))
HLS_ROOT    = Path(os.getenv("HLS_ROOT",    "/tmp/hls"))
STATIC_DIR  = Path(os.getenv("STATIC_DIR",  "../frontend"))
TCP_HOST    = os.getenv("TCP_HOST", "0.0.0.0")
TCP_PORT    = int(os.getenv("TCP_PORT", "9000"))
HTTP_PORT   = int(os.getenv("HTTP_PORT", "8080"))

HLS_ROOT.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "streams": [
        {
            "id": "cam1",
            "name": "Kamera 1",
            "camera_url": "rtsp://user:pass@192.168.1.10/stream1",
            "standby_video": "standby/loop.mp4",
            "hls_segment_duration": 2,
        }
    ]
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    log.warning("No config.json found – using built-in default.")
    return DEFAULT_CONFIG


# ─── FFmpeg Worker (einzelner Prozess) ────────────────────────────────────────

class FfmpegWorker:
    """Startet und hält einen FFmpeg-Prozess dauerhaft am Leben."""

    def __init__(self, name: str, cmd: list):
        self.name     = name
        self.cmd      = cmd
        self._proc: Optional[subprocess.Popen] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True, name=self.name)
        self._thread.start()

    def stop(self):
        self._running = False
        self._kill()

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _kill(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
        self._proc = None

    def _loop(self):
        while self._running:
            log.info(f"[{self.name}] start: {' '.join(self.cmd)}")
            try:
                self._proc = subprocess.Popen(
                    self.cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                while self._running:
                    try:
                        self._proc.wait(timeout=1)
                        err = self._proc.stderr.read().decode(errors="replace")
                        log.warning(f"[{self.name}] exited: {err[-400:]}")
                        break
                    except subprocess.TimeoutExpired:
                        continue
                self._kill()
            except FileNotFoundError:
                log.error(f"[{self.name}] ffmpeg not found!")
                time.sleep(10)
            except Exception as e:
                log.error(f"[{self.name}] error: {e}")
            if self._running:
                time.sleep(2)


# ─── Stream Worker (dual: standby + live) ────────────────────────────────────

class StreamWorker:
    """
    Verwaltet zwei FfmpegWorker pro Kamera (Standby + Live).
    Beide laufen immer gleichzeitig. Der Modus bestimmt nur,
    welchen HLS-Stream das Frontend anzeigt.
    """

    def __init__(self, cfg: dict):
        self.id   = cfg["id"]
        self.name = cfg.get("name", cfg["id"])

        camera_url    = cfg["camera_url"]
        standby_video = cfg.get("standby_video", "standby/loop.mp4")
        seg_dur       = int(cfg.get("hls_segment_duration", 2))

        if not os.path.isabs(standby_video):
            standby_video = str(Path(__file__).parent / standby_video)

        # HLS nur für Standby — Live läuft via go2rtc/WebRTC
        standby_dir = HLS_ROOT / self.id / "standby"
        standby_dir.mkdir(parents=True, exist_ok=True)

        standby_cmd = [
            "ffmpeg", "-y", "-loglevel", "warning",
            "-stream_loop", "-1", "-re",
            "-i", standby_video,
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-b:v", "800k", "-maxrate", "1000k", "-bufsize", "1000k",
            "-g", "30", "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", "64k",
            "-f", "hls",
            "-hls_time", "1",
            "-hls_list_size", "3",
            "-hls_flags", "delete_segments+append_list+independent_segments+split_by_time",
            "-hls_segment_filename", str(standby_dir / "seg%05d.ts"),
            str(standby_dir / "index.m3u8"),
        ]

        self._standby_worker = FfmpegWorker(f"{self.id}/standby", standby_cmd)

        self._mode = "standby"
        self._lock = threading.Lock()
        self._subscribers: set = set()   # asyncio.Queue per SSE-Client

    # ── public ────────────────────────────────────────────────────────────────

    def start(self):
        self._standby_worker.start()
        log.info(f"[{self.id}] Standby-Worker gestartet (Live via go2rtc/WebRTC)")

    def stop(self):
        self._standby_worker.stop()

    @property
    def mode(self) -> str:
        return self._mode

    def set_live(self):
        with self._lock:
            if self._mode != "live":
                self._mode = "live"
                log.info(f"[{self.id}] → LIVE")
                self._notify()

    def set_standby(self):
        with self._lock:
            if self._mode != "standby":
                self._mode = "standby"
                log.info(f"[{self.id}] → STANDBY")
                self._notify()

    def toggle(self):
        if self._mode == "standby":
            self.set_live()
        else:
            self.set_standby()

    def status(self) -> dict:
        return {
            "id":          self.id,
            "name":        self.name,
            "mode":        self._mode,
            "standby_url": f"/hls/{self.id}/standby/index.m3u8",
            "webrtc_src":  self.id,   # go2rtc stream name
            "standby_ok":  self._standby_worker.alive(),
        }

    # ── SSE ───────────────────────────────────────────────────────────────────

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=20)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers.discard(q)

    def _notify(self):
        payload = json.dumps(self.status())
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass


# ─── Stream Manager ───────────────────────────────────────────────────────────

class StreamManager:
    def __init__(self, config: dict):
        self.workers: Dict[str, StreamWorker] = {}
        for s in config.get("streams", []):
            w = StreamWorker(s)
            self.workers[w.id] = w

    def start_all(self):
        for w in self.workers.values():
            w.start()

    def stop_all(self):
        for w in self.workers.values():
            w.stop()

    def get(self, sid: str) -> Optional[StreamWorker]:
        return self.workers.get(sid)

    def all_status(self) -> list:
        return [w.status() for w in self.workers.values()]


# ─── TCP Command Server ────────────────────────────────────────────────────────

async def handle_tcp_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    manager: StreamManager,
):
    addr = writer.get_extra_info("peername")
    log.info(f"TCP connect: {addr}")
    try:
        while True:
            data = await reader.readline()
            if not data:
                break
            line = data.decode().strip()
            if not line:
                continue
            log.info(f"TCP [{addr}] ← {line}")

            # JSON oder Plaintext parsen
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                parts = line.split()
                cmd = {"action": parts[0], "stream": parts[1] if len(parts) > 1 else ""}

            action    = cmd.get("action", "").lower()
            stream_id = cmd.get("stream", "")

            if action == "status":
                response = {"streams": manager.all_status()}

            elif action in ("live", "standby", "toggle"):
                targets = (
                    list(manager.workers.values())
                    if stream_id in ("*", "")
                    else ([w] if (w := manager.get(stream_id)) else [])
                )
                if not targets and stream_id not in ("*", ""):
                    response = {"error": f"unknown stream: {stream_id}"}
                else:
                    for w in targets:
                        if action == "live":      w.set_live()
                        elif action == "standby": w.set_standby()
                        elif action == "toggle":  w.toggle()
                    response = {"streams": [w.status() for w in targets]}
            else:
                response = {"error": f"unknown action: {action}"}

            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()

    except asyncio.IncompleteReadError:
        pass
    except Exception as e:
        log.error(f"TCP error: {e}")
    finally:
        writer.close()
        log.info(f"TCP disconnect: {addr}")


async def start_tcp_server(manager: StreamManager):
    srv = await asyncio.start_server(
        lambda r, w: handle_tcp_client(r, w, manager),
        TCP_HOST, TCP_PORT,
    )
    log.info(f"TCP server on {TCP_HOST}:{TCP_PORT}")
    async with srv:
        await srv.serve_forever()


# ─── FastAPI ───────────────────────────────────────────────────────────────────

config  = load_config()
manager = StreamManager(config)

app = FastAPI(title="Stream Manager", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# HLS-Segmente
app.mount("/hls", StaticFiles(directory=str(HLS_ROOT)), name="hls")

# Frontend
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def root():
        return FileResponse(str(STATIC_DIR / "index.html"))


GO2RTC_URL = os.getenv("GO2RTC_URL", "http://172.17.0.1:1984")  # Docker host IP

# WebRTC Proxy — vermeidet CORS, Browser spricht nur Port 8080
@app.post("/api/webrtc")
async def webrtc_proxy(request: Request):
    import httpx
    src = request.query_params.get("src", "")
    body = await request.body()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{GO2RTC_URL}/api/webrtc?src={src}",
            content=body,
            headers={"Content-Type": "application/sdp"},
            timeout=10,
        )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type="application/sdp",
    )

# Vollbild-View pro Kamera: /cam1, /cam2, etc.
@app.get("/cam{sid}")
async def cam_fullscreen(sid: str, request: Request):
    w = manager.get("cam" + sid)
    if not w: raise HTTPException(404, f"Stream cam{sid} nicht gefunden")
    s = w.status()
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{s["name"]}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  html, body {{ width:100%; height:100%; background:#000; overflow:hidden; }}
  video {{ width:100%; height:100%; object-fit:cover; display:block; }}
  .wrap {{ position:relative; width:100%; height:100%; }}
  .standby, .live {{ position:absolute; inset:0; width:100%; height:100%; transition:opacity .15s; }}
  .standby {{ z-index:1; opacity:1; }}
  .live    {{ z-index:2; opacity:0; }}
  body.live-mode .live    {{ opacity:1; }}
  body.live-mode .standby {{ opacity:0; }}
</style>
</head>
<body class="{"live-mode" if s["mode"] == "live" else ""}">
<div class="wrap">
  <video class="standby" id="vsb" autoplay muted playsinline loop></video>
  <video class="live"    id="vlv" autoplay muted playsinline></video>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.4.12/hls.min.js"></script>
<script>
const STREAM_ID = "cam{sid}";

// HLS Standby
(function() {{
  const v = document.getElementById("vsb");
  if (Hls.isSupported()) {{
    const h = new Hls({{ manifestLoadingMaxRetry:999, fragLoadingMaxRetry:6 }});
    h.loadSource("/hls/" + STREAM_ID + "/standby/index.m3u8");
    h.attachMedia(v);
    h.on(Hls.Events.MANIFEST_PARSED, () => v.play().catch(()=>{{}}));
  }} else if (v.canPlayType("application/vnd.apple.mpegurl")) {{
    v.src = "/hls/" + STREAM_ID + "/standby/index.m3u8";
    v.play().catch(()=>{{}});
  }}
}})();

// WebRTC Live
class WebRTCPlayer {{
  constructor(videoEl, streamName) {{
    this.video = videoEl; this.stream = streamName;
    this.pc = null; this.active = false; this.retryTimer = null;
  }}
  async start() {{ this.active = true; await this._connect(); }}
  stop() {{ this.active=false; clearTimeout(this.retryTimer); if(this.pc){{this.pc.close();this.pc=null;}} this.video.srcObject=null; }}
  async _connect() {{
    if (!this.active) return;
    try {{
      const pc = new RTCPeerConnection({{ iceServers:[{{urls:"stun:stun.l.google.com:19302"}}], bundlePolicy:"max-bundle" }});
      this.pc = pc;
      pc.ontrack = e => {{ if(this.video.srcObject!==e.streams[0]){{ this.video.srcObject=e.streams[0]; this.video.play().catch(()=>{{}}); }} }};
      pc.oniceconnectionstatechange = () => {{ if(["disconnected","failed","closed"].includes(pc.iceConnectionState)) this._retry(); }};
      pc.addTransceiver("video",{{direction:"recvonly"}});
      pc.addTransceiver("audio",{{direction:"recvonly"}});
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await new Promise(res => {{
        if(pc.iceGatheringState==="complete"){{res();return;}}
        pc.onicegatheringstatechange=()=>{{if(pc.iceGatheringState==="complete")res();}};
        setTimeout(res,3000);
      }});
      const resp = await fetch("/api/webrtc?src="+this.stream, {{
        method:"POST", headers:{{"Content-Type":"application/sdp"}}, body:pc.localDescription.sdp
      }});
      if(!resp.ok) throw new Error("HTTP "+resp.status);
      await pc.setRemoteDescription({{type:"answer", sdp:await resp.text()}});
    }} catch(e) {{ this._retry(); }}
  }}
  _retry() {{ if(!this.active)return; if(this.pc){{this.pc.close();this.pc=null;}} clearTimeout(this.retryTimer); this.retryTimer=setTimeout(()=>this._connect(),3000); }}
}}

const player = new WebRTCPlayer(document.getElementById("vlv"), STREAM_ID);
player.start();

// SSE — Modus-Updates
const es = new EventSource("/api/streams/" + STREAM_ID + "/events");
es.onmessage = e => {{
  const s = JSON.parse(e.data);
  document.body.className = s.mode === "live" ? "live-mode" : "";
}};
</script>
</body>
</html>"""
    return HTMLResponse(html)

# REST API
@app.get("/api/streams")
async def api_streams():
    return manager.all_status()

@app.get("/api/streams/{sid}")
async def api_stream(sid: str):
    w = manager.get(sid)
    if not w: raise HTTPException(404)
    return w.status()

@app.post("/api/streams/{sid}/live")
async def api_live(sid: str):
    w = manager.get(sid)
    if not w: raise HTTPException(404)
    w.set_live(); return w.status()

@app.post("/api/streams/{sid}/standby")
async def api_standby(sid: str):
    w = manager.get(sid)
    if not w: raise HTTPException(404)
    w.set_standby(); return w.status()

@app.post("/api/streams/{sid}/toggle")
async def api_toggle(sid: str):
    w = manager.get(sid)
    if not w: raise HTTPException(404)
    w.toggle(); return w.status()


# Server-Sent Events — sofortiger Push bei Modus-Wechsel (kein Polling nötig)
@app.get("/api/streams/{sid}/events")
async def api_events(sid: str, request: Request):
    w = manager.get(sid)
    if not w: raise HTTPException(404)

    q = w.subscribe()

    async def generator():
        yield f"data: {json.dumps(w.status())}\n\n"   # Sofort-Status
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            w.unsubscribe(q)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─── Lifecycle ────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    manager.start_all()
    asyncio.create_task(start_tcp_server(manager))

@app.on_event("shutdown")
async def shutdown():
    manager.stop_all()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=HTTP_PORT, reload=False)
