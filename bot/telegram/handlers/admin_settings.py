"""Администратор: тарифы, расписание, адреса, реквизиты СБП, настройки, статистика, экспорт, ответы клиентам."""
from __future__ import annotations

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from bot.db.connection import from_db_dt
from bot.domain.enums import DISTRIBUTION_MODE_TITLES, WEEKDAY_SHORT, DistributionMode, OrderStatus, PaymentStatus
from bot.domain.errors import ValidationError
from bot.domain.models import Actor
from bot.services import Services
from bot.services.reports import PERIODS
from bot.services.settings import PAYMENT_KEYS, TEXT_KEYS

from .. import texts as t
from ..callbacks import AAddrCb, AExportCb, ASchedCb, ASettingCb, AStatsCb, ATariffCb, SupportCb
from ..keyboards import build, rows
from ..notifier import Notifier
from .admin_common import admin_router, ask_input, input_handler, parse_int
from .utils import chunk_text, respond

router = admin_router("admin_settings")


# ================================================================ тарифы


async def _tariffs_view(services: Services) -> tuple[str, object]:
    service = await services.catalog.service()
    tariffs = await services.catalog.tariffs(service.id, active_only=False)
    lines = [f"<b>🏷 Тарифы — {t.h(service.title)}</b>",
             "Изменения действуют только для новых покупок; оплаченные подписки не меняются.\n"]
    for tr in tariffs:
        state = "✅" if tr.is_active else "⏸ отключён"
        lines.append(f"• {t.h(tr.title)}: {tr.price_rub} ₽ / {tr.duration_days} дн. {state}")
    buttons = [(f"{tr.title} — {tr.price_rub} ₽", ATariffCb(action="view", tariff_id=tr.id).pack()) for tr in tariffs]
    buttons.append(("➕ Добавить тариф", ATariffCb(action="new").pack()))
    return "\n".join(lines), build(buttons)


@router.message(F.text == t.BTN_A_TARIFFS)
async def tariffs_panel(message: Message, state: FSMContext, services: Services) -> None:
    await state.clear()
    text, markup = await _tariffs_view(services)
    await message.answer(text, reply_markup=markup)


@router.callback_query(ATariffCb.filter(F.action == "list"))
async def tariffs_list(callback: CallbackQuery, services: Services) -> None:
    await callback.answer()
    text, markup = await _tariffs_view(services)
    await respond(callback, text, markup)


async def _tariff_card(tariff_id: int, services: Services) -> tuple[str, object]:
    tr = await services.catalog.tariff(tariff_id)
    text = (
        f"<b>Тариф «{t.h(tr.title)}»</b>\n"
        f"Стоимость: {tr.price_rub} ₽\n"
        f"Срок действия: {tr.duration_days} календарных дней\n"
        f"Доступен для покупки: {'да' if tr.is_active else 'нет'}"
    )
    markup = rows(
        [("💵 Изменить стоимость", ATariffCb(action="price", tariff_id=tr.id).pack())],
        [("📆 Изменить срок", ATariffCb(action="duration", tariff_id=tr.id).pack())],
        [("⏸ Отключить" if tr.is_active else "▶️ Включить", ATariffCb(action="toggle", tariff_id=tr.id).pack())],
        [("◀️ К тарифам", ATariffCb(action="list").pack())],
    )
    return text, markup


@router.callback_query(ATariffCb.filter(F.action == "view"))
async def tariff_view(callback: CallbackQuery, callback_data: ATariffCb, services: Services) -> None:
    await callback.answer()
    text, markup = await _tariff_card(callback_data.tariff_id, services)
    await respond(callback, text, markup)


@router.callback_query(ATariffCb.filter(F.action == "toggle"))
async def tariff_toggle(callback: CallbackQuery, callback_data: ATariffCb, actor: Actor, services: Services) -> None:
    tr = await services.catalog.tariff(callback_data.tariff_id)
    await services.catalog.update_tariff(actor, tr.id, is_active=not tr.is_active)
    await callback.answer("Сохранено")
    text, markup = await _tariff_card(tr.id, services)
    await respond(callback, text, markup)


@router.callback_query(ATariffCb.filter(F.action.in_({"price", "duration"})))
async def tariff_edit(callback: CallbackQuery, callback_data: ATariffCb, state: FSMContext,
                      services: Services) -> None:
    tr = await services.catalog.tariff(callback_data.tariff_id)
    await callback.answer()
    prompt = (f"Новая стоимость тарифа «{t.h(tr.title)}» в рублях:" if callback_data.action == "price"
              else f"Новый срок действия тарифа «{t.h(tr.title)}» в календарных днях:")
    await ask_input(callback.message, state, f"tariff_{callback_data.action}", prompt, tariff_id=tr.id)


