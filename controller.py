#!/usr/bin/env python3
"""
Xenia Shot Controller — closed-loop espresso shot controller
for the Xenia dual boiler machine, with LLM-assisted coaching.

Usage:
    python controller.py           # connect to real machine
    python controller.py --demo    # simulation mode (no machine needed)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import sys
import time
from collections import deque
from enum import Enum
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web
import websockets
from websockets.asyncio.server import serve as ws_serve, ServerConnection

# ── Configuration ────────────────────────────────────────────────────────────

DATA_DIR   = Path(__file__).parent / "data"
SHOTS_FILE = DATA_DIR / "shots.json"
CONFIG_FILE = DATA_DIR / "config.json"
UI_DIR     = Path(__file__).parent / "ui"

DEFAULT_CONFIG = {
    "machine": {"type": "xenia_http", "host": "http://192.168.2.41"},
    "llm": {"base_url": "https://api.anthropic.com", "api_key": "", "model": "claude-sonnet-4-5"},
}

app_config: dict = {}

POLL_INTERVAL      = 0.3   # seconds — idle polling
POLL_INTERVAL_SHOT = 0.1   # seconds — active shot (3× faster)
WS_PORT = 8765
HTTP_PORT = 8766

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("xenia")

# ── Config helpers ────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load config from disk, then overlay environment variables.

    Environment variables take precedence over config.json so Docker / CI
    deployments can be configured without writing files.

    Supported env vars:
        XENIA_HOST            → machine.host
        XENIA_LLM_BASE_URL    → llm.base_url
        XENIA_LLM_API_KEY     → llm.api_key
        XENIA_LLM_MODEL       → llm.model
    """
    import copy
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text())
            for section in ("machine", "llm"):
                if section in saved and isinstance(saved[section], dict):
                    cfg[section].update(saved[section])
        except Exception as e:
            log.warning("Could not load config.json: %s", e)

    # Environment variable overrides
    env_map = {
        "XENIA_HOST":         ("machine", "host"),
        "XENIA_LLM_BASE_URL": ("llm",     "base_url"),
        "XENIA_LLM_API_KEY":  ("llm",     "api_key"),
        "XENIA_LLM_MODEL":    ("llm",     "model"),
    }
    for env_key, (section, field) in env_map.items():
        val = os.environ.get(env_key, "").strip()
        if val:
            cfg[section][field] = val
            log.info("Config override from env: %s → %s.%s", env_key, section, field)

    return cfg


def save_config(cfg: dict):
    try:
        DATA_DIR.mkdir(exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception as e:
        log.error("Failed to save config: %s", e)


def masked_config(cfg: dict) -> dict:
    """Return a copy of config with api_key masked."""
    import copy
    c = copy.deepcopy(cfg)
    if c.get("llm", {}).get("api_key"):
        c["llm"]["api_key"] = "●●●●"
    return c


# ── Enums ─────────────────────────────────────────────────────────────────────

class Phase(str, Enum):
    IDLE         = "IDLE"
    PRE_INFUSION = "PRE_INFUSION"
    RAMP         = "RAMP"
    EXTRACTION   = "EXTRACTION"
    DECLINING    = "DECLINING"
    DONE         = "DONE"

class Mode(str, Enum):
    AUTO   = "AUTO"
    MANUAL = "MANUAL"

# ── Chat Session ──────────────────────────────────────────────────────────────

class ChatSession:
    def __init__(self):
        self.messages: list[dict] = []

    def add(self, role: str, content: str) -> dict:
        now = time.localtime()
        msg = {
            "role": role,
            "content": content,
            "ts": time.time(),
            "time_display": time.strftime("%H:%M:%S", now),
        }
        self.messages.append(msg)
        return msg

    def to_list(self) -> list[dict]:
        return list(self.messages)

    def to_context_str(self) -> str:
        """Last 8 messages as 'Role: content' lines."""
        tail = self.messages[-8:]
        lines = []
        for m in tail:
            role = m["role"].capitalize()
            lines.append(f"{role}: {m['content']}")
        return "\n".join(lines)


# ── State ─────────────────────────────────────────────────────────────────────

class ShotState:
    def __init__(self):
        self.reset()

    def reset(self):
        # sensor values
        self.pressure: float = 0.0
        self.flow_rate: float = 0.0
        self.flow_total: float = 0.0
        self.bg_temp: float = 0.0
        self.bb_temp: float = 0.0
        self.bb_power: int = 0
        self.bg_power: int = 0
        self.ma_status: int = 0

        # shot control
        self.phase: Phase = Phase.IDLE
        self.mode: Mode = Mode.AUTO
        self.shot_active: bool = False
        self.shot_start_ts: float = 0.0
        self.elapsed: float = 0.0

        # targets
        self.target_pressure: float = 9.0
        self.target_time: float = 30.0
        self.target_temp: float = 93.0

        # internal
        self._prev_flow: float = 0.0
        self._prev_flow_ts: float = 0.0
        self._flow_samples: deque = deque(maxlen=3)
        self._pressure_history: deque = deque(maxlen=20)  # ~6s at 300ms
        self._offline_streak: int = 0
        self._prev_pressure: float = 0.0
        self._prev_pressure_ts: float = 0.0

        # phase timing
        self._phase_start_ts: float = 0.0
        self._current_target_pressure: float = 0.0

        # channeling recovery
        self._channeling_detected: bool = False
        self._channeling_ts: float = 0.0
        self._choke_active: bool = False

        # current shot data
        self._shot_curve: list = []

        # alerts
        self.alert: Optional[str] = None
        self._alert_ts: float = 0.0

        # machine online
        self.machine_online: bool = False

        # flow baseline
        self._flow_baseline: float = 0.0

        # chat
        self.chat: ChatSession = ChatSession()

    def set_alert(self, msg: Optional[str]):
        self.alert = msg
        if msg:
            self._alert_ts = time.time()
            log.warning("ALERT: %s", msg)

    def clear_alert_if_stale(self, ttl=10.0):
        if self.alert and (time.time() - self._alert_ts) > ttl:
            self.alert = None


state = ShotState()
connected_clients: set = set()

# ── LLM Client ───────────────────────────────────────────────────────────────

COACHING_SYSTEM_PROMPT = """You are an expert espresso barista assistant watching a live extraction.
You receive real-time sensor data and provide concise, live commentary on what is happening during the shot.
Comment on the current phase, pressure behaviour, ramp progress, and extraction quality.
Keep each comment to 1-3 sentences. Be direct, practical, speak like a knowledgeable friend.
Always respond with a comment — never stay silent. Do NOT repeat observations already given this session."""


def _is_anthropic(base_url: str) -> bool:
    return "anthropic.com" in base_url


async def llm_call(messages: list[dict], system: str, max_tokens: int = 200) -> Optional[str]:
    """LLM call supporting both Anthropic and OpenAI-compatible APIs.
    Auto-detected from base_url: api.anthropic.com → Anthropic Messages API,
    anything else → OpenAI /chat/completions format."""
    cfg = app_config.get("llm", {})
    api_key = cfg.get("api_key", "").strip()
    if not api_key:
        return None

    base_url = cfg.get("base_url", "https://api.anthropic.com").rstrip("/")
    model = cfg.get("model", "claude-sonnet-4-5")

    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:

            if _is_anthropic(base_url):
                # ── Anthropic Messages API ────────────────────────────────
                url = f"{base_url}/v1/messages"
                payload = {
                    "model": model,
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": messages,
                }
                headers = {
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                }
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.warning("Anthropic API error %d: %s", resp.status, body[:200])
                        return None
                    data = await resp.json()
                    return data["content"][0]["text"].strip()

            else:
                # ── OpenAI-compatible /chat/completions ───────────────────
                url = f"{base_url}/chat/completions"
                payload = {
                    "model": model,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "system", "content": system}] + messages,
                }
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                }
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.warning("LLM API error %d: %s", resp.status, body[:200])
                        return None
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"].strip()

    except asyncio.TimeoutError:
        log.warning("LLM call timed out")
        return None
    except Exception as e:
        log.warning("LLM call failed: %s", e)
        return None


