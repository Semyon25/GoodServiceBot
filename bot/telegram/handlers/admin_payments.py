"""Администратор: платежи — проверка, подтверждение, отклонение, возвраты."""
from __future__ import annotations

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.domain.enums import PAYMENT_STATUS_TITLES, PaymentStatus
from bot.domain.models import Actor
from bot.services import Services

from .. import texts as t
from ..callbacks import APayCb, MenuCb
from ..cards import admin_payment_kb
from ..keyboards import rows
from ..notifier import Notifier
from .admin_common import admin_router, ask_input, input_handler
from .utils import respond

router = admin_router("admin_payments")

PAGE_SIZE = 15


async def _panel(services: Services) -> tuple[str, object]:
    counts = {s: await services.repos.payments.count(status=s.value) for s in PaymentStatus}
    lines = ["<b>💰 Платежи</b>"]
    lines += [f"{PAYMENT_STATUS_TITLES[s]}: {counts[s]}" for s in PaymentStatus]
    buttons = [
        [(f"⏳ Ожидают подтверждения ({counts[PaymentStatus.AWAITING_CONFIRMATION]})",
          APayCb(action="list", status=PaymentStatus.AWAITING_CONFIRMATION.value).pack())],
        [(f"🕓 Ожидают оплаты ({counts[PaymentStatus.AWAITING_PAYMENT]})",
          APayCb(action="list", status=PaymentStatus.AWAITING_PAYMENT.value).pack())],
        [("✅ Подтверждённые", APayCb(action="list", status=PaymentStatus.CONFIRMED.value).pack()),
         ("⛔ Отклонённые", APayCb(action="list", status=PaymentStatus.REJECTED.value).pack())],
        [("↩️ Возвращённые", APayCb(action="list", status=PaymentStatus.REFUNDED.value).pack()),
         ("📄 Все", APayCb(action="list", status="all").pack())],
    ]
    return "\n".join(lines), rows(*buttons)


@router.message(F.text == t.BTN_A_PAYMENTS)
async def payments_panel(message: Message, state: FSMContext, services: Services) -> None:
    await state.clear()
    text, markup = await _panel(services)
    await message.answer(text, reply_markup=markup)


@router.callback_query(APayCb.filter(F.action == "panel"))
async def payments_panel_cb(callback: CallbackQuery, services: Services) -> None:
    await callback.answer()
    text, markup = await _panel(services)
    await respond(callback, text, markup)


