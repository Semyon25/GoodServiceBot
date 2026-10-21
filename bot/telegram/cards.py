"""Inline-клавиатуры карточек заказов и платежей (кнопки зависят от статуса)."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from bot.domain.enums import OrderStatus as O
from bot.domain.enums import PaymentStatus as P
from bot.domain.models import Order, Payment
from bot.domain.transitions import (
    ORDER_ACTIVE_STATUSES,
    ORDER_FINAL_STATUSES,
    order_transition_allowed,
    payment_transition_allowed,
)

from .callbacks import AOrderCb, APayCb, AUserCb, ExecCb, OrderCb
from .keyboards import rows


def executor_order_kb(order: Order, back_view: str = "") -> InlineKeyboardMarkup:
    buttons = []
    if order.status in (O.NEW, O.ASSIGNED):
        buttons.append([("✋ Принять", ExecCb(action="accept", order_id=order.id).pack())])
    if order.status == O.ACCEPTED:
        buttons.append([("▶️ Начать выполнение", ExecCb(action="start", order_id=order.id).pack())])
    if order.status in (O.ACCEPTED, O.IN_PROGRESS):
        buttons.append([("✅ Выполнено", ExecCb(action="done", order_id=order.id).pack())])
    if order.status in (O.ASSIGNED, O.ACCEPTED, O.IN_PROGRESS):
        buttons.append([("⚠️ Не удалось выполнить", ExecCb(action="fail", order_id=order.id).pack())])
    buttons.append([("🔄 Обновить", ExecCb(action="view", order_id=order.id, view=back_view).pack())])
    if back_view:
        buttons.append([("◀️ К списку", ExecCb(action="list", view=back_view).pack())])
    return rows(*buttons)


def admin_order_kb(order: Order, back: bool = True) -> InlineKeyboardMarkup:
    buttons = []
    if order_transition_allowed(order.status, O.ASSIGNED):
        label = "👷 Переназначить" if order.executor_id else "👷 Назначить исполнителя"
        buttons.append([(label, AOrderCb(action="assign", order_id=order.id).pack())])
    if order.status in ORDER_ACTIVE_STATUSES:
        buttons.append([("❌ Отменить заявку", AOrderCb(action="cancel", order_id=order.id).pack())])
    if order.status in ORDER_FINAL_STATUSES:
        buttons.append([("♻️ Разрешить повторный заказ в смену", AOrderCb(action="exception", order_id=order.id).pack())])
    buttons.append(
        [
            ("📜 История", AOrderCb(action="history", order_id=order.id).pack()),
            ("👤 Клиент", AUserCb(action="view", user_id=order.user_id).pack()),
        ]
    )
    buttons.append([("🔄 Обновить", AOrderCb(action="view", order_id=order.id).pack())])
    if back:
        buttons.append([("◀️ К списку заказов", AOrderCb(action="list").pack())])
    return rows(*buttons)


def admin_payment_kb(payment: Payment, back: bool = True) -> InlineKeyboardMarkup:
    buttons = []
    if payment_transition_allowed(payment.status, P.CONFIRMED):
        buttons.append([("✅ Подтвердить оплату", APayCb(action="confirm_ask", payment_id=payment.id).pack())])
    if payment_transition_allowed(payment.status, P.REJECTED):
        buttons.append([("⛔ Отклонить", APayCb(action="reject", payment_id=payment.id).pack())])
    if payment_transition_allowed(payment.status, P.REFUNDED):
        buttons.append([("↩️ Оформить возврат", APayCb(action="refund", payment_id=payment.id).pack())])
    extra = []
    if payment.proof_file_id:
        extra.append(("🧾 Чек", APayCb(action="proof", payment_id=payment.id).pack()))
    extra.append(("📜 История", APayCb(action="history", payment_id=payment.id).pack()))
    extra.append(("👤 Клиент", AUserCb(action="view", user_id=payment.user_id).pack()))
    buttons.append(extra)
    buttons.append([("🔄 Обновить", APayCb(action="view", payment_id=payment.id).pack())])
    if back:
        buttons.append([("◀️ К платежам", APayCb(action="panel").pack())])
    return rows(*buttons)


def client_order_kb(order: Order) -> InlineKeyboardMarkup:
    return rows(
        [("🔄 Обновить статус", OrderCb(action="view", order_id=order.id).pack())],
        [("📦 Мои заказы", OrderCb(action="list").pack())],
    )