# ── Coaching Engine ───────────────────────────────────────────────────────────

class CoachingEngine:
    COACH_INTERVAL = 5.0    # minimum seconds between coaching checks
    COMMENT_INTERVAL = 10.0  # fire a comment at least every N seconds during active shot

    def __init__(self):
        self.reset_for_shot()

    def reset_for_shot(self):
        self._last_coach_ts: float = 0.0
        self._last_insight_ts: float = 0.0
        self._insights_given: set = set()
        self._busy: bool = False

    def _detect_anomalies(self, st: ShotState) -> list[str]:
        anomalies = []
        if st.phase != Phase.EXTRACTION:
            # Shot slow can apply even outside EXTRACTION if elapsed too long
            if st.shot_active and st.elapsed > st.target_time * 1.25:
                anomalies.append("shot_slow")
            return anomalies

        # pressure_high: pressure > target + 1.5 bar sustained (check recent history)
        high_count = sum(
            1 for (_, p) in st._pressure_history
            if p > st.target_pressure + 1.5
        )
        if high_count >= 3:
            anomalies.append("pressure_high")

        # pressure_low: pressure < target - 1.5 bar sustained
        low_count = sum(
            1 for (_, p) in st._pressure_history
            if st.shot_active and p > 0.5 and p < st.target_pressure - 1.5
        )
        if low_count >= 3:
            anomalies.append("pressure_low")

        # pressure_drop_rapid: drop > 1.5 bar over last 5 samples
        samples = list(st._pressure_history)
        if len(samples) >= 5:
            recent = samples[-5:]
            drop = recent[0][1] - recent[-1][1]
            if drop > 1.5:
                anomalies.append("pressure_drop_rapid")

        # shot_slow
        if st.elapsed > st.target_time * 1.25:
            anomalies.append("shot_slow")

        return anomalies

    def _build_context(self, st: ShotState, anomalies: list[str]) -> str:
        # Pressure trend
        samples = list(st._pressure_history)
        if len(samples) >= 4:
            recent_avg = sum(p for _, p in samples[-4:]) / 4
            old_avg = sum(p for _, p in samples[:4]) / 4
            diff = recent_avg - old_avg
            if diff > 0.3:
                trend = "rising"
            elif diff < -0.3:
                trend = "falling"
            else:
                trend = "stable"
        else:
            trend = "unknown"

        prior = ", ".join(self._insights_given) if self._insights_given else "none"

        lines = [
            f"Phase: {st.phase.value}",
            f"Elapsed: {st.elapsed:.0f}s (target: {st.target_time:.0f}s)",
            f"Pressure: {st.pressure:.2f} bar (scripted target now: {st._current_target_pressure:.1f} bar, final target: {st.target_pressure:.1f} bar, trend: {trend})",
            f"Boiler temps — BG: {st.bg_temp:.1f}°C, BB: {st.bb_temp:.1f}°C",
            f"Anomalies detected: {', '.join(anomalies) if anomalies else 'none'}",
            f"Observations already made this shot: {prior if prior else 'none — this is the first comment'}",
        ]
        return "\n".join(lines)

    async def maybe_coach(self, st: ShotState, chat: ChatSession) -> Optional[dict]:
        if self._busy:
            return None

        now = time.time()
        since_last_check = now - self._last_coach_ts
        since_last_comment = now - self._last_insight_ts

        if since_last_check < self.COACH_INTERVAL:
            return None

        # Don't comment on IDLE or DONE phases
        if st.phase in (Phase.IDLE, Phase.DONE):
            self._last_coach_ts = now
            return None

        # Fire if: anomaly detected, OR enough time has passed since last comment
        anomalies = self._detect_anomalies(st)
        new_anomalies = [a for a in anomalies if a not in self._insights_given]
        due_for_comment = since_last_comment >= self.COMMENT_INTERVAL

        if not new_anomalies and not due_for_comment:
            self._last_coach_ts = now
            return None

        self._busy = True
        self._last_coach_ts = now

        try:
            context = self._build_context(st, new_anomalies)
            response = await llm_call(
                [{"role": "user", "content": context}],
                system=COACHING_SYSTEM_PROMPT,
                max_tokens=200,
            )

            if not response:
                return None

            # Track anomaly categories given
            for a in new_anomalies:
                self._insights_given.add(a)

            self._last_insight_ts = now
            return chat.add("assistant", response)
        except Exception as e:
            log.warning("CoachingEngine error: %s", e)
            return None
        finally:
            self._busy = False


