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
SEGMENT_SECONDS = float(CFG.get("segment_seconds", 30))
ANALYZE_LEAD_SECONDS = float(CFG.get("analyze_lead_seconds", 25))
PERSONA = CFG.get(
    "persona",
    "你叫小听。语气简短、口语化，像和用户一起听歌时随口点评。不要编造听不到的内容。",
)

PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None

# Where room chat gets injected. Two shapes:
#   QQ session  "Tauru:FriendMessage:1125961157" → aiocqhttp platform bucket
#   anything else (e.g. "music-room")           → webchat adapter bucket
CHAT_SESSION_ID = str(CFG.get("chat_session_id", "music-room"))
CHAT_USER = str(CFG.get("chat_user", "music-room"))
CHAT_PLATFORM_ID = str(CFG.get("chat_platform_id", "Tauru"))

_CONTEXT = None


def bind_context(context) -> None:
    """Capture the running AstrBot Context for native event injection."""
    global _CONTEXT
    _CONTEXT = context


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
    global CHAT_SESSION_ID, CHAT_USER, CHAT_PLATFORM_ID
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
    SEGMENT_SECONDS = max(10.0, min(float(CFG.get("segment_seconds", 30)), 180.0))
    ANALYZE_LEAD_SECONDS = max(0.0, min(float(CFG.get("analyze_lead_seconds", 25)), 60.0))
    PERSONA = str(CFG.get("persona", PERSONA) or PERSONA)[:2000]
    CHAT_SESSION_ID = str(CFG.get("chat_session_id", "music-room") or "music-room")
    CHAT_USER = str(CFG.get("chat_user", "music-room") or "music-room")
    CHAT_PLATFORM_ID = str(CFG.get("chat_platform_id", "Tauru") or "Tauru")
    REPLY_ALL_CHAT = bool(CFG.get("reply_all_chat", True))
    IDENTITY_COOKIE = "mt_identity=" + issue_identity_token(IDENTITY_SECRET) if IDENTITY_SECRET else ""


def gemini_url() -> str:
    return GEMINI_ENDPOINT.replace("{model}", GEMINI_MODEL)

# ---------------------------------------------------------------------------
# relay Gemini (native generateContent, chrome TLS fingerprint, via mihomo)
# ---------------------------------------------------------------------------

# Gemini 3.x rejects thinkingBudget (400); older models reject thinkingLevel.
# Remember which one the endpoint accepts so we only probe once.
THINKING_STYLE: str | None = None  # None = unknown, "level" or "budget"


