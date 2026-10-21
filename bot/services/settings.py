"""Настройки сервиса, изменяемые администратором (хранятся в system_settings)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from bot.db.repositories import Repositories
from bot.domain.enums import DistributionMode
from bot.domain.errors import ValidationError
from bot.domain.models import Actor, Service
from bot.domain.schedule import ScheduleConfig, format_hhmm, parse_hhmm, validate_schedule

from .access import require_admin

SCHEDULE_KEYS = ("work_days", "accept_start", "accept_end", "work_end")

PAYMENT_KEYS = {
    "payment.sbp_phone": "Номер телефона получателя",
    "payment.sbp_bank": "Банк получателя",
    "payment.sbp_recipient": "Имя получателя",
    "payment.instructions": "Инструкция по переводу",
}

TEXT_KEYS = {
    "texts.welcome": "Приветствие (/start)",
    "texts.help": "Текст раздела «Помощь»",
}


class Clock:
    def __init__(self, tz: ZoneInfo):
        self.tz = tz

    def now(self) -> datetime:
        return datetime.now(self.tz)


class SettingsService:
    def __init__(self, repos: Repositories, tz: ZoneInfo, clock: Callable[[], datetime]):
        self.repos = repos
        self.tz = tz
        self._now = clock
        self._cache: dict[str, Any] | None = None

    async def all(self) -> dict[str, Any]:
        if self._cache is None:
            self._cache = await self.repos.settings.get_all()
        return self._cache

    async def get(self, key: str, default: Any = None) -> Any:
        return (await self.all()).get(key, default)

    async def set(self, actor: Actor, key: str, value: Any) -> None:
        require_admin(actor)
        async with self.repos.transaction():
            await self.repos.settings.set(key, value, actor.tg_id, self._now())
            await self.repos.audit.log(actor.tg_id, "settings.set", f"{key} = {value!r}", self._now())
        self._cache = None

    # --- расписание ---

    async def schedule_data(self, service: Service | None = None) -> dict[str, Any]:
        settings = await self.all()
        data = {k: settings.get(f"schedule.{k}") for k in SCHEDULE_KEYS}
        data = {k: v for k, v in data.items() if v is not None}
        if service and service.schedule:
            data.update(service.schedule)
        return data

    async def schedule(self, service: Service | None = None) -> ScheduleConfig:
        return ScheduleConfig.from_settings(await self.schedule_data(service), self.tz)

    async def set_schedule_time(self, actor: Actor, key: str, raw: str) -> None:
        require_admin(actor)
        if key not in ("accept_start", "accept_end", "work_end"):
            raise ValidationError("Неизвестный параметр расписания.")
        try:
            value = format_hhmm(parse_hhmm(raw))
        except ValueError:
            raise ValidationError("Введите время в формате ЧЧ:ММ, например 19:00.") from None
        data = await self.schedule_data()
        data[key] = value
        self._validate(data)
        await self.set(actor, f"schedule.{key}", value)

    async def toggle_work_day(self, actor: Actor, weekday: int) -> None:
        require_admin(actor)
        data = await self.schedule_data()
        days = set(int(d) for d in data.get("work_days", []))
        days ^= {weekday}
        data["work_days"] = sorted(days)
        self._validate(data)
        await self.set(actor, "schedule.work_days", sorted(days))

    def _validate(self, data: dict[str, Any]) -> None:
        try:
            validate_schedule(ScheduleConfig.from_settings(data, self.tz))
        except ValueError as exc:
            raise ValidationError(str(exc)) from None

    # --- распределение заявок ---

    async def distribution_mode(self) -> DistributionMode:
        return DistributionMode(await self.get("orders.distribution_mode", DistributionMode.MANUAL.value))

    async def unassigned_alert_minutes(self) -> int:
        return int(await self.get("orders.unassigned_alert_minutes", 15))

    # --- оплата ---

    async def payment_details(self) -> dict[str, str]:
        settings = await self.all()
        return {key: str(settings.get(key, "")) for key in PAYMENT_KEYS}
