"""AstrBot entrypoint for the Music Together listening companion.

Room chat is injected into AstrBot's native pipeline (webchat adapter, or
aiocqhttp for a QQ session bucket) so conversations and memories land in a
real session bucket. Reaction danmaku (♪, segment comments, pause acks,
song closers) goes straight back over the socket and is never recorded as
conversation.
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star, register

_BASE = Path(__file__).resolve().parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

import bot

_KEYS = (
    "enabled", "server_url", "identity_secret", "room_id", "nickname",
    "create_if_missing", "gemini_endpoint", "gemini_key", "gemini_model",
    "proxy", "max_reactions", "segment_seconds", "analyze_lead_seconds",
    "persona", "chat_session_id", "chat_platform_id", "chat_user",
    "reply_all_chat", "dashboard_port", "dashboard_host",
)


def _config_dict(config: AstrBotConfig) -> dict[str, Any]:
    if isinstance(config, Mapping):
        return {key: config[key] for key in _KEYS if key in config}
    return {key: config.get(key) for key in _KEYS}


@register("astrbot_plugin_music_bot", "Tauru-t6", "Music Together Companion", "0.3.1")
class MusicBotPlugin(Star):
    """Own the listener task from AstrBot's plugin lifecycle."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._task: asyncio.Task | None = None

    async def initialize(self) -> None:
        bot.apply_config(_config_dict(self.config))
        bot.bind_context(self.context)
        try:
            from astrbot.api.star import StarTools
            data_dir = StarTools.get_data_dir("astrbot_plugin_music_bot")
            bot.set_state_path(Path(data_dir) / "state.json")
        except Exception:
            pass
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        enabled = self.config.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in {"0", "false", "off", "no"}
        if not enabled:
            logger.info("[music-bot] disabled by configuration")
            return
        try:
            await bot.main()
        except asyncio.CancelledError:
            raise
        except SystemExit as exc:
            logger.error("[music-bot] not started: %s", exc)
        except Exception:
            logger.exception("[music-bot] listener stopped unexpectedly")

    async def terminate(self) -> None:
        await bot.shutdown()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
