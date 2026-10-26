"""Загрузка конфигурации приложения из переменных окружения / файла .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: tuple[int, ...]  # порядок важен: на первого назначаются заявки по умолчанию
    db_path: Path
    timezone: ZoneInfo
    log_level: str
    jobs_interval_seconds: int
    proxy_url: str | None = None


def _parse_admin_ids(raw: str) -> tuple[int, ...]:
    ids: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            admin_id = int(part)
        except ValueError as exc:
            raise ConfigError(f"ADMIN_IDS: некорректный Telegram ID «{part}»") from exc
        if admin_id not in ids:
            ids.append(admin_id)
    return tuple(ids)


def load_config(env_file: str | os.PathLike | None = None) -> Config:
    load_dotenv(env_file or Path.cwd() / ".env")

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("Не задан BOT_TOKEN (см. .env.example)")

    admin_ids = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
    if not admin_ids:
        raise ConfigError("Не задан ни один администратор в ADMIN_IDS (см. .env.example)")

    return Config(
        bot_token=token,
        admin_ids=admin_ids,
        db_path=Path(os.getenv("DB_PATH", "data/bot.sqlite3")),
        timezone=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        jobs_interval_seconds=int(os.getenv("JOBS_INTERVAL_SECONDS", "60")),
        proxy_url=os.getenv("PROXY_URL", "").strip() or None,
    )
