"""Администратор: пользователи — поиск, роли, блокировка, адрес, история."""
from __future__ import annotations

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.domain.enums import Role, UserStatus
from bot.domain.models import Actor, User
from bot.domain.transitions import ORDER_ACTIVE_STATUSES
from bot.services import Services

from .. import texts as t
from ..callbacks import AOrderCb, APayCb, AUserCb
from ..keyboards import build, rows
from ..notifier import Notifier
from .admin_common import admin_router, ask_input, input_handler
from .client import start_address_form
from .utils import respond

router = admin_router("admin_users")


def _panel_markup():
    return rows(
        [("🔎 Найти по ID или @username", AUserCb(action="search").pack())],
        [("👷 Исполнители", AUserCb(action="executors").pack())],
        [("🚫 Заблокированные", AUserCb(action="blocked").pack()),
         ("🆕 Новые", AUserCb(action="recent").pack())],
    )


@router.message(F.text == t.BTN_A_USERS)
async def users_panel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "<b>👥 Пользователи</b>\nЧтобы назначить исполнителя, попросите его отправить боту /start, "
        "затем найдите его по Telegram ID или @username.",
        reply_markup=_panel_markup(),
    )


@router.callback_query(AUserCb.filter(F.action == "panel"))
async def users_panel_cb(callback: CallbackQuery) -> None:
    await callback.answer()
    await respond(callback, "<b>👥 Пользователи</b>", _panel_markup())


