"""Интерфейс исполнителя."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.domain.enums import FAIL_REASON_TITLES, DistributionMode, FailReason
from bot.domain.models import Actor, Order
from bot.services import Services
from bot.services.orders import ExecutorView

from .. import texts as t
from ..callbacks import ExecCb, ExecFailCb
from ..cards import executor_order_kb
from ..keyboards import build, cancel_input, rows
from ..middlewares import NOT_MENU, IsExecutor
from ..notifier import Notifier
from ..states import FailForm
from .utils import respond

router = Router(name="executor")
router.message.filter(IsExecutor())
router.callback_query.filter(IsExecutor())

VIEW_TITLES = {
    ExecutorView.NEW: "Новые заявки",
    ExecutorView.ASSIGNED: "Назначенные мне заявки",
    ExecutorView.IN_WORK: "Заявки в работе",
    ExecutorView.DONE_SHIFT: "Выполненные за смену",
    ExecutorView.HISTORY: "История заказов",
}

BUTTON_VIEWS = {
    t.BTN_EX_NEW: ExecutorView.NEW,
    t.BTN_EX_ASSIGNED: ExecutorView.ASSIGNED,
    t.BTN_EX_IN_WORK: ExecutorView.IN_WORK,
    t.BTN_EX_DONE: ExecutorView.DONE_SHIFT,
    t.BTN_EX_HISTORY: ExecutorView.HISTORY,
}


def _sorted_for_route(orders: list[Order], view: str) -> list[Order]:
    if view in (ExecutorView.ASSIGNED, ExecutorView.IN_WORK):
        # для удобства маршрута: по адресу и секции, внутри — по времени создания
        return sorted(orders, key=lambda o: (o.address_title, o.section, o.created_at))
    return orders


async def _list_view(actor: Actor, services: Services, view: str) -> tuple[str, object]:
    orders = _sorted_for_route(await services.orders.executor_orders(actor, view), view)
    title = f"<b>{VIEW_TITLES[view]}</b>"
    if view == ExecutorView.NEW and await services.settings.distribution_mode() == DistributionMode.MANUAL:
        return title + "\nЗаявки распределяет администратор — смотрите раздел «📌 Назначенные».", None
    if not orders:
        return title + "\nЗаявок нет.", None
    lines = [title, f"Всего: {len(orders)}"]
    buttons = [(t.order_short(o), ExecCb(action="view", order_id=o.id, view=view).pack()) for o in orders[:50]]
    return "\n".join(lines), build(buttons)


@router.message(F.text.in_(BUTTON_VIEWS.keys()))
async def list_orders(message: Message, state: FSMContext, actor: Actor, services: Services) -> None:
    await state.clear()
    text, markup = await _list_view(actor, services, BUTTON_VIEWS[message.text])
    await message.answer(text, reply_markup=markup)


@router.callback_query(ExecCb.filter(F.action == "list"))
async def list_orders_cb(callback: CallbackQuery, callback_data: ExecCb, actor: Actor, services: Services) -> None:
    await callback.answer()
    text, markup = await _list_view(actor, services, callback_data.view or ExecutorView.ASSIGNED)
    await respond(callback, text, markup)


async def _show_card(callback: CallbackQuery, order: Order, services: Services, view: str, note: str = "") -> None:
    text = t.order_card(order, services.tz)
    if note:
        text = f"{note}\n\n{text}"
    await respond(callback, text, executor_order_kb(order, back_view=view))


@router.callback_query(ExecCb.filter(F.action == "view"))
async def view_order(callback: CallbackQuery, callback_data: ExecCb, actor: Actor, services: Services) -> None:
    order = await services.orders.get_for_executor(actor, callback_data.order_id)
    await callback.answer()
    await _show_card(callback, order, services, callback_data.view)


@router.callback_query(ExecCb.filter(F.action == "accept"))
async def accept(callback: CallbackQuery, callback_data: ExecCb, actor: Actor, services: Services,
                 notifier: Notifier) -> None:
    order = await services.orders.executor_accept(actor, callback_data.order_id)
    await callback.answer("Заявка принята")
    await _show_card(callback, order, services, ExecutorView.IN_WORK, "✋ Вы приняли заявку.")
    await notifier.order_accepted(order)


@router.callback_query(ExecCb.filter(F.action == "start"))
async def start(callback: CallbackQuery, callback_data: ExecCb, actor: Actor, services: Services) -> None:
    order = await services.orders.executor_start(actor, callback_data.order_id)
    await callback.answer("Выполнение начато")
    await _show_card(callback, order, services, ExecutorView.IN_WORK, "▶️ Выполнение начато.")


@router.callback_query(ExecCb.filter(F.action == "done"))
async def done(callback: CallbackQuery, callback_data: ExecCb, actor: Actor, services: Services,
               notifier: Notifier) -> None:
    order = await services.orders.executor_done(actor, callback_data.order_id)
    await callback.answer("Отмечено как выполненное")
    await _show_card(callback, order, services, ExecutorView.IN_WORK, "✅ Заявка выполнена. Клиент получил уведомление.")
    await notifier.order_done(order)


@router.callback_query(ExecCb.filter(F.action == "fail"))
async def fail_ask(callback: CallbackQuery, callback_data: ExecCb, actor: Actor, services: Services) -> None:
    order = await services.orders.get_for_executor(actor, callback_data.order_id)
    await callback.answer()
    buttons = [(title, ExecFailCb(order_id=order.id, reason=reason.value).pack())
               for reason, title in FAIL_REASON_TITLES.items()]
    buttons.append(("◀️ Назад", ExecCb(action="view", order_id=order.id, view=ExecutorView.IN_WORK).pack()))
    await respond(callback, f"Заявка №{order.id}. Укажите причину, по которой не удалось выполнить:", build(buttons))


@router.callback_query(ExecFailCb.filter())
async def fail_reason(callback: CallbackQuery, callback_data: ExecFailCb, state: FSMContext, actor: Actor,
                      services: Services, notifier: Notifier) -> None:
    reason = FailReason(callback_data.reason)
    if reason == FailReason.OTHER:
        await services.orders.get_for_executor(actor, callback_data.order_id)
        await state.set_state(FailForm.comment)
        await state.update_data(order_id=callback_data.order_id)
        await callback.answer()
        await respond(callback, "Опишите причину одним сообщением (обязательно):", cancel_input())
        return
    order = await services.orders.executor_fail(actor, callback_data.order_id, reason, None)
    await callback.answer("Отмечено")
    await _show_card(callback, order, services, ExecutorView.HISTORY, "⚠️ Отмечено: не удалось выполнить.")
    await notifier.order_failed(order, actor.user)


@router.message(FailForm.comment, F.text, NOT_MENU)
async def fail_comment(message: Message, state: FSMContext, actor: Actor, services: Services,
                       notifier: Notifier) -> None:
    data = await state.get_data()
    order = await services.orders.executor_fail(actor, int(data["order_id"]), FailReason.OTHER, message.text)
    await state.clear()
    await message.answer(
        "⚠️ Отмечено: не удалось выполнить.\n\n" + t.order_card(order, services.tz),
        reply_markup=rows([("◀️ К заявкам в работе", ExecCb(action="list", view=ExecutorView.IN_WORK).pack())]),
    )
    await notifier.order_failed(order, actor.user)
