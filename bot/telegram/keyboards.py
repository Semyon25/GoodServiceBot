"""Клавиатуры."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.domain.models import Actor

from . import texts as t
from .callbacks import MenuCb


def _reply(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=b) for b in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


def client_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [t.BTN_ORDER],
        [t.BTN_SUBSCRIPTION, t.BTN_ADDRESS],
        [t.BTN_TARIFFS, t.BTN_MY_ORDERS],
        [t.BTN_HELP],
    ]
    if is_admin:
        rows.append([t.BTN_ADMIN_PANEL])
    return _reply(rows)


def executor_menu() -> ReplyKeyboardMarkup:
    return _reply(
        [
            [t.BTN_EX_NEW, t.BTN_EX_ASSIGNED],
            [t.BTN_EX_IN_WORK, t.BTN_EX_DONE],
            [t.BTN_EX_HISTORY],
        ]
    )


def admin_menu() -> ReplyKeyboardMarkup:
    return _reply(
        [
            [t.BTN_A_ORDERS, t.BTN_A_PAYMENTS],
            [t.BTN_A_USERS, t.BTN_A_TARIFFS],
            [t.BTN_A_SCHEDULE, t.BTN_A_ADDRESSES],
            [t.BTN_A_SBP, t.BTN_A_SETTINGS],
            [t.BTN_A_STATS, t.BTN_A_EXPORT],
            [t.BTN_A_CLIENT_MODE],
        ]
    )


def menu_for(actor: Actor) -> ReplyKeyboardMarkup:
    if actor.is_admin:
        return admin_menu()
    if actor.user.is_executor:
        return executor_menu()
    return client_menu()


def single(text: str, callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=callback_data)]])


def cancel_input() -> InlineKeyboardMarkup:
    return single("✖️ Отмена", MenuCb(action="cancel_input").pack())


def build(buttons: list[tuple[str, str]], width: int = 1) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for text, data in buttons:
        kb.button(text=text, callback_data=data)
    kb.adjust(width)
    return kb.as_markup()


def rows(*rows_: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows_ if row
        ]
    )