@router.callback_query(AUserCb.filter(F.action == "search"))
async def user_search(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await ask_input(callback.message, state, "user_search", "Введите Telegram ID (число) или @username:")


@input_handler("user_search")
async def user_search_input(message: Message, data: dict, actor: Actor, services: Services,
                            notifier: Notifier) -> None:
    user = await services.users.find(actor, message.text)
    if not user:
        await message.answer(
            "Пользователь не найден. Он должен хотя бы раз отправить боту /start.",
            reply_markup=rows([("🔎 Искать снова", AUserCb(action="search").pack())]),
        )
        return
    text, markup = await _card(user, services)
    await message.answer(text, reply_markup=markup)


async def _user_list(callback: CallbackQuery, title: str, users: list[User]) -> None:
    if not users:
        await respond(callback, f"<b>{title}</b>\nСписок пуст.", rows([("◀️ Назад", AUserCb(action="panel").pack())]))
        return
    buttons = [(u.display, AUserCb(action="view", user_id=u.id).pack()) for u in users[:50]]
    buttons.append(("◀️ Назад", AUserCb(action="panel").pack()))
    await respond(callback, f"<b>{title}</b> — {len(users)}", build(buttons))


@router.callback_query(AUserCb.filter(F.action == "executors"))
async def executors_list(callback: CallbackQuery, services: Services) -> None:
    await callback.answer()
    await _user_list(callback, "Исполнители", await services.users.list_executors(active_only=False))


@router.callback_query(AUserCb.filter(F.action == "blocked"))
async def blocked_list(callback: CallbackQuery, services: Services) -> None:
    await callback.answer()
    users = [u for u in await services.repos.users.list() if u.is_blocked]
    await _user_list(callback, "Заблокированные", users)


@router.callback_query(AUserCb.filter(F.action == "recent"))
async def recent_list(callback: CallbackQuery, services: Services) -> None:
    await callback.answer()
    users = sorted(await services.repos.users.list(), key=lambda u: u.id, reverse=True)[:30]
    await _user_list(callback, "Последние зарегистрированные", users)


async def _card(user: User, services: Services) -> tuple[str, object]:
    is_admin = user.tg_id in services.users.admin_ids
    lines = [t.user_card(user, services.tz, is_admin)]
    address = await services.addresses.current(user.id)
    lines.append("\n<b>Адрес</b>\n" + (t.address_text(address) if address else "не указан"))
    now = services.clock()
    subs = await services.repos.subscriptions.list_current_and_future(user.id, now)
    lines.append("\n<b>Подписка</b>")
    lines += [t.subscription_line(s, services.tz, now) for s in subs] or ["нет действующей"]
    service = await services.catalog.service()
    open_payment = await services.payments.open_payment(user.id, service.id)
    if open_payment:
        lines.append(f"Открытый платёж №{open_payment.id}: {t.payment_status(open_payment)}")
    orders_total = await services.repos.orders.count(user_id=user.id)
    lines.append(f"\nЗаказов всего: {orders_total}")
    if user.is_executor:
        active = await services.repos.orders.count(executor_id=user.id, statuses=list(ORDER_ACTIVE_STATUSES))
        done = await services.repos.orders.count(executor_id=user.id, statuses=["done"])
        lines.append(f"Как исполнитель: активных {active}, выполнено {done}")

    buttons = []
    if not is_admin:
        if user.is_executor:
            buttons.append([("👤 Сделать клиентом", AUserCb(action="role", user_id=user.id).pack())])
        else:
            buttons.append([("👷 Назначить исполнителем", AUserCb(action="role", user_id=user.id).pack())])
        label = "✅ Разблокировать" if user.is_blocked else "🚫 Заблокировать"
        buttons.append([(label, AUserCb(action="block", user_id=user.id).pack())])
    buttons.append([("🏠 Изменить адрес", AUserCb(action="address", user_id=user.id).pack())])
    buttons.append([("📦 Заказы", AUserCb(action="orders", user_id=user.id).pack()),
                    ("💰 Платежи", AUserCb(action="payments", user_id=user.id).pack())])
    buttons.append([("◀️ Пользователи", AUserCb(action="panel").pack())])
    return "\n".join(lines), rows(*buttons)


@router.callback_query(AUserCb.filter(F.action == "view"))
async def user_view(callback: CallbackQuery, callback_data: AUserCb, services: Services) -> None:
    user = await services.users.get(callback_data.user_id)
    await callback.answer()
    text, markup = await _card(user, services)
    await respond(callback, text, markup)


@router.callback_query(AUserCb.filter(F.action == "role"))
async def user_role(callback: CallbackQuery, callback_data: AUserCb, actor: Actor, services: Services,
                    notifier: Notifier) -> None:
    user = await services.users.get(callback_data.user_id)
    new_role = Role.CLIENT if user.is_executor else Role.EXECUTOR
    note = ""
    if user.is_executor:
        active = await services.repos.orders.count(executor_id=user.id, statuses=list(ORDER_ACTIVE_STATUSES))
        if active:
            note = f"⚠️ У исполнителя остались активные заявки ({active}) — переназначьте их.\n\n"
    user = await services.users.set_role(actor, user.id, new_role)
    await callback.answer("Роль изменена")
    text, markup = await _card(user, services)
    await respond(callback, note + text, markup)
    await notifier.role_changed(user)


@router.callback_query(AUserCb.filter(F.action == "block"))
async def user_block(callback: CallbackQuery, callback_data: AUserCb, actor: Actor, services: Services,
                     notifier: Notifier) -> None:
    user = await services.users.get(callback_data.user_id)
    new_status = UserStatus.ACTIVE if user.is_blocked else UserStatus.BLOCKED
    user = await services.users.set_status(actor, user.id, new_status)
    await callback.answer("Заблокирован" if user.is_blocked else "Разблокирован")
    text, markup = await _card(user, services)
    await respond(callback, text, markup)
    if not user.is_blocked:
        await notifier.send(user.tg_id, "Доступ к сервису восстановлен. Нажмите /start.")


@router.callback_query(AUserCb.filter(F.action == "address"))
async def user_address(callback: CallbackQuery, callback_data: AUserCb, state: FSMContext,
                       services: Services) -> None:
    user = await services.users.get(callback_data.user_id)
    await callback.answer()
    await callback.message.answer(f"Изменение адреса клиента {t.h(user.display)}.")
    await start_address_form(callback.message, state, services, target_user_id=user.id)


@router.callback_query(AUserCb.filter(F.action == "orders"))
async def user_orders(callback: CallbackQuery, callback_data: AUserCb, actor: Actor, services: Services) -> None:
    user = await services.users.get(callback_data.user_id)
    if user.is_executor:
        orders = await services.orders.admin_list(actor, executor_id=user.id, newest_first=True, limit=20)
    else:
        orders = await services.orders.admin_list(actor, user_id=user.id, newest_first=True, limit=20)
    await callback.answer()
    buttons = [(t.order_short(o), AOrderCb(action="view", order_id=o.id).pack()) for o in orders]
    buttons.append(("◀️ К пользователю", AUserCb(action="view", user_id=user.id).pack()))
    title = f"Заказы {t.h(user.display)}" + (" (как исполнителя)" if user.is_executor else "")
    await respond(callback, f"<b>{title}</b>" + ("" if orders else "\nЗаказов нет."), build(buttons))


@router.callback_query(AUserCb.filter(F.action == "payments"))
async def user_payments(callback: CallbackQuery, callback_data: AUserCb, actor: Actor, services: Services) -> None:
    user = await services.users.get(callback_data.user_id)
    payments = await services.payments.list(actor, user_id=user.id, limit=20)
    await callback.answer()
    buttons = [
        (f"№{p.id} · {p.amount_rub} ₽ · {t.payment_status(p)}", APayCb(action="view", payment_id=p.id).pack())
        for p in payments
    ]
    buttons.append(("◀️ К пользователю", AUserCb(action="view", user_id=user.id).pack()))
    await respond(callback, f"<b>Платежи {t.h(user.display)}</b>" + ("" if payments else "\nПлатежей нет."),
                  build(buttons))
