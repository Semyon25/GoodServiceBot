"""Отправка уведомлений клиентам, исполнителям и администраторам."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable

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
from .cards import admin_order_kb, admin_payment_kb, executor_order_kb
from .keyboards import client_menu, executor_menu, rows

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot: Bot, services: Services, admin_ids: Iterable[int]):
        self.bot = bot
        self.services = services
        self.admin_ids = tuple(admin_ids)
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

    async def _admin_card(self, order: Order, header: str) -> None:
        """Карточка заявки каждому администратору; назначенному — с кнопками исполнителя."""
        users = await self.services.orders.users_by_id([order.user_id, order.executor_id])
        text = header + t.order_card(
            order, self.tz, client=users.get(order.user_id), executor=users.get(order.executor_id)
        )
        for admin_id in self.admin_ids:
            admin = await self.services.repos.users.get_by_tg(admin_id)
            await self.send(admin_id, text, admin_order_kb(order, back=False, viewer_id=admin.id if admin else None))

    @staticmethod
    def _order_place(order: Order) -> str:
        return f"{t.h(order.address_title)}, секция {order.section}, кв. {t.h(order.apartment)}"

    async def order_created(self, order: Order) -> None:
        """Клиент получает ответ в самом диалоге заказа, здесь — только сотрудники."""
        await self._admin_card(order, "🆕 Новая заявка\n\n")
        if await self.services.settings.distribution_mode() == DistributionMode.POOL:
            executors = await self.services.users.list_executors()
            markup = rows([("Открыть заявку", ExecCb(action="view", order_id=order.id, view="new").pack())])
            for executor in executors:
                await self.send(executor.tg_id, f"🆕 Доступна новая заявка: {self._order_place(order)}", markup)

    async def order_assigned(self, change: OrderChange) -> None:
        order = change.order
        executor = await self.services.repos.users.get(order.executor_id) if order.executor_id else None
        if executor and executor.tg_id in self.admin_ids:
            client = await self.services.repos.users.get(order.user_id)
            await self.send(
                executor.tg_id,
                "📌 Вам назначена заявка\n\n" + t.order_card(order, self.tz, client=client),
                admin_order_kb(order, back=False, viewer_id=executor.id),
            )
        elif executor:
            await self.send(
                executor.tg_id,
                "📌 Вам назначена заявка\n\n" + t.order_card(order, self.tz, numbered=False),
                executor_order_kb(order, back_view="assigned"),
            )
        if change.previous_executor_id and change.previous_executor_id != order.executor_id:
            await self.to_user(
                change.previous_executor_id,
                f"🔁 Заявка ({self._order_place(order)}) передана другому исполнителю. Выполнять её не нужно.",
            )

    async def order_done(self, order: Order) -> None:
        await self.to_user(order.user_id, "🎉 Мусор вынесен! Спасибо, что пользуетесь сервисом.")

    async def order_failed(self, order: Order, executor: User) -> None:
        reason = t.h(t.fail_text(order) or "")
        await self.to_user(
            order.user_id,
            f"😔 Не удалось вынести мусор.\nПричина: {reason}\n\n"
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
            f"❌ Заявка на вынос мусора отменена администратором.\nПричина: {t.h(order.cancel_reason)}",
        )
        if change.previous_executor_id:
            await self.to_user(
                change.previous_executor_id,
                f"❌ Заявка ({self._order_place(order)}) отменена администратором. Выполнять её не нужно.\n"
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
            f"Начало: {t.subscription_first_day(sub, self.tz)}\n"
            f"Окончание: {t.subscription_last_day(sub, self.tz)} (включительно)\n\n"
            "Теперь можно заказывать вынос мусора.",
        )

    async def payment_rejected(self, payment: Payment) -> None:
        reason = f"\nПричина: {t.h(payment.reject_reason)}" if payment.reject_reason else ""
        await self.to_user(
            payment.user_id,
            f"⛔ Оплата на {payment.amount_rub} ₽ не подтверждена.{reason}\n\n"
            "Если вы уже перевели деньги, напишите администратору через раздел «Помощь». "
            "Также можно пройти оплату заново.",
            rows([("💳 Тарифы и оплата", TariffCb(action="list").pack())]),
        )

    async def payment_refunded(self, payment: Payment) -> None:
        await self.to_user(
            payment.user_id,
            f"↩️ По оплате «{t.h(payment.tariff_title)}» оформлен возврат {payment.amount_rub} ₽.\n"
            f"Причина: {t.h(payment.refund_reason)}\nСвязанная подписка аннулирована.",
        )

    # ------------------------------------------------------------- подписки

    async def subscription_event(self, event: SubscriptionEvent) -> None:
        markup = rows([("💳 Продлить подписку", TariffCb(action="list").pack())])
        if event.kind == "reminder":
            text = (
                f"⏳ Ваша подписка «{t.h(event.subscription.tariff_title)}» действует до "
                f"{t.subscription_last_day(event.subscription, self.tz)} включительно.\n"
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