coaching_engine = CoachingEngine()

# ── Scripts cache ─────────────────────────────────────────────────────────────

_scripts_cache: dict = {}   # { id(int): title(str) }
_post_in_flight: bool = False  # suppress offline detection while a POST is running


async def fetch_scripts_list(session: aiohttp.ClientSession) -> dict:
    """Fetch available scripts from machine. Returns {id: title}."""
    host = app_config.get("machine", {}).get("host", "http://192.168.2.102")
    try:
        async with session.get(
            f"{host}/api/v2/scripts/list",
            timeout=aiohttp.ClientTimeout(total=3.0),
        ) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                idx_list   = data.get("index_list", [])
                title_list = data.get("title_list", [])
                return {int(idx): title for idx, title in zip(idx_list, title_list)}
    except Exception as e:
        log.warning("fetch_scripts_list failed: %s", e)
    return {}


async def execute_script(session: aiohttp.ClientSession, script_id: int) -> bool:
    """Execute a script on the machine by ID.

    The ESP32 has a single-threaded HTTP server: while it processes the POST,
    it cannot answer GET /overview requests.  We set _post_in_flight so the
    control loop won't declare the machine offline during the blocking POST,
    and keep it set for a short grace period afterwards.
    """
    global _post_in_flight
    host = app_config.get("machine", {}).get("host", "http://192.168.2.102")
    _post_in_flight = True
    try:
        # ESP32 quirk: expects Content-Type: application/x-www-form-urlencoded
        # but the *body* is a raw JSON string (what jQuery $.post does when passed a string).
        # Using json= sends application/json which the ESP ignores.
        raw_body = json.dumps({"ID": script_id})
        async with session.post(
            f"{host}/api/v2/scripts/execute",
            data=raw_body,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
            timeout=aiohttp.ClientTimeout(total=8.0),
        ) as resp:
            body = await resp.text()
            log.info("execute_script %d → %s %s", script_id, resp.status, body[:80])
            return resp.status == 200
    except asyncio.TimeoutError:
        # ESP32 accepted the POST but its single-threaded server couldn't send
        # a response while starting the script.  Almost certainly ran fine.
        log.info("execute_script %d: POST timed out (script likely started OK)", script_id)
        return True
    except Exception as e:
        log.warning("execute_script %d failed: %s", script_id, e)
        return False
    finally:
        # Keep the flag set for 5 s so polling gaps don't trigger offline alarm
        async def _clear_flag():
            await asyncio.sleep(5.0)
            global _post_in_flight
            _post_in_flight = False
        asyncio.create_task(_clear_flag())


# ── Machine communication ─────────────────────────────────────────────────────

async def fetch_overview(session: aiohttp.ClientSession) -> Optional[dict]:
    host = app_config.get("machine", {}).get("host", "http://192.168.2.102")
    api_base = f"{host}/api/v2"
    try:
        async with session.get(f"{api_base}/overview", timeout=aiohttp.ClientTimeout(total=1.0)) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
    except Exception as e:
        log.debug("Overview fetch failed: %s", e)
    return None


async def fetch_diagram(session: aiohttp.ClientSession) -> Optional[dict]:
    """Lightweight shot-data endpoint — smaller payload, same sensor fields.
    Falls back to /overview if unavailable."""
    host = app_config.get("machine", {}).get("host", "http://192.168.2.102")
    api_base = f"{host}/api/v2"
    try:
        async with session.get(
            f"{api_base}/diagram_get",
            timeout=aiohttp.ClientTimeout(total=0.5),   # tight timeout for low latency
        ) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
    except Exception as e:
        log.debug("diagram_get fetch failed: %s", e)
    return None


async def post_machine(session: aiohttp.ClientSession, endpoint: str, payload: dict) -> bool:
    host = app_config.get("machine", {}).get("host", "http://192.168.2.102")
    api_base = f"{host}/api/v2"
    try:
        async with session.post(
            f"{api_base}/{endpoint}",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=2.0),
        ) as resp:
            body = await resp.text()
            log.debug("POST /%s %s → %s %s", endpoint, payload, resp.status, body[:80])
            return resp.status == 200
    except Exception as e:
        log.debug("POST /%s failed: %s", endpoint, e)
    return False


async def set_target_temp(session: aiohttp.ClientSession, temp: float):
    await post_machine(session, "inc_dec", {"BG_SET_TEMP": temp, "BB_SET_TEMP": temp})


async def set_pump_pressure(session: aiohttp.ClientSession, pressure: float):
    ok = await post_machine(session, "inc_dec", {"PU_SET_PRESS": pressure})
    if not ok:
        log.warning("Failed to set pump pressure to %.1f bar", pressure)
    return ok


