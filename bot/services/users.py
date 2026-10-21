"""Пользователи, роли и адреса клиентов."""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from bot.db.connection import IntegrityError
from bot.db.repositories import Repositories
from bot.domain.enums import Role, UserStatus
from bot.domain.errors import AccessDenied, NotFound, ValidationError
from bot.domain.models import Actor, Address, User, UserAddress

from .access import require_active, require_admin

MAX_FLOOR = 100
MIN_FLOOR = -5
MAX_APARTMENT_LEN = 10
MAX_INTERCOM_LEN = 32
MAX_INSTRUCTIONS_LEN = 500


def parse_floor(raw: str) -> int:
    try:
        floor = int(raw.strip())
    except ValueError:
        raise ValidationError("Этаж нужно указать числом, например 7.") from None
    if not MIN_FLOOR <= floor <= MAX_FLOOR:
        raise ValidationError(f"Этаж должен быть от {MIN_FLOOR} до {MAX_FLOOR}.")
    return floor


def parse_apartment(raw: str) -> str:
    value = raw.strip()
    if not value or len(value) > MAX_APARTMENT_LEN:
        raise ValidationError(f"Номер квартиры должен содержать от 1 до {MAX_APARTMENT_LEN} символов.")
    return value


def parse_intercom(raw: str) -> str:
    value = raw.strip()
    if not value or len(value) > MAX_INTERCOM_LEN:
        raise ValidationError(f"Код домофона должен содержать от 1 до {MAX_INTERCOM_LEN} символов.")
    return value


def parse_instructions(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    if len(value) > MAX_INSTRUCTIONS_LEN:
        raise ValidationError(f"Инструкции не должны быть длиннее {MAX_INSTRUCTIONS_LEN} символов.")
    return value or None


class UserService:
    def __init__(self, repos: Repositories, admin_ids: frozenset[int], clock: Callable[[], datetime]):
        self.repos = repos
        self.admin_ids = admin_ids
        self._now = clock

    async def register(self, tg_id: int, username: str | None) -> User:
        return await self.repos.users.upsert(tg_id, username, self._now())

    async def actor(self, tg_id: int, username: str | None) -> Actor:
        user = await self.register(tg_id, username)
        return Actor(user=user, is_admin=tg_id in self.admin_ids)

    async def get(self, user_id: int) -> User:
        user = await self.repos.users.get(user_id)
        if not user:
            raise NotFound("Пользователь не найден.")
        return user

    async def find(self, actor: Actor, query: str) -> User | None:
        require_admin(actor)
        query = query.strip()
        if query.lstrip("-").isdigit():
            return await self.repos.users.get_by_tg(int(query))
        return await self.repos.users.get_by_username(query)

    async def list_executors(self, active_only: bool = True) -> list[User]:
        executors = await self.repos.users.list(role=Role.EXECUTOR)
        if active_only:
            executors = [u for u in executors if not u.is_blocked]
        return executors

    async def set_role(self, actor: Actor, user_id: int, role: Role) -> User:
        require_admin(actor)
        user = await self.get(user_id)
        async with self.repos.transaction():
            await self.repos.users.set_role(user_id, role.value)
            await self.repos.audit.log(actor.tg_id, "user.role", f"{user.display}: {user.role} → {role.value}", self._now())
        return await self.get(user_id)

    async def set_status(self, actor: Actor, user_id: int, status: UserStatus) -> User:
        require_admin(actor)
        user = await self.get(user_id)
        if user.tg_id in self.admin_ids and status == UserStatus.BLOCKED:
            raise ValidationError("Нельзя заблокировать администратора.")
        async with self.repos.transaction():
            await self.repos.users.set_status(user_id, status.value)
            await self.repos.audit.log(
                actor.tg_id, "user.status", f"{user.display}: {user.status} → {status.value}", self._now()
            )
        return await self.get(user_id)


class AddressService:
    def __init__(self, repos: Repositories, clock: Callable[[], datetime]):
        self.repos = repos
        self._now = clock

    async def list_addresses(self, active_only: bool = True) -> list[Address]:
        return await self.repos.addresses.list(active_only)

    async def get_address(self, address_id: int) -> Address:
        address = await self.repos.addresses.get(address_id)
        if not address:
            raise NotFound("Адрес не найден.")
        return address

    async def current(self, user_id: int) -> UserAddress | None:
        return await self.repos.user_addresses.get_current(user_id)

    async def save_user_address(
        self,
        actor: Actor,
        user_id: int,
        address_id: int,
        section: int,
        floor: int,
        apartment: str,
        intercom_code: str,
        instructions: str | None,
    ) -> UserAddress:
        require_active(actor)
        if actor.user.id != user_id and not actor.is_admin:
            raise AccessDenied()
        address = await self.get_address(address_id)
        if not address.is_active:
            raise ValidationError("Этот адрес сейчас не обслуживается. Выберите другой.")
        if not 1 <= section <= address.sections_count:
            raise ValidationError(f"Для адреса «{address.title}» доступны секции с 1 по {address.sections_count}.")
        floor = parse_floor(str(floor))
        apartment = parse_apartment(apartment)
        intercom_code = parse_intercom(intercom_code)
        instructions = parse_instructions(instructions)
        try:
            await self.repos.user_addresses.replace_current(
                user_id, address_id, section, floor, apartment, intercom_code, instructions, self._now()
            )
        except IntegrityError as exc:
            if "section_out_of_range" in str(exc):
                raise ValidationError("Номер секции превышает количество секций адреса.") from None
            raise
        if actor.user.id != user_id:
            await self.repos.audit.log(actor.tg_id, "user.address", f"user_id={user_id}", self._now())
        current = await self.current(user_id)
        assert current is not None
        return current

    # --- управление списком адресов (администратор) ---

    async def create_address(self, actor: Actor, title: str, sections_count: int) -> Address:
        require_admin(actor)
        title = title.strip()
        if not title or len(title) > 100:
            raise ValidationError("Название адреса должно содержать от 1 до 100 символов.")
        if not 1 <= sections_count <= 100:
            raise ValidationError("Количество секций должно быть от 1 до 100.")
        try:
            address_id = await self.repos.addresses.create(title, sections_count)
        except IntegrityError:
            raise ValidationError("Адрес с таким названием уже существует.") from None
        await self.repos.audit.log(actor.tg_id, "address.create", f"{title}, секций: {sections_count}", self._now())
        return await self.get_address(address_id)

    async def set_sections(self, actor: Actor, address_id: int, sections_count: int) -> Address:
        require_admin(actor)
        if not 1 <= sections_count <= 100:
            raise ValidationError("Количество секций должно быть от 1 до 100.")
        address = await self.get_address(address_id)
        conflicts = await self.repos.user_addresses.count_with_section_above(address_id, sections_count)
        if conflicts:
            raise ValidationError(
                f"У {conflicts} клиентов указана секция больше {sections_count}. "
                "Сначала измените их адреса."
            )
        await self.repos.addresses.update(address_id, sections_count=sections_count)
        await self.repos.audit.log(
            actor.tg_id, "address.sections", f"{address.title}: {address.sections_count} → {sections_count}", self._now()
        )
        return await self.get_address(address_id)

    async def toggle_active(self, actor: Actor, address_id: int) -> Address:
        require_admin(actor)
        address = await self.get_address(address_id)
        await self.repos.addresses.update(address_id, is_active=int(not address.is_active))
        await self.repos.audit.log(
            actor.tg_id, "address.active", f"{address.title}: {'выкл' if address.is_active else 'вкл'}", self._now()
        )
        return await self.get_address(address_id)
