# ☕ Xenia Shot Controller

> Real-time espresso shot monitor and AI barista coach for the **Xenia Dual Boiler** machine.

[![CI](https://github.com/simoncharmms/xenia-shot-controller/actions/workflows/ci.yml/badge.svg)](https://github.com/simoncharmms/xenia-shot-controller/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](Dockerfile)

Live pressure chart at 100 ms resolution, automatic channeling detection, LLM-powered shot commentary during extraction, and full script execution — all in a single Python file and a vanilla browser UI.

![screenshot placeholder](https://placehold.co/900x480/0a0e1a/c4a882?text=Xenia+Shot+Controller)

---

## Features

| | |
|---|---|
| 📈 **Live chart** | Pressure at up to 10 Hz via `/api/v2/diagram_get` during active shots |
| 🤖 **AI Barista Coach** | LLM commentary every ~10 s throughout the shot; anomaly alerts fire instantly |
| 🎬 **Script launcher** | Browse and run all machine brew profiles from the UI |
| 🔍 **Auto shot detection** | Tracking starts when pressure exceeds 1.5 bar — no manual trigger needed in AUTO mode |
| 📋 **Shot log** | Every extraction saved with full pressure curve, phases, and chat history |
| 🎭 **Demo mode** | Realistic simulation — no machine required |
| 🐳 **Docker-ready** | Single `docker compose up` with env-var configuration |

---

## Quick Start

### Docker (recommended)

```bash
git clone https://github.com/simoncharmms/xenia-shot-controller.git
cd xenia-shot-controller

cp .env.example .env
# → edit .env: set XENIA_HOST, XENIA_LLM_API_KEY, etc.

docker compose up
```

Open **http://localhost:8766**.

**Demo mode** (no machine needed):

```bash
docker compose --profile demo up xenia-demo
```

---

### Local / bare-metal

```bash
git clone https://github.com/simoncharmms/xenia-shot-controller.git
cd xenia-shot-controller

cp data/config.example.json data/config.json
# → edit data/config.json

./start.sh          # creates venv, installs deps, launches
./start.sh --demo   # simulation mode
```

Open **http://localhost:8766**.

---

## Configuration

### Option A — environment variables (Docker / CI)

Copy `.env.example` → `.env` and fill in:

```dotenv
XENIA_HOST=http://192.168.x.x
XENIA_LLM_BASE_URL=https://api.anthropic.com
XENIA_LLM_API_KEY=sk-ant-…
XENIA_LLM_MODEL=claude-sonnet-4-5
```

Environment variables take precedence over `data/config.json`.

### Option B — config file (local)

Copy `data/config.example.json` → `data/config.json`:

```json
{
  "machine": {
    "type": "xenia_http",
    "host": "http://192.168.x.x"
  },
  "llm": {
    "base_url": "https://api.anthropic.com",
    "api_key": "YOUR_API_KEY",
    "model": "claude-sonnet-4-5"
  }
}
```

`data/config.json` is git-ignored — your key stays local.

### Option C — Settings UI

Click ⚙️ in the top-right of the dashboard. Changes are saved to `data/config.json` immediately.

---

### Configuration reference

| Variable / field | Description | Default |
|---|---|---|
| `XENIA_HOST` / `machine.host` | Xenia local IP | `http://192.168.2.41` |
| `XENIA_LLM_BASE_URL` / `llm.base_url` | Any OpenAI-compatible endpoint | `https://api.anthropic.com` |
| `XENIA_LLM_API_KEY` / `llm.api_key` | API key — leave blank to disable coaching | *(empty)* |
| `XENIA_LLM_MODEL` / `llm.model` | Model name | `claude-sonnet-4-5` |

The LLM field accepts any OpenAI-compatible provider: Anthropic, OpenAI, Ollama (`http://localhost:11434/v1`), OpenRouter, etc.

---

## Project Structure

```
xenia-shot-controller/
├── controller.py               # asyncio backend — polling, WebSocket, HTTP, LLM
├── Dockerfile                  # multi-stage, non-root, healthcheck included
├── compose.yaml                # docker compose (normal + demo profiles)
├── .env.example                # env var template
├── requirements.txt            # aiohttp, websockets
├── start.sh                    # venv bootstrap + launch for bare-metal
├── ui/
│   ├── index.html              # dashboard
│   ├── app.js                  # WebSocket client + Chart.js
│   └── styles.css              # dark theme
├── data/
│   ├── config.example.json     # copy to config.json and edit
│   └── shots.json              # auto-created; persistent shot log
└── .github/
    └── workflows/ci.yml        # lint + docker build + smoke-test
```

---

## UI Overview

### AUTO vs MANUAL mode

**AUTO** (default) — select a script from the Scripts panel and brew. Tracking starts automatically when pressure rises above 1.5 bar. Target values come from the running script. Start/Stop buttons and manual sliders are hidden.

**MANUAL** — exposes Target Pressure / Time / Temp sliders plus a Live Pressure override slider for mid-shot adjustments.

### AI Barista Coach

The coach fires commentary throughout the shot, including:

| Event | Trigger |
|---|---|
| Phase commentary | Every ~10 s during PRE_INFUSION → RAMP → EXTRACTION → DECLINING |
| Channeling alert | Rapid pressure drop (>1.5 bar in 6 s) |
| Pressure high | >target+1.5 bar sustained over 3 samples |
| Pressure low | <target−1.5 bar sustained over 3 samples |
| Shot slow | Elapsed > 1.25× target time |

Direct questions work anytime — the chat uses live sensor state as context.

---

## API / Ports

| Port | Protocol | Description |
|---|---|---|
| `8766` | HTTP | UI, `/health`, `/api/config`, `/api/shots` |
| `8765` | WebSocket | Real-time sensor stream & commands |

### Health check

```
GET /health
```

```json
{
  "ok": true,
  "machine_online": true,
  "shot_active": false,
  "phase": "IDLE",
  "demo": false
}
```

---

## Xenia API Reference

The controller uses the official v2 API plus endpoints discovered on firmware 3.13:

| Endpoint | Method | Description |
|---|---|---|
| `/api/v2/overview` | GET | Full sensor snapshot (idle polling) |
| `/api/v2/diagram_get` | GET | Lightweight shot data — used at 100 ms during extraction |
| `/api/v2/scripts/list` | GET | `index_list` + `title_list` |
| `/api/v2/scripts/execute` | POST | Run script: `{"ID": 16}` with `Content-Type: application/x-www-form-urlencoded` |
| `/api/v2/scripts/stop` | GET | Stop running script |
| `/api/v2/inc_dec` | POST | Set brew group / boiler temperatures, pump pressure |

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
               GET /diagram_get (100 ms)   ├── coaching (LLM, every 10 s)
               GET /overview   (300 ms)    ├── phase state machine
                                           └── shot save / post-shot reflection
```

- Single `asyncio` event loop — polling, WebSocket, HTTP, LLM all concurrent, no threads
- **100 ms** poll during active shots (via `/diagram_get`) → smooth chart
- **300 ms** poll at idle (via `/overview`) → picks up MA_STATUS, temps
- Shot data stored in `data/shots.json` — full pressure curve + chat history, last 100 shots kept

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Machine shows OFFLINE | Check `XENIA_HOST` / `machine.host`, confirm machine is powered on and reachable |
| Scripts panel empty | Hit ↻ refresh; check machine is reachable on the network |
| Script doesn't fire | Confirm firmware ≥ 3.13; the execute endpoint is undocumented on older builds |
| Port already in use | `./start.sh` kills any old instance automatically |
| LLM not responding | Check `api_key` is set; coach silently skips if unconfigured |
| Chart not updating | Open browser console — check WebSocket connection |
| Docker container unhealthy | `docker logs xenia` — usually a wrong `XENIA_HOST` value |

---

## License

MIT — see [LICENSE](LICENSE).