async def stop_script(session: aiohttp.ClientSession):
    host = app_config.get("machine", {}).get("host", "http://192.168.2.102")
    api_base = f"{host}/api/v2"
    try:
        async with session.get(f"{api_base}/scripts/stop", timeout=aiohttp.ClientTimeout(total=2.0)) as resp:
            log.info("scripts/stop → %s", resp.status)
    except Exception as e:
        log.error("scripts/stop failed: %s", e)


# ── Flow rate helpers ─────────────────────────────────────────────────────────

def update_flow_rate(now: float, current_ml: float) -> float:
    dt = now - state._prev_flow_ts
    if dt < 0.05:
        return state.flow_rate

    raw = (current_ml - state._prev_flow) / dt if dt > 0 else 0.0
    raw = max(0.0, raw)

    state._prev_flow = current_ml
    state._prev_flow_ts = now
    state._flow_samples.append(raw)

    smoothed = sum(state._flow_samples) / len(state._flow_samples)
    return round(smoothed, 3)


# ── Channeling / choke detection ──────────────────────────────────────────────

def detect_channeling(now: float) -> bool:
    if len(state._pressure_history) < 2:
        return False
    old_ts, old_p = state._pressure_history[0]
    dt = now - old_ts
    if dt > 0.5:
        return False
    pressure_drop = old_p - state.pressure
    return pressure_drop > 1.0 and state.flow_rate > 2.5


def detect_choke() -> bool:
    return (
        state.phase == Phase.EXTRACTION
        and state.flow_rate < 0.4
        and state.pressure >= state._current_target_pressure - 0.5
    )


# ── Broadcast helpers ─────────────────────────────────────────────────────────

async def broadcast(msg: dict):
    if not connected_clients:
        return
    data = json.dumps(msg)
    dead = set()
    for ws in connected_clients:
        try:
            await ws.send(data)
        except Exception:
            dead.add(ws)
    connected_clients.difference_update(dead)


async def broadcast_sensor():
    await broadcast({
        "type": "sensor_update",
        "ts": time.time(),
        "pressure": state.pressure,
        "flow_rate": state.flow_rate,
        "flow_total": state.flow_total,
        "bg_temp": state.bg_temp,
        "bb_temp": state.bb_temp,
        "bb_power": state.bb_power,
        "bg_power": state.bg_power,
        "phase": state.phase.value,
        "elapsed": state.elapsed,
        "alert": state.alert,
        "mode": state.mode.value,
        "shot_active": state.shot_active,
        "target_pressure": state.target_pressure,
        "target_time": state.target_time,
        "target_temp": state.target_temp,
        "machine_online": state.machine_online,
        "current_target_pressure": state._current_target_pressure,
        "demo": _demo_mode,
    })


async def broadcast_chat(msg: dict):
    await broadcast({"type": "chat_message", **msg})


# ── Shot lifecycle hooks ──────────────────────────────────────────────────────

def _init_shot_state(now: float, flow_baseline: float):
    """Shared init logic when a shot starts."""
    state.shot_active     = True
    state.shot_start_ts   = now
    state._phase_start_ts = now
    state._flow_baseline  = flow_baseline
    state._shot_curve     = []
    state._channeling_detected = False
    state._choke_active   = False
    state._flow_samples.clear()
    state.alert = None
    state.phase = Phase.PRE_INFUSION
    state._current_target_pressure = 3.5
    state.chat = ChatSession()
    coaching_engine.reset_for_shot()


async def on_shot_start():
    """Broadcast events and fire start message after shot begins."""
    await broadcast({"type": "chat_clear"})
    asyncio.create_task(send_shot_start_message())


async def send_shot_start_message():
    response = await llm_call(
        [{"role": "user", "content": (
            f"A shot just started. Target: {state.target_pressure:.1f} bar, "
            f"{state.target_time:.0f}s. Send one brief, warm, encouraging sentence to the barista."
        )}],
        system="You are a friendly espresso barista assistant. Be very brief (one sentence).",
        max_tokens=60,
    )
    if response:
        msg = state.chat.add("assistant", response)
        await broadcast_chat(msg)


async def post_shot_reflection(shot: dict):
    await asyncio.sleep(1.5)
    prompt = (
        f"The shot just finished. Duration: {shot['duration_s']:.0f}s (target: {shot['target_time']:.0f}s), "
        f"peak pressure: {shot['peak_pressure']:.1f} bar.\n\n"
        f"Ask the barista one brief, friendly question about how it tasted and what bean or recipe they used, "
        f"so you can help them improve next time. One sentence only."
    )
    response = await llm_call(
        [{"role": "user", "content": prompt}],
        system="You are a friendly espresso barista assistant. Be warm and concise.",
        max_tokens=80,
    )
    if response:
        msg = state.chat.add("assistant", response)
        await broadcast_chat(msg)


# ── Coaching task ─────────────────────────────────────────────────────────────

_last_coaching_check_ts: float = 0.0


async def do_coaching():
    msg = await coaching_engine.maybe_coach(state, state.chat)
    if msg:
        await broadcast_chat(msg)


# ── Chat command handler ──────────────────────────────────────────────────────

