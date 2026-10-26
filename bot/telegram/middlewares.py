"""Middleware и фильтры ролей."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F
from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.domain.errors import UserBlocked
from bot.domain.models import Actor
from bot.services import Services

from .texts import ALL_MENU_BUTTONS

# сообщение — не нажатие кнопки главного меню (меню должно прерывать ввод в формах)
NOT_MENU = ~F.text.in_(ALL_MENU_BUTTONS)


class ActorMiddleware(BaseMiddleware):
    """Регистрирует пользователя по Telegram ID и кладёт в data['actor']. Блокирует заблокированных."""

    def __init__(self, services: Services):
        self.services = services

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return None
        actor = await self.services.users.actor(tg_user.id, tg_user.username)
        data["actor"] = actor
        if actor.user.is_blocked and not actor.is_admin:
            text = UserBlocked().message
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            elif isinstance(event, Message):
                await event.answer(text)
            return None
        return await handler(event, data)


class PrivateChatMiddleware(BaseMiddleware):
    """Бот работает только в личных сообщениях."""

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        chat = data.get("event_chat")
        if chat is not None and chat.type != "private":
            return None
        return await handler(event, data)


class IsAdmin(Filter):
    async def __call__(self, event: TelegramObject, actor: Actor | None = None) -> bool:
        return bool(actor and actor.is_admin)


class IsExecutor(Filter):
    """Исполнители и администраторы (администратор всегда может выполнять заявки)."""

    async def __call__(self, event: TelegramObject, actor: Actor | None = None) -> bool:
        return bool(actor and (actor.is_admin or (actor.user.is_executor and not actor.user.is_blocked)))


class IsClient(Filter):
    """Клиентские функции: клиенты и администраторы (в режиме клиента), но не исполнители."""

    async def __call__(self, event: TelegramObject, actor: Actor | None = None) -> bool:
        return bool(actor and (actor.is_admin or not actor.user.is_executor))
