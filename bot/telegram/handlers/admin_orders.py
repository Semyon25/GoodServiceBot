"""Администратор: заказы — фильтры, назначение исполнителей, отмена, история, исключения."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.domain.enums import ORDER_STATUS_TITLES, OrderStatus
from bot.domain.models import Actor
from bot.domain.schedule import last_shift_date
from bot.services import Services

from .. import texts as t
from ..callbacks import AFilterCb, AOrderCb
from ..cards import admin_order_kb
from ..keyboards import build, rows
from ..notifier import Notifier
from .admin_common import admin_router, ask_input, input_handler
from .utils import respond

router = admin_router("admin_orders")

PAGE_SIZE = 20

# Фильтры списка заказов хранятся в памяти для каждого администратора
_filters: dict[int, dict[str, Any]] = {}


async def _default_filters(services: Services) -> dict[str, Any]:
    shift_date = last_shift_date(services.clock(), await services.settings.schedule())
    return {"date": shift_date.isoformat() if shift_date else None, "status": "", "address": 0, "executor": 0}


async def _get_filters(actor: Actor, services: Services) -> dict[str, Any]:
    if actor.tg_id not in _filters:
        _filters[actor.tg_id] = await _default_filters(services)
    return _filters[actor.tg_id]


def _query(f: dict[str, Any]) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if f["date"]:
        query["shift_date"] = date.fromisoformat(f["date"])
    if f["status"]:
        query["statuses"] = [f["status"]]
    if f["address"]:
        query["address_id"] = f["address"]
    if f["executor"] == -1:
        query["unassigned"] = True
    elif f["executor"]:
        query["executor_id"] = f["executor"]
    return query


async def _panel(actor: Actor, services: Services) -> tuple[str, object]:
    f = await _get_filters(actor, services)
    query = _query(f)
    total = await services.repos.orders.count(**query)
    address_title = "все"
    if f["address"]:
        address_title = (await services.addresses.get_address(f["address"])).title
    executor_title = "все"
    if f["executor"] == -1:
        executor_title = "без исполнителя"
    elif f["executor"]:
        user = await services.repos.users.get(f["executor"])
        executor_title = user.display if user else "?"
    status_title = ORDER_STATUS_TITLES[OrderStatus(f["status"])] if f["status"] else "все"
    shift_title = t.fmt_date(date.fromisoformat(f["date"])) if f["date"] else "все даты"
    text = (
        "<b>📋 Заказы</b>\n"
        f"Вынос: {shift_title}\n"
        f"Статус: {status_title}\n"
        f"Адрес: {t.h(address_title)}\n"
        f"Исполнитель: {t.h(executor_title)}\n\n"
        f"Найдено: <b>{total}</b>"
    )
    markup = rows(
        [
            ("◀️ День", AFilterCb(field="date", value="prev").pack()),
            ("Текущий вынос", AFilterCb(field="date", value="cur").pack()),
            ("День ▶️", AFilterCb(field="date", value="next").pack()),
        ],
        [("📅 Все даты", AFilterCb(field="date", value="all").pack())],
        [
            ("Статус", AFilterCb(field="status", value="?").pack()),
            ("Адрес", AFilterCb(field="address", value="?").pack()),
            ("Исполнитель", AFilterCb(field="executor", value="?").pack()),
        ],
        [("🧹 Сбросить фильтры", AFilterCb(field="reset").pack())],
        [(f"📄 Показать список ({total})", AOrderCb(action="list").pack())],
    )
    return text, markup


@router.message(F.text == t.BTN_A_ORDERS)
async def orders_panel(message: Message, state: FSMContext, actor: Actor, services: Services) -> None:
    await state.clear()
    text, markup = await _panel(actor, services)
    await message.answer(text, reply_markup=markup)


@router.callback_query(AOrderCb.filter(F.action == "panel"))
async def orders_panel_cb(callback: CallbackQuery, actor: Actor, services: Services) -> None:
    await callback.answer()
    text, markup = await _panel(actor, services)
    await respond(callback, text, markup)


@router.callback_query(AFilterCb.filter())
async def change_filter(callback: CallbackQuery, callback_data: AFilterCb, actor: Actor, services: Services) -> None:
    f = await _get_filters(actor, services)
    field, value = callback_data.field, callback_data.value
    await callback.answer()

    if value == "?":
        back = ("◀️ Назад", AOrderCb(action="panel").pack())
        if field == "status":
            buttons = [("Все", AFilterCb(field="status", value="all").pack())]
            buttons += [(title, AFilterCb(field="status", value=s.value).pack()) for s, title in ORDER_STATUS_TITLES.items()]
        elif field == "address":
            buttons = [("Все", AFilterCb(field="address", value="0").pack())]
            buttons += [(a.title, AFilterCb(field="address", value=str(a.id)).pack())
                        for a in await services.addresses.list_addresses(active_only=False)]
        else:
            buttons = [
                ("Все", AFilterCb(field="executor", value="0").pack()),
                ("Без исполнителя", AFilterCb(field="executor", value="-1").pack()),
            ]
            buttons += [(u.display, AFilterCb(field="executor", value=str(u.id)).pack())
                        for u in await services.orders.assignees()]
        await respond(callback, "Выберите значение фильтра:", build(buttons + [back], width=2))
        return

    if field == "reset":
        _filters[actor.tg_id] = await _default_filters(services)
    elif field == "date":
        current = date.fromisoformat(f["date"]) if f["date"] else None
        if value == "all":
            f["date"] = None
        elif value == "cur" or current is None:
            f["date"] = (await _default_filters(services))["date"]
        else:
            delta = -1 if value == "prev" else 1
            f["date"] = (current + timedelta(days=delta)).isoformat()
    elif field == "status":
        f["status"] = "" if value == "all" else OrderStatus(value).value
    elif field == "address":
        f["address"] = int(value)
    elif field == "executor":
        f["executor"] = int(value)
    text, markup = await _panel(actor, services)
    await respond(callback, text, markup)


@router.callback_query(AOrderCb.filter(F.action == "list"))
async def orders_list(callback: CallbackQuery, callback_data: AOrderCb, actor: Actor, services: Services) -> None:
    f = await _get_filters(actor, services)
    query = _query(f)
    page = max(callback_data.arg, 0)
    total = await services.repos.orders.count(**query)
    orders = await services.orders.admin_list(actor, limit=PAGE_SIZE, offset=page * PAGE_SIZE, **query)
    await callback.answer()
    if not orders:
        await respond(callback, "Заказов по выбранным фильтрам нет.",
                      rows([("◀️ К фильтрам", AOrderCb(action="panel").pack())]))
        return
    buttons = [[(t.order_short(o), AOrderCb(action="view", order_id=o.id).pack())] for o in orders]
    nav = []
    if page > 0:
        nav.append(("⬅️", AOrderCb(action="list", arg=page - 1).pack()))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(("➡️", AOrderCb(action="list", arg=page + 1).pack()))
    buttons.append(nav)
    buttons.append([("◀️ К фильтрам", AOrderCb(action="panel").pack())])
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    await respond(callback, f"<b>Заказы</b> — найдено {total}, страница {page + 1} из {pages}", rows(*buttons))


async def _card_text(order, services: Services) -> str:
    users = await services.orders.users_by_id([order.user_id, order.executor_id])
    return t.order_card(order, services.tz, client=users.get(order.user_id), executor=users.get(order.executor_id))


@router.callback_query(AOrderCb.filter(F.action == "view"))
async def order_view(callback: CallbackQuery, callback_data: AOrderCb, actor: Actor, services: Services) -> None:
    order = await services.orders.get_for_admin(actor, callback_data.order_id)
    await callback.answer()
    await respond(callback, await _card_text(order, services), admin_order_kb(order, viewer_id=actor.user.id))


@router.callback_query(AOrderCb.filter(F.action == "assign"))
async def order_assign(callback: CallbackQuery, callback_data: AOrderCb, actor: Actor, services: Services) -> None:
    order = await services.orders.get_for_admin(actor, callback_data.order_id)
    executors = [e for e in await services.orders.assignees() if e.id != order.executor_id]
    await callback.answer()
    if not executors:
        await respond(
            callback,
            "Нет доступных исполнителей. Назначьте роль исполнителя в разделе «👥 Пользователи».",
            rows([("◀️ К заявке", AOrderCb(action="view", order_id=order.id).pack())]),
        )
        return
    buttons = [(("👑 " if e.tg_id in services.users.admin_ids else "") + e.display,
                AOrderCb(action="assign_to", order_id=order.id, arg=e.id).pack()) for e in executors]
    buttons.append(("◀️ К заявке", AOrderCb(action="view", order_id=order.id).pack()))
    await respond(callback, f"Заявка №{order.id}. Выберите исполнителя:", build(buttons))


@router.callback_query(AOrderCb.filter(F.action == "assign_to"))
async def order_assign_to(callback: CallbackQuery, callback_data: AOrderCb, actor: Actor, services: Services,
                          notifier: Notifier) -> None:
    change = await services.orders.assign(actor, callback_data.order_id, callback_data.arg)
    await callback.answer("Исполнитель назначен")
    await respond(callback, "👷 Исполнитель назначен.\n\n" + await _card_text(change.order, services),
                  admin_order_kb(change.order, viewer_id=actor.user.id))
    await notifier.order_assigned(change)


@router.callback_query(AOrderCb.filter(F.action == "cancel"))
async def order_cancel(callback: CallbackQuery, callback_data: AOrderCb, state: FSMContext, actor: Actor,
                       services: Services) -> None:
    order = await services.orders.get_for_admin(actor, callback_data.order_id)
    await callback.answer()
    await ask_input(callback.message, state, "order_cancel", f"Заявка №{order.id}. Напишите причину отмены:",
                    order_id=order.id)


@input_handler("order_cancel")
async def order_cancel_input(message: Message, data: dict, actor: Actor, services: Services,
                             notifier: Notifier) -> None:
    change = await services.orders.cancel(actor, int(data["order_id"]), message.text)
    await message.answer("❌ Заявка отменена.\n\n" + await _card_text(change.order, services),
                         reply_markup=admin_order_kb(change.order, viewer_id=actor.user.id))
    await notifier.order_cancelled(change)


@router.callback_query(AOrderCb.filter(F.action == "history"))
async def order_history(callback: CallbackQuery, callback_data: AOrderCb, actor: Actor, services: Services) -> None:
    entries = await services.orders.history(actor, callback_data.order_id)
    titles = {s.value: title for s, title in ORDER_STATUS_TITLES.items()}
    await callback.answer()
    await respond(
        callback,
        f"<b>История заявки №{callback_data.order_id}</b>\n" + t.history_text(entries, titles, services.tz),
        rows([("◀️ К заявке", AOrderCb(action="view", order_id=callback_data.order_id).pack())]),
    )


@router.callback_query(AOrderCb.filter(F.action == "exception"))
async def order_exception(callback: CallbackQuery, callback_data: AOrderCb, actor: Actor, services: Services,
                          notifier: Notifier) -> None:
    order = await services.orders.grant_exception(actor, callback_data.order_id)
    await callback.answer("Повторный заказ разрешён", show_alert=True)
    await notifier.to_user(
        order.user_id,
        "Администратор разрешил оформить ещё одну заявку на вынос мусора. "
        "Нажмите «🗑 Заказать вынос мусора», когда пакет будет у двери.",
    )