async def handle_user_chat(content: str):
    user_msg = state.chat.add("user", content)
    await broadcast_chat(user_msg)

    if state.shot_active:
        shot_ctx = (
            f"Current shot: {state.phase.value} | Elapsed: {state.elapsed:.0f}s | "
            f"Pressure: {state.pressure:.1f} bar | BG: {state.bg_temp:.1f}°C | BB: {state.bb_temp:.1f}°C"
        )
    else:
        shot_ctx = "No shot currently in progress."

    history = state.chat.to_context_str()
    full_content = f"{shot_ctx}\n\nConversation so far:\n{history}\n\nBarista: {content}"

    # Use a conversational system prompt for direct user messages — NOT the coaching
    # prompt which instructs the LLM to reply NO_INSIGHT when nothing is anomalous,
    # which would silently swallow direct questions.
    CHAT_SYSTEM_PROMPT = (
        "You are an expert espresso barista assistant. "
        "Answer the barista's question directly and helpfully. "
        "Keep replies concise (1-4 sentences). Be warm and practical."
    )

    response = await llm_call(
        [{"role": "user", "content": full_content}],
        system=CHAT_SYSTEM_PROMPT,
        max_tokens=300,
    )

    if response:
        msg = state.chat.add("assistant", response)
        await broadcast_chat(msg)
    elif not response:
        msg = state.chat.add("system", "⚙️ LLM not configured — add your API key in Settings (⚙️ top right) to enable AI coaching.")
        await broadcast_chat(msg)


# ── Control loop ──────────────────────────────────────────────────────────────

async def control_loop(session: Optional[aiohttp.ClientSession] = None):
    global _last_coaching_check_ts, _post_in_flight
    demo_mode = session is None
    sim = DemoSimulator() if demo_mode else None

    log.info("Control loop started (demo=%s)", demo_mode)

    while True:
        now = time.time()

        if demo_mode:
            raw = sim.tick(state)
        else:
            # During an active shot use the lighter /diagram_get endpoint at 100ms.
            # At idle use /overview (0.3s) which carries machine status fields.
            if state.shot_active:
                raw = await fetch_diagram(session)
                if raw is not None:
                    # diagram_get lacks MA_STATUS / SB_STATUS — preserve existing values
                    raw.setdefault("MA_STATUS", state.ma_status)
                    raw.setdefault("SB_STATUS", 0)
            else:
                raw = await fetch_overview(session)

            if raw is None:
                if not _post_in_flight:
                    state._offline_streak += 1
                    if state._offline_streak >= 10 and state.machine_online:
                        log.warning("Machine went offline (%d consecutive failures)", state._offline_streak)
                        state.machine_online = False
                else:
                    log.debug("Fetch failed but POST in flight — not counting as offline")
                poll = POLL_INTERVAL_SHOT if state.shot_active else POLL_INTERVAL
                await asyncio.sleep(poll)
                continue
            state._offline_streak = 0
            if not state.machine_online:
                log.info("Machine came online")
                state.machine_online = True

        # ── Parse sensor data ────────────────────────────────────────────────
        pressure_raw = float(raw.get("PU_SENS_PRESS", 0.0) or 0.0)
        flow_ml_raw  = float(raw.get("PU_SENS_FLOW_METER_ML", 0.0) or 0.0)
        bg_temp_raw  = float(raw.get("BG_SENS_TEMP_A", 0.0) or 0.0)
        bb_temp_raw  = float(raw.get("BB_SENS_TEMP_A", 0.0) or 0.0)

        state.pressure  = round(pressure_raw, 2)
        state.bg_temp   = round(bg_temp_raw, 1)
        state.bb_temp   = round(bb_temp_raw, 1)
        state.bb_power  = int(raw.get("BB_LEVEL_PW_CONTROL", 0) or 0)
        state.bg_power  = int(raw.get("BG_LEVEL_PW_CONTROL", 0) or 0)
        state.ma_status = int(raw.get("MA_STATUS", 0) or 0)
        state.machine_online = True

        # no volumetric sensor — zero out flow fields
        state.flow_rate  = 0.0
        state.flow_total = 0.0
        state._prev_flow = flow_ml_raw
        state._prev_flow_ts = now

        # pressure history
        state._pressure_history.append((now, state.pressure))
        while state._pressure_history and (now - state._pressure_history[0][0]) > 6.0:
            state._pressure_history.popleft()

        # ── Auto-detect shot start from pressure ────────────────────────────
        if not state.shot_active and not _demo_mode:
            if state.pressure > 1.5:
                log.info("Pressure detected (%.1f bar) — auto-starting shot tracking", state.pressure)
                _init_shot_state(now, flow_ml_raw)
                await on_shot_start()

        # ── Shot elapsed time ────────────────────────────────────────────────
        if state.shot_active:
            state.elapsed = round(now - state.shot_start_ts, 1)
        else:
            state.elapsed = 0.0

        # ── Phase state machine ──────────────────────────────────────────────
        if state.shot_active and state.mode == Mode.AUTO:
            await run_phase_machine(session, now)

        # ── Alert clearing ───────────────────────────────────────────────────
        state.clear_alert_if_stale(ttl=15.0)

        # ── Record curve point ───────────────────────────────────────────────
        if state.shot_active:
            state._shot_curve.append({
                "t": round(state.elapsed, 2),
                "p": state.pressure,
                "f": state.flow_rate,
            })

        # ── Auto-stop at target time ─────────────────────────────────────────
        if state.shot_active and state.elapsed >= state.target_time:
            log.info("Target time reached (%.1fs) — stopping shot", state.elapsed)
            await finish_shot(session, reason="time_reached")

        # ── Broadcast to WebSocket clients ───────────────────────────────────
        await broadcast_sensor()

        # ── Fire coaching check every 5s during active shot ─────────────────
        if state.shot_active and (now - _last_coaching_check_ts) >= 5.0:
            _last_coaching_check_ts = now
            asyncio.create_task(do_coaching())

        poll = POLL_INTERVAL_SHOT if state.shot_active else POLL_INTERVAL
        await asyncio.sleep(poll)


