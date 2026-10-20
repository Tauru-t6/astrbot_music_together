#!/usr/bin/env python3
"""music-bot: listens together with you in a music-together room.

Joins the room as a Socket.IO client, watches playback, downloads the
current track once, splits it into segments in memory, and asks Gemini to
listen to each segment *in sync with playback* — analysis is gated on the
playing state, so pausing the song pauses the bot's listening too.

Owner-following: the bot stays in the room where OWNER_NICKNAME is and
leaves to search for them when they move. Room votes are auto-approved.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import socketio
from curl_cffi import requests as crequests

try:
    from .audio_split import split_mp3_seconds
except ImportError:
    from audio_split import split_mp3_seconds

try:
    from astrbot.api import logger as log
except ImportError:  # standalone mode (python bot.py)
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("music-bot")

BASE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("MUSIC_BOT_CONFIG", BASE / "config.json"))
STATE_PATH = Path(os.environ.get("MUSIC_BOT_STATE", BASE / "state.json"))

# 公益中转站会把广告拼在响应尾部，从最早的广告特征词处截断
_AD_MARKERS = ("公益中转", "严禁倒卖", "官方域名", "zhicloud", "QQ群：")


def strip_relay_ads(text: str) -> str:
    cut = None
    for m in _AD_MARKERS:
        i = text.find(m)
        if i != -1 and (cut is None or i < cut):
            cut = i
    if cut is None:
        return text
    if cut > 0 and text[cut - 1] == "【":
        cut -= 1  # 连同广告块的左括号一起截掉
    head = re.sub(r"[—\-–\s]*$", "", text[:cut])
    return head.strip()


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

OWNER_NICKNAME = str(CFG.get("owner_nickname", "") or "")
AUTO_VOTE = bool(CFG.get("auto_vote", True))
DANMAKU_PER_SONG = 4
IGNORE_NICKNAMES: set[str] = set()
REALTIME_ENABLED = bool(CFG.get("realtime_enabled", False))
REALTIME_URL = str(CFG.get("realtime_url", "") or "")
REALTIME_KEY = str(CFG.get("realtime_key", "") or "")
REALTIME_MODEL = str(CFG.get("realtime_model", "qwen3.8-omni-flash-realtime") or "qwen3.8-omni-flash-realtime")
REALTIME_VOICE = str(CFG.get("realtime_voice", "Sunny") or "Sunny")
REALTIME_COMMIT_SECONDS = max(15.0, min(float(CFG.get("realtime_commit_seconds", 35) or 35), 120.0))
REALTIME_DUAL = bool(CFG.get("realtime_dual", False))
REALTIME_MAX_PER_SONG = max(1, min(int(CFG.get("realtime_max_per_song", 2) or 2), 12))

# Where room chat gets injected. Two shapes:
#   QQ session  "Tauru:FriendMessage:1125961157" → aiocqhttp platform bucket
#   anything else (e.g. "music-room")           → webchat adapter bucket
CHAT_SESSION_ID = str(CFG.get("chat_session_id", "music-room"))
CHAT_USER = str(CFG.get("chat_user", "music-room"))
CHAT_PLATFORM_ID = str(CFG.get("chat_platform_id", "Tauru"))

BOT_UID = "music-bot-01"
REPLY_ALL_CHAT = True
DASH_PORT = 6385
_CONTEXT = None


def bind_context(context) -> None:
    """Capture the running AstrBot Context for native event injection."""
    global _CONTEXT
    _CONTEXT = context


def issue_identity_token(secret: str, uid: str = BOT_UID) -> str:
    """Self-sign an mt_identity cookie (same HMAC scheme as the server)."""
    now_ms = int(time.time() * 1000)
    payload = {"uid": uid, "iat": now_ms, "exp": now_ms + 30 * 86400 * 1000, "ver": 1}
    unsigned = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), unsigned.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")
    return f"{unsigned}.{sig}"


def identity_cookie(uid: str = BOT_UID) -> str:
    return "mt_identity=" + issue_identity_token(IDENTITY_SECRET, uid) if IDENTITY_SECRET else ""


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
    global OWNER_NICKNAME, AUTO_VOTE, DANMAKU_PER_SONG, IGNORE_NICKNAMES
    global REALTIME_ENABLED, REALTIME_URL, REALTIME_KEY, REALTIME_MODEL, REALTIME_VOICE, REALTIME_COMMIT_SECONDS, REALTIME_DUAL, REALTIME_MAX_PER_SONG
    global CHAT_SESSION_ID, CHAT_USER, CHAT_PLATFORM_ID
    global REPLY_ALL_CHAT, DASH_PORT

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
    OWNER_NICKNAME = str(CFG.get("owner_nickname", "") or "").strip()
    AUTO_VOTE = bool(CFG.get("auto_vote", True))
    DANMAKU_PER_SONG = max(0, min(int(CFG.get("danmaku_per_song", 4) or 4), 12))
    ignore_raw = CFG.get("ignore_nicknames", "") or ""
    if isinstance(ignore_raw, (list, tuple)):
        ignore_raw = ",".join(str(x) for x in ignore_raw)
    IGNORE_NICKNAMES = {x.strip() for x in str(ignore_raw).split(",") if x.strip()}
    REALTIME_ENABLED = bool(CFG.get("realtime_enabled", False))
    REALTIME_URL = str(CFG.get("realtime_url", "") or "").strip()
    REALTIME_KEY = str(CFG.get("realtime_key", "") or "").strip()
    REALTIME_MODEL = str(CFG.get("realtime_model", "qwen3.8-omni-flash-realtime") or "qwen3.8-omni-flash-realtime")
    REALTIME_VOICE = str(CFG.get("realtime_voice", "Sunny") or "Sunny")
    REALTIME_COMMIT_SECONDS = max(15.0, min(float(CFG.get("realtime_commit_seconds", 35) or 35), 120.0))
    REALTIME_DUAL = bool(CFG.get("realtime_dual", False))
    REALTIME_MAX_PER_SONG = max(1, min(int(CFG.get("realtime_max_per_song", 2) or 2), 12))
    CHAT_SESSION_ID = str(CFG.get("chat_session_id", "music-room") or "music-room")
    CHAT_USER = str(CFG.get("chat_user", "music-room") or "music-room")
    CHAT_PLATFORM_ID = str(CFG.get("chat_platform_id", "Tauru") or "Tauru")
    REPLY_ALL_CHAT = bool(CFG.get("reply_all_chat", True))
    DASH_PORT = max(0, min(int(CFG.get("dashboard_port", 6385) or 6385), 65535))


def gemini_url() -> str:
    # Endpoint with a {model} placeholder gets it replaced (official Google
    # format); a relay that already embeds the model in its path is used
    # verbatim and gemini_model is ignored.
    if "{model}" in GEMINI_ENDPOINT:
        return GEMINI_ENDPOINT.replace("{model}", GEMINI_MODEL)
    return GEMINI_ENDPOINT

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

INJECT_LOCK = asyncio.Lock()


async def inject_room_chat(user: str, content: str) -> list[str]:
    """Inject one room chat message into the pipeline; collect the reply."""
    if _CONTEXT is None:
        return []
    # The reply-capture sink is process-wide, so only one injection may be
    # in flight at a time (matters once several rooms chat simultaneously).
    async with INJECT_LOCK:
        return await _inject_room_chat_locked(user, content)


async def _inject_room_chat_locked(user: str, content: str) -> list[str]:
    context = _CONTEXT
    try:
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
            abm.message = [Plain(content)]  # adapters expect a plain list of components
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

# ---------------------------------------------------------------------------
# music-together REST helpers (identity cookie auth)
# ---------------------------------------------------------------------------

def rest_get(path: str, params: dict) -> dict | None:
    cookie = identity_cookie()
    if not cookie:
        log.warning("REST %s skipped: identity cookie not configured", path)
        return None
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = SERVER.rstrip("/") + path + (("?" + query) if query else "")
    for attempt in (1, 2):
        try:
            r = crequests.get(
                url, cookies={"mt_identity": cookie.split("=", 1)[1]},
                proxies=PROXIES, timeout=30,
            )
            if r.status_code == 200:
                return r.json()
            log.warning("REST %s -> %s %s", path, r.status_code, r.text[:120])
            return None  # a real answer (even an error) - no retry
        except Exception as exc:
            log.warning("REST %s failed (attempt %s/2): %s", path, attempt, exc)
            time.sleep(1)
    return None

# ---------------------------------------------------------------------------
# audio / prompts
# ---------------------------------------------------------------------------

def duration_of(track: dict) -> float:
    try:
        return float(track.get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


async def _fetch_audio(url: str, track: dict) -> tuple[bytes, str] | None:
    parsed = urlparse(str(url))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        log.warning("audio URL has unsupported scheme: %s", str(url)[:120])
        return None
    mime = "audio/mp3"
    if ".m4a" in str(url) or "m4a" in str(track.get("urlId", "")):
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
    return None


_AUDIO_CACHE: dict = {}
_AUDIO_CACHE_TS: dict = {}


async def download_audio(track: dict) -> tuple[bytes | None, str]:
    cache_key = str(track.get("urlId") or "")
    now = time.time()
    if cache_key and cache_key in _AUDIO_CACHE:
        if now - _AUDIO_CACHE_TS.get(cache_key, 0) < 1800:
            log.info("audio cache hit: %s", cache_key)
            return _AUDIO_CACHE[cache_key]
    url = track.get("streamUrl")
    source = track.get("source", "netease")
    duration = duration_of(track)
    # We now pull the 64kbps stream (~8KB/s) to keep the Gemini payload small,
    # so the trial-clip floor has to sit below that: a ~30s netease trial clip
    # is only ~240KB no matter how long the track claims to be.
    min_bytes = int(duration * 6 * 1024) if duration else 10000

    async def refresh_url() -> str | None:
        if not track.get("urlId"):
            return None
        data = await asyncio.to_thread(rest_get, "/api/music/url", {
            "source": source,
            "urlId": track.get("urlId"),
            "bitrate": 64,
        })
        return data.get("url") if data else None

    # Ask the server for the 64kbps stream up front: the streamUrl baked into
    # the search result is the user's full-bitrate one (~5-9MB per track), and
    # that is what made Gemini audio calls stall for 120s+. Fall back to the
    # baked URL only when the refresh fails.
    fresh = await refresh_url()
    got = None
    if fresh and fresh != url:
        got = await _fetch_audio(fresh, track)
        if got is not None and len(got[0]) >= min_bytes:
            if cache_key:
                _AUDIO_CACHE[cache_key] = got
                _AUDIO_CACHE_TS[cache_key] = now
            return got
    fallback = await _fetch_audio(url, track) if url else None
    if fallback is not None and (got is None or len(fallback[0]) > len(got[0])):
        got = fallback
        if cache_key and len(got[0]) >= min_bytes:
            _AUDIO_CACHE[cache_key] = got
            _AUDIO_CACHE_TS[cache_key] = now
        return got
    if got is not None:
        log.warning("audio looks short: %.0fs track but %.0f KB - trial clip?",
                    duration, len(got[0]) / 1024)
        return got
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
        "at 是相对这个片段开头的秒数；最多 2 条，片头 2 秒内不要有；"
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
        if at <= seg_start:
            continue  # before segment start (within-segment late reactions are ok) — never burst old reactions
        if session.duration and at > session.duration - 3:
            continue
        out.append({"at": round(at, 1), "text": text})
        if len(out) >= 1:
            break  # one remark per segment — keep it quiet
    return out

# ---------------------------------------------------------------------------
# listening session
# ---------------------------------------------------------------------------

class ListenSession:
    """One track: playback anchor + reactions posted as the song gets there."""

    def __init__(self, track: dict, play_state: dict, room_id: str = ""):
        self.track = track
        self.room_id = room_id
        self.anchor_time = float(play_state.get("currentTime", 0))
        self.anchor_ts = float(play_state.get("serverTimestamp", time.time() * 1000))
        self.paused = not play_state.get("isPlaying", False)
        self.duration = duration_of(track)
        self.notes: list[str] = []
        self.pause_replied = False
        self.task: asyncio.Task | None = None
        self.sync_task: asyncio.Task | None = None
        self.pause_task: asyncio.Task | None = None
        self.rt_task: asyncio.Task | None = None

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


# ---------------------------------------------------------------------------
# realtime companion (qwen omni realtime, raw WS - no SDK dependency)
# ---------------------------------------------------------------------------

class RealtimeSession:
    """One song's realtime listening session on a qwen-omni realtime WS.

    The bot feeds the song's 16k PCM paced to the playback clock and calls
    commit() every REALTIME_COMMIT_SECONDS, so the model drops a short
    remark on what it just heard. Replies land in a thread-safe queue the
    feeding task drains (thread -> asyncio handoff without bridges).
    """

    def __init__(self) -> None:
        self.closed = False
        self.ready = threading.Event()   # set on session.updated
        self.replies: list[str] = []
        self.ws = None
        self._lock = threading.Lock()

    def start(self) -> None:
        import websocket  # vendored in libs/ for the MaiBot plugin
        sep = "&" if "?" in REALTIME_URL else "?"
        self.ws = websocket.WebSocketApp(
            f"{REALTIME_URL}{sep}model={REALTIME_MODEL}",
            header={"Authorization": "Bearer " + REALTIME_KEY},
            on_open=self._on_open, on_message=self._on_message,
            on_close=self._on_close, on_error=self._on_error)
        threading.Thread(target=self.ws.run_forever,
                         kwargs={"ping_interval": 20, "ping_timeout": 10},
                         daemon=True, name="realtime-ws").start()

    def _on_open(self, ws) -> None:
        cfg = {
            "modalities": ["text"],
            "voice": REALTIME_VOICE,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm24",
            "input_audio_transcription": {"model": None},
            "turn_detection": {"type": "server_vad", "threshold": 0.2,
                               "prefix_padding_ms": 300,
                               "silence_duration_ms": 800},
            "instructions": PERSONA + " 你正在和主人实时一起听歌。每听到一段音乐，"
                            "就随口短评一句刚听到的内容：不超过20字，点评这段里最"
                            "抓耳的细节（编曲/音色/节奏），不要复述歌词。",
        }
        self._send({"event_id": "rt-su", "type": "session.update", "session": cfg})
        log.info("[realtime] session updated (voice=%s, model=%s)",
                 REALTIME_VOICE, REALTIME_MODEL)

    def _on_message(self, ws, message) -> None:
        try:
            d = json.loads(message)
        except (ValueError, TypeError):
            return
        t = d.get("type", "")
        if t == "session.updated":
            self.ready.set()
        if t in ("response.text.done", "response.audio_transcript.done"):
            txt = str(d.get("text") or d.get("transcript") or "").strip()
            if txt:
                with self._lock:
                    self.replies.append(txt)
        elif "error" in t:
            log.warning("[realtime] %s: %s", t, str(d)[:220])

    def _on_close(self, ws, code, msg) -> None:
        if not self.closed:
            log.info("[realtime] ws closed: %s %s", code, str(msg)[:80])

    def _on_error(self, ws, err) -> None:
        log.warning("[realtime] ws error: %s", str(err)[:120])

    def _send(self, payload: dict) -> None:
        try:
            self.ws.send(json.dumps(payload))
        except Exception as exc:
            log.warning("[realtime] send failed: %s", exc)

    def append(self, pcm_chunk: bytes) -> None:
        self._send({"event_id": f"rt-a{time.time_ns()}",
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm_chunk).decode()})

    def commit(self) -> None:
        self._send({"event_id": f"rt-c{time.time_ns()}",
                    "type": "input_audio_buffer.commit"})

    def drain_replies(self) -> list[str]:
        with self._lock:
            out, self.replies = self.replies, []
        return out

    def close(self) -> None:
        self.closed = True
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# dashboard state (read by the status page)
# ---------------------------------------------------------------------------

RECENT_EVENTS: list[dict] = []  # [{ts, kind, text, room}] newest last, capped


def record_event(kind: str, text: str, room: str = "") -> None:
    RECENT_EVENTS.append({"ts": time.time(), "kind": kind, "text": text, "room": room})
    del RECENT_EVENTS[:-50]


# Fields the dashboard is allowed to read/write. Secret values are masked in
# GET and only replaced when the client sends a non-masked value.
DASH_EDITABLE = {
    "gemini_endpoint": "text",
    "gemini_key": "secret",
    "gemini_model": "text",
    "segment_seconds": "float",
    "analyze_lead_seconds": "float",
    "persona": "text",
    "nickname": "text",
    "owner_nickname": "text",
    "auto_vote": "bool",
    "danmaku_per_song": "float",
    "ignore_nicknames": "text",
    "realtime_enabled": "bool",
    "realtime_commit_seconds": "float",
    "realtime_dual": "bool",
    "reply_all_chat": "bool",
    "proxy": "text",
    "chat_session_id": "text",
    "chat_platform_id": "text",
    "chat_user": "text",
}


def _overrides_path() -> Path:
    return STATE_PATH.with_name("dashboard_overrides.json")


def _load_overrides() -> dict:
    return load_json(_overrides_path(), {})


def apply_overrides() -> None:
    """Re-apply config with dashboard overrides layered on top."""
    merged = dict(CFG)
    merged.update(_load_overrides())
    apply_config(merged)


def dashboard_config() -> dict:
    masked = {}
    for key, kind in DASH_EDITABLE.items():
        val = CFG.get(key, "")
        if kind == "secret" and val:
            masked[key] = "********"
        else:
            masked[key] = val
    return masked


def dashboard_update(payload: dict) -> dict:
    """Validate + persist dashboard config overrides, then hot-apply."""
    overrides = _load_overrides()
    changed = {}
    for key, kind in DASH_EDITABLE.items():
        if key not in payload:
            continue
        val = payload[key]
        if kind == "secret" and (not val or val == "********"):
            continue  # untouched masked field
        if kind == "float":
            try:
                val = float(val)
            except (TypeError, ValueError):
                continue
        elif kind == "bool":
            val = bool(val)
        else:
            val = str(val)
        overrides[key] = val
        changed[key] = val
    save_json(_overrides_path(), overrides)
    apply_overrides()
    log.info("dashboard config updated: %s", sorted(changed))
    return dashboard_config()


async def _handle_dashboard(reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
    try:
        request = await asyncio.wait_for(reader.read(65536), timeout=5)
        head, _, rest = request.partition(b"\r\n\r\n")
        line = head.split(b"\r\n", 1)[0].decode(errors="replace")
        parts = line.split(" ")
        method = parts[0].upper() if parts else "GET"
        path = parts[1] if len(parts) > 1 else "/"

        status = "200 OK"
        if path.startswith("/api/status"):
            body = json.dumps(dashboard_status(), ensure_ascii=False).encode()
            ctype = "application/json; charset=utf-8"
        elif path.startswith("/api/config"):
            if method == "POST":
                try:
                    payload = json.loads(rest.decode("utf-8") or "{}")
                    out = dashboard_update(payload)
                except (json.JSONDecodeError, ValueError) as exc:
                    status = "400 Bad Request"
                    out = {"error": str(exc)}
            else:
                out = dashboard_config()
            body = json.dumps(out, ensure_ascii=False).encode()
            ctype = "application/json; charset=utf-8"
        else:
            body = _DASHBOARD_HTML.encode()
            ctype = "text/html; charset=utf-8"
        writer.write(
            b"HTTP/1.1 " + status.encode() +
            b"\r\nContent-Type: " + ctype.encode() +
            b"\r\nContent-Length: " + str(len(body)).encode() +
            b"\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


_DASHBOARD_SERVER: asyncio.AbstractServer | None = None

try:
    _DASHBOARD_HTML = (BASE / "dashboard.html").read_text(encoding="utf-8")
except OSError:
    _DASHBOARD_HTML = "<!doctype html><title>music-bot</title><p>dashboard.html missing</p>"


async def start_dashboard() -> None:
    """Serve the status page on 127.0.0.1:DASH_PORT (bind-all if configured)."""
    global _DASHBOARD_SERVER
    if _DASHBOARD_SERVER is not None or not DASH_PORT:
        return
    host = str(CFG.get("dashboard_host", "0.0.0.0") or "0.0.0.0")
    try:
        _DASHBOARD_SERVER = await asyncio.start_server(
            _handle_dashboard, host, DASH_PORT)
        log.info("dashboard: http://%s:%s/", host, DASH_PORT)
    except OSError as exc:
        log.warning("dashboard port %s unavailable: %s", DASH_PORT, exc)


async def stop_dashboard() -> None:
    global _DASHBOARD_SERVER
    if _DASHBOARD_SERVER is not None:
        _DASHBOARD_SERVER.close()
        await _DASHBOARD_SERVER.wait_closed()
        _DASHBOARD_SERVER = None


def dashboard_status() -> dict:
    """Snapshot for the status page."""
    bot = BOT
    if bot is None:
        return {
            "connected": False, "room_id": "", "nickname": NICKNAME,
            "owner_nickname": OWNER_NICKNAME, "server": SERVER,
            "listening": False, "track": {}, "position": 0.0, "duration": 0.0,
            "paused": False, "notes": [], "events": RECENT_EVENTS[-20:],
            "now": time.time(),
        }
    sess = bot.session
    track: dict = {}
    pos = 0.0
    dur = 0.0
    paused = False
    if sess:
        track = {
            "title": sess.track.get("title", ""),
            "artist": "/".join(sess.track.get("artist") or []),
            "cover": sess.track.get("coverUrl") or sess.track.get("cover") or "",
        }
        pos = round(sess.position(), 1)
        dur = sess.duration
        paused = sess.paused
    return {
        "connected": bool(bot.sio.connected),
        "room_id": bot.room_id or "",
        "room_name": bot.room_name,
        "owner_nickname": OWNER_NICKNAME,
        "owner_in_room": bot.owner_here,
        "nickname": NICKNAME,
        "server": SERVER,
        "listening": sess is not None,
        "track": track,
        "position": pos,
        "duration": dur,
        "paused": paused,
        "notes": list(sess.notes[-12:]) if sess else [],
        "events": RECENT_EVENTS[-20:],
        "now": time.time(),
    }


# ---------------------------------------------------------------------------
# the bot: one socket, owner-following
# ---------------------------------------------------------------------------

VOTE_LABELS = {
    "next": "切歌",
    "prev": "上一首",
    "pause": "暂停播放",
    "resume": "继续播放",
    "play-track": "指定播放",
    "remove-track": "投票移除",
    "set-mode": "切播放模式",
}

BOT: "MusicBot | None" = None


class MusicBot:
    """Single companion that follows OWNER_NICKNAME around the lobby."""

    def __init__(self) -> None:
        self.sio = socketio.AsyncClient()
        self.room_id: str = ""
        self.room_name: str = ""
        self.session: ListenSession | None = None
        self.last_revision = -1
        self.last_chat_reply = 0.0
        self._last_post = 0.0
        self.humans = 0
        self.owner_here = False
        self.owner_id = ""
        self.probing = False
        self.empty_since: float | None = None
        self.created_room = False
        self._stopped = False
        self._last_create = 0.0
        self._follow_task: asyncio.Task | None = None
        self._discovery_task: asyncio.Task | None = None
        self._voted_ids: set[str] = set()
        self._probe_skip: dict[str, float] = {}   # roomId -> blacklist until
        self._room_counts: dict[str, int] = {}    # roomId -> last userCount
        self._state_event = asyncio.Event()
        self._last_state: dict = {}
        self._bind()

    # -- wiring --------------------------------------------------------

    def _rebuild_sio(self) -> None:
        """Last-resort recovery: swap in a fresh client so the run loop can
        escape a wedged connect/wait on the old one."""
        old = self.sio
        self.sio = socketio.AsyncClient()
        self._bind()
        try:
            asyncio.get_running_loop().create_task(old.disconnect())
        except Exception:
            pass
        log.info("socket client rebuilt; the run loop reconnects within ~30s")

    def _bind(self) -> None:
        s = self.sio
        s.on("connect", self._on_connect)
        s.on("disconnect", self._on_disconnect)
        s.on("room:list_update", self._on_rooms)
        s.on("room:state", self._on_state)
        s.on("room:error", self._on_room_error)
        s.on("room:user_joined", self._on_user_joined)
        s.on("room:user_left", self._on_user_left)
        s.on("room:deleted", self._on_room_deleted)
        s.on("player:play", self._on_play)
        s.on("player:pause", self._on_pause)
        s.on("player:resume", self._on_resume)
        s.on("player:seek", self._on_seek)
        s.on("player:sync", self._on_sync)
        s.on("player:next", self._on_next)
        s.on("chat:message", self._on_chat)
        s.on("vote:started", self._on_vote_started)
        s.on("vote:result", self._on_vote_result)

    # -- lifecycle -----------------------------------------------------

    async def run(self) -> None:
        while not self._stopped:
            try:
                await asyncio.wait_for(self.sio.connect(
                    SERVER, headers={"Cookie": identity_cookie(BOT_UID)},
                    socketio_path="socket.io",
                ), timeout=30)
            except Exception as exc:
                if self._stopped:
                    return
                log.warning("connect failed: %s; retry in 10s", exc)
                await asyncio.sleep(10)
                continue
            if self._discovery_task is None or self._discovery_task.done():
                self._discovery_task = asyncio.create_task(self._discovery_loop())
            room = ROOM_ID_CFG or str(STATE.get("room_id", "") or "")
            if room:
                join_payload: dict = {"roomId": room, "nickname": NICKNAME}
                rejoin = str(STATE.get("rejoin_token", "") or "")
                if rejoin and str(STATE.get("room_id", "")) == room:
                    join_payload["rejoinToken"] = rejoin
                await self.sio.emit("room:join", join_payload)
            else:
                await self.sio.emit("room:list")
            # sio.wait() can hang forever when a half-dead socket never
            # emits a disconnect event; poll connectivity instead so a dead
            # link always falls through to the reconnect path.
            while not self._stopped and self.sio.connected:
                await asyncio.sleep(3)
            if self._stopped:
                return
            log.warning("connection lost; reconnecting in 5s")
            try:
                await asyncio.wait_for(self.sio.disconnect(), timeout=5)
            except Exception:
                pass
            await asyncio.sleep(5)

    async def stop(self) -> None:
        self._stopped = True
        self._cancel_follow()
        if self._discovery_task and not self._discovery_task.done():
            self._discovery_task.cancel()
        self.stop_listen()
        if self.sio.connected:
            try:
                await self.sio.emit("room:leave", {})
            except Exception:
                pass
            await self.sio.disconnect()

    async def _discovery_loop(self) -> None:
        """Room-list refresh while idle, plus a connection watchdog.

        A half-closed socket can leave ``sio.wait()`` hanging forever (no
        disconnect event ever arrives), so whenever the client reports
        disconnected we force a teardown to re-engage the reconnect loop.
        """
        dead = 0
        try:
            while not self._stopped:
                await asyncio.sleep(30)
                if self._stopped:
                    continue
                if not self.sio.connected:
                    dead += 1
                    if dead == 2:
                        log.warning("watchdog: socket down 60s; forcing teardown")
                        try:
                            await asyncio.wait_for(self.sio.disconnect(), timeout=5)
                        except Exception:
                            pass
                    elif dead >= 6:
                        log.warning("watchdog: still down after 3min; rebuilding client")
                        dead = 0
                        self._rebuild_sio()
                    continue
                dead = 0
                if self.room_id or self.probing or ROOM_ID_CFG:
                    continue  # in a room / already searching / pinned
                await self.sio.emit("room:list")
        except asyncio.CancelledError:
            raise

    # -- lobby / room entry ---------------------------------------------

    async def _on_room_error(self, data: dict) -> None:
        log.warning("room error: %s", str(data)[:160])

    async def _on_rooms(self, rooms) -> None:
        """room:list_update — find and join the room where the owner is."""
        if ROOM_ID_CFG or self._stopped or self.probing:
            return
        if self.room_id:
            return  # already somewhere; owner-left logic handles moving
        if not isinstance(rooms, list):
            return
        entries = [(str(r.get("id")), str(r.get("name") or ""),
                    int(r.get("userCount") or 0))
                   for r in rooms if isinstance(r, dict)]
        # membership changes re-open probing; stable rooms stay blacklisted
        for rid, _n, cnt in entries:
            if self._room_counts.get(rid) != cnt:
                self._probe_skip.pop(rid, None)
            self._room_counts[rid] = cnt
        now_ts = time.time()
        log.info("room list: %s | current=", entries)
        occupied = [e for e in entries if e[2] >= 1
                    and self._probe_skip.get(e[0], 0) <= now_ts]
        if not occupied:
            if not OWNER_NICKNAME and CFG.get("create_if_missing", True) \
                    and not self.created_room \
                    and time.time() - self._last_create > 60:
                self._last_create = time.time()
                self.created_room = True
                log.info("no occupied rooms; creating a waiting room")
                await self.sio.emit("room:create", {"nickname": NICKNAME, "roomName": "一起听歌"})
            return
        if OWNER_NICKNAME:
            occupied.sort(key=lambda e: 0 if OWNER_NICKNAME in e[1] else 1)
        self.probing = True
        try:
            for rid, rname, _cnt in occupied[:3]:
                log.info("probing room %s (%s) for %s", rid, rname,
                         OWNER_NICKNAME or "humans")
                self._state_event.clear()
                self._last_state = {}
                await self.sio.emit("room:join", {"roomId": rid, "nickname": NICKNAME})
                try:
                    await asyncio.wait_for(self._state_event.wait(), timeout=6.0)
                except asyncio.TimeoutError:
                    log.info("probe %s: no room:state, skipping", rid)
                    continue
                st = self._last_state or {}
                users = st.get("users") or []
                if self._owner_in(users):
                    log.info("owner found in room %s (%s)", rid, st.get("name"))
                    await self._enter_room(st)
                    return
                log.info("probe %s: owner not inside; leaving and blacklisting 30min", rid)
                self._probe_skip[rid] = time.time() + 1800.0
                await self.sio.emit("room:leave", {})
                await asyncio.sleep(0.6)
            log.info("%s not in any occupied room; waiting for the next update",
                     OWNER_NICKNAME or "humans")
        finally:
            self.probing = False

    def _owner_in(self, users: list) -> bool:
        if not OWNER_NICKNAME:
            return self.humans > 0
        for u in users:
            nick = u.get("nickname") if isinstance(u, dict) else str(u)
            if nick == OWNER_NICKNAME:
                return True
        return False

    def _learn_owner(self, users: list) -> None:
        """Remember the owner's server userId (vote maps are keyed by id)."""
        if not OWNER_NICKNAME:
            return
        for u in users:
            if isinstance(u, dict) \
                    and u.get("nickname") == OWNER_NICKNAME and u.get("id"):
                uid = str(u["id"])
                if uid != self.owner_id:
                    self.owner_id = uid
                    log.info("owner userId learned: %s", uid)
                return

    async def _enter_room(self, room: dict) -> None:
        self.probing = False
        self.room_id = room.get("id", self.room_id)
        if self.room_id:
            STATE["room_id"] = self.room_id
            save_json(STATE_PATH, STATE)
        await self._process_state(room)

    # -- room state ------------------------------------------------------

    async def _on_state(self, room: dict) -> None:
        self._last_state = room or {}
        self._state_event.set()
        if self.probing:
            return  # probe caller decides
        await self._process_state(room)

    async def _process_state(self, room: dict) -> None:
        rid = room.get("id")
        if rid:
            self.room_id = str(rid)
            STATE["room_id"] = self.room_id
            save_json(STATE_PATH, STATE)
        self.room_name = str(room.get("name", "") or "")
        users = room.get("users") or []
        bot_nicks = {NICKNAME, *IGNORE_NICKNAMES}
        self.humans = sum(
            1 for u in users
            if (u.get("nickname") if isinstance(u, dict) else str(u)) not in bot_nicks)
        self.owner_here = self._owner_in(users)
        self._learn_owner(users)
        log.info("in room %s (%s), users=%s, owner_here=%s",
                 self.room_id, self.room_name, len(users), self.owner_here)
        if OWNER_NICKNAME and not ROOM_ID_CFG and not self.created_room \
                and not self.owner_here:
            # landed in a room without the owner (stale saved room / drift)
            log.info("owner %s not here; leaving to search", OWNER_NICKNAME)
            asyncio.create_task(self._leave_and_discover())
            return
        if not ROOM_ID_CFG and not self.created_room and self.humans == 0:
            if self.empty_since is None:
                self.empty_since = time.time()
                log.info("room empty of humans; leaving in 30s unless someone rejoins")
                asyncio.create_task(self._leave_if_still_empty())
        else:
            self.empty_since = None
        current = room.get("currentTrack")
        play = room.get("playState") or {}
        if current and play:
            self.start_listen(current, play)

    async def _leave_if_still_empty(self) -> None:
        await asyncio.sleep(30)
        if (not self._stopped and self.humans == 0 and self.empty_since
                and time.time() - self.empty_since >= 30 and self.sio.connected):
            log.info("room has no real users for 30s; leaving")
            await self._leave_and_discover()

    async def _leave_and_discover(self) -> None:
        if not self.room_id:
            return  # already out; another path handled it
        self.room_id = ""  # claim: concurrent callers become no-ops
        self._cancel_follow()
        self.stop_listen()
        self.empty_since = None
        self.owner_here = False
        self.humans = 0
        try:
            if self.sio.connected:
                await self.sio.emit("room:leave", {})
        except Exception as exc:
            log.warning("room leave failed: %s", exc)
        self.room_name = ""
        STATE.pop("room_id", None)
        STATE.pop("rejoin_token", None)
        save_json(STATE_PATH, STATE)
        if self.sio.connected and not self._stopped:
            await self.sio.emit("room:list")

    # -- owner tracking ---------------------------------------------------

    async def _on_user_joined(self, data: dict) -> None:
        d = data if isinstance(data, dict) else {}
        nick = str(d.get("nickname", "") or "")
        if OWNER_NICKNAME and nick == OWNER_NICKNAME:
            self.owner_here = True
            self.created_room = False  # the waiting room has its human now
            self._cancel_follow()
            if d.get("id"):
                self.owner_id = str(d["id"])
            log.info("owner rejoined; staying")

    async def _on_user_left(self, data: dict) -> None:
        d = data if isinstance(data, dict) else {}
        nick = str(d.get("nickname", "") or "")
        if OWNER_NICKNAME and nick == OWNER_NICKNAME and not self._stopped:
            self.owner_here = False
            self._cancel_follow()
            log.info("owner left the room; following in 3s unless they return")
            self._follow_task = asyncio.create_task(self._follow_owner_left())

    def _cancel_follow(self) -> None:
        if self._follow_task and not self._follow_task.done():
            self._follow_task.cancel()
        self._follow_task = None

    async def _follow_owner_left(self) -> None:
        try:
            await asyncio.sleep(3.0)
        except asyncio.CancelledError:
            return
        if self._stopped or self.owner_here or not self.sio.connected:
            return
        log.info("owner is gone; leaving to follow")
        record_event("sys", f"{OWNER_NICKNAME} 离开了，bot 跟着走了", room=self.room_id)
        await self._leave_and_discover()

    async def _on_room_deleted(self, data: dict) -> None:
        rid = (data or {}).get("roomId")
        if rid and rid == self.room_id:
            log.info("current room was deleted by server; re-discovering")
            await self._leave_and_discover()

    # -- playback ---------------------------------------------------------

    @staticmethod
    def _same_track(a: dict, b: dict) -> bool:
        ia = a.get("urlId") or a.get("title")
        ib = b.get("urlId") or b.get("title")
        return bool(ia) and ia == ib

    def start_listen(self, track: dict, play_state: dict) -> None:
        dur = duration_of(track)
        new_pos = float(play_state.get("currentTime", 0) or 0)
        if dur and new_pos >= dur - 2:
            return  # song already over - a stale state can't restart it
        rev = play_state.get("revision")
        if rev is not None:
            self.last_revision = rev
        sess = self.session
        if sess is not None and self._same_track(sess.track, track) \
                and sess.task is not None and not sess.task.done():
            # Same track — never restart or re-anchor. The analysis continues
            # from where it was, so a reconnect or state refresh can't skip
            # unprocessed segments. Only sync play/pause state.
            playing = bool(play_state.get("isPlaying", False))
            if playing and sess.paused:
                sess.resume(play_state)
            elif not playing and not sess.paused:
                sess.pause(play_state)
            return
        self.stop_listen()
        self.session = ListenSession(track, play_state, room_id=self.room_id)
        task = asyncio.create_task(self.listen_along(track))
        task.add_done_callback(self._listen_done)
        self.session.task = task

    def _listen_done(self, task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.error("listen_along crashed: %r", exc)

    def stop_listen(self) -> None:
        sess = self.session
        if sess:
            for task in (sess.task, sess.sync_task, sess.pause_task, sess.rt_task):
                if task:
                    task.cancel()
        self.session = None

    async def post_chat(self, text: str) -> None:
        if not text or not self.sio.connected or not self.room_id:
            return  # roomless emits get NOT_IN_ROOM errors from the server
        text = strip_relay_ads(text)
        if not text:
            log.warning("DEBUG: post_chat stripped to empty")
            return
        if not self.room_id:
            log.warning("DEBUG: post_chat no room_id")
        gap = time.time() - self._last_post
        if gap < 8.0:
            await asyncio.sleep(3.5 - gap)  # 双模型并行时避免同秒双响
        self._last_post = time.time()
        log.info("chat -> %s", text[:80])
        record_event("danmaku", text[:200], room=self.room_id)
        await self.sio.emit("chat:message", {"content": text[:200]})

    async def wait_until(self, session: ListenSession, target: float) -> bool:
        """Sleep until playback reaches `target`. False if the song ended first."""
        while not self._stopped:
            if session.paused:
                await asyncio.sleep(1.0)
                continue
            pos = session.position()
            if session.duration and pos >= session.duration - 1.0:
                return False
            if pos >= target:
                return True
            await asyncio.sleep(0.5)
        return False

    async def _sync_loop(self, session: ListenSession) -> None:
        """Ask the server for its authority playback clock every ~10s."""
        try:
            while self.sio.connected and not self._stopped:
                await asyncio.sleep(10)
                await self.sio.emit("player:sync_request", {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("sync loop stopped: %s", exc)

    async def _pause_reaction_loop(self, session: ListenSession) -> None:
        """Say something once when the user pauses mid-song (deduped per pause)."""
        try:
            while not self._stopped:
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
                    await self.post_chat(reply[:60])
        except asyncio.CancelledError:
            raise

    async def listen_along(self, track: dict) -> None:
        session = self.session
        if not session:
            return
        title = track.get("title", "?")
        artist = "/".join(track.get("artist") or [])
        log.info("listening along: %s - %s", title, artist)
        record_event("sys", f"开始一起听《{title}》- {artist}", room=self.room_id)

        audio, mime = await download_audio(track)
        segments: list[tuple[float, bytes]] = []
        if audio:
            audio = await self._to_mp3(audio, mime)
            mime = "audio/mp3" if audio else mime
        if audio and mime == "audio/mp3":
            segments = await asyncio.to_thread(
                split_mp3_seconds, audio, SEGMENT_SECONDS, session.duration
            )
            log.info("split into %d segments", len(segments))
        elif audio:
            log.info("audio is %s; cannot split, knowledge mode", mime)

        rt_pcm = None
        if REALTIME_ENABLED and REALTIME_URL and REALTIME_KEY and audio:
            rt_pcm = await self._to_pcm16(audio)

        session.task = asyncio.current_task()
        session.sync_task = asyncio.create_task(self._sync_loop(session))
        session.pause_task = asyncio.create_task(self._pause_reaction_loop(session))

        if rt_pcm is not None:
            if not REALTIME_DUAL:
                if await self._listen_realtime(rt_pcm, session,
                                               max_replies=DANMAKU_PER_SONG,
                                               primary=True):
                    return
                log.warning("realtime unavailable; gemini fallback")
            else:
                session.rt_task = asyncio.create_task(self._listen_realtime(
                    rt_pcm, session, max_replies=REALTIME_MAX_PER_SONG,
                    primary=False))
                log.info("dual commentary: realtime<=%s + gemini<=%s",
                         REALTIME_MAX_PER_SONG, DANMAKU_PER_SONG)

        earlier: list[str] = []
        budget = max(0, DANMAKU_PER_SONG)
        react_at: set[int] = set()
        if segments and budget:
            n = len(segments)
            if budget >= n:
                react_at = set(range(n))
            elif budget == 1:
                react_at = {0}
            else:
                react_at = {int(round(i * (n - 1) / (budget - 1))) for i in range(budget)}
        # Analyse every segment we plan to comment on in the background, right
        # from the top of the song, and let a separate poster loop drop each
        # remark when playback actually reaches its timestamp. Doing it inline
        # (one segment at a time, just before it plays) meant the Gemini call
        # routinely finished after that segment had already passed, so most
        # remarks were skipped and the survivors bunched up.
        scheduled: list[dict] = []

        async def analyze(seg_idx: int, seg_start: float, seg_bytes: bytes) -> None:
            seg_end = min(seg_start + SEGMENT_SECONDS,
                          session.duration or seg_start + SEGMENT_SECONDS)
            prompt = segment_prompt(track, seg_start, seg_end,
                                    session.duration, True, list(earlier))
            parts = [{"text": prompt},
                     {"inline_data": {"mime_type": "audio/mp3",
                                      "data": base64.b64encode(seg_bytes).decode()}}]
            log.info("analyzing segment %s-%ss...", int(seg_start), int(seg_end))
            try:
                parsed = await asyncio.wait_for(
                    asyncio.to_thread(gemini_generate, parts, False, 2, 60),
                    timeout=150)
            except asyncio.TimeoutError:
                log.warning("seg %ss: analysis timed out, skipping", int(seg_start))
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("seg %ss: analysis failed: %r", int(seg_start), exc)
                return
            reactions = clamp_segment_reactions(parsed, seg_start, SEGMENT_SECONDS,
                                                session) if parsed else []
            if not reactions:
                log.info("DEBUG-SEG: seg %ss reactions=%s", int(seg_start),
                         str(parsed)[:200] if parsed else 'None')
                return
            for r in reactions:
                scheduled.append({"at": float(r["at"]), "text": r["text"], "posted": False})
                log.info("scheduled %ss: %s", int(r["at"]), r["text"][:30])

        targets = [(i, s, b) for i, (s, b) in enumerate(segments) if i in react_at]
        analysis: asyncio.Task | None = None
        if targets:
            sem = asyncio.Semaphore(2)

            async def run_one(i: int, s: float, b: bytes) -> None:
                # Stagger by playback position: segment N is analysed roughly
                # ANALYZE_LEAD_SECONDS before it starts playing, so the requests
                # are naturally spaced out instead of all firing at once (a
                # burst of parallel audio uploads is what stalled Gemini).
                if s > 0:
                    if not await self.wait_until(session, max(1.0, s - ANALYZE_LEAD_SECONDS)):
                        return
                async with sem:
                    if self._stopped or not self.sio.connected:
                        return
                    await analyze(i, s, b)

            async def analyze_all() -> None:
                await asyncio.gather(*(run_one(*t) for t in targets))

            analysis = asyncio.create_task(analyze_all())
        try:
            said = 0
            while analysis is not None and not self._stopped and self.sio.connected:
                pending = [x for x in scheduled if not x["posted"]]
                if analysis.done() and not pending:
                    break
                if said >= budget:
                    break
                due = [x for x in pending if x["at"] <= session.position() + 0.3]
                if due:
                    nxt = min(due, key=lambda x: x["at"])
                    nxt["posted"] = True
                    said += 1
                    await self.post_chat(nxt["text"])
                    earlier.append(nxt["text"])
                    session.notes.append(f"{int(nxt['at'])}s {nxt['text'][:20]}")
                    log.info("posted %ss: %s (pos=%.0fs)", int(nxt["at"]),
                             nxt["text"][:20], session.position())
                    continue
                if session.duration and session.position() >= session.duration - 1:
                    break
                await asyncio.sleep(0.5)
        finally:
            if analysis is not None and not analysis.done():
                analysis.cancel()

        # knowledge-mode fallback when audio was unavailable or unsplittable
        if not segments:
            prompt = segment_prompt(track, 0, session.duration or 240,
                                    session.duration, False, [])
            parsed = await asyncio.to_thread(gemini_generate, [{"text": prompt}], True)
            reactions = parsed.get("reactions") or [] if parsed else []
            cleaned = []
            for item in reactions[:DANMAKU_PER_SONG]:
                try:
                    at = max(3.0, float(item.get("at", 0)))
                    text = str(item.get("text", "")).strip()
                except (TypeError, ValueError):
                    continue
                if text and len(text) <= 40:
                    cleaned.append({"at": at, "text": text})
            for r in sorted(cleaned, key=lambda x: x["at"]):
                await self.post_chat(r["text"])
                session.notes.append(f"{int(r['at'])}s {r['text'][:20]}")

        await self._song_closing(session, title, artist)

    async def _to_mp3(self, audio: bytes, mime: str) -> bytes | None:
        """Any audio bytes -> mono 48kbps mp3 via ffmpeg.

        netease serves the 64kbps stream as m4a/AAC, which the in-memory MP3
        frame splitter cannot handle — and the m4a at full bitrate is what
        stalled Gemini for 120s. Re-encoding once up front gives us a
        splittable, ~180KB-per-30s payload for every source.
        """
        if mime == "audio/mp3" and audio[:3] == b"ID3":
            return audio
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-loglevel", "error", "-i", "pipe:0",
                "-vn", "-ac", "1", "-ar", "22050", "-b:a", "48k",
                "-f", "mp3", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE)
            out, _ = await proc.communicate(audio)
            if proc.returncode == 0 and len(out) > 10000:
                log.info("audio transcoded to mp3: %.1f KB (from %s)",
                         len(out) / 1024, mime)
                return out
            log.warning("ffmpeg mp3 transcode failed (rc=%s)", proc.returncode)
        except FileNotFoundError:
            log.warning("ffmpeg not found; cannot transcode %s", mime)
        except Exception as exc:
            log.warning("mp3 transcode error: %s", exc)
        return None

    async def _to_pcm16(self, audio: bytes) -> bytes | None:
        """mp3/m4a bytes -> 16kHz mono s16le PCM via ffmpeg."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-loglevel", "error", "-i", "pipe:0",
                "-ar", "16000", "-ac", "1", "-f", "s16le", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE)
            out, _ = await proc.communicate(audio)
            if proc.returncode == 0 and len(out) > 32000:
                return out
            log.warning("ffmpeg pcm conversion failed (rc=%s)", proc.returncode)
        except FileNotFoundError:
            log.warning("ffmpeg not found; realtime mode unavailable")
        except Exception as exc:
            log.warning("pcm conversion error: %s", exc)
        return None

    async def _listen_realtime(self, pcm: bytes, session: ListenSession,
                               max_replies: int = 0, primary: bool = True) -> bool:
        """Realtime mode: feed the song into a qwen-omni WS session paced to
        the playback clock; the model drops remarks that we post as danmaku
        (still capped by the per-song budget). False = session never came up
        (caller should fall back to the Gemini path)."""
        if max_replies <= 0:
            max_replies = DANMAKU_PER_SONG
        if primary:
            session.task = asyncio.current_task()
            session.sync_task = asyncio.create_task(self._sync_loop(session))
        rt = RealtimeSession()
        rt.start()
        for _ in range(30):
            if rt.ready.is_set():
                break
            await asyncio.sleep(0.5)
        if not rt.ready.is_set():
            log.warning("realtime session did not come up; gemini fallback")
            rt.close()
            return False
        if session.position() * 32000 >= len(pcm):
            log.info("playback already past the available audio; gemini fallback")
            rt.close()
            return False
        log.info("realtime listening: %.0fs pcm (commit every %ss)",
                 len(pcm) / 32000.0, REALTIME_COMMIT_SECONDS)
        record_event("sys", "realtime 模式陪听中", room=self.room_id)
        CHUNK = 3200 * 2  # 200ms of 16k s16le
        since_commit = 0.0
        pos_bytes = 0
        rt_said = 0
        try:
            while not self._stopped and not rt.closed:
                for text in rt.drain_replies():
                    if rt_said >= max_replies or len(session.notes) >= DANMAKU_PER_SONG:
                        rt.close()
                        log.info("realtime: danmaku budget spent; closing session")
                        return True
                    await self.post_chat(text[:60])
                    rt_said += 1
                    session.notes.append(f"{int(session.position())}s {text[:20]}")
                if session.paused:
                    await asyncio.sleep(0.4)
                    continue
                target_bytes = min(int(session.position() * 32000), len(pcm))
                if target_bytes >= len(pcm):
                    if since_commit > 1.0:
                        rt.commit()  # flush the tail
                        for _ in range(12):
                            if rt.drain_replies():
                                break
                            await asyncio.sleep(1)
                    break
                if target_bytes > pos_bytes:
                    if target_bytes > pos_bytes + 32000 * 6:
                        pos_bytes = target_bytes  # big seek: skip ahead
                    data = pcm[pos_bytes:min(pos_bytes + CHUNK, target_bytes)]
                    rt.append(data)
                    pos_bytes += len(data)
                    since_commit += len(data) / 32000.0
                    if since_commit >= REALTIME_COMMIT_SECONDS:
                        rt.commit()
                        since_commit = 0.0
                await asyncio.sleep(0.18)
        finally:
            rt.close()
        if primary:
            await self._song_closing(session,
                                     session.track.get("title", "?"),
                                     "/".join(session.track.get("artist") or []))
        return True

    async def _song_closing(self, session: ListenSession, title: str, artist: str) -> None:
        if session.duration:
            await self.wait_until(session, session.duration - 1.0)
        same_room = bool(self.room_id) and self.room_id == session.room_id
        if session.notes:
            notes = "；".join(session.notes[-4:])
            asyncio.create_task(inject_room_chat(
                CHAT_USER,
                f"我们刚一起听完《{title}》-{artist}。我边听边说的：{notes[:180]}。"
                "用你自己的风格给这段听歌收个尾，一句话，不超过 40 字。"))
            if same_room:
                await self.post_chat("🎵 听完了。")

    async def _on_connect(self, *args) -> None:
        log.info("connected to %s", SERVER)

    async def _on_disconnect(self, *args) -> None:
        log.warning("disconnected; will retry")

    async def _on_play(self, data: dict) -> None:
        track = (data or {}).get("track")
        play = (data or {}).get("playState") or {}
        if not track:
            return
        revision = play.get("revision")
        if revision is not None and revision == self.last_revision \
                and self.session is not None:
            return
        self.start_listen(track, play)

    async def _on_pause(self, data: dict) -> None:
        if self.session:
            self.session.pause(data or {})
            log.info("playback paused; bot pauses listening too")

    async def _on_resume(self, data: dict) -> None:
        if self.session:
            self.session.resume(data or {})
            log.info("playback resumed")

    async def _on_seek(self, data: dict) -> None:
        if self.session:
            self.session.seek(data or {})

    async def _on_sync(self, data: dict) -> None:
        if self.session:
            self.session.sync(data or {})

    async def _on_next(self, _data: dict) -> None:
        self.stop_listen()

    # -- chat --------------------------------------------------------------

    async def _on_chat(self, message: dict) -> None:
        content = str(message.get("content", ""))
        user = str(message.get("nickname", ""))
        msg_type = str(message.get("type", "user"))
        log.info("chat <- %s: %s", user or "?", content[:60])
        if msg_type == "system":
            return
        if not content or user == NICKNAME or user in IGNORE_NICKNAMES:
            return  # own messages and fellow bots (e.g. the MaiBot companion)
        if not REPLY_ALL_CHAT and NICKNAME not in content \
                and not content.startswith("@" + NICKNAME):
            return
        now = time.time()
        if now - self.last_chat_reply < 8:
            return  # avoid rapid-fire loops
        self.last_chat_reply = now
        clean = content.replace(NICKNAME, "").strip()
        if not clean:
            return
        if self.session is None:
            # Between songs / not listening — still answer, minus song context.
            replies = await inject_room_chat(user, clean[:100])
            reply = "".join(replies).strip()
            if not reply:
                reply = await asyncio.to_thread(
                    gemini_text,
                    f"{PERSONA}\n用户在房间聊天里说：“{clean[:100]}”。"
                    "简短回应，不超过 50 字，只输出回应本身。")
            if reply:
                await self.post_chat(reply[:120])
            return
        title = self.session.track.get("title", "")
        artist = "/".join(self.session.track.get("artist") or [])
        pos = int(self.session.position())
        # 正文以用户原话开头(记忆桶里存的是这句),场景作为简短上下文跟在后面
        replies = await inject_room_chat(
            user,
            f"{clean[:100]}\n"
            f"[听歌场景: 《{title}》-{artist} 第{pos}秒, 简短口语回应, ≤50字]",
        )
        reply = "".join(replies).strip()
        if not reply:
            reply = await asyncio.to_thread(
                gemini_text,
                f"{PERSONA}\n你们正在一起听《{title}》-{artist}，播放到第 {pos} 秒。"
                f"用户说：“{clean[:100]}”。简短回应，不超过 50 字，只输出回应本身。")
        if reply:
            await self.post_chat(reply[:120])

    # -- votes ---------------------------------------------------------------

    async def _on_vote_started(self, data: dict) -> None:
        """Mirror the owner's vote: they approve → bot approves, they reject →
        bot rejects, they don't vote → bot stays out of it entirely."""
        if not AUTO_VOTE or not isinstance(data, dict):
            return
        if not self.owner_id:
            return  # owner unknown in this room — never push votes either way
        vid = str(data.get("id", "") or "")
        if vid and vid in self._voted_ids:
            return  # already mirrored this vote
        votes = data.get("votes")
        if not isinstance(votes, dict) or self.owner_id not in votes:
            return  # owner hasn't voted; server re-broadcasts on every cast
        owner_choice = bool(votes[self.owner_id])
        action = str(data.get("action", "") or "?")
        by = str(data.get("initiatorNickname", "") or "?")
        label = VOTE_LABELS.get(action, action)
        choice = "同意" if owner_choice else "反对"
        log.info("vote %s (by %s): owner %s — following", label, by, choice)
        record_event("sys", f"「{label}」投票：主人{choice}，bot 跟随", room=self.room_id)
        if vid:
            self._voted_ids.add(vid)
            if len(self._voted_ids) > 50:
                self._voted_ids = set(sorted(self._voted_ids)[-25:])
        await asyncio.sleep(random.uniform(0.8, 2.0))
        if self._stopped or not self.sio.connected:
            return
        await self.sio.emit("vote:cast", {"approve": owner_choice})

    async def _on_vote_result(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        action = str(data.get("action", "") or "?")
        label = VOTE_LABELS.get(action, action)
        outcome = "通过" if data.get("passed") else "未通过"
        log.info("vote %s: %s", label, outcome)
        record_event("sys", f"「{label}」投票{outcome}", room=self.room_id)


# ---------------------------------------------------------------------------

async def main() -> None:
    global BOT
    apply_overrides()  # layer dashboard-saved overrides on top of base config
    if not SERVER:
        raise SystemExit("server_url missing in music-bot configuration")
    if not IDENTITY_SECRET:
        raise SystemExit("identity_secret missing in music-bot configuration")
    if not GEMINI_ENDPOINT or not GEMINI_KEY:
        raise SystemExit("gemini_endpoint and gemini_key are required")
    if _CONTEXT is not None:
        await wait_for_platform()  # let AstrBot finish platform startup
    bot = MusicBot()
    BOT = bot
    await bot.run()


async def shutdown() -> None:
    """Stop active work and release the Socket.IO connection."""
    global BOT
    bot = BOT
    BOT = None
    if bot is not None:
        try:
            await bot.stop()
        except Exception:
            log.exception("music-bot shutdown failed")
    await stop_dashboard()


if __name__ == "__main__":
    asyncio.run(main())
