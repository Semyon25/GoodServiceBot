"""Общее для админ-разделов: роутер с проверкой роли и ввод текстовых значений."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, Message

from bot.domain.errors import DomainError, ValidationError
from bot.domain.models import Actor
from bot.services import Services

from .. import texts as t
from ..keyboards import cancel_input
from ..middlewares import NOT_MENU, IsAdmin
from ..notifier import Notifier
from ..states import AdminInput

InputHandler = Callable[[Message, dict[str, Any], Actor, Services, Notifier], Awaitable[None]]
_INPUT_HANDLERS: dict[str, InputHandler] = {}


def admin_router(name: str) -> Router:
    router = Router(name=name)
    router.message.filter(IsAdmin())
    router.callback_query.filter(IsAdmin())
    return router


def input_handler(kind: str) -> Callable[[InputHandler], InputHandler]:
    """Регистрирует обработчик введённого администратором текста для данного kind."""

    def decorator(func: InputHandler) -> InputHandler:
        _INPUT_HANDLERS[kind] = func
        return func

    return decorator


async def ask_input(
    message: Message,
    state: FSMContext,
    kind: str,
    prompt: str,
    markup: InlineKeyboardMarkup | None = None,
    **params: Any,
) -> None:
    await state.clear()
    await state.set_state(AdminInput.value)
    await state.update_data(input_kind=kind, **params)
    await message.answer(prompt, reply_markup=markup or cancel_input())


router = admin_router("admin_input")


@router.message(AdminInput.value, F.text, NOT_MENU)
async def admin_input(message: Message, state: FSMContext, actor: Actor, services: Services,
                      notifier: Notifier) -> None:
    data = await state.get_data()
    handler = _INPUT_HANDLERS.get(data.get("input_kind", ""))
    if handler is None:
        await state.clear()
        await message.answer("Ввод устарел, начните действие заново.")
        return
    try:
        await handler(message, data, actor, services, notifier)
    except DomainError as exc:
        # оставляем состояние — администратор может ввести значение ещё раз
        await message.answer(f"{t.h(exc.message)}\nПопробуйте ещё раз или нажмите «Отмена».", reply_markup=cancel_input())
        return
    await state.clear()


@router.message(AdminInput.value, NOT_MENU)
async def admin_input_wrong(message: Message) -> None:
    await message.answer("Ожидается текстовое сообщение.", reply_markup=cancel_input())


def parse_int(raw: str, what: str) -> int:
    try:
        return int(raw.strip().replace(" ", ""))
    except ValueError:
        raise ValidationError(f"{what}: нужно целое число.") from None
