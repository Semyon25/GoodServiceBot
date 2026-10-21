from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message


async def respond(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Обновляет сообщение с кнопками; если это невозможно (фото, старое сообщение) — отправляет новое."""
    message = callback.message
    if isinstance(message, Message) and message.text is not None:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return
    elif isinstance(message, Message):
        try:
            await message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
    if callback.message is not None:
        await callback.bot.send_message(callback.message.chat.id, text, reply_markup=markup)
    else:
        await callback.bot.send_message(callback.from_user.id, text, reply_markup=markup)


async def drop_markup(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass


def chunk_text(text: str, limit: int = 4000) -> list[str]:
    parts, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        parts.append(current)
    return parts
