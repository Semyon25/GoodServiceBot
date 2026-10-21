"""Услуги и тарифы."""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from bot.db.repositories import Repositories
from bot.domain.errors import NotFound, ValidationError
from bot.domain.models import Actor, Service, Tariff

from .access import require_admin

DEFAULT_SERVICE_CODE = "trash"


class CatalogService:
    def __init__(self, repos: Repositories, clock: Callable[[], datetime]):
        self.repos = repos
        self._now = clock

    async def service(self, code: str = DEFAULT_SERVICE_CODE) -> Service:
        service = await self.repos.services.get_by_code(code)
        if not service:
            raise NotFound(f"Услуга «{code}» не найдена.")
        return service

    async def service_by_id(self, service_id: int) -> Service:
        service = await self.repos.services.get(service_id)
        if not service:
            raise NotFound("Услуга не найдена.")
        return service

    async def tariffs(self, service_id: int | None = None, active_only: bool = True) -> list[Tariff]:
        return await self.repos.tariffs.list(service_id, active_only)

    async def tariff(self, tariff_id: int) -> Tariff:
        tariff = await self.repos.tariffs.get(tariff_id)
        if not tariff:
            raise NotFound("Тариф не найден.")
        return tariff

    async def create_tariff(
        self, actor: Actor, service_id: int, title: str, price_rub: int, duration_days: int
    ) -> Tariff:
        require_admin(actor)
        title = title.strip()
        if not title or len(title) > 50:
            raise ValidationError("Название тарифа должно содержать от 1 до 50 символов.")
        self._check_price(price_rub)
        self._check_duration(duration_days)
        await self.service_by_id(service_id)
        tariff_id = await self.repos.tariffs.create(service_id, title, price_rub, duration_days)
        await self.repos.audit.log(
            actor.tg_id, "tariff.create", f"{title}: {price_rub} ₽ / {duration_days} дн.", self._now()
        )
        return await self.tariff(tariff_id)

    async def update_tariff(self, actor: Actor, tariff_id: int, **fields) -> Tariff:
        """Меняет тариф для новых покупок. Оплаченные подписки хранят свои сумму и срок."""
        require_admin(actor)
        tariff = await self.tariff(tariff_id)
        if "price_rub" in fields:
            self._check_price(fields["price_rub"])
        if "duration_days" in fields:
            self._check_duration(fields["duration_days"])
        if "is_active" in fields:
            fields["is_active"] = int(bool(fields["is_active"]))
        await self.repos.tariffs.update(tariff_id, **fields)
        await self.repos.audit.log(actor.tg_id, "tariff.update", f"{tariff.title}: {fields}", self._now())
        return await self.tariff(tariff_id)

    @staticmethod
    def _check_price(price: int) -> None:
        if not isinstance(price, int) or not 1 <= price <= 1_000_000:
            raise ValidationError("Стоимость должна быть целым числом рублей от 1 до 1 000 000.")

    @staticmethod
    def _check_duration(days: int) -> None:
        if not isinstance(days, int) or not 1 <= days <= 366:
            raise ValidationError("Срок действия должен быть от 1 до 366 дней.")