async def run_phase_machine(session, now: float):
    phase_elapsed = now - state._phase_start_ts

    if state.phase == Phase.PRE_INFUSION:
        state._current_target_pressure = 3.5
        if session is not None:
            await set_pump_pressure(session, state._current_target_pressure)

        if phase_elapsed >= 8.0 or (phase_elapsed >= 5.0 and state.flow_rate > 0.3):
            log.info("Phase → RAMP (flow=%.2f, elapsed=%.1f)", state.flow_rate, phase_elapsed)
            transition_phase(Phase.RAMP)

    elif state.phase == Phase.RAMP:
        ramp_duration = 10.0
        progress = min(1.0, phase_elapsed / ramp_duration)
        ramp_p = 3.5 + (state.target_pressure - 3.5) * progress
        state._current_target_pressure = round(ramp_p, 2)
        if session:
            await set_pump_pressure(session, state._current_target_pressure)

        if progress >= 1.0:
            log.info("Phase → EXTRACTION")
            transition_phase(Phase.EXTRACTION)

    elif state.phase == Phase.EXTRACTION:
        state._current_target_pressure = state.target_pressure

        if detect_channeling(now):
            if not state._channeling_detected:
                state._channeling_detected = True
                state._channeling_ts = now
                elapsed_str = fmt_time(state.elapsed)
                state.set_alert(f"⚠️ Channeling detected at {elapsed_str}")
                state._current_target_pressure = max(3.0, state.target_pressure - 2.0)
                log.warning("Channeling! Dropping pressure to %.1f", state._current_target_pressure)
        else:
            if state._channeling_detected and (now - state._channeling_ts) > 10.0:
                log.info("Recovering from channeling, ramping back to target")
                state._channeling_detected = False
                state._current_target_pressure = state.target_pressure

        if detect_choke():
            if not state._choke_active:
                state._choke_active = True
                new_p = max(4.0, state._current_target_pressure - 0.5)
                state.set_alert(f"⚠️ Choke detected — reducing to {new_p:.1f} bar")
                state._current_target_pressure = new_p
                state.target_pressure = new_p
        else:
            state._choke_active = False

        if session:
            await set_pump_pressure(session, state._current_target_pressure)

        # No volumetric sensor — skip flow-based DECLINING trigger.
        # Shots end via target_time; manual pressure back-off not applicable here.

    elif state.phase == Phase.DECLINING:
        backoff = max(4.0, state._current_target_pressure - 0.3 * POLL_INTERVAL)
        state._current_target_pressure = round(backoff, 2)
        if session:
            await set_pump_pressure(session, state._current_target_pressure)

        if state._current_target_pressure <= 4.0 or phase_elapsed > 10.0:
            log.info("Phase → DONE")
            await finish_shot(session, reason="natural_end")


def transition_phase(new_phase: Phase):
    state.phase = new_phase
    state._phase_start_ts = time.time()
    state._channeling_detected = False
    state._choke_active = False


async def finish_shot(session, reason: str = "manual"):
    log.info("Shot finished (reason=%s, yield=%.1fml, elapsed=%.1fs)",
             reason, state.flow_total, state.elapsed)

    if session:
        await stop_script(session)
        await set_pump_pressure(session, 0.0)

    shot_record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "time_display": time.strftime("%H:%M"),
        "yield_ml": state.flow_total,
        "duration_s": state.elapsed,
        "peak_pressure": max((p["p"] for p in state._shot_curve), default=0.0),
        "target_pressure": state.target_pressure,
        "target_time": state.target_time,
        "target_temp": state.target_temp,
        "reason": reason,
        "curve": state._shot_curve,
        "chat": state.chat.to_list(),
    }
    await save_shot(shot_record)

    state.shot_active = False
    state.phase = Phase.DONE
    state._shot_curve = []

    asyncio.create_task(post_shot_reflection(shot_record))

    await asyncio.sleep(2.0)
    state.phase = Phase.IDLE


async def save_shot(shot: dict):
    try:
        DATA_DIR.mkdir(exist_ok=True)
        shots = []
        if SHOTS_FILE.exists():
            try:
                shots = json.loads(SHOTS_FILE.read_text())
            except Exception:
                shots = []
        shots.append(shot)
        shots = shots[-100:]  # keep last 100
        SHOTS_FILE.write_text(json.dumps(shots, indent=2))
        log.info("Shot saved to %s", SHOTS_FILE)
    except Exception as e:
        log.error("Failed to save shot: %s", e)


# ── WebSocket server ──────────────────────────────────────────────────────────

async def ws_handler(websocket: ServerConnection):
    connected_clients.add(websocket)
    log.info("WebSocket client connected (%d total)", len(connected_clients))

    try:
        # 1. Current sensor state
        await broadcast_sensor()

        # 2. Shot log (last 20)
        shots = []
        if SHOTS_FILE.exists():
            try:
                shots = json.loads(SHOTS_FILE.read_text())
            except Exception:
                pass
        await websocket.send(json.dumps({"type": "shot_log", "shots": shots[-20:]}))

        # 3. Config (masked)
        await websocket.send(json.dumps({"type": "config", "config": masked_config(app_config)}))

        # 4. Chat history (if shot active and chat has messages)
        if state.shot_active and state.chat.messages:
            await websocket.send(json.dumps({
                "type": "chat_history",
                "messages": state.chat.to_list(),
            }))

        # 5. Scripts list
        global _scripts_cache
        scripts = _scripts_cache
        if not scripts and _machine_session and not _demo_mode:
            scripts = await fetch_scripts_list(_machine_session)
            if scripts:
                _scripts_cache = scripts
        if scripts:
            await websocket.send(json.dumps({
                "type": "scripts_list",
                "scripts": [{"id": k, "title": v} for k, v in sorted(scripts.items())],
            }))

    except Exception as e:
        log.debug("Error sending initial state: %s", e)

    try:
        async for raw_msg in websocket:
            try:
                cmd = json.loads(raw_msg)
                await handle_command(cmd, websocket)
            except json.JSONDecodeError:
                log.warning("Received invalid JSON from client")
            except Exception as e:
                log.error("Error handling command: %s", e, exc_info=True)
                try:
                    await websocket.send(json.dumps({"type": "error", "msg": f"Server error: {e}"}))
                except Exception:
                    pass
    except websockets.exceptions.ConnectionClosedOK:
        pass
    except Exception as e:
        log.debug("WebSocket client disconnected: %s", e)
    finally:
        connected_clients.discard(websocket)
        log.info("WebSocket client disconnected (%d remaining)", len(connected_clients))


