#!/usr/bin/env python3
"""music-bot: listens together with you in a music-together room.

Joins the room as a Socket.IO client, watches playback, downloads the
current track once, splits it into segments in memory, and asks Gemini to
listen to each segment *in sync with playback* — analysis is gated on the
playing state, so pausing the song pauses the bot's listening too.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import socketio
from curl_cffi import requests as crequests

try:
    from .audio_split import split_mp3_seconds
except ImportError:
    from audio_split import split_mp3_seconds

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
log = logging.getLogger("music-bot")

BASE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("MUSIC_BOT_CONFIG", BASE / "config.json"))
STATE_PATH = Path(os.environ.get("MUSIC_BOT_STATE", BASE / "state.json"))

# ---------------------------------------------------------------------------
# config / state / identity
# ---------------------------------------------------------------------------

def load_json(path: Path, default: dict) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("%s is invalid, using defaults", path.name)
    return default


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


CFG = load_json(CONFIG_PATH, {})
STATE = load_json(STATE_PATH, {})

SERVER = CFG.get("server_url", "")
NICKNAME = CFG.get("nickname", "小听")
ROOM_ID_CFG = str(CFG.get("room_id", "") or "")
IDENTITY_SECRET = str(CFG.get("identity_secret", ""))
GEMINI_ENDPOINT = CFG.get(
    "gemini_endpoint",
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
)
GEMINI_KEY = str(CFG.get("gemini_key", ""))
GEMINI_MODEL = CFG.get("gemini_model", "gemini-2.5-flash")
PROXY = str(CFG.get("proxy", ""))
MAX_REACTIONS = int(CFG.get("max_reactions", 4))
SEGMENT_SECONDS = float(CFG.get("segment_seconds", 45))
ANALYZE_LEAD_SECONDS = float(CFG.get("analyze_lead_seconds", 20))
PERSONA = CFG.get(
    "persona",
    "你叫小听。语气简短、口语化，像和用户一起听歌时随口点评。不要编造听不到的内容。",
)

PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None

ASTRBOT_URL = str(CFG.get("astrbot_chat_url", "http://127.0.0.1:6185/api/v1/chat"))
ASTRBOT_KEY = str(CFG.get("astrbot_api_key", ""))
ASTRBOT_SESSION = str(CFG.get("astrbot_session", "music-bot"))
ASTRBOT_USER = str(CFG.get("astrbot_username", "music-bot"))
ASTRBOT_PROVIDER = str(CFG.get("astrbot_provider", ""))


def issue_identity_token(secret: str, uid: str = "music-bot-01") -> str:
    """Self-sign an mt_identity cookie (same HMAC scheme as the server)."""
    now_ms = int(time.time() * 1000)
    payload = {"uid": uid, "iat": now_ms, "exp": now_ms + 30 * 86400 * 1000, "ver": 1}
    unsigned = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), unsigned.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")
    return f"{unsigned}.{sig}"


IDENTITY_COOKIE = "mt_identity=" + issue_identity_token(IDENTITY_SECRET) if IDENTITY_SECRET else ""


def set_state_path(path: Path) -> None:
    """Move the state file (e.g. into the AstrBot plugin data dir) and reload it."""
    global STATE_PATH, STATE
    if STATE:
        return  # already joined a room with the current state; keep it
    STATE_PATH = Path(path)
    STATE = load_json(STATE_PATH, {})


def apply_config(config: dict) -> None:
    """Apply AstrBot/plugin configuration without re-importing this module."""
    global CFG, SERVER, NICKNAME, ROOM_ID_CFG, IDENTITY_SECRET
    global GEMINI_ENDPOINT, GEMINI_KEY, GEMINI_MODEL, PROXY, PROXIES
    global MAX_REACTIONS, SEGMENT_SECONDS, ANALYZE_LEAD_SECONDS, PERSONA
    global ASTRBOT_URL, ASTRBOT_KEY, ASTRBOT_SESSION, ASTRBOT_USER, ASTRBOT_PROVIDER
    global REPLY_ALL_CHAT, IDENTITY_COOKIE

    CFG = dict(config or {})
    SERVER = str(CFG.get("server_url", "") or "").strip().rstrip("/")
    NICKNAME = str(CFG.get("nickname", "小听") or "小听")[:40]
    ROOM_ID_CFG = str(CFG.get("room_id", "") or "")
    IDENTITY_SECRET = str(CFG.get("identity_secret", "") or "")
    GEMINI_ENDPOINT = str(CFG.get("gemini_endpoint", "") or "").strip()
    GEMINI_KEY = str(CFG.get("gemini_key", "") or "")
    GEMINI_MODEL = str(CFG.get("gemini_model", "gemini-2.5-flash") or "gemini-2.5-flash")
    PROXY = str(CFG.get("proxy", "") or "").strip()
    PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None
    MAX_REACTIONS = max(1, min(int(CFG.get("max_reactions", 4)), 12))
    SEGMENT_SECONDS = max(10.0, min(float(CFG.get("segment_seconds", 45)), 180.0))
    ANALYZE_LEAD_SECONDS = max(0.0, min(float(CFG.get("analyze_lead_seconds", 20)), 60.0))
    PERSONA = str(CFG.get("persona", PERSONA) or PERSONA)[:2000]
    ASTRBOT_URL = str(CFG.get("astrbot_chat_url", "http://127.0.0.1:6185/api/v1/chat") or "").strip()
    ASTRBOT_KEY = str(CFG.get("astrbot_api_key", "") or "")
    ASTRBOT_SESSION = str(CFG.get("astrbot_session", "music-bot") or "music-bot")
    ASTRBOT_USER = str(CFG.get("astrbot_username", "music-bot") or "music-bot")
    ASTRBOT_PROVIDER = str(CFG.get("astrbot_provider", "") or "")
    REPLY_ALL_CHAT = bool(CFG.get("reply_all_chat", True))
    IDENTITY_COOKIE = "mt_identity=" + issue_identity_token(IDENTITY_SECRET) if IDENTITY_SECRET else ""


def gemini_url() -> str:
    return GEMINI_ENDPOINT.replace("{model}", GEMINI_MODEL)

# ---------------------------------------------------------------------------
# relay Gemini (native generateContent, chrome TLS fingerprint, via mihomo)
# ---------------------------------------------------------------------------

def gemini_generate(parts: list[dict], thinking: bool = True,
                    attempts: int = 3, timeout: int = 120) -> dict | None:
    """Call native generateContent. thinking=False = fast mode (reactions).

    Sync function (callers wrap in to_thread). Retries transient connection
    resets; every attempt is hard-deadlined so a hung call can't stall.
    """
    gen_cfg: dict = {"response_mime_type": "application/json"}
    if not thinking:
        gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": gen_cfg,
    }
    for attempt in range(1, attempts + 1):
        t0 = time.time()
        try:
            r = crequests.post(
                gemini_url(),
                json=body,
                headers={"x-goog-api-key": GEMINI_KEY},
                proxies=PROXIES,
                impersonate="chrome",
                timeout=timeout,
            )
            if r.status_code != 200:
                log.warning("gemini http %s: %s", r.status_code, r.text[:200])
                if r.status_code not in (408, 425, 429) and r.status_code < 500:
                    return None
                if attempt < attempts:
                    time.sleep(2)
                    continue
                return None
            parts_out = r.json()["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts_out)
            m = re.search(r"\{[\s\S]*\}", text)
            log.info("gemini ok in %.0fs", time.time() - t0)
            result = json.loads(m.group(0)) if m else None
            if result is None and attempt < attempts:
                time.sleep(2)
                continue
            return result
        except Exception as exc:
            log.warning("gemini attempt %s/%s failed after %.0fs: %s",
                        attempt, attempts, time.time() - t0, str(exc)[:120])
            if attempt < attempts:
                time.sleep(2)
    return None


def gemini_text(prompt: str, timeout: int = 60) -> str | None:
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        r = crequests.post(
            gemini_url(),
            json=body,
            headers={"x-goog-api-key": GEMINI_KEY},
            proxies=PROXIES,
            impersonate="chrome",
            timeout=timeout,
        )
        if r.status_code != 200:
            log.warning("gemini text http %s", r.status_code)
            return None
        parts_out = r.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts_out).strip()
    except (KeyError, IndexError, TypeError, json.JSONDecodeError, OSError) as exc:
        log.warning("gemini text failed: %s", str(exc)[:120])
        return None


def astrbot_chat(context_text: str, timeout: int = 90) -> str | None:
    """Ask AstrBot (full pipeline: Tauru persona, memory, companion plugins)."""
    if not ASTRBOT_KEY:
        return None
    body = {
        "message": context_text,
        "session_id": ASTRBOT_SESSION,
        "username": ASTRBOT_USER,
        "selected_provider": ASTRBOT_PROVIDER,
    }
    try:
        r = crequests.post(
            ASTRBOT_URL,
            json=body,
            headers={"Authorization": "Bearer " + ASTRBOT_KEY},
            timeout=timeout,
        )
        if r.status_code != 200:
            log.warning("astrbot chat http %s", r.status_code)
            return None
        texts = []
        for line in r.text.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if event.get("type") == "plain":
                texts.append(str(event.get("data", "")))
        reply = "".join(texts).strip()
        return reply or None
    except Exception as exc:
        log.warning("astrbot chat failed: %s", exc)
        return None

# ---------------------------------------------------------------------------
# music-together REST helpers (identity cookie auth)
# ---------------------------------------------------------------------------

def rest_get(path: str, params: dict) -> dict | None:
    if not IDENTITY_COOKIE:
        log.warning("REST %s skipped: identity cookie not configured", path)
        return None
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = SERVER.rstrip("/") + path + (("?" + query) if query else "")
    try:
        r = crequests.get(
            url, cookies={"mt_identity": IDENTITY_COOKIE.split("=", 1)[1]},
            proxies=PROXIES, impersonate="chrome", timeout=30,
        )
        if r.status_code == 200:
            return r.json()
        log.warning("REST %s -> %s %s", path, r.status_code, r.text[:120])
    except Exception as exc:
        log.warning("REST %s failed: %s", path, exc)
    return None

# ---------------------------------------------------------------------------
# audio / prompts
# ---------------------------------------------------------------------------

def duration_of(track: dict) -> float:
    try:
        return float(track.get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


async def download_audio(track: dict) -> tuple[bytes | None, str]:
    url = track.get("streamUrl")
    source = track.get("source", "netease")
    if not url:
        data = rest_get("/api/music/url", {
            "source": source,
            "urlId": track.get("urlId"),
            "bitrate": 192,
        })
        if not data or not data.get("url"):
            return None, ""
        url = data["url"]
    parsed = urlparse(str(url))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        log.warning("audio URL has unsupported scheme: %s", str(url)[:120])
        return None, ""
    mime = "audio/mp3"
    if ".m4a" in url or "m4a" in str(track.get("urlId", "")):
        mime = "audio/mp4"
    try:
        r = await asyncio.to_thread(
            lambda: crequests.get(
                url, proxies=PROXIES, impersonate="chrome",
                timeout=60, allow_redirects=True,
            )
        )
        if r.status_code == 200 and len(r.content) > 10000:
            log.info("audio downloaded: %.1f KB (%s)", len(r.content) / 1024, mime)
            return r.content, mime
        log.warning("audio download bad: %s %s bytes", r.status_code, len(r.content))
    except Exception as exc:
        log.warning("audio download failed: %s", exc)
    return None, ""


def segment_prompt(track: dict, seg_start: float, seg_end: float, total: float,
                   has_audio: bool, earlier: list[str]) -> str:
    title = track.get("title", "")
    artist = "/".join(track.get("artist") or [])
    pos_note = f"目前已播到第 {int(seg_start)} 秒。" if seg_start else "这首歌刚开始放。"
    audio_line = (
        f"附上的是这首歌第 {int(seg_start)}~{int(seg_end)} 秒的片段，请边听边反应。"
        if has_audio else
        "你拿不到这段音频，只能根据歌名、歌手和已有笔记推测此刻的听感，不确定的不要编造。"
    )
    earlier_note = (
        f"你之前已经说过：{'；'.join(earlier[-3:])}。不要重复。"
        if earlier else "这是你今天的第一句反应。"
    )
    return (
        f"{PERSONA}\n"
        f"正在和用户一起听：{title} - {artist}（全曲 {int(total)} 秒）。{pos_note}\n"
        f"{audio_line}\n{earlier_note}\n"
        "输出严格 JSON，不要 Markdown：\n"
        '{"reactions":[{"at":秒,"text":"反应"}]}\n'
        "at 是相对这个片段开头的秒数；最多 3 条，片头 2 秒内不要有；"
        "每条不超过 20 个字，短、平、直，像随口嘀咕。没有值得说的就给空数组。"
    )


def clamp_segment_reactions(parsed: dict, seg_start: float, seg_len: float,
                            session: "ListenSession") -> list[dict]:
    reactions = parsed.get("reactions") or []
    out: list[dict] = []
    for item in reactions:
        try:
            local_at = float(item.get("at", 0))
            text = str(item.get("text", "")).strip()
        except (TypeError, ValueError):
            continue
        if not text or len(text) > 40:
            continue
        at = seg_start + max(1.0, min(local_at, seg_len - 1))
        pos = session.position()
        if at <= pos + 1.0:
            continue  # already past — never burst old reactions
        if session.duration and at > session.duration - 3:
            continue
        out.append({"at": round(at, 1), "text": text})
        if len(out) >= 3:
            break
    return out

# ---------------------------------------------------------------------------
# listening session
# ---------------------------------------------------------------------------

class ListenSession:
    """One track: playback anchor + reactions posted as the song gets there."""

    def __init__(self, track: dict, play_state: dict):
        self.track = track
        self.anchor_time = float(play_state.get("currentTime", 0))
        self.anchor_ts = float(play_state.get("serverTimestamp", time.time() * 1000))
        self.paused = not play_state.get("isPlaying", False)
        self.duration = duration_of(track)
        self.notes: list[str] = []
        self.task: asyncio.Task | None = None

    def position(self) -> float:
        if self.paused:
            return self.anchor_time
        return self.anchor_time + max(0.0, time.time() * 1000 - self.anchor_ts) / 1000

    def pause(self, play_state: dict) -> None:
        self.anchor_time = float(play_state.get("currentTime", self.position()))
        self.paused = True

    def resume(self, play_state: dict) -> None:
        self.anchor_time = float(play_state.get("currentTime", self.anchor_time))
        self.anchor_ts = float(play_state.get("serverTimestamp", time.time() * 1000))
        self.paused = False

    def seek(self, play_state: dict) -> None:
        self.anchor_time = float(play_state.get("currentTime", 0))
        self.anchor_ts = float(play_state.get("serverTimestamp", time.time() * 1000))


SESSION: ListenSession | None = None
LAST_REVISION = -1
LAST_CHAT_REPLY = 0.0
REPLY_ALL_CHAT = bool(CFG.get("reply_all_chat", True))

# ---------------------------------------------------------------------------

sio = socketio.AsyncClient()


async def post_chat(text: str) -> None:
    if not text or not sio.connected:
        return
    log.info("chat -> %s", text[:80])
    await sio.emit("chat:message", {"content": text[:200]})


async def wait_until(session: ListenSession, target: float) -> bool:
    """Sleep until playback reaches `target`. False if the song ended first."""
    while True:
        if session.paused:
            await asyncio.sleep(1.0)
            continue
        pos = session.position()
        if session.duration and pos >= session.duration - 1.0:
            return False
        if pos >= target:
            return True
        await asyncio.sleep(0.5)


async def listen_along(track: dict) -> None:
    session = SESSION
    if not session:
        return
    title = track.get("title", "?")
    artist = "/".join(track.get("artist") or [])
    log.info("listening along: %s - %s", title, artist)

    audio, mime = await download_audio(track)
    segments: list[tuple[float, bytes]] = []
    if audio and mime == "audio/mp3":
        segments = await asyncio.to_thread(
            split_mp3_seconds, audio, SEGMENT_SECONDS, session.duration
        )
        log.info("split into %d segments", len(segments))
    elif audio:
        log.info("audio is %s; cannot split, knowledge mode", mime)

    await post_chat(f"♪ 《{title}》，一起听。")

    earlier: list[str] = []
    for seg_start, seg_bytes in segments:
        seg_end = min(seg_start + SEGMENT_SECONDS,
                      session.duration or seg_start + SEGMENT_SECONDS)
        pos = session.position()
        if pos >= seg_end - 1:
            continue  # already played past this segment

        # Sync with playback: start "listening" to this segment a little
        # before it begins, and never while the user has it paused.
        lead_target = max(1.0, seg_start - ANALYZE_LEAD_SECONDS)
        if seg_start > 0:
            await wait_until(session, lead_target)
        while session.paused:
            await asyncio.sleep(1.0)
        if sio.connected is False:
            return

        prompt = segment_prompt(track, seg_start, seg_end,
                                session.duration, True, earlier)
        parts = [{"text": prompt},
                 {"inline_data": {"mime_type": "audio/mp3",
                                  "data": base64.b64encode(seg_bytes).decode()}}]
        log.info("analyzing segment %s-%ss...", int(seg_start), int(seg_end))
        try:
            parsed = await asyncio.wait_for(
                asyncio.to_thread(gemini_generate, parts, False), timeout=160)
        except asyncio.TimeoutError:
            log.warning("seg %ss: analysis timed out, skipping", int(seg_start))
            parsed = None
        reactions = clamp_segment_reactions(parsed, seg_start, SEGMENT_SECONDS, session) \
            if parsed else []
        if not reactions:
            log.info("seg %ss: nothing to say", int(seg_start))
            continue

        for r in reactions:
            if not await wait_until(session, r["at"]):
                break
            await post_chat(r["text"])
            earlier.append(r["text"])
            session.notes.append(f"{int(r['at'])}s {r['text']}")

    # knowledge-mode fallback when audio was unavailable or unsplittable
    if not segments:
        prompt = segment_prompt(track, 0, session.duration or 240,
                                session.duration, False, [])
        parsed = await asyncio.to_thread(gemini_generate, [{"text": prompt}], True)
        reactions = parsed.get("reactions") or [] if parsed else []
        cleaned = []
        for item in reactions[:MAX_REACTIONS]:
            try:
                at = max(3.0, float(item.get("at", 0)))
                text = str(item.get("text", "")).strip()
            except (TypeError, ValueError):
                continue
            if text and len(text) <= 40:
                cleaned.append({"at": at, "text": text})
        for r in sorted(cleaned, key=lambda x: x["at"]):
            if not await wait_until(session, r["at"]):
                break
            await post_chat(r["text"])
            session.notes.append(f"{int(r['at'])}s {r['text']}")

    # wait for natural end, then let AstrBot close it out in Tauru's voice
    if session.duration:
        await wait_until(session, session.duration - 1.0)
    if session.notes:
        notes = "；".join(session.notes[-4:])
        reply = await asyncio.to_thread(
            astrbot_chat,
            f"（场景：我们刚一起听完《{title}》-{artist}。我边听边说的：{notes[:180]}。"
            "用你自己的风格给这段听歌收个尾，一句话，不超过 40 字。）")
        await post_chat(("🎵 " + reply[:120]) if reply else "🎵 听完了。")
    else:
        await post_chat("🎵 听完了。")


def start_listen(track: dict, play_state: dict) -> None:
    global SESSION
    stop_listen()
    SESSION = ListenSession(track, play_state)
    SESSION.task = asyncio.create_task(listen_along(track))


def stop_listen() -> None:
    global SESSION
    if SESSION and SESSION.task:
        SESSION.task.cancel()
    SESSION = None


async def shutdown() -> None:
    """Stop active work and release the Socket.IO connection."""
    stop_listen()
    if sio.connected:
        await sio.disconnect()

# ---------------------------------------------------------------------------
# socket wiring
# ---------------------------------------------------------------------------

ROOM_ID: str = ""


@sio.event
async def connect() -> None:
    log.info("connected to %s", SERVER)
    room = ROOM_ID_CFG or STATE.get("room_id", "")
    if room:
        join_payload: dict = {"roomId": room, "nickname": NICKNAME}
        rejoin = str(STATE.get("rejoin_token", "") or "")
        if rejoin and STATE.get("room_id") == room:
            join_payload["rejoinToken"] = rejoin
        await sio.emit("room:join", join_payload)
    else:
        await sio.emit("room:list")


@sio.event
async def disconnect() -> None:
    log.warning("disconnected; will retry")


@sio.on("room:created")
async def on_created(data: dict) -> None:
    STATE["room_id"] = data["roomId"]
    save_json(STATE_PATH, STATE)
    log.info("room created: %s", data["roomId"])


@sio.on("room:rejoin_token")
async def on_rejoin(data: dict) -> None:
    STATE["room_id"] = data["roomId"]
    STATE["rejoin_token"] = data.get("token", "")
    save_json(STATE_PATH, STATE)


@sio.on("room:state")
async def on_state(room: dict) -> None:
    global ROOM_ID
    ROOM_ID = room.get("id", ROOM_ID)
    STATE["room_id"] = ROOM_ID
    save_json(STATE_PATH, STATE)
    log.info("in room %s (%s), users=%s", ROOM_ID, room.get("name"), len(room.get("users") or []))
    current = room.get("currentTrack")
    play = room.get("playState") or {}
    if current and play:
        start_listen(current, play)


@sio.on("room:list_update")
async def on_rooms(rooms: list) -> None:
    if ROOM_ID or ROOM_ID_CFG:
        return
    if rooms:
        target = rooms[0]["id"]
        log.info("joining existing room %s", target)
        await sio.emit("room:join", {"roomId": target, "nickname": NICKNAME})
    elif CFG.get("create_if_missing", True):
        log.info("no rooms; creating one")
        await sio.emit("room:create", {"nickname": NICKNAME, "roomName": "一起听歌"})


@sio.on("player:play")
async def on_play(data: dict) -> None:
    track = (data or {}).get("track")
    play = (data or {}).get("playState") or {}
    if not track:
        return
    global LAST_REVISION
    revision = play.get("revision")
    if revision is not None and revision == LAST_REVISION:
        return
    LAST_REVISION = revision if revision is not None else LAST_REVISION
    start_listen(track, play)


@sio.on("player:pause")
async def on_pause(data: dict) -> None:
    if SESSION:
        SESSION.pause(data or {})
        log.info("playback paused; bot pauses listening too")


@sio.on("player:resume")
async def on_resume(data: dict) -> None:
    if SESSION:
        SESSION.resume(data or {})
        log.info("playback resumed")


@sio.on("player:seek")
async def on_seek(data: dict) -> None:
    if SESSION:
        SESSION.seek(data or {})


@sio.on("player:next")
async def on_next(_data: dict) -> None:
    stop_listen()


@sio.on("chat:message")
async def on_chat(message: dict) -> None:
    content = str(message.get("content", ""))
    user = str(message.get("nickname", ""))
    msg_type = str(message.get("type", "user"))
    log.info("chat <- %s: %s", user or "?", content[:60])
    if msg_type == "system":
        return
    if user == NICKNAME or not content:
        return
    if not REPLY_ALL_CHAT and NICKNAME not in content \
            and not content.startswith("@" + NICKNAME):
        return
    global LAST_CHAT_REPLY
    now = time.time()
    if now - LAST_CHAT_REPLY < 8:
        return  # avoid rapid-fire loops
    LAST_CHAT_REPLY = now
    if SESSION is None:
        return
    clean = content.replace(NICKNAME, "").strip()
    if not clean:
        return
    title = SESSION.track.get("title", "")
    artist = "/".join(SESSION.track.get("artist") or [])
    pos = int(SESSION.position())
    reply = await asyncio.to_thread(
        astrbot_chat,
        f"（场景：我们正在房间一起听《{title}》-{artist}，播放到第 {pos} 秒。"
        f"用户刚刚在房间聊天里对你说：“{clean[:100]}”。"
        "结合此刻的听感自然回应，口语化，不要超过 50 字。）",
    )
    if not reply:
        reply = await asyncio.to_thread(
            gemini_text,
            f"{PERSONA}\n你们正在一起听《{title}》-{artist}，播放到第 {pos} 秒。"
            f"用户说：“{clean[:100]}”。简短回应，不超过 50 字，只输出回应本身。")
    if reply:
        await post_chat(reply[:120])

# ---------------------------------------------------------------------------

async def main() -> None:
    if not SERVER:
        raise SystemExit("server_url missing in music-bot configuration")
    if not IDENTITY_SECRET:
        raise SystemExit("identity_secret missing in music-bot configuration")
    if not GEMINI_ENDPOINT or not GEMINI_KEY:
        raise SystemExit("gemini_endpoint and gemini_key are required")
    while not sio.connected:
        try:
            await sio.connect(
                SERVER,
                headers={"Cookie": IDENTITY_COOKIE},
                socketio_path="socket.io",
            )
        except Exception as exc:
            log.warning("connect failed: %s; retry in 10s", exc)
            await asyncio.sleep(10)
    await sio.wait()


if __name__ == "__main__":
    asyncio.run(main())
