from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.db.connection import Database
from bot.db.migrations import migrate
from bot.db.repositories import Repositories
from bot.domain.enums import Role
from bot.domain.models import Actor
from bot.services import Services

MSK = ZoneInfo("Europe/Moscow")
ADMIN_TG = 1000


def msk(*args) -> datetime:
    return datetime(*args, tzinfo=MSK)


class FakeClock:
    def __init__(self, moment: datetime):
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def set(self, *args) -> None:
        self.moment = msk(*args)


class Env:
    def __init__(self, db: Database, services: Services, clock: FakeClock):
        self.db = db
        self.services = services
        self.clock = clock
        self.repos: Repositories = services.repos

    async def actor(self, tg_id: int, role: Role = Role.CLIENT, username: str | None = None) -> Actor:
        actor = await self.services.users.actor(tg_id, username)
        if actor.user.role != role:
            await self.repos.users.set_role(actor.user.id, role.value)
            actor = await self.services.users.actor(tg_id, username)
        return actor

    async def admin(self) -> Actor:
        return await self.actor(ADMIN_TG, username="admin")

    async def service(self):
        return await self.services.catalog.service()

    async def client_with_address(self, tg_id: int = 10) -> Actor:
        actor = await self.actor(tg_id, username=f"client{tg_id}")
        address = (await self.services.addresses.list_addresses())[0]
        await self.services.addresses.save_user_address(actor, actor.user.id, address.id, 3, 5, "45", "45K1234", None)
        return actor

    async def subscribe(self, actor: Actor, tariff_title: str = "Неделя"):
        tariffs = await self.services.catalog.tariffs()
        tariff = next(t for t in tariffs if t.title == tariff_title)
        payment = await self.services.payments.select_tariff(actor, tariff.id)
        await self.services.payments.submit_proof(actor, payment.id, "photo", "file-id", None)
        return await self.services.payments.confirm(await self.admin(), payment.id)


@pytest.fixture
async def env(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    await db.connect()
    await migrate(db)
    clock = FakeClock(msk(2026, 10, 21, 20, 0))  # среда, 20:00 — идёт приём заявок
    services = Services(Repositories(db), frozenset({ADMIN_TG}), MSK, clock)
    yield Env(db, services, clock)
    await db.close()
