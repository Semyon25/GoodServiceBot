"""Старт, переключение меню, отмена ввода и обработка ошибок."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message

from bot.domain.errors import DomainError
from bot.domain.models import Actor
from bot.services import Services

from .. import texts as t
from ..callbacks import MenuCb
from ..keyboards import admin_menu, client_menu, menu_for
from ..middlewares import IsAdmin
from ..notifier import Notifier

log = logging.getLogger(__name__)
router = Router(name="common")


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext, actor: Actor, services: Services) -> None:
    await state.clear()
    if actor.is_admin:
        await message.answer(
            "Панель администратора. Для проверки клиентских функций нажмите «👤 Режим клиента».",
            reply_markup=admin_menu(),
        )
        return
    if actor.user.is_executor:
        await message.answer("Меню исполнителя. Выберите список заявок.", reply_markup=menu_for(actor))
        return
    welcome = await services.settings.text("texts.welcome")
    address = await services.addresses.current(actor.user.id)
    text = t.h(welcome)
    if not address:
        text += "\n\nДля начала укажите адрес — нажмите «🏠 Мой адрес»."
    await message.answer(text, reply_markup=client_menu())


@router.message(F.text == t.BTN_A_CLIENT_MODE, IsAdmin())
async def to_client_mode(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Режим клиента. Вернуться: «🛠 Админ-панель».", reply_markup=client_menu(is_admin=True))


@router.message(F.text == t.BTN_ADMIN_PANEL, IsAdmin())
async def to_admin_mode(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Панель администратора.", reply_markup=admin_menu())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, actor: Actor) -> None:
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=menu_for(actor))


@router.callback_query(MenuCb.filter(F.action == "cancel_input"))
async def cancel_input(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    if callback.message:
        await callback.message.edit_text("Действие отменено.")


fallback_router = Router(name="fallback")


@fallback_router.message()
async def unknown_message(message: Message, actor: Actor) -> None:
    await message.answer("Не понял сообщение. Воспользуйтесь кнопками меню.", reply_markup=menu_for(actor))


@fallback_router.callback_query()
async def unknown_callback(callback: CallbackQuery) -> None:
    await callback.answer("Кнопка устарела или недоступна.", show_alert=True)


errors_router = Router(name="errors")


@errors_router.errors()
async def on_error(event: ErrorEvent, notifier: Notifier) -> bool:
    exc = event.exception
    update = event.update
    callback = update.callback_query
    message = update.message
    if isinstance(exc, DomainError):
        text = exc.message
    else:
        log.exception("Ошибка обработки обновления %s", update.update_id, exc_info=exc)
        text = t.TEMP_ERROR
        user_id = (callback or message).from_user.id if (callback or message) and (callback or message).from_user else "?"
        await notifier.internal_error(
            f"{type(exc).__name__}: {exc}\nupdate_id={update.update_id}, user={user_id}", key=type(exc).__name__
        )
    try:
        if callback:
            await callback.answer(text[:200], show_alert=True)
        elif message:
            await message.answer(t.h(text))
    except Exception:  # noqa: BLE001 — ответить не удалось, ошибка уже залогирована
        log.debug("Не удалось сообщить пользователю об ошибке", exc_info=True)
    return True