async def handle_command(cmd: dict, ws):
    global _machine_session
    action = cmd.get("cmd")
    log.info("Command: %s", action)

    if action == "start_shot":
        if state.shot_active:
            await ws.send(json.dumps({"type": "error", "msg": "Shot already active"}))
            return
        state.target_pressure = float(cmd.get("target_pressure", 9.0))
        state.target_time     = float(cmd.get("target_time", 30.0))
        state.target_temp     = float(cmd.get("target_temp", 93.0))
        now = time.time()
        _init_shot_state(now, state._prev_flow)
        log.info("Shot tracking started: target=%.1f bar, time=%.0fs, temp=%.1f°C",
                 state.target_pressure, state.target_time, state.target_temp)
        await on_shot_start()
        msg = "✅ Tracking started — now press START on the machine" if not _demo_mode else "✅ Shot started (demo mode)"
        await ws.send(json.dumps({"type": "info", "msg": msg}))

    elif action == "stop_shot":
        if state.shot_active:
            await finish_shot(_machine_session, reason="manual_stop")

    elif action == "set_pressure":
        state.target_pressure = float(cmd.get("value", state.target_pressure))
        state._current_target_pressure = state.target_pressure
        if _machine_session and state.shot_active:
            await set_pump_pressure(_machine_session, state.target_pressure)

    elif action == "set_temp":
        state.target_temp = float(cmd.get("value", state.target_temp))
        if _machine_session:
            await set_target_temp(_machine_session, state.target_temp)

    elif action == "set_mode":
        m = cmd.get("mode", "AUTO").upper()
        state.mode = Mode.AUTO if m == "AUTO" else Mode.MANUAL

    elif action == "get_shots":
        shots = []
        if SHOTS_FILE.exists():
            try:
                shots = json.loads(SHOTS_FILE.read_text())
            except Exception:
                pass
        await ws.send(json.dumps({"type": "shot_log", "shots": shots[-20:]}))

    elif action == "get_scripts":
        global _scripts_cache
        scripts = {}
        if _machine_session and not _demo_mode:
            scripts = await fetch_scripts_list(_machine_session)
            if scripts:
                _scripts_cache = scripts
        elif _scripts_cache:
            scripts = _scripts_cache
        await ws.send(json.dumps({
            "type": "scripts_list",
            "scripts": [{"id": k, "title": v} for k, v in sorted(scripts.items())],
        }))

    elif action == "execute_script":
        script_id = int(cmd.get("id", 0))
        if not script_id:
            await ws.send(json.dumps({"type": "error", "msg": "No script ID provided"}))
            return
        if _demo_mode:
            await ws.send(json.dumps({"type": "info", "msg": f"▶ Demo: would run script #{script_id}"}))
            return
        if not _machine_session:
            await ws.send(json.dumps({"type": "error", "msg": "Machine not connected"}))
            return
        title = _scripts_cache.get(script_id, f"Script #{script_id}")
        await ws.send(json.dumps({"type": "info", "msg": f"▶ Starting: {title}"}))
        ok = await execute_script(_machine_session, script_id)
        if not ok:
            await ws.send(json.dumps({"type": "error", "msg": f"Script execution failed (check machine)"}))
        else:
            await ws.send(json.dumps({"type": "info", "msg": f"✅ Script running: {title}"}))

    elif action == "chat_message":
        content = str(cmd.get("content", "")).strip()
        if content:
            asyncio.create_task(handle_user_chat(content))

    elif action == "set_config":
        new_cfg = cmd.get("config", {})
        import copy
        updated = copy.deepcopy(app_config)
        for section in ("machine", "llm"):
            if section in new_cfg and isinstance(new_cfg[section], dict):
                if section not in updated:
                    updated[section] = {}
                for k, v in new_cfg[section].items():
                    # Skip masked api_key
                    if k == "api_key" and v == "●●●●":
                        continue
                    updated[section][k] = v
        app_config.update(updated)
        save_config(app_config)
        log.info("Config updated")
        await ws.send(json.dumps({"type": "config", "config": masked_config(app_config)}))


# ── HTTP server for UI ────────────────────────────────────────────────────────

