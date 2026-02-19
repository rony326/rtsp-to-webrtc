# Stream Manager — WebRTC-Variante

Dual-Stream-Manager für Netzwerkkameras mit minimaler Latenz. Standby läuft als HLS-Loop, Live wird via go2rtc als WebRTC direkt im Browser wiedergegeben.

## Funktionsweise

- **Standby**: FFmpeg loopt ein lokales Video als HLS-Stream
- **Live**: go2rtc leitet den RTSP-Stream ohne Re-Encoding als WebRTC weiter
- **Umschaltung**: CSS opacity-Transition (~150ms), kein Reload, kein Schwarzbild
- **Latenz**: <500ms (WebRTC, kein Transcoding)
- **Steuerung**: TCP Port 9000 (JSON) oder REST API Port 8080

## Voraussetzungen

- Docker + Docker Compose
- Netzwerkkamera mit RTSP-Stream und H.264-Codec
- Standby-Video als MP4 (z.B. `standby/loop.mp4`)

## Installation

```bash
git clone <repo>
cd stream-manager-webrtc

# Standby-Video ablegen
mkdir standby
cp /pfad/zu/loop.mp4 standby/

# Konfigurationen anpassen
nano backend/config.json
nano go2rtc.yaml

# Starten
docker compose up -d
```

## Konfiguration

`backend/config.json`:
```json
{
  "streams": [
    {
      "id": "cam1",
      "name": "Eingang",
      "camera_url": "rtsp://user:pass@192.168.1.123/stream",
      "standby_video": "standby/loop.mp4"
    }
  ]
}
```

`go2rtc.yaml`:
```yaml
streams:
  cam1: rtsp://user:pass@192.168.1.123/stream

api:
  listen: ":1984"
  origin: "*"
```

> **Wichtig:** Stream-ID in `config.json` und `go2rtc.yaml` müssen übereinstimmen.

Mehrere Kameras: in beiden Dateien je einen Eintrag ergänzen.

## Steuerung

**TCP (Port 9000):**
```bash
echo '{"action":"live","stream":"cam1"}'    | nc localhost 9000
echo '{"action":"standby","stream":"cam1"}' | nc localhost 9000
echo '{"action":"toggle","stream":"cam1"}'  | nc localhost 9000
echo '{"action":"toggle","stream":"*"}'     | nc localhost 9000
echo '{"action":"status"}'                  | nc localhost 9000
```

**REST API (Port 8080):**
```bash
curl -X POST http://localhost:8080/api/streams/cam1/live
curl -X POST http://localhost:8080/api/streams/cam1/standby
curl -X POST http://localhost:8080/api/streams/cam1/toggle
curl http://localhost:8080/api/streams
```

**Node-RED:** HTTP Request Node → `POST http://<host>:8080/api/streams/cam1/toggle`

## Web-Oberfläche

| URL | Beschreibung |
|-----|-------------|
| `http://<host>:8080/` | Übersicht alle Streams |
| `http://<host>:8080/cam1` | Vollbild cam1, keine Buttons |
| `http://<host>:1984/` | go2rtc eigene Web-UI (Test/Debug) |

## Ports

| Port | Dienst |
|------|--------|
| 8080 | HTTP — Web-UI, HLS-Segmente, REST API, WebRTC-Proxy |
| 9000 | TCP — JSON-Steuerung |
| 1984 | go2rtc API (intern) |
| 8554 | go2rtc RTSP |
| 8555 | go2rtc WebRTC UDP |

## Projektstruktur

```
stream-manager-webrtc/
├── backend/
│   ├── main.py          # FastAPI + FFmpeg-Worker + WebRTC-Proxy
│   ├── config.json      # Stream-Konfiguration
│   └── requirements.txt
├── frontend/
│   └── index.html       # Web-UI
├── standby/
│   └── loop.mp4         # Standby-Video (selbst ablegen)
├── go2rtc.yaml          # go2rtc Stream-Konfiguration
├── Dockerfile
└── docker-compose.yml
```

## Architektur

```
Browser
  │
  ├── HLS  → :8080/hls/cam1/standby/  (Standby-Loop via FFmpeg)
  └── WebRTC → :8080/api/webrtc       (Proxy)
                    │
                    └── go2rtc :1984  (RTSP → WebRTC, kein Transcoding)
                                │
                                └── Kamera :554 (RTSP)
```

## Troubleshooting

**WebRTC verbindet nicht:**
```bash
# go2rtc Streams prüfen
curl http://localhost:1984/api/streams

# Logs
docker logs gsa-stream-manager-go2rtc-1
```

**3 Karten statt 1 in der UI:**
```bash
# Altes HLS-Volume löschen
docker compose down
docker volume rm <projektname>_hls_data
docker compose up -d
```

**go2rtc Config wird nicht geladen (`{}`):**
```bash
# Prüfen ob Config korrekt gemountet
docker exec <go2rtc-container> cat /config/go2rtc.yaml
# Falls Verzeichnis statt Datei: compose down, volume prune, compose up
```
