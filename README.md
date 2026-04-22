# ☕ Xenia Shot Controller

A real-time espresso shot monitor and AI barista coach for the **Xenia Dual Boiler** machine.

Live pressure chart at 100ms resolution, automatic channeling detection, LLM-powered coaching during extraction, and full script execution — all in ~600 lines of Python and a vanilla browser UI.

---

## Features

- **Real-time chart** — pressure at up to 10 Hz (`/api/v2/diagram_get` during active shots)
- **Script launcher** — browse and run all machine scripts from the UI
- **AI Barista Coach** — Anthropic/OpenAI LLM watches the extraction and calls out anomalies
- **Auto shot detection** — tracking starts when pressure exceeds 1.5 bar, no manual trigger needed
- **Shot log** — every extraction saved with full pressure curve, phases, and chat history
- **Demo mode** — realistic simulation, no machine required

---

## Quick Start

```bash
git clone https://github.com/simoncharmms/xenia-shot-controller.git
cd xenia-shot-controller

# Copy and edit config
cp data/config.example.json data/config.json
# → set machine host IP and (optionally) your LLM API key

# Launch
./start.sh
```

Open **http://localhost:8766** in your browser.

**Demo / no machine:**
```bash
./start.sh --demo
```

**Restart:**
```bash
./start.sh   # kills any running instance automatically
```

---

## Requirements

- Python 3.9+
- The `start.sh` script creates a `.venv` and installs everything automatically

Dependencies (see `requirements.txt`):
```
aiohttp
websockets
```

---

## Configuration

Copy `data/config.example.json` → `data/config.json` and fill in:

```json
{
  "machine": {
    "type": "xenia_http",
    "host": "http://192.168.x.x"
  },
  "llm": {
    "base_url": "https://api.anthropic.com",
    "api_key": "sk-ant-...",
    "model": "claude-sonnet-4-5"
  }
}
```

- **`machine.host`** — your Xenia's local IP (find it in your router or the Xenia web UI)
- **`llm.api_key`** — optional; leave blank to disable AI coaching
- **`llm.base_url`** — any OpenAI-compatible endpoint works (Ollama, OpenRouter, etc.)

`data/config.json` is gitignored — your API key stays local.

---

## Project Structure

```
xenia-shot-controller/
├── controller.py          # asyncio backend — polling, WebSocket, HTTP, LLM
├── requirements.txt
├── start.sh               # venv bootstrap + launch (kills previous instance)
├── ui/
│   ├── index.html         # dashboard
│   ├── app.js             # WebSocket client + Chart.js
│   └── styles.css         # dark theme matching coffee.html
├── data/
│   ├── config.example.json   # copy to config.json and edit
│   └── shots.json            # auto-created; persistent shot log
└── LICENSE
```

---

## UI Overview

### Auto vs Manual mode

**AUTO** (default) — run a script from the Scripts panel and let the machine handle profiling. Tracking starts automatically when pressure rises. Target values shown in stats are read from live sensor data.

**MANUAL** — exposes pressure/time/temp sliders for custom targets. Use the Live Pressure slider to override pressure mid-shot.

### Scripts panel

All brew profiles stored on the machine appear here. Click to **select**, then **▶ Run** to execute. The controller uses `POST /api/v2/scripts/execute` — confirmed working on firmware ESP v3.13.

### Chat / AI Coach

The coach fires every 5s during extraction and when anomalies are detected:

| Anomaly | Trigger |
|---------|---------|
| Channeling | Rapid pressure drop (>1.5 bar in 6s) |
| Pressure high | >target+1.5 bar sustained |
| Pressure low | <target-1.5 bar sustained |
| Shot slow | Elapsed > 1.25× target time |

Direct questions work anytime — the chat uses the current shot state as context.

---

## Xenia API reference

The controller uses the official v2 API plus some undocumented endpoints discovered on firmware 3.13:

| Endpoint | Method | Description |
|---|---|---|
| `/api/v2/overview` | GET | Full sensor snapshot (idle polling) |
| `/api/v2/diagram_get` | GET | Lightweight shot data — used during active extraction |
| `/api/v2/scripts/list` | GET | `index_list` + `title_list` |
| `/api/v2/scripts/execute` | POST | Run script: `{"ID": 16}` with `Content-Type: application/x-www-form-urlencoded` |
| `/api/v2/scripts/stop` | GET | Stop running script |
| `/api/v2/scripts/download` | GET | All scripts as JSON |
| `/api/v2/switches` | GET | Switch-to-script assignments |
| `/api/v2/machine/control` | POST | On/off/eco (`{"action": 1}`) |
| `/api/v2/inc_dec` | POST | Set brew temps |

> **ESP32 quirk:** `POST /api/v2/scripts/execute` requires `Content-Type: application/x-www-form-urlencoded` with the body as a raw JSON string — not `application/json`. This matches what jQuery's `$.post(url, JSON.stringify(data))` sends.

---

## Architecture

```
Browser ◄──── HTTP :8766 ──── aiohttp (serves ui/)
   │
   └──── WebSocket :8765 ──── asyncio server
                                   │
                              control_loop()
                                   │
                    ┌──────────────┴──────────────┐
               poll Xenia API              asyncio tasks
               GET /diagram_get (100ms)    ├── coaching (LLM, every 5s)
               GET /overview   (300ms)     ├── phase state machine
                                           └── shot save / reflection
```

- Single `asyncio` event loop — polling, WebSocket, HTTP, LLM all concurrent
- **100ms** poll during active shots (via `/diagram_get`) → smooth chart
- **300ms** poll at idle (via `/overview`) → picks up MA_STATUS, temps
- Shot data stored in `data/shots.json` — full pressure curve + chat history

---

## Demo Mode

`--demo` runs a `DemoSimulator` that generates realistic sensor curves:
pre-infusion → ramp → extraction → declining. No network calls. Great for UI work while the machine is off.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Machine shows OFFLINE | Check IP in `data/config.json`, machine powered on? |
| Scripts panel empty | Hit ↻ refresh, or check machine is reachable |
| Script doesn't fire | Confirm firmware ≥ 3.13; the execute endpoint is undocumented on older builds |
| Port already in use | `./start.sh` kills the old instance automatically |
| LLM not responding | Check `api_key` in config; coach silently skips if unconfigured |
| Chart not updating | Open browser console — check WebSocket connection |

---

## License

MIT — see [LICENSE](LICENSE).

---

*Beans: Supremo Bio Espresso, Unterhaching.*
