"""Отправка уведомлений клиентам, исполнителям и администраторам."""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup

from bot.domain.enums import DistributionMode, Role
from bot.domain.models import Order, Payment, Subscription, User
from bot.services import Services
from bot.services.orders import OrderChange
from bot.services.payments import SubscriptionEvent

from . import texts as t
from .callbacks import AOrderCb, ExecCb, TariffCb
from .cards import admin_order_kb, admin_payment_kb, client_order_kb, executor_order_kb
from .keyboards import client_menu, executor_menu, rows

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot: Bot, services: Services, admin_ids: frozenset[int]):
        self.bot = bot
        self.services = services
        self.admin_ids = admin_ids
        self.tz = services.tz
        self._error_sent_at: dict[str, float] = {}

    # ------------------------------------------------------------ транспорт

    async def send(self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
        for attempt in range(2):
            try:
                await self.bot.send_message(chat_id, text, reply_markup=markup)
                return True
            except TelegramRetryAfter as exc:
                if attempt == 0:
                    await asyncio.sleep(exc.retry_after)
                    continue
                log.warning("Telegram rate limit for %s", chat_id)
            except TelegramForbiddenError:
                log.info("Пользователь %s заблокировал бота", chat_id)
            except TelegramAPIError as exc:
                log.warning("Не удалось отправить сообщение %s: %s", chat_id, exc)
            return False
        return False

    async def to_user(self, user_id: int | None, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
        if not user_id:
            return False
        user = await self.services.repos.users.get(user_id)
        return await self.send(user.tg_id, text, markup) if user else False

    async def to_admins(self, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        for admin_id in self.admin_ids:
            await self.send(admin_id, text, markup)

    async def internal_error(self, text: str, key: str = "generic") -> None:
        """Сообщает администраторам о внутренней ошибке (не чаще раза в минуту на тип)."""
        now = time.monotonic()
        if now - self._error_sent_at.get(key, 0) < 60:
            return
        self._error_sent_at[key] = now
        await self.to_admins(f"🚨 <b>Ошибка обработки</b>\n{t.h(text)[:3500]}")

    # --------------------------------------------------------------- заказы

    async def order_created(self, order: Order, client: User) -> None:
        await self.send(
            client.tg_id,
            f"✅ Заявка №{order.id} на вынос мусора создана.\n"
            "Мы сообщим, когда исполнитель примет её и когда мусор будет вынесен.",
            client_order_kb(order),
        )
        await self.to_admins(
            "🆕 Новая заявка\n\n" + t.order_card(order, self.tz, client=client), admin_order_kb(order, back=False)
        )
        if await self.services.settings.distribution_mode() == DistributionMode.POOL:
            executors = await self.services.users.list_executors()
            markup = rows([("Открыть заявку", ExecCb(action="view", order_id=order.id, view="new").pack())])
            for executor in executors:
                await self.send(
                    executor.tg_id,
                    f"🆕 Доступна новая заявка №{order.id}: {t.h(order.address_title)}, секция {order.section}",
                    markup,
                )

    async def order_assigned(self, change: OrderChange) -> None:
        order = change.order
        await self.to_user(
            order.executor_id,
            "📌 Вам назначена заявка\n\n" + t.order_card(order, self.tz),
            executor_order_kb(order, back_view="assigned"),
        )
        if change.previous_executor_id and change.previous_executor_id != order.executor_id:
            await self.to_user(
                change.previous_executor_id,
                f"🔁 Заявка №{order.id} переназначена другому исполнителю. Выполнять её не нужно.",
            )

    async def order_accepted(self, order: Order) -> None:
        await self.to_user(
            order.user_id,
            f"👍 Исполнитель принял заявку №{order.id} и скоро заберёт пакет.",
            client_order_kb(order),
        )

    async def order_done(self, order: Order) -> None:
        await self.to_user(order.user_id, f"🎉 Мусор вынесен! Заявка №{order.id} выполнена. Спасибо!")

    async def order_failed(self, order: Order, executor: User) -> None:
        reason = t.h(t.fail_text(order) or "")
        await self.to_user(
            order.user_id,
            f"😔 Не удалось выполнить заявку №{order.id}.\nПричина: {reason}\n\n"
            "Если есть вопросы, напишите администратору через раздел «Помощь».",
        )
        await self.to_admins(
            "⚠️ Заявку не удалось выполнить\n\n" + t.order_card(order, self.tz, executor=executor),
            admin_order_kb(order, back=False),
        )

    async def order_cancelled(self, change: OrderChange) -> None:
        order = change.order
        await self.to_user(
            order.user_id,
            f"❌ Заявка №{order.id} отменена администратором.\nПричина: {t.h(order.cancel_reason)}",
        )
        if change.previous_executor_id:
            await self.to_user(
                change.previous_executor_id,
                f"❌ Заявка №{order.id} отменена администратором. Выполнять её не нужно.\n"
                f"Причина: {t.h(order.cancel_reason)}",
            )

    async def unassigned_orders(self, orders: list[Order]) -> None:
        if not orders:
            return
        buttons = [
            [(f"№{o.id} · {o.address_title}, с{o.section}", AOrderCb(action="view", order_id=o.id).pack())]
            for o in orders[:20]
        ]
        await self.to_admins(
            f"⏰ Заявки без исполнителя: {len(orders)}. Назначьте исполнителя.", rows(*buttons)
        )

    # -------------------------------------------------------------- платежи

    async def payment_submitted(self, payment: Payment, client: User) -> None:
        caption = "💰 <b>Платёж ожидает подтверждения</b>\nПроверьте фактическое поступление денег.\n\n"
        caption += t.payment_card(payment, self.tz, client)
        markup = admin_payment_kb(payment, back=False)
        for admin_id in self.admin_ids:
            try:
                if payment.proof_type == "photo":
                    await self.bot.send_photo(admin_id, payment.proof_file_id, caption=caption, reply_markup=markup)
                elif payment.proof_type == "document":
                    await self.bot.send_document(admin_id, payment.proof_file_id, caption=caption, reply_markup=markup)
                else:
                    await self.send(admin_id, caption, markup)
            except TelegramAPIError as exc:
                log.warning("Не удалось отправить чек администратору %s: %s", admin_id, exc)
                await self.send(admin_id, caption, markup)

    async def payment_confirmed(self, payment: Payment, sub: Subscription) -> None:
        await self.to_user(
            payment.user_id,
            f"✅ Оплата подтверждена!\n\n"
            f"Подписка: <b>{t.h(sub.tariff_title)}</b>\n"
            f"Начало: {t.fmt_dt(sub.starts_at, self.tz)}\n"
            f"Окончание: {t.fmt_dt(sub.ends_at, self.tz)}\n\n"
            "Теперь в рабочую смену можно заказывать вынос мусора.",
        )

    async def payment_rejected(self, payment: Payment) -> None:
        reason = f"\nПричина: {t.h(payment.reject_reason)}" if payment.reject_reason else ""
        await self.to_user(
            payment.user_id,
            f"⛔ Оплата по платежу №{payment.id} на {payment.amount_rub} ₽ не подтверждена.{reason}\n\n"
            "Если вы уже перевели деньги, напишите администратору через раздел «Помощь». "
            "Также можно пройти оплату заново.",
            rows([("💳 Тарифы и оплата", TariffCb(action="list").pack())]),
        )

    async def payment_refunded(self, payment: Payment) -> None:
        await self.to_user(
            payment.user_id,
            f"↩️ По платежу №{payment.id} оформлен возврат {payment.amount_rub} ₽.\n"
            f"Причина: {t.h(payment.refund_reason)}\nСвязанная подписка аннулирована.",
        )

    # ------------------------------------------------------------- подписки

    async def subscription_event(self, event: SubscriptionEvent) -> None:
        markup = rows([("💳 Продлить подписку", TariffCb(action="list").pack())])
        if event.kind == "reminder":
            text = (
                f"⏳ Ваша подписка «{t.h(event.subscription.tariff_title)}» заканчивается "
                f"{t.fmt_dt(event.subscription.ends_at, self.tz)}.\n"
                "Чтобы не прерывать обслуживание, оплатите следующий период заранее — "
                "он начнётся сразу после окончания текущего."
            )
        else:
            text = (
                f"📭 Подписка «{t.h(event.subscription.tariff_title)}» закончилась. "
                "Чтобы снова заказывать вынос мусора, оплатите новый период."
            )
        await self.send(event.user.tg_id, text, markup)

    async def role_changed(self, user: User) -> None:
        if user.role == Role.EXECUTOR:
            text, markup = "Вам назначена роль исполнителя. Меню обновлено.", executor_menu()
        else:
            text, markup = "Ваша роль изменена на «Клиент». Меню обновлено.", client_menu()
        try:
            await self.bot.send_message(user.tg_id, text, reply_markup=markup)
        except TelegramAPIError as exc:
            log.info("Не удалось уведомить %s о смене роли: %s", user.tg_id, exc)