def parse_model_json(text: str) -> dict | None:
    """Tolerantly extract the first JSON object from model output.

    Handles Markdown fences, trailing commas, and full-width quotes that
    Gemini occasionally emits despite response_mime_type=application/json.
    """
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    raw = m.group(0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    fixed = raw.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    fixed = re.sub(r",(\s*[}\]])", r"\1", fixed)  # trailing commas
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        return None


def _thinking_cfg(style: str | None) -> dict:
    if style == "level":
        return {"thinkingLevel": "low"}
    return {"thinkingBudget": 0}


def gemini_proxies() -> dict | None:
    """Bypass the proxy for localhost endpoints and the kdysite relay —
    ~1MB audio uploads through a proxy time out, and kdysite is directly
    reachable (it sits behind Cloudflare, hence the chrome fingerprint)."""
    ep = GEMINI_ENDPOINT.lower()
    if "kdysite" in ep or "127.0.0.1" in ep or "localhost" in ep:
        return None
    return PROXIES


def gemini_generate(parts: list[dict], thinking: bool = True,
                    attempts: int = 3, timeout: int = 120) -> dict | None:
    """Call native generateContent. thinking=False = fast mode (reactions).

    Sync function (callers wrap in to_thread). Retries transient connection
    resets; every attempt is hard-deadlined so a hung call can't stall.
    """
    global THINKING_STYLE
    for attempt in range(1, attempts + 1):
        gen_cfg: dict = {"response_mime_type": "application/json"}
        if not thinking:
            if THINKING_STYLE is None:
                THINKING_STYLE = "level"  # Gemini 3.x dialect; older models 400 and we switch
            gen_cfg["thinkingConfig"] = _thinking_cfg(THINKING_STYLE)
        body = {
            "contents": [{"parts": parts}],
            "generationConfig": gen_cfg,
        }
        t0 = time.time()
        try:
            r = crequests.post(
                gemini_url(),
                json=body,
                headers={"x-goog-api-key": GEMINI_KEY},
                proxies=gemini_proxies(),
                impersonate="chrome",
                timeout=timeout,
            )
            if r.status_code == 400 and not thinking and "thinking" in r.text.lower():
                # wrong thinking-config dialect for this model — switch and retry
                THINKING_STYLE = "budget" if THINKING_STYLE != "budget" else "level"
                log.info("gemini thinking dialect switched to %s", THINKING_STYLE)
                continue
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
            result = parse_model_json(text)
            log.info("gemini ok in %.0fs", time.time() - t0)
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
            proxies=gemini_proxies(),
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


# ---------------------------------------------------------------------------
# chat injection through AstrBot's native pipeline
# ---------------------------------------------------------------------------
#
# Room chat is wrapped as a platform event and committed to the pipeline so
# replies and memories land in a real session bucket. CHAT_SESSION_ID picks
# the bucket:
#   QQ form  ("Tauru:FriendMessage:1125961157") → aiocqhttp platform, the
#     original session string is used as-is so memories land in the owner's
#     real conversation bucket;
#   otherwise → webchat adapter, session webchat!{CHAT_USER}!{it}.
#
# Reply capture differs by platform:
#   webchat: reply flows into webchat_queue_mgr back-queue keyed by the
#     injected message_id (WebChatMessageEvent.send writes there);
#   aiocqhttp (QQ): the adapter delivers the reply to QQ directly, so we
#     capture it by wrapping the event class's send path once.


async def inject_room_chat(user: str, content: str) -> list[str]:
    """Inject one room chat message into the pipeline; collect the reply."""
    context = _CONTEXT
    if context is None:
        return []
    try:
        from astrbot.core.message.message_event_result import MessageChain
        from astrbot.core.platform import AstrBotMessage, MessageMember, MessageType
        from astrbot.api.message_components import Plain

        message_id = f"mt_{int(time.time() * 1000)}_{id(content) % 10000}"
        use_qq = "FriendMessage" in CHAT_SESSION_ID and "webchat" not in CHAT_SESSION_ID

        reply_texts: list[str] = []
        if use_qq:
            platform = context.get_platform_inst(CHAT_PLATFORM_ID)
            if platform is None:
                log.warning("platform %s not available (offline?); skipping chat injection",
                            CHAT_PLATFORM_ID)
                return []
            session_id = CHAT_SESSION_ID.split(":")[-1]
            self_id = str(getattr(platform, "_self_id", "") or "")
            capture = _install_qq_send_capture()
        else:
            from astrbot.core.platform.sources.webchat.webchat_queue_mgr import (
                webchat_queue_mgr,
            )
            platform = context.get_platform_inst("webchat")
            if platform is None:
                log.warning("webchat platform not found; skipping chat injection")
                return []
            session_id = f"webchat!{CHAT_USER}!{CHAT_SESSION_ID}"
            self_id = "webchat"
            reply_queue = webchat_queue_mgr.get_or_create_back_queue(
                message_id, CHAT_SESSION_ID
            )

        try:
            abm = AstrBotMessage()
            abm.self_id = self_id
            abm.sender = MessageMember(
                session_id if use_qq else CHAT_USER, user or "room_user"
            )
            abm.type = MessageType.FRIEND_MESSAGE
            abm.session_id = session_id
            abm.message_id = message_id
            abm.message = MessageChain(chain=[Plain(content)])
            abm.message_str = content
            abm.raw_message = ("music-bot", CHAT_USER, CHAT_SESSION_ID)
            abm.timestamp = int(time.time())

            if use_qq and capture is not None:
                capture.queue = reply_texts

            await platform.handle_msg(abm)
            log.info("injected room chat: %s: %s", user, content[:60])

            if use_qq:
                deadline = time.time() + 30.0
                while time.time() < deadline and not reply_texts:
                    await asyncio.sleep(1.0)
                return reply_texts
            replies: list[str] = []
            deadline = time.time() + 30.0
            while time.time() < deadline:
                try:
                    item = await asyncio.wait_for(reply_queue.get(), timeout=3.0)
                except asyncio.TimeoutError:
                    if replies:
                        break
                    continue
                data = item if isinstance(item, dict) else {}
                if data.get("type") == "plain":
                    piece = str(data.get("data", ""))
                    if piece.strip():
                        replies.append(piece)
                elif data.get("type") == "end":
                    break
            return replies
        finally:
            if not use_qq:
                from astrbot.core.platform.sources.webchat.webchat_queue_mgr import (
                    webchat_queue_mgr,
                )
                webchat_queue_mgr.remove_back_queue(message_id)
            if use_qq and capture is not None:
                capture.queue = None
    except Exception as exc:
        log.warning("chat injection failed: %s", exc)
        return []


class _QqSendCapture:
    """Shared sink for QQ replies captured from the adapter send path."""

    def __init__(self) -> None:
        self.queue: list[str] | None = None
        self.installed = False


_QQ_CAPTURE = _QqSendCapture()


def _install_qq_send_capture() -> _QqSendCapture | None:
    """Wrap AiocqhttpMessageEvent.send once to capture outgoing replies.

    Only intercepts while an injection is waiting (capture.queue is not
    None), so normal QQ traffic is untouched.
    """
    if _QQ_CAPTURE.installed:
        return _QQ_CAPTURE
    try:
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )

        original_send = AiocqhttpMessageEvent.send

        async def captured_send(self, message):
            if _QQ_CAPTURE.queue is not None:
                for comp in getattr(message, "chain", []) or []:
                    text = getattr(comp, "text", "") or ""
                    if text.strip():
                        _QQ_CAPTURE.queue.append(text)
            return await original_send(self, message)

        AiocqhttpMessageEvent.send = captured_send  # type: ignore[method-assign]
        _QQ_CAPTURE.installed = True
        log.info("QQ send capture installed")
        return _QQ_CAPTURE
    except Exception as exc:
        log.warning("QQ send capture unavailable: %s", exc)
        return None


