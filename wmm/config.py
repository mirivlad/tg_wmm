"""Validated environment configuration; no user secrets in repository."""
from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class Settings:
    bot_token: str
    allowed_ids: frozenset[int]
    database_path: str
    timezone: str
    proxy_url: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token or ":" not in token:
            raise ValueError("BOT_TOKEN is required (obtain from @BotFather).")
        raw = os.getenv("ALLOWED_USER_IDS", "").strip()
        try:
            ids = frozenset(int(x.strip()) for x in raw.split(",") if x.strip())
        except ValueError as exc:
            raise ValueError("ALLOWED_USER_IDS must contain comma-separated Telegram numeric IDs.") from exc
        if not ids or any(x <= 0 for x in ids):
            raise ValueError("Set ALLOWED_USER_IDS (fail-closed: public access is disabled).")
        zone = os.getenv("TIMEZONE", "Europe/Moscow").strip()
        try:
            ZoneInfo(zone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Invalid TIMEZONE: {zone}") from exc
        return cls(token, ids, os.getenv("DATABASE_PATH", "/data/wmm.db"),
                   zone, os.getenv("PROXY_URL", "").strip() or None)