@input_handler("tariff_price")
async def tariff_price_input(message: Message, data: dict, actor: Actor, services: Services, notifier: Notifier) -> None:
    await services.catalog.update_tariff(actor, int(data["tariff_id"]), price_rub=parse_int(message.text, "Стоимость"))
    text, markup = await _tariff_card(int(data["tariff_id"]), services)
    await message.answer("Сохранено.\n\n" + text, reply_markup=markup)


@input_handler("tariff_duration")
async def tariff_duration_input(message: Message, data: dict, actor: Actor, services: Services,
                                notifier: Notifier) -> None:
    await services.catalog.update_tariff(actor, int(data["tariff_id"]), duration_days=parse_int(message.text, "Срок"))
    text, markup = await _tariff_card(int(data["tariff_id"]), services)
    await message.answer("Сохранено.\n\n" + text, reply_markup=markup)


@router.callback_query(ATariffCb.filter(F.action == "new"))
async def tariff_new(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await ask_input(callback.message, state, "tariff_new",
                    "Введите новый тариф в формате:\n<code>Название; стоимость в рублях; срок в днях</code>\n"
                    "Например: <code>Две недели; 380; 14</code>")


@input_handler("tariff_new")
async def tariff_new_input(message: Message, data: dict, actor: Actor, services: Services, notifier: Notifier) -> None:
    parts = [p.strip() for p in message.text.split(";")]
    if len(parts) != 3:
        raise ValidationError("Нужно три значения через «;»: название, стоимость, срок.")
    service = await services.catalog.service()
    tariff = await services.catalog.create_tariff(
        actor, service.id, parts[0], parse_int(parts[1], "Стоимость"), parse_int(parts[2], "Срок")
    )
    text, markup = await _tariff_card(tariff.id, services)
    await message.answer("Тариф добавлен.\n\n" + text, reply_markup=markup)


# ============================================================= расписание

TIME_KEYS = {
    "accept_start": "Начало приёма заявок",
    "accept_end": "Окончание приёма заявок",
    "work_end": "Окончание выполнения",
}


async def _schedule_view(services: Services) -> tuple[str, object]:
    cfg = await services.settings.schedule()
    text = "<b>🕒 Расписание</b>\n" + t.schedule_text(cfg) + (
        "\n\nВремя окончания, меньшее времени начала, относится к следующим суткам. "
        "Интервал после полуночи относится к смене предыдущего дня."
    )
    day_buttons = [
        (("✅ " if d in cfg.work_days else "▫️ ") + WEEKDAY_SHORT[d], ASchedCb(action="day", value=str(d)).pack())
        for d in range(7)
    ]
    current = {"accept_start": cfg.accept_start, "accept_end": cfg.accept_end, "work_end": cfg.work_end}
    markup = rows(
        day_buttons[:4],
        day_buttons[4:],
        *[[(f"{title}: {current[key]:%H:%M}", ASchedCb(action="time", value=key).pack())] for key, title in
          TIME_KEYS.items()],
    )
    return text, markup


@router.message(F.text == t.BTN_A_SCHEDULE)
async def schedule_panel(message: Message, state: FSMContext, services: Services) -> None:
    await state.clear()
    text, markup = await _schedule_view(services)
    await message.answer(text, reply_markup=markup)


@router.callback_query(ASchedCb.filter(F.action == "day"))
async def schedule_day(callback: CallbackQuery, callback_data: ASchedCb, actor: Actor, services: Services) -> None:
    await services.settings.toggle_work_day(actor, int(callback_data.value))
    await callback.answer("Сохранено")
    text, markup = await _schedule_view(services)
    await respond(callback, text, markup)


@router.callback_query(ASchedCb.filter(F.action == "time"))
async def schedule_time(callback: CallbackQuery, callback_data: ASchedCb, state: FSMContext) -> None:
    if callback_data.value not in TIME_KEYS:
        raise ValidationError("Неизвестный параметр.")
    await callback.answer()
    await ask_input(callback.message, state, "schedule_time",
                    f"{TIME_KEYS[callback_data.value]} — введите время в формате ЧЧ:ММ:", key=callback_data.value)


@input_handler("schedule_time")
async def schedule_time_input(message: Message, data: dict, actor: Actor, services: Services,
                              notifier: Notifier) -> None:
    await services.settings.set_schedule_time(actor, data["key"], message.text)
    text, markup = await _schedule_view(services)
    await message.answer("Сохранено.\n\n" + text, reply_markup=markup)


# ================================================================= адреса


async def _addresses_view(services: Services) -> tuple[str, object]:
    addresses = await services.addresses.list_addresses(active_only=False)
    lines = ["<b>🏢 Адреса обслуживания</b>"]
    for a in addresses:
        lines.append(f"• {t.h(a.title)} — секций: {a.sections_count}" + ("" if a.is_active else " (отключён)"))
    buttons = [(a.title, AAddrCb(action="view", address_id=a.id).pack()) for a in addresses]
    buttons.append(("➕ Добавить адрес", AAddrCb(action="new").pack()))
    return "\n".join(lines), build(buttons)


@router.message(F.text == t.BTN_A_ADDRESSES)
async def addresses_panel(message: Message, state: FSMContext, services: Services) -> None:
    await state.clear()
    text, markup = await _addresses_view(services)
    await message.answer(text, reply_markup=markup)


@router.callback_query(AAddrCb.filter(F.action == "list"))
async def addresses_list(callback: CallbackQuery, services: Services) -> None:
    await callback.answer()
    text, markup = await _addresses_view(services)
    await respond(callback, text, markup)


async def _address_card(address_id: int, services: Services) -> tuple[str, object]:
    a = await services.addresses.get_address(address_id)
    text = (f"<b>{t.h(a.title)}</b>\nСекций: {a.sections_count}\n"
            f"Доступен клиентам: {'да' if a.is_active else 'нет'}")
    markup = rows(
        [("🔢 Изменить количество секций", AAddrCb(action="sections", address_id=a.id).pack())],
        [("⏸ Отключить" if a.is_active else "▶️ Включить", AAddrCb(action="toggle", address_id=a.id).pack())],
        [("◀️ К адресам", AAddrCb(action="list").pack())],
    )
    return text, markup


@router.callback_query(AAddrCb.filter(F.action == "view"))
async def address_view(callback: CallbackQuery, callback_data: AAddrCb, services: Services) -> None:
    await callback.answer()
    text, markup = await _address_card(callback_data.address_id, services)
    await respond(callback, text, markup)


@router.callback_query(AAddrCb.filter(F.action == "toggle"))
async def address_toggle(callback: CallbackQuery, callback_data: AAddrCb, actor: Actor, services: Services) -> None:
    await services.addresses.toggle_active(actor, callback_data.address_id)
    await callback.answer("Сохранено")
    text, markup = await _address_card(callback_data.address_id, services)
    await respond(callback, text, markup)


@router.callback_query(AAddrCb.filter(F.action == "sections"))
async def address_sections(callback: CallbackQuery, callback_data: AAddrCb, state: FSMContext,
                           services: Services) -> None:
    a = await services.addresses.get_address(callback_data.address_id)
    await callback.answer()
    await ask_input(callback.message, state, "address_sections",
                    f"Количество секций для «{t.h(a.title)}» (сейчас {a.sections_count}):", address_id=a.id)


@input_handler("address_sections")
async def address_sections_input(message: Message, data: dict, actor: Actor, services: Services,
                                 notifier: Notifier) -> None:
    await services.addresses.set_sections(actor, int(data["address_id"]), parse_int(message.text, "Секции"))
    text, markup = await _address_card(int(data["address_id"]), services)
    await message.answer("Сохранено.\n\n" + text, reply_markup=markup)


@router.callback_query(AAddrCb.filter(F.action == "new"))
async def address_new(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await ask_input(callback.message, state, "address_new",
                    "Введите адрес в формате:\n<code>Название; количество секций</code>\n"
                    "Например: <code>Луговая, 5; 6</code>")


@input_handler("address_new")
async def address_new_input(message: Message, data: dict, actor: Actor, services: Services,
                            notifier: Notifier) -> None:
    title, sep, sections = message.text.rpartition(";")
    if not sep:
        raise ValidationError("Нужно два значения через «;»: название и количество секций.")
    address = await services.addresses.create_address(actor, title, parse_int(sections, "Секции"))
    text, markup = await _address_card(address.id, services)
    await message.answer("Адрес добавлен.\n\n" + text, reply_markup=markup)


# =========================================================== реквизиты СБП


async def _sbp_view(services: Services) -> tuple[str, object]:
    details = await services.settings.payment_details()
    text = (
        "<b>🏦 Реквизиты для оплаты через СБП</b>\nТак их видит клиент:\n\n"
        f"{t.requisites_text(details)}\n\n<b>Инструкция:</b>\n{t.h(details['payment.instructions'])}"
    )
    buttons = [(f"✏️ {title}", ASettingCb(action="edit", key=key).pack()) for key, title in PAYMENT_KEYS.items()]
    return text, build(buttons)


@router.message(F.text == t.BTN_A_SBP)
async def sbp_panel(message: Message, state: FSMContext, services: Services) -> None:
    await state.clear()
    text, markup = await _sbp_view(services)
    await message.answer(text, reply_markup=markup)


# ============================================================== настройки

EDITABLE_KEYS = {**PAYMENT_KEYS, **TEXT_KEYS}


async def _settings_view(services: Services) -> tuple[str, object]:
    mode = await services.settings.distribution_mode()
    minutes = await services.settings.unassigned_alert_minutes()
    text = (
        "<b>⚙️ Настройки</b>\n"
        f"Распределение заявок: <b>{DISTRIBUTION_MODE_TITLES[mode]}</b>\n"
        f"Напоминать о заявке без исполнителя через: {minutes} мин. (0 — не напоминать)"
    )
    other = DistributionMode.POOL if mode == DistributionMode.MANUAL else DistributionMode.MANUAL
    markup = rows(
        [(f"🔀 Режим: {DISTRIBUTION_MODE_TITLES[other]}", ASettingCb(action="mode", key=other.value).pack())],
        [("⏰ Время напоминания", ASettingCb(action="alert").pack())],
        *[[(f"✏️ {title}", ASettingCb(action="edit", key=key).pack())] for key, title in TEXT_KEYS.items()],
        [("📜 Журнал действий администраторов", ASettingCb(action="audit").pack())],
    )
    return text, markup


@router.message(F.text == t.BTN_A_SETTINGS)
async def settings_panel(message: Message, state: FSMContext, services: Services) -> None:
    await state.clear()
    text, markup = await _settings_view(services)
    await message.answer(text, reply_markup=markup)


@router.callback_query(ASettingCb.filter(F.action == "mode"))
async def settings_mode(callback: CallbackQuery, callback_data: ASettingCb, actor: Actor, services: Services) -> None:
    mode = DistributionMode(callback_data.key)
    await services.settings.set(actor, "orders.distribution_mode", mode.value)
    await callback.answer("Режим изменён")
    text, markup = await _settings_view(services)
    await respond(callback, text, markup)


@router.callback_query(ASettingCb.filter(F.action == "alert"))
async def settings_alert(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await ask_input(callback.message, state, "alert_minutes",
                    "Через сколько минут напоминать о заявке без исполнителя? (0 — не напоминать)")


@input_handler("alert_minutes")
async def settings_alert_input(message: Message, data: dict, actor: Actor, services: Services,
                               notifier: Notifier) -> None:
    minutes = parse_int(message.text, "Минуты")
    if not 0 <= minutes <= 1440:
        raise ValidationError("Значение должно быть от 0 до 1440.")
    await services.settings.set(actor, "orders.unassigned_alert_minutes", minutes)
    text, markup = await _settings_view(services)
    await message.answer("Сохранено.\n\n" + text, reply_markup=markup)


@router.callback_query(ASettingCb.filter(F.action == "edit"))
async def settings_edit(callback: CallbackQuery, callback_data: ASettingCb, state: FSMContext,
                        services: Services) -> None:
    if callback_data.key not in EDITABLE_KEYS:
        raise ValidationError("Неизвестная настройка.")
    current = await services.settings.get(callback_data.key, "")
    await callback.answer()
    await ask_input(
        callback.message, state, "setting_text",
        f"<b>{EDITABLE_KEYS[callback_data.key]}</b>\nТекущее значение:\n{t.h(current)}\n\nВведите новое значение:",
        key=callback_data.key,
    )


@input_handler("setting_text")
async def settings_edit_input(message: Message, data: dict, actor: Actor, services: Services,
                              notifier: Notifier) -> None:
    key = data["key"]
    if key not in EDITABLE_KEYS:
        raise ValidationError("Неизвестная настройка.")
    value = message.text.strip()
    if not value or len(value) > 2000:
        raise ValidationError("Значение должно содержать от 1 до 2000 символов.")
    await services.settings.set(actor, key, value)
    if key in PAYMENT_KEYS:
        text, markup = await _sbp_view(services)
    else:
        text, markup = await _settings_view(services)
    await message.answer("Сохранено.\n\n" + text, reply_markup=markup)


@router.callback_query(ASettingCb.filter(F.action == "audit"))
async def settings_audit(callback: CallbackQuery, services: Services) -> None:
    entries = await services.repos.audit.recent(30)
    await callback.answer()
    if not entries:
        await callback.message.answer("Журнал пуст.")
        return
    lines = ["<b>📜 Последние действия администраторов</b>"]
    for e in entries:
        lines.append(f"{t.fmt_dt(from_db_dt(e['created_at']), services.tz)} · ID {e['actor_tg']} · "
                     f"{t.h(e['action'])}: {t.h(e['details'] or '')}")
    for part in chunk_text("\n".join(lines)):
        await callback.message.answer(part)


# ============================================================= статистика


def _stats_markup():
    return build([(title, AStatsCb(period=key).pack()) for key, title in PERIODS.items()], width=2)


@router.message(F.text == t.BTN_A_STATS)
async def stats_panel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("<b>📊 Статистика</b>\nВыберите период:", reply_markup=_stats_markup())


@router.callback_query(AStatsCb.filter())
async def stats_show(callback: CallbackQuery, callback_data: AStatsCb, actor: Actor, services: Services) -> None:
    stats = await services.reports.stats(actor, callback_data.period)
    by_status = stats.orders_by_status
    pays = stats.payments
    confirmed = pays.get(PaymentStatus.CONFIRMED.value, (0, 0))
    lines = [
        f"<b>📊 Статистика: {stats.period_title}</b>",
        "",
        f"Заявок: <b>{stats.orders_total}</b>",
        f"Выполнено: {by_status.get(OrderStatus.DONE.value, 0)}",
        f"Не удалось выполнить: {by_status.get(OrderStatus.FAILED.value, 0)}",
        f"Отменено: {by_status.get(OrderStatus.CANCELLED.value, 0)}",
    ]
    active = sum(by_status.get(s.value, 0) for s in (OrderStatus.NEW, OrderStatus.ASSIGNED, OrderStatus.ACCEPTED,
                                                    OrderStatus.IN_PROGRESS))
    lines.append(f"В процессе: {active}")
    if stats.by_executor:
        lines.append("\n<b>По исполнителям</b> (выполнено / всего)")
        lines += [f"• {t.h(name)}: {done} / {total}" for name, done, total in stats.by_executor]
    subs = stats.subscriptions
    lines += [
        "\n<b>Подписки</b> (на текущий момент)",
        f"Активные: {subs.get('active', 0)}",
        f"Оплачены на будущее: {subs.get('future', 0)}",
        f"Истёкшие: {subs.get('expired', 0)}",
        f"Аннулированные: {subs.get('cancelled', 0)}",
        "\n<b>Платежи</b>",
        f"Подтверждённые: {confirmed[0]} на сумму <b>{confirmed[1]} ₽</b>",
        f"Отклонённые: {pays.get(PaymentStatus.REJECTED.value, (0, 0))[0]}",
        f"Возвращённые: {pays.get(PaymentStatus.REFUNDED.value, (0, 0))[0]}",
        f"Ожидают подтверждения: {pays.get(PaymentStatus.AWAITING_CONFIRMATION.value, (0, 0))[0]}",
    ]
    await callback.answer()
    await respond(callback, "\n".join(lines), _stats_markup())


# ================================================================= экспорт


@router.message(F.text == t.BTN_A_EXPORT)
async def export_panel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "<b>📤 Экспорт в CSV</b>\nФайлы открываются в Excel (разделитель «;», кодировка UTF-8).",
        reply_markup=build(
            [
                ("📦 Заказы", AExportCb(kind="orders").pack()),
                ("💰 Платежи", AExportCb(kind="payments").pack()),
                ("👥 Пользователи", AExportCb(kind="users").pack()),
            ],
            width=3,
        ),
    )


@router.callback_query(AExportCb.filter())
async def export_file(callback: CallbackQuery, callback_data: AExportCb, actor: Actor, services: Services) -> None:
    if callback_data.kind not in ("orders", "payments", "users"):
        raise ValidationError("Неизвестный тип выгрузки.")
    filename, content = await services.reports.export_csv(actor, callback_data.kind)
    await callback.answer("Готовлю файл…")
    await callback.message.answer_document(BufferedInputFile(content, filename=filename))


# ========================================================= ответ клиенту


@router.callback_query(SupportCb.filter(F.action == "reply"))
async def support_reply(callback: CallbackQuery, callback_data: SupportCb, state: FSMContext) -> None:
    await callback.answer()
    await ask_input(callback.message, state, "support_reply", "Напишите ответ клиенту:", user_tg=callback_data.user_tg)


@input_handler("support_reply")
async def support_reply_input(message: Message, data: dict, actor: Actor, services: Services,
                              notifier: Notifier) -> None:
    sent = await notifier.send(int(data["user_tg"]), f"💬 <b>Ответ администратора</b>\n\n{t.h(message.text)}")
    await message.answer("Ответ отправлен." if sent else "Не удалось доставить ответ (пользователь заблокировал бота?).")