async def wait_for_platform(timeout: float = 20.0) -> bool:
    """Wait until AstrBot finishes platform startup before injecting."""
    context = _CONTEXT
    if context is None:
        return False
    end = time.time() + timeout
    while time.time() < end:
        if context.get_platform_inst("webchat") is not None:
            return True
        await asyncio.sleep(1)
    return False


async def astrbot_reply(context_text: str) -> str | None:
    """Ask AstrBot's full pipeline (persona, memory, companion plugins)."""
    if _CONTEXT is None:
        return None
    replies = await inject_room_chat(CHAT_USER, context_text)
    reply = "".join(replies).strip()
    return reply or None

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
        self.pause_replied = False
        self.task: asyncio.Task | None = None
        self.sync_task: asyncio.Task | None = None
        self.pause_task: asyncio.Task | None = None

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
        self.pause_replied = False

    def seek(self, play_state: dict) -> None:
        self.anchor_time = float(play_state.get("currentTime", 0))
        self.anchor_ts = float(play_state.get("serverTimestamp", time.time() * 1000))

    def sync(self, play_state: dict) -> None:
        """Soft-correct the playback anchor from the server's authority clock.

        The server broadcasts sync frames (~10s). Without this, client clock
        skew and missed pause/seek events drift the reaction schedule.
        """
        ct = play_state.get("currentTime")
        if ct is None:
            return
        server_pos = float(ct)
        drift = abs(server_pos - self.position())
        if drift > 2.0:
            log.info("clock sync: drift %.1fs, re-anchoring", drift)
            self.anchor_time = server_pos
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


async def sync_loop(session: ListenSession) -> None:
    """Ask the server for its authority playback clock every ~10s."""
    try:
        while sio.connected:
            await asyncio.sleep(10)
            await sio.emit("player:sync_request", {})
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("sync loop stopped: %s", exc)


