"""AstrBot entrypoint for the Music Together listening companion."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Star

import bot


def _config_dict(config: AstrBotConfig) -> dict[str, Any]:
    if isinstance(config, Mapping):
        return dict(config)
    keys = (
        "enabled", "server_url", "identity_secret", "room_id", "nickname",
        "create_if_missing", "gemini_endpoint", "gemini_key", "gemini_model",
        "proxy", "max_reactions", "segment_seconds", "analyze_lead_seconds",
        "persona", "astrbot_chat_url", "astrbot_api_key", "astrbot_session",
        "astrbot_username", "astrbot_provider", "reply_all_chat",
    )
    return {key: config.get(key) for key in keys}


class MusicBotPlugin(Star):
    """Own the listener task from AstrBot's plugin lifecycle."""

    def __init__(self, context: Any, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        bot.apply_config(_config_dict(config))
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


def create_plugin(context: Any, config: AstrBotConfig) -> MusicBotPlugin:
    return MusicBotPlugin(context, config)