async def http_handler(request: web.Request) -> web.Response:
    path = request.path

    # ── Health check ─────────────────────────────────────────────────────────
    if path == "/health":
        return web.Response(
            text=json.dumps({
                "ok": True,
                "machine_online": state.machine_online,
                "shot_active": state.shot_active,
                "phase": state.phase.value,
                "demo": _demo_mode,
            }),
            content_type="application/json",
        )

    # ── API routes ────────────────────────────────────────────────────────────
    if path == "/api/config":
        if request.method == "GET":
            return web.Response(
                text=json.dumps(masked_config(app_config)),
                content_type="application/json",
            )
        elif request.method == "POST":
            try:
                body = await request.json()
                import copy
                updated = copy.deepcopy(app_config)
                for section in ("machine", "llm"):
                    if section in body and isinstance(body[section], dict):
                        if section not in updated:
                            updated[section] = {}
                        for k, v in body[section].items():
                            if k == "api_key" and v == "●●●●":
                                continue
                            updated[section][k] = v
                app_config.update(updated)
                save_config(app_config)
                return web.Response(
                    text=json.dumps({"ok": True, "config": masked_config(app_config)}),
                    content_type="application/json",
                )
            except Exception as e:
                return web.Response(status=400, text=json.dumps({"error": str(e)}), content_type="application/json")

    if path == "/api/shots":
        shots = []
        if SHOTS_FILE.exists():
            try:
                shots = json.loads(SHOTS_FILE.read_text())
            except Exception:
                pass
        return web.Response(text=json.dumps(shots), content_type="application/json")

    # ── Static files ──────────────────────────────────────────────────────────
    rel = path.lstrip("/") or "index.html"
    file_path = UI_DIR / rel
    if not file_path.exists() or not file_path.is_file():
        return web.Response(status=404, text="Not found")
    content_types = {
        ".html": "text/html",
        ".js":   "application/javascript",
        ".css":  "text/css",
        ".json": "application/json",
    }
    ct = content_types.get(file_path.suffix, "application/octet-stream")
    return web.Response(body=file_path.read_bytes(), content_type=ct)


# ── Demo / simulation mode ────────────────────────────────────────────────────

class DemoSimulator:
    """Simulates a realistic espresso shot for UI testing."""

    def __init__(self):
        self._t0 = None
        self._cumulative_flow = 0.0
        self._last_tick = time.time()
        self._bg_temp = 93.2
        self._bb_temp = 93.0
        self._noise = lambda: random.gauss(0, 0.05)

    def tick(self, st: ShotState) -> dict:
        now = time.time()
        dt = now - self._last_tick
        self._last_tick = now

        if st.shot_active:
            elapsed = now - st.shot_start_ts
            pressure = self._sim_pressure(elapsed, st)
            flow_rate = self._sim_flow(elapsed, pressure)
            self._cumulative_flow += flow_rate * dt
        else:
            pressure = 0.0
            flow_rate = 0.0
            self._cumulative_flow = 0.0

        self._bg_temp += random.gauss(0, 0.01)
        self._bb_temp += random.gauss(0, 0.01)
        self._bg_temp = max(91.0, min(95.0, self._bg_temp))
        self._bb_temp = max(91.0, min(95.0, self._bb_temp))

        return {
            "PU_SENS_PRESS":         round(pressure + self._noise(), 2),
            "PU_SENS_FLOW_METER_ML": round(self._cumulative_flow, 2),
            "BG_SENS_TEMP_A":        round(self._bg_temp, 1),
            "BB_SENS_TEMP_A":        round(self._bb_temp, 1),
            "BG_LEVEL_PW_CONTROL":   random.randint(0, 500),
            "BB_LEVEL_PW_CONTROL":   random.randint(0, 500),
            "MA_STATUS":             1 if st.shot_active else 0,
            "MA_LAST_EXTRACTION_ML": str(round(self._cumulative_flow, 1)),
        }

    def _sim_pressure(self, elapsed: float, st: ShotState) -> float:
        target = st.target_pressure
        if elapsed < 8:
            p = 3.5
        elif elapsed < 18:
            p = 3.5 + (target - 3.5) * (elapsed - 8) / 10
        elif elapsed < 45:
            p = target + math.sin(elapsed * 0.3) * 0.2
        else:
            p = max(0.0, target - (elapsed - 45) * 0.3)
        return max(0.0, p)

    def _sim_flow(self, elapsed: float, pressure: float) -> float:
        if elapsed < 5:
            return 0.0
        elif elapsed < 8:
            return max(0.0, (elapsed - 5) * 0.2)
        elif elapsed < 18:
            return max(0.0, (elapsed - 8) * 0.15)
        elif elapsed < 45:
            return 1.6 + math.sin(elapsed * 0.2) * 0.2 + random.gauss(0, 0.05)
        else:
            return max(0.0, 1.8 - (elapsed - 45) * 0.15)


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_time(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


# ── Entry point ───────────────────────────────────────────────────────────────

_machine_session: Optional[aiohttp.ClientSession] = None
_demo_mode: bool = False


async def main(demo: bool = False):
    global _machine_session, _demo_mode, app_config
    _demo_mode = demo

    DATA_DIR.mkdir(exist_ok=True)
    if not SHOTS_FILE.exists():
        SHOTS_FILE.write_text("[]")

    app_config = load_config()
    log.info("Config loaded: machine=%s, llm_model=%s",
             app_config["machine"]["host"], app_config["llm"]["model"])

    if demo:
        log.info("🎭 DEMO MODE — no machine connection")
        _machine_session = None
    else:
        log.info("🔌 Connecting to machine at %s", app_config["machine"]["host"])
        _machine_session = aiohttp.ClientSession()

    ws_server = await ws_serve(ws_handler, "0.0.0.0", WS_PORT)
    log.info("WebSocket server on ws://localhost:%d", WS_PORT)

    app = web.Application()
    app.router.add_get("/health", http_handler)
    app.router.add_route("*", "/api/config", http_handler)
    app.router.add_get("/api/shots", http_handler)
    app.router.add_get("/{path:.*}", http_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    log.info("UI server on http://localhost:%d", HTTP_PORT)
    log.info("Open http://localhost:%d in your browser", HTTP_PORT)

    try:
        await control_loop(_machine_session)
    finally:
        ws_server.close()
        await ws_server.wait_closed()
        await runner.cleanup()
        if _machine_session:
            await _machine_session.close()


if __name__ == "__main__":
    demo_mode = "--demo" in sys.argv
    try:
        asyncio.run(main(demo=demo_mode))
    except KeyboardInterrupt:
        log.info("Shutdown requested")
