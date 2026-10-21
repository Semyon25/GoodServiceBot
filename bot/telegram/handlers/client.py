"""Сценарии клиента: адрес, тарифы и оплата, заказ выноса, мои заказы, помощь."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.domain.enums import PaymentStatus
from bot.domain.errors import AlreadyOrdered, DomainError, NoAddress, NoSubscription, PaymentPending, ValidationError
from bot.domain.models import Actor
from bot.services import Services
from bot.services.users import parse_apartment, parse_floor, parse_instructions, parse_intercom

from .. import texts as t
from ..callbacks import AddrCb, OrderCb, PayCb, SupportCb, TariffCb
from ..cards import client_order_kb
from ..keyboards import build, cancel_input, rows
from ..middlewares import NOT_MENU, IsClient
from ..notifier import Notifier
from ..states import AddressForm, PaymentProof, SupportForm
from .utils import drop_markup, respond

router = Router(name="client")
router.message.filter(IsClient())
router.callback_query.filter(IsClient())


# =============================================================== адрес


async def _address_target(state: FSMContext, actor: Actor) -> int:
    """Чей адрес редактируется: свой или (для администратора) выбранного клиента."""
    data = await state.get_data()
    target = data.get("target_user_id")
    if target and actor.is_admin:
        return int(target)
    return actor.user.id


async def start_address_form(message: Message, state: FSMContext, services: Services,
                             target_user_id: int | None = None) -> None:
    await state.clear()
    if target_user_id:
        await state.update_data(target_user_id=target_user_id)
    await state.set_state(AddressForm.address)
    addresses = await services.addresses.list_addresses()
    markup = build(
        [(a.title, AddrCb(action="pick", value=a.id).pack()) for a in addresses]
        + [("✖️ Отмена", AddrCb(action="cancel").pack())]
    )
    await message.answer("Шаг 1 из 6. Выберите адрес дома:", reply_markup=markup)


@router.message(F.text == t.BTN_ADDRESS)
async def my_address(message: Message, state: FSMContext, actor: Actor, services: Services) -> None:
    await state.clear()
    current = await services.addresses.current(actor.user.id)
    if not current:
        await message.answer("Адрес ещё не указан.")
        await start_address_form(message, state, services)
        return
    await message.answer(
        "<b>Ваш адрес</b>\n" + t.address_text(current),
        reply_markup=rows([("✏️ Изменить адрес", AddrCb(action="start").pack())]),
    )


@router.callback_query(AddrCb.filter(F.action == "start"))
async def address_start(callback: CallbackQuery, state: FSMContext, services: Services) -> None:
    await callback.answer()
    await drop_markup(callback)
    await start_address_form(callback.message, state, services)


@router.callback_query(AddrCb.filter(F.action == "cancel"))
async def address_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await respond(callback, "Изменение адреса отменено.")


@router.callback_query(AddressForm.address, AddrCb.filter(F.action == "pick"))
async def address_pick(callback: CallbackQuery, callback_data: AddrCb, state: FSMContext, services: Services) -> None:
    address = await services.addresses.get_address(callback_data.value)
    if not address.is_active:
        raise ValidationError("Этот адрес сейчас не обслуживается.")
    await state.update_data(address_id=address.id, address_title=address.title, sections=address.sections_count)
    await state.set_state(AddressForm.section)
    await callback.answer()
    buttons = [(str(n), AddrCb(action="section", value=n).pack()) for n in range(1, address.sections_count + 1)]
    kb = build(buttons, width=6)
    kb.inline_keyboard.append(build([("✖️ Отмена", AddrCb(action="cancel").pack())]).inline_keyboard[0])
    await respond(callback, f"Адрес: <b>{t.h(address.title)}</b>\nШаг 2 из 6. Выберите секцию:", kb)


@router.callback_query(AddressForm.section, AddrCb.filter(F.action == "section"))
async def address_section(callback: CallbackQuery, callback_data: AddrCb, state: FSMContext) -> None:
    data = await state.get_data()
    if not 1 <= callback_data.value <= int(data["sections"]):
        raise ValidationError("Такой секции нет у выбранного адреса.")
    await state.update_data(section=callback_data.value)
    await state.set_state(AddressForm.floor)
    await callback.answer()
    await respond(
        callback,
        f"Адрес: <b>{t.h(data['address_title'])}</b>, секция {callback_data.value}\n"
        "Шаг 3 из 6. Напишите номер этажа:",
    )


@router.message(AddressForm.floor, F.text, NOT_MENU)
async def address_floor(message: Message, state: FSMContext) -> None:
    try:
        floor = parse_floor(message.text)
    except ValidationError as exc:
        await message.answer(exc.message)
        return
    await state.update_data(floor=floor)
    await state.set_state(AddressForm.apartment)
    await message.answer("Шаг 4 из 6. Напишите номер квартиры:")


@router.message(AddressForm.apartment, F.text, NOT_MENU)
async def address_apartment(message: Message, state: FSMContext) -> None:
    try:
        apartment = parse_apartment(message.text)
    except ValidationError as exc:
        await message.answer(exc.message)
        return
    await state.update_data(apartment=apartment)
    await state.set_state(AddressForm.intercom)
    await message.answer("Шаг 5 из 6. Напишите код домофона (как его набирать):")


@router.message(AddressForm.intercom, F.text, NOT_MENU)
async def address_intercom(message: Message, state: FSMContext) -> None:
    try:
        code = parse_intercom(message.text)
    except ValidationError as exc:
        await message.answer(exc.message)
        return
    await state.update_data(intercom_code=code)
    await state.set_state(AddressForm.instructions)
    await message.answer(
        "Шаг 6 из 6. Дополнительные инструкции для исполнителя (необязательно).\n"
        "Например: «пакет справа от двери». Напишите текст или нажмите «Пропустить».",
        reply_markup=rows([("Пропустить", AddrCb(action="skip").pack())]),
    )


async def _show_address_confirm(target: Message, state: FSMContext, edit: bool = False) -> None:
    data = await state.get_data()
    await state.set_state(AddressForm.confirm)
    text = (
        "<b>Проверьте данные</b>\n"
        f"Адрес: <b>{t.h(data['address_title'])}</b>\n"
        f"Секция: {data['section']}\n"
        f"Этаж: {data['floor']}\n"
        f"Квартира: {t.h(data['apartment'])}\n"
        f"Код домофона: {t.h(data['intercom_code'])}\n"
        f"Инструкции: {t.h(data.get('instructions') or '—')}"
    )
    markup = rows(
        [("✅ Сохранить", AddrCb(action="save").pack())],
        [("🔁 Заполнить заново", AddrCb(action="restart").pack())],
        [("✖️ Отмена", AddrCb(action="cancel").pack())],
    )
    if edit:
        await target.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.message(AddressForm.instructions, F.text, NOT_MENU)
async def address_instructions(message: Message, state: FSMContext) -> None:
    try:
        instructions = parse_instructions(message.text)
    except ValidationError as exc:
        await message.answer(exc.message)
        return
    await state.update_data(instructions=instructions)
    await _show_address_confirm(message, state)


@router.callback_query(AddressForm.instructions, AddrCb.filter(F.action == "skip"))
async def address_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(instructions=None)
    await callback.answer()
    await _show_address_confirm(callback.message, state, edit=True)


@router.callback_query(AddrCb.filter(F.action == "restart"))
async def address_restart(callback: CallbackQuery, state: FSMContext, actor: Actor, services: Services) -> None:
    target = await _address_target(state, actor)
    await callback.answer()
    await drop_markup(callback)
    await start_address_form(callback.message, state, services, target if target != actor.user.id else None)


@router.callback_query(AddressForm.confirm, AddrCb.filter(F.action == "save"))
async def address_save(callback: CallbackQuery, state: FSMContext, actor: Actor, services: Services) -> None:
    data = await state.get_data()
    target = await _address_target(state, actor)
    saved = await services.addresses.save_user_address(
        actor,
        target,
        int(data["address_id"]),
        int(data["section"]),
        int(data["floor"]),
        data["apartment"],
        data["intercom_code"],
        data.get("instructions"),
    )
    await state.clear()
    await callback.answer("Адрес сохранён")
    who = "Адрес клиента сохранён." if target != actor.user.id else "✅ Адрес сохранён."
    await respond(callback, f"{who}\n\n" + t.address_text(saved))


@router.callback_query(AddrCb.filter())
async def address_stale(callback: CallbackQuery) -> None:
    await callback.answer("Эта форма уже неактуальна. Откройте «🏠 Мой адрес» заново.", show_alert=True)


# ====================================================== тарифы и оплата


async def _tariffs_view(actor: Actor, services: Services) -> tuple[str, object]:
    service = await services.catalog.service()
    now = services.clock()
    lines = ["<b>Тарифы на вынос мусора</b>"]
    tariffs = await services.catalog.tariffs(service.id)
    for tariff in tariffs:
        lines.append(f"• {t.h(tariff.title)} — <b>{tariff.price_rub} ₽</b> за {tariff.duration_days} календарных дней")
    if not tariffs:
        lines.append("Сейчас нет доступных тарифов.")

    subs = await services.subscriptions.current_and_future(actor, actor.user.id)
    if subs:
        lines.append("\n<b>Ваши подписки</b>")
        lines += [t.subscription_line(s, services.tz, now) for s in subs]
        lines.append("Новый период начнётся после окончания уже оплаченного.")

    buttons = []
    open_payment = await services.payments.open_payment(actor.user.id, service.id)
    if open_payment and open_payment.status == PaymentStatus.AWAITING_CONFIRMATION:
        lines.append(
            f"\n⏳ Ваш платёж №{open_payment.id} на {open_payment.amount_rub} ₽ ожидает подтверждения администратором. "
            "Повторно оплачивать не нужно."
        )
    else:
        if open_payment:
            lines.append(f"\nЕсть неоплаченный платёж №{open_payment.id} на {open_payment.amount_rub} ₽.")
            buttons.append(
                (f"➡️ Продолжить оплату №{open_payment.id}", TariffCb(action="pick", tariff_id=open_payment.tariff_id).pack())
            )
        buttons += [
            (f"{tr.title} — {tr.price_rub} ₽", TariffCb(action="pick", tariff_id=tr.id).pack()) for tr in tariffs
        ]
        lines.append("\nВыберите тариф для оплаты переводом через СБП:")
    return "\n".join(lines), build(buttons) if buttons else None


@router.message(F.text == t.BTN_TARIFFS)
async def tariffs(message: Message, state: FSMContext, actor: Actor, services: Services) -> None:
    await state.clear()
    text, markup = await _tariffs_view(actor, services)
    await message.answer(text, reply_markup=markup)


@router.callback_query(TariffCb.filter(F.action == "list"))
async def tariffs_cb(callback: CallbackQuery, state: FSMContext, actor: Actor, services: Services) -> None:
    await state.clear()
    await callback.answer()
    text, markup = await _tariffs_view(actor, services)
    await respond(callback, text, markup)


@router.callback_query(TariffCb.filter(F.action == "pick"))
async def tariff_pick(callback: CallbackQuery, callback_data: TariffCb, actor: Actor, services: Services) -> None:
    try:
        payment = await services.payments.select_tariff(actor, callback_data.tariff_id)
    except PaymentPending as exc:
        await callback.answer()
        await respond(callback, f"⏳ {exc.message}")
        return
    details = await services.settings.payment_details()
    await callback.answer()
    await respond(
        callback,
        t.payment_instructions(payment, details),
        rows(
            [("✅ Я оплатил", PayCb(action="paid", payment_id=payment.id).pack())],
            [("↩️ Выбрать другой тариф", TariffCb(action="list").pack())],
        ),
    )


@router.callback_query(PayCb.filter(F.action == "paid"))
async def payment_paid(callback: CallbackQuery, callback_data: PayCb, state: FSMContext, actor: Actor,
                       services: Services) -> None:
    payment = await services.payments.get(actor, callback_data.payment_id)
    if payment.status == PaymentStatus.AWAITING_CONFIRMATION:
        await callback.answer("Платёж уже на проверке у администратора.", show_alert=True)
        return
    if payment.status != PaymentStatus.AWAITING_PAYMENT:
        await callback.answer(f"Платёж уже обработан: {t.payment_status(payment)}.", show_alert=True)
        return
    await state.set_state(PaymentProof.waiting)
    await state.update_data(payment_id=payment.id)
    await callback.answer()
    await drop_markup(callback)
    await callback.message.answer(
        f"Пришлите чек или скриншот перевода {payment.amount_rub} ₽ из банковского приложения "
        "(фото или файл). Это нужно администратору для проверки.\n\n"
        "Подписка активируется после того, как администратор проверит поступление денег.",
        reply_markup=rows([("✖️ Отмена", PayCb(action="cancel_proof", payment_id=payment.id).pack())]),
    )


@router.callback_query(PayCb.filter(F.action == "cancel_proof"))
async def payment_cancel_proof(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await respond(
        callback,
        "Отправка подтверждения отменена. Вернуться к оплате можно в разделе «💳 Тарифы и оплата».",
    )


@router.message(PaymentProof.waiting, F.photo | F.document)
async def payment_proof(message: Message, state: FSMContext, actor: Actor, services: Services,
                        notifier: Notifier) -> None:
    data = await state.get_data()
    if message.photo:
        proof_type, file_id = "photo", message.photo[-1].file_id
    else:
        proof_type, file_id = "document", message.document.file_id
    payment, changed = await services.payments.submit_proof(
        actor, int(data["payment_id"]), proof_type, file_id, (message.caption or "")[:500] or None
    )
    await state.clear()
    if not changed:
        await message.answer("Этот платёж уже передан на проверку. Дождитесь решения администратора.")
        return
    await message.answer(
        f"Спасибо! Платёж №{payment.id} передан на проверку администратору.\n"
        "Статус: «Ожидает подтверждения». Мы сообщим, как только оплата будет подтверждена."
    )
    await notifier.payment_submitted(payment, actor.user)


@router.message(PaymentProof.waiting, NOT_MENU)
async def payment_proof_wrong(message: Message) -> None:
    await message.answer("Пришлите, пожалуйста, чек или скриншот перевода — фото или файл.")


# ============================================================ подписка


@router.message(F.text == t.BTN_SUBSCRIPTION)
async def my_subscription(message: Message, state: FSMContext, actor: Actor, services: Services) -> None:
    await state.clear()
    service = await services.catalog.service()
    now = services.clock()
    active = await services.subscriptions.active(actor.user.id, service.id)
    subs = await services.subscriptions.current_and_future(actor, actor.user.id)
    lines = []
    if active:
        lines.append(
            f"✅ Подписка активна: <b>{t.h(active.tariff_title)}</b>\n"
            f"Начало: {t.fmt_dt(active.starts_at, services.tz)}\n"
            f"Окончание: {t.fmt_dt(active.ends_at, services.tz)}"
        )
    else:
        lines.append("Действующей подписки нет.")
    future = [s for s in subs if s.starts_at > now]
    if future:
        lines.append("\n<b>Оплаченные следующие периоды</b>")
        lines += [t.subscription_line(s, services.tz, now) for s in future]
    open_payment = await services.payments.open_payment(actor.user.id, service.id)
    if open_payment:
        lines.append(f"\nПлатёж №{open_payment.id} на {open_payment.amount_rub} ₽: {t.payment_status(open_payment)}.")
    await message.answer(
        "\n".join(lines), reply_markup=rows([("💳 Тарифы и оплата", TariffCb(action="list").pack())])
    )


# =========================================================== заказ выноса


@router.message(F.text == t.BTN_ORDER)
async def order_start(message: Message, state: FSMContext, actor: Actor, services: Services) -> None:
    await state.clear()
    service = await services.catalog.service()
    try:
        await services.orders.check_can_order(actor, service)
    except DomainError as exc:
        await _order_refusal(message, exc, actor, services)
        return
    address = await services.addresses.current(actor.user.id)
    await message.answer(
        "Выставьте пакет с мусором у двери квартиры и нажмите кнопку ниже.\n\n" + t.address_text(address),
        reply_markup=rows([("🗑 Мусор выставлен — забрать", OrderCb(action="placed").pack())]),
    )


async def _order_refusal(message: Message, exc: DomainError, actor: Actor, services: Services) -> None:
    if isinstance(exc, AlreadyOrdered) and exc.order:
        await message.answer(
            "Заявка на текущую смену уже создана. Повторный заказ в эту смену невозможен.\n\n"
            + t.order_card(exc.order, services.tz),
            reply_markup=client_order_kb(exc.order),
        )
        return
    if isinstance(exc, NoSubscription):
        service = await services.catalog.service()
        pending = await services.payments.open_payment(actor.user.id, service.id)
        if pending and pending.status == PaymentStatus.AWAITING_CONFIRMATION:
            await message.answer(
                f"⏳ Ваша оплата (платёж №{pending.id}) ожидает подтверждения администратором. "
                "Заказ станет доступен сразу после подтверждения."
            )
            return
        await message.answer(
            exc.message, reply_markup=rows([("💳 Перейти к тарифам", TariffCb(action="list").pack())])
        )
        return
    if isinstance(exc, NoAddress):
        await message.answer(exc.message, reply_markup=rows([("🏠 Указать адрес", AddrCb(action="start").pack())]))
        return
    await message.answer(t.h(exc.message))


@router.callback_query(OrderCb.filter(F.action == "placed"))
async def order_placed(callback: CallbackQuery) -> None:
    await callback.answer()
    await respond(
        callback,
        "Пакет уже стоит у двери. Создать заявку на вынос?",
        rows([("✅ Подтвердить", OrderCb(action="confirm").pack()), ("◀️ Назад", OrderCb(action="back").pack())]),
    )


@router.callback_query(OrderCb.filter(F.action == "back"))
async def order_back(callback: CallbackQuery) -> None:
    await callback.answer()
    await respond(
        callback,
        "Когда пакет будет у двери, нажмите кнопку ниже.",
        rows([("🗑 Мусор выставлен — забрать", OrderCb(action="placed").pack())]),
    )


@router.callback_query(OrderCb.filter(F.action == "confirm"))
async def order_confirm(callback: CallbackQuery, actor: Actor, services: Services, notifier: Notifier) -> None:
    await drop_markup(callback)  # защита от повторного нажатия
    service = await services.catalog.service()
    try:
        order = await services.orders.create_order(actor, service)
    except DomainError as exc:
        await callback.answer()
        await _order_refusal(callback.message, exc, actor, services)
        return
    await callback.answer("Заявка создана")
    await respond(callback, t.order_card(order, services.tz, title=f"✅ <b>Заявка №{order.id} создана</b>"),
                  client_order_kb(order))
    await notifier.order_created(order, actor.user)


# ============================================================ мои заказы


async def _orders_list(actor: Actor, services: Services) -> tuple[str, object]:
    orders = await services.orders.client_orders(actor, limit=10)
    if not orders:
        return "У вас пока нет заказов.", None
    lines = ["<b>Ваши последние заказы</b>"]
    for o in orders:
        lines.append(f"№{o.id} · {t.fmt_date(o.shift_date)} · {t.order_status(o)}")
    buttons = [(f"№{o.id} · {o.shift_date:%d.%m} · {t.order_status(o)}", OrderCb(action="view", order_id=o.id).pack())
               for o in orders]
    return "\n".join(lines), build(buttons, width=1)


@router.message(F.text == t.BTN_MY_ORDERS)
async def my_orders(message: Message, state: FSMContext, actor: Actor, services: Services) -> None:
    await state.clear()
    text, markup = await _orders_list(actor, services)
    await message.answer(text, reply_markup=markup)


@router.callback_query(OrderCb.filter(F.action == "list"))
async def my_orders_cb(callback: CallbackQuery, actor: Actor, services: Services) -> None:
    await callback.answer()
    text, markup = await _orders_list(actor, services)
    await respond(callback, text, markup)


@router.callback_query(OrderCb.filter(F.action == "view"))
async def order_view(callback: CallbackQuery, callback_data: OrderCb, actor: Actor, services: Services) -> None:
    order = await services.orders.get_for_client(actor, callback_data.order_id)
    await callback.answer()
    await respond(callback, t.order_card(order, services.tz), client_order_kb(order))


# ================================================================ помощь


@router.message(F.text == t.BTN_HELP)
async def help_(message: Message, state: FSMContext, services: Services) -> None:
    await state.clear()
    text = t.h(await services.settings.get("texts.help", ""))
    schedule = await services.settings.schedule()
    await message.answer(
        f"{text}\n\n<b>Расписание</b>\n{t.schedule_text(schedule)}",
        reply_markup=rows([("✉️ Написать администратору", SupportCb(action="write").pack())]),
    )


@router.callback_query(SupportCb.filter(F.action == "write"))
async def support_write(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SupportForm.waiting)
    await callback.answer()
    await callback.message.answer("Напишите ваш вопрос одним сообщением — мы передадим его администратору.",
                                  reply_markup=cancel_input())


@router.message(SupportForm.waiting, F.text, NOT_MENU)
async def support_message(message: Message, state: FSMContext, actor: Actor, notifier: Notifier) -> None:
    await state.clear()
    text = message.text.strip()[:3000]
    await notifier.to_admins(
        f"✉️ <b>Обращение от {t.h(actor.user.display)}</b>\n\n{t.h(text)}",
        rows([("↩️ Ответить", SupportCb(action="reply", user_tg=actor.tg_id).pack())]),
    )
    await message.answer("Сообщение отправлено администратору. Ответ придёт в этот чат.")


@router.message(SupportForm.waiting, NOT_MENU)
async def support_wrong(message: Message) -> None:
    await message.answer("Пожалуйста, напишите вопрос текстом.")