@router.callback_query(APayCb.filter(F.action == "list"))
async def payments_list(callback: CallbackQuery, callback_data: APayCb, actor: Actor, services: Services) -> None:
    status = None if callback_data.status == "all" else callback_data.status
    page = max(callback_data.page, 0)
    total = await services.repos.payments.count(status=status)
    payments = await services.payments.list(actor, status=status, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    users = await services.repos.users.get_many(p.user_id for p in payments)
    await callback.answer()
    title = PAYMENT_STATUS_TITLES[PaymentStatus(status)] if status else "Все платежи"
    if not payments:
        await respond(callback, f"<b>{title}</b>\nПлатежей нет.", rows([("◀️ К платежам", APayCb(action="panel").pack())]))
        return
    buttons = [[(t.payment_short(p, services.tz, users.get(p.user_id)), APayCb(action="view", payment_id=p.id).pack())]
               for p in payments]
    nav = []
    if page > 0:
        nav.append(("⬅️", APayCb(action="list", status=callback_data.status, page=page - 1).pack()))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(("➡️", APayCb(action="list", status=callback_data.status, page=page + 1).pack()))
    buttons.append(nav)
    buttons.append([("◀️ К платежам", APayCb(action="panel").pack())])
    await respond(callback, f"<b>{title}</b> — {total}", rows(*buttons))


async def _card(payment_id: int, actor: Actor, services: Services) -> tuple[str, object]:
    payment = await services.payments.get(actor, payment_id)
    client = await services.repos.users.get(payment.user_id)
    text = t.payment_card(payment, services.tz, client)
    sub = await services.repos.subscriptions.get_by_payment(payment.id)
    if sub:
        text += (f"\n\nПодписка: {t.fmt_dt(sub.starts_at, services.tz)} — {t.fmt_dt(sub.ends_at, services.tz)}"
                 f" ({sub.status})")
    return text, admin_payment_kb(payment)


@router.callback_query(APayCb.filter(F.action == "view"))
async def payment_view(callback: CallbackQuery, callback_data: APayCb, actor: Actor, services: Services) -> None:
    text, markup = await _card(callback_data.payment_id, actor, services)
    await callback.answer()
    await respond(callback, text, markup)


@router.callback_query(APayCb.filter(F.action == "proof"))
async def payment_proof(callback: CallbackQuery, callback_data: APayCb, actor: Actor, services: Services) -> None:
    payment = await services.payments.get(actor, callback_data.payment_id)
    await callback.answer()
    if not payment.proof_file_id:
        await callback.message.answer("Клиент не приложил подтверждение.")
        return
    caption = f"Чек к платежу №{payment.id} на {payment.amount_rub} ₽"
    if payment.proof_type == "photo":
        await callback.message.answer_photo(payment.proof_file_id, caption=caption)
    else:
        await callback.message.answer_document(payment.proof_file_id, caption=caption)


@router.callback_query(APayCb.filter(F.action == "confirm_ask"))
async def payment_confirm_ask(callback: CallbackQuery, callback_data: APayCb, actor: Actor,
                              services: Services) -> None:
    payment = await services.payments.get(actor, callback_data.payment_id)
    client = await services.repos.users.get(payment.user_id)
    await callback.answer()
    await respond(
        callback,
        f"Подтвердить фактическое поступление <b>{payment.amount_rub} ₽</b> по платежу №{payment.id} "
        f"от {t.user_line(client)}?\n\nПодписка «{t.h(payment.tariff_title)}» будет активирована.",
        rows(
            [("✅ Да, деньги поступили", APayCb(action="confirm", payment_id=payment.id).pack())],
            [("◀️ Назад", APayCb(action="view", payment_id=payment.id).pack())],
        ),
    )


@router.callback_query(APayCb.filter(F.action == "confirm"))
async def payment_confirm(callback: CallbackQuery, callback_data: APayCb, actor: Actor, services: Services,
                          notifier: Notifier) -> None:
    payment, sub = await services.payments.confirm(actor, callback_data.payment_id)
    await callback.answer("Оплата подтверждена")
    text, markup = await _card(payment.id, actor, services)
    await respond(callback, "✅ Оплата подтверждена, подписка активирована.\n\n" + text, markup)
    await notifier.payment_confirmed(payment, sub)


@router.callback_query(APayCb.filter(F.action == "reject"))
async def payment_reject(callback: CallbackQuery, callback_data: APayCb, state: FSMContext, actor: Actor,
                         services: Services) -> None:
    payment = await services.payments.get(actor, callback_data.payment_id)
    await callback.answer()
    markup = rows(
        [("Без комментария", APayCb(action="reject_now", payment_id=payment.id).pack())],
        [("✖️ Отмена", MenuCb(action="cancel_input").pack())],
    )
    await ask_input(callback.message, state, "payment_reject",
                    f"Платёж №{payment.id}. Напишите причину отклонения (её увидит клиент):", markup,
                    payment_id=payment.id)


async def _do_reject(payment_id: int, reason: str | None, actor: Actor, services: Services,
                     notifier: Notifier) -> str:
    payment = await services.payments.reject(actor, payment_id, reason)
    await notifier.payment_rejected(payment)
    return "⛔ Платёж отклонён, клиент уведомлён."


@input_handler("payment_reject")
async def payment_reject_input(message: Message, data: dict, actor: Actor, services: Services,
                               notifier: Notifier) -> None:
    await message.answer(await _do_reject(int(data["payment_id"]), message.text, actor, services, notifier))


@router.callback_query(APayCb.filter(F.action == "reject_now"))
async def payment_reject_now(callback: CallbackQuery, callback_data: APayCb, state: FSMContext, actor: Actor,
                             services: Services, notifier: Notifier) -> None:
    await state.clear()
    text = await _do_reject(callback_data.payment_id, None, actor, services, notifier)
    await callback.answer("Отклонено")
    await respond(callback, text)


@router.callback_query(APayCb.filter(F.action == "refund"))
async def payment_refund(callback: CallbackQuery, callback_data: APayCb, state: FSMContext, actor: Actor,
                         services: Services) -> None:
    payment = await services.payments.get(actor, callback_data.payment_id)
    await callback.answer()
    await ask_input(
        callback.message, state, "payment_refund",
        f"Возврат по платежу №{payment.id} на {payment.amount_rub} ₽.\n"
        "Сначала верните деньги клиенту, затем напишите причину возврата. "
        "Связанная подписка будет аннулирована.",
        payment_id=payment.id,
    )


@input_handler("payment_refund")
async def payment_refund_input(message: Message, data: dict, actor: Actor, services: Services,
                               notifier: Notifier) -> None:
    payment = await services.payments.refund(actor, int(data["payment_id"]), message.text)
    await message.answer("↩️ Возврат зафиксирован, подписка аннулирована, клиент уведомлён.")
    await notifier.payment_refunded(payment)


@router.callback_query(APayCb.filter(F.action == "history"))
async def payment_history(callback: CallbackQuery, callback_data: APayCb, actor: Actor, services: Services) -> None:
    entries = await services.payments.history(actor, callback_data.payment_id)
    titles = {s.value: title for s, title in PAYMENT_STATUS_TITLES.items()}
    await callback.answer()
    await respond(
        callback,
        f"<b>История платежа №{callback_data.payment_id}</b>\n" + t.history_text(entries, titles, services.tz),
        rows([("◀️ К платежу", APayCb(action="view", payment_id=callback_data.payment_id).pack())]),
    )