async def pause_reaction_loop(session: ListenSession) -> None:
    """Say something once when the user pauses mid-song (deduped per pause)."""
    try:
        while True:
            await asyncio.sleep(1.5)
            if not session.paused or session.pause_replied:
                continue
            session.pause_replied = True
            pos = int(session.anchor_time)
            title = session.track.get("title", "")
            reply = await asyncio.to_thread(
                gemini_text,
                f"{PERSONA}\n你们正在一起听《{title}》，用户在第 {pos} 秒按了暂停。"
                "随口说一句，不超过 20 字，只输出这句话本身。")
            if reply:
                await post_chat(reply[:60])
    except asyncio.CancelledError:
        raise


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
    session.task = asyncio.current_task()
    session.sync_task = asyncio.create_task(sync_loop(session))
    session.pause_task = asyncio.create_task(pause_reaction_loop(session))

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

    # wait for natural end, then let AstrBot close it out in persona's voice
    if session.duration:
        await wait_until(session, session.duration - 1.0)
    if session.notes:
        notes = "；".join(session.notes[-4:])
        reply = await astrbot_reply(
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
    if SESSION:
        for task in (SESSION.task, SESSION.sync_task, SESSION.pause_task):
            if task:
                task.cancel()
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
LAST_EMPTY_SINCE: float | None = None


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


async def leave_empty_room() -> None:
    """Leave a room with no real users so the bot doesn't squat alone."""
    global ROOM_ID, LAST_EMPTY_SINCE
    log.info("room has no real users for 30s; leaving")
    stop_listen()
    try:
        await sio.emit("room:leave", {})
    except Exception as exc:
        log.warning("room leave failed: %s", exc)
    ROOM_ID = ""
    LAST_EMPTY_SINCE = time.time()
    await sio.emit("room:list")


@sio.on("room:created")
async def on_created(data: dict) -> None:
    STATE["room_id"] = data["roomId"]
    save_json(STATE_PATH, STATE)
    log.info("room created: %s", data["roomId"])


@sio.on("room:deleted")
async def on_deleted(data: dict) -> None:
    """The server deleted our saved room (empty grace period expired)."""
    global ROOM_ID
    if data and data.get("roomId") in (ROOM_ID, STATE.get("room_id")):
        ROOM_ID = ""
        STATE.pop("room_id", None)
        STATE.pop("rejoin_token", None)
        save_json(STATE_PATH, STATE)
        log.info("saved room was deleted by server; will re-discover")
        await sio.emit("room:list")


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
    users = room.get("users") or []
    real_users = [u for u in users if (u.get("nickname") if isinstance(u, dict) else u) != NICKNAME]
    log.info("in room %s (%s), users=%s", ROOM_ID, room.get("name"), len(users))
    if not ROOM_ID_CFG and not real_users:
        if STATE.get("room_id") == ROOM_ID:
            # we created / rejoined this room ourselves — stay and wait for
            # humans instead of churning a create/leave loop
            log.info("room empty; staying put and waiting for humans")
        else:
            asyncio.create_task(_leave_if_still_empty(ROOM_ID))
    current = room.get("currentTrack")
    play = room.get("playState") or {}
    if current and play:
        start_listen(current, play)


async def _leave_if_still_empty(room_id: str) -> None:
    await asyncio.sleep(30)
    if ROOM_ID == room_id:
        await leave_empty_room()


@sio.on("room:list_update")
async def on_rooms(rooms: list) -> None:
    if ROOM_ID_CFG:
        return
    if LAST_EMPTY_SINCE and time.time() - LAST_EMPTY_SINCE < 60:
        return  # don't immediately re-create after leaving an empty room
    occupied = [r for r in rooms
                if isinstance(r, dict) and int(r.get("userCount") or 0) >= 1]
    # Prefer a room with humans over the empty one we created and saved —
    # the user will open their own room, not the bot's.
    if occupied and (not ROOM_ID or occupied[0]["id"] != ROOM_ID):
        if ROOM_ID:
            await sio.emit("room:leave", {})
        target = occupied[0]["id"]
        log.info("joining occupied room %s", target)
        await sio.emit("room:join", {"roomId": target, "nickname": NICKNAME})
    elif not ROOM_ID and CFG.get("create_if_missing", True):
        log.info("no occupied rooms; creating one")
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


@sio.on("player:sync")
async def on_sync(data: dict) -> None:
    if SESSION:
        SESSION.sync(data or {})


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
    replies = await inject_room_chat(
        user,
        f"（场景：我们正在房间一起听《{title}》-{artist}，播放到第 {pos} 秒。"
        f"房间用户 {user} 说：“{clean[:100]}”。"
        "结合此刻的听感自然回应，口语化，不要超过 50 字。）",
    )
    reply = "".join(replies).strip()
    if not reply:
        reply = await asyncio.to_thread(
            gemini_text,
            f"{PERSONA}\n你们正在一起听《{title}》-{artist}，播放到第 {pos} 秒。"
            f"用户说：“{clean[:100]}”。简短回应，不超过 50 字，只输出回应本身。")
    if reply:
        await post_chat(reply[:120])

# ---------------------------------------------------------------------------

async def discovery_loop() -> None:
    """Re-check the room list every 30s while squatting alone in our own
    room, so we notice when the user opens their own room."""
    while True:
        await asyncio.sleep(30)
        if not sio.connected or ROOM_ID_CFG:
            continue
        if SESSION is not None:
            continue  # actively listening — don't disturb playback
        await sio.emit("room:list")


async def main() -> None:
    if not SERVER:
        raise SystemExit("server_url missing in music-bot configuration")
    if not IDENTITY_SECRET:
        raise SystemExit("identity_secret missing in music-bot configuration")
    if not GEMINI_ENDPOINT or not GEMINI_KEY:
        raise SystemExit("gemini_endpoint and gemini_key are required")
    if _CONTEXT is not None:
        await wait_for_platform()  # let AstrBot finish platform startup
    asyncio.create_task(discovery_loop())
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
