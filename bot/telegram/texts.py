"""Тексты и форматирование карточек для Telegram (HTML)."""
from __future__ import annotations

import html
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from bot.domain.enums import (
    FAIL_REASON_TITLES,
    ORDER_STATUS_TITLES,
    PAYMENT_STATUS_TITLES,
    ROLE_TITLES,
    USER_STATUS_TITLES,
    WEEKDAY_SHORT,
    FailReason,
    OrderStatus,
    PaymentStatus,
    Role,
    UserStatus,
)
from bot.domain.models import Order, Payment, StatusHistoryEntry, Subscription, User, UserAddress
from bot.domain.schedule import ScheduleConfig, pickup_days_text, pickup_time_text, pickup_when_text
from bot.domain.transitions import ORDER_ACTIVE_STATUSES

# --- кнопки главного меню клиента ---
BTN_ORDER = "🗑 Заказать вынос мусора"
BTN_SUBSCRIPTION = "📅 Моя подписка"
BTN_ADDRESS = "🏠 Мой адрес"
BTN_TARIFFS = "💳 Тарифы и оплата"
BTN_MY_ORDERS = "📦 Мои заказы"
BTN_HELP = "❓ Помощь"
BTN_ADMIN_PANEL = "🛠 Админ-панель"

# --- исполнитель ---
BTN_EX_NEW = "🆕 Новые заявки"
BTN_EX_ASSIGNED = "📌 Назначенные"
BTN_EX_IN_WORK = "🚶 В работе"
BTN_EX_DONE = "✅ Выполненные за смену"
BTN_EX_HISTORY = "🗂 История заказов"
BTN_A_MY_ORDERS = "🧹 Мои заявки"

# --- администратор ---
BTN_A_ORDERS = "📋 Заказы"
BTN_A_PAYMENTS = "💰 Платежи"
BTN_A_USERS = "👥 Пользователи"
BTN_A_TARIFFS = "🏷 Тарифы"
BTN_A_SCHEDULE = "🕒 Расписание"
BTN_A_ADDRESSES = "🏢 Адреса"
BTN_A_SBP = "🏦 Реквизиты СБП"
BTN_A_SETTINGS = "⚙️ Настройки"
BTN_A_STATS = "📊 Статистика"
BTN_A_EXPORT = "📤 Экспорт CSV"
BTN_A_CLIENT_MODE = "👤 Режим клиента"

ALL_MENU_BUTTONS = frozenset(
    {
        BTN_ORDER, BTN_SUBSCRIPTION, BTN_ADDRESS, BTN_TARIFFS, BTN_MY_ORDERS, BTN_HELP, BTN_ADMIN_PANEL,
        BTN_EX_NEW, BTN_EX_ASSIGNED, BTN_EX_IN_WORK, BTN_EX_DONE, BTN_EX_HISTORY, BTN_A_MY_ORDERS,
        BTN_A_ORDERS, BTN_A_PAYMENTS, BTN_A_USERS, BTN_A_TARIFFS, BTN_A_SCHEDULE, BTN_A_ADDRESSES,
        BTN_A_SBP, BTN_A_SETTINGS, BTN_A_STATS, BTN_A_EXPORT, BTN_A_CLIENT_MODE,
    }
)

TEMP_ERROR = "⚠️ Произошла временная ошибка. Попробуйте ещё раз чуть позже."


def h(value: object | None) -> str:
    """Экранирование пользовательских данных для HTML."""
    return html.escape(str(value)) if value is not None else ""


def fmt_dt(value: datetime | None, tz: ZoneInfo) -> str:
    if value is None:
        return "—"
    return value.astimezone(tz).strftime("%d.%m.%Y %H:%M")


def fmt_date(value: date | None) -> str:
    if value is None:
        return "—"
    return f"{value:%d.%m.%Y} ({WEEKDAY_SHORT[value.weekday()]})"


def fmt_day(value: date | datetime | None, tz: ZoneInfo | None = None) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        value = value.astimezone(tz).date()
    return value.strftime("%d.%m.%Y")


def order_status(order: Order) -> str:
    return ORDER_STATUS_TITLES[OrderStatus(order.status)]


# Клиенту не важны внутренние этапы («Назначен», «Принят», «В работе»)
CLIENT_STATUS_TITLES = {
    OrderStatus.NEW: "Ожидает выноса",
    OrderStatus.ASSIGNED: "Ожидает выноса",
    OrderStatus.ACCEPTED: "Ожидает выноса",
    OrderStatus.IN_PROGRESS: "Ожидает выноса",
    OrderStatus.DONE: "Мусор вынесен",
    OrderStatus.FAILED: "Не удалось вынести",
    OrderStatus.CANCELLED: "Отменена",
}


def client_status(order: Order) -> str:
    return CLIENT_STATUS_TITLES[OrderStatus(order.status)]


def payment_status(payment: Payment) -> str:
    return PAYMENT_STATUS_TITLES[PaymentStatus(payment.status)]


def user_line(user: User | None) -> str:
    return h(user.display) if user else "—"


def address_text(ua: UserAddress) -> str:
    lines = [
        f"Адрес: <b>{h(ua.address_title)}</b>",
        f"Секция: {ua.section}",
        f"Этаж: {ua.floor}",
        f"Квартира: {h(ua.apartment)}",
        f"Код домофона: {h(ua.intercom_code)}",
    ]
    if ua.instructions:
        lines.append(f"Инструкции: {h(ua.instructions)}")
    return "\n".join(lines)


def order_short(order: Order, numbered: bool = True) -> str:
    prefix = f"№{order.id}" if numbered else f"{order.shift_date:%d.%m}"
    return f"{prefix} · {order.address_title}, с{order.section}, кв {order.apartment} · {order_status(order)}"


def fail_text(order: Order) -> str | None:
    if not order.fail_reason:
        return None
    reason = FAIL_REASON_TITLES.get(FailReason(order.fail_reason), order.fail_reason)
    return f"{reason}" + (f": {order.comment}" if order.comment else "")


def order_card(
    order: Order,
    tz: ZoneInfo,
    *,
    with_access: bool = True,
    executor: User | None = None,
    client: User | None = None,
    title: str | None = None,
    numbered: bool = True,
) -> str:
    """Карточка заявки для исполнителя и администратора."""
    lines = [title or (f"<b>Заявка №{order.id}</b>" if numbered else "<b>Заявка на вынос</b>")]
    lines.append(f"Статус: <b>{order_status(order)}</b>")
    lines.append(f"Вынос: {fmt_date(order.shift_date)}")
    lines.append(f"Создана: {fmt_dt(order.created_at, tz)}")
    lines.append(f"Адрес: <b>{h(order.address_title)}</b>, секция {order.section}")
    lines.append(f"Этаж: {order.floor}, квартира: {h(order.apartment)}")
    if with_access:
        lines.append(f"Код домофона: <code>{h(order.intercom_code)}</code>")
    if order.instructions:
        lines.append(f"Инструкции: {h(order.instructions)}")
    if client:
        lines.append(f"Клиент: {user_line(client)}")
    if executor:
        lines.append(f"Исполнитель: {user_line(executor)}")
    if order.started_at:
        lines.append(f"Начато: {fmt_dt(order.started_at, tz)}")
    if order.finished_at:
        lines.append(f"Завершено: {fmt_dt(order.finished_at, tz)}")
    reason = fail_text(order)
    if reason:
        lines.append(f"Причина: {h(reason)}")
    if order.cancel_reason:
        lines.append(f"Причина отмены: {h(order.cancel_reason)}")
    return "\n".join(lines)


def client_order_card(order: Order, cfg: ScheduleConfig, now: datetime, title: str | None = None) -> str:
    """Карточка заявки для клиента: только статус и когда заберут / забрали."""
    lines = [title or "<b>🗑 Вынос мусора</b>", f"Статус: <b>{client_status(order)}</b>"]
    if order.status in ORDER_ACTIVE_STATUSES:
        lines.append(f"Заберём: {pickup_when_text(order.shift_date, now, cfg)}")
    elif order.status == OrderStatus.DONE and order.finished_at:
        lines.append(f"Забрали: {order.finished_at.astimezone(cfg.tz):%d.%m.%Y в %H:%M}")
    reason = fail_text(order)
    if reason:
        lines.append(f"Причина: {h(reason)}")
    if order.cancel_reason:
        lines.append(f"Причина отмены: {h(order.cancel_reason)}")
    return "\n".join(lines)


def payment_card(payment: Payment, tz: ZoneInfo, client: User | None = None) -> str:
    lines = [
        f"<b>Платёж №{payment.id}</b>",
        f"Статус: <b>{payment_status(payment)}</b>",
        f"Тариф: {h(payment.tariff_title)} ({payment.duration_days} дн.)",
        f"Сумма: <b>{payment.amount_rub} ₽</b>",
        f"Создан: {fmt_dt(payment.created_at, tz)}",
    ]
    if client:
        lines.append(f"Клиент: {user_line(client)}")
    if payment.paid_marked_at:
        lines.append(f"Клиент сообщил об оплате: {fmt_dt(payment.paid_marked_at, tz)}")
    if payment.proof_file_id:
        proof = "фото" if payment.proof_type == "photo" else "файл"
        lines.append(f"Подтверждение: {proof}" + (f" — {h(payment.proof_caption)}" if payment.proof_caption else ""))
    if payment.confirmed_at:
        lines.append(f"Подтверждён: {fmt_dt(payment.confirmed_at, tz)} (админ {payment.confirmed_by_tg})")
    if payment.rejected_at:
        lines.append(f"Отклонён: {fmt_dt(payment.rejected_at, tz)}" + (
            f" — {h(payment.reject_reason)}" if payment.reject_reason else ""))
    if payment.refunded_at:
        lines.append(f"Возврат: {fmt_dt(payment.refunded_at, tz)} — {h(payment.refund_reason)}")
    return "\n".join(lines)


def payment_short(payment: Payment, tz: ZoneInfo, client: User | None = None) -> str:
    who = f" · {client.display}" if client else ""
    return f"№{payment.id} · {payment.amount_rub} ₽{who} · {payment.created_at.astimezone(tz):%d.%m %H:%M}"


def subscription_first_day(sub: Subscription, tz: ZoneInfo) -> str:
    return fmt_day(sub.starts_at, tz)


def subscription_last_day(sub: Subscription, tz: ZoneInfo) -> str:
    """Последний день действия (включительно): подписка заканчивается в 00:00 следующего дня."""
    return fmt_day(sub.ends_at - timedelta(seconds=1), tz)


def subscription_period(sub: Subscription, tz: ZoneInfo) -> str:
    return f"{subscription_first_day(sub, tz)} — {subscription_last_day(sub, tz)}"


def subscription_line(sub: Subscription, tz: ZoneInfo, now: datetime) -> str:
    state = "действует" if sub.is_active_at(now) else ("начнётся" if sub.starts_at > now else "истекла")
    return f"• {h(sub.tariff_title)}: {subscription_period(sub, tz)} ({state})"


def requisites_text(details: dict[str, str]) -> str:
    return (
        f"Телефон получателя: <code>{h(details['payment.sbp_phone'])}</code>\n"
        f"Банк получателя: {h(details['payment.sbp_bank'])}\n"
        f"Получатель: {h(details['payment.sbp_recipient'])}"
    )


def payment_instructions(payment: Payment, details: dict[str, str]) -> str:
    return (
        f"<b>Оплата подписки «{h(payment.tariff_title)}»</b>\n"
        f"Сумма к переводу: <b>{payment.amount_rub} ₽</b>\n"
        f"Срок действия: {payment.duration_days} дн., начиная с дня подтверждения оплаты\n\n"
        f"{requisites_text(details)}\n\n"
        f"{h(details['payment.instructions'])}\n\n"
        "В комментарии к переводу ничего указывать не нужно.\n\n"
        "После перевода нажмите «✅ Я оплатил» и пришлите чек или скриншот."
    )


def schedule_text(cfg: ScheduleConfig) -> str:
    return (
        f"Вынос мусора: {pickup_days_text(cfg)} {pickup_time_text(cfg)}\n"
        "Заказать вынос клиент может в любое время.\n"
        f"Заявки, оформленные после {cfg.order_cutoff:%H:%M}, попадают в следующий вынос.\n"
        f"Часовой пояс: {cfg.tz.key}"
    )


def history_text(entries: list[StatusHistoryEntry], titles: dict, tz: ZoneInfo) -> str:
    if not entries:
        return "История пуста."
    lines = []
    for e in entries:
        old = titles.get(e.old_status, e.old_status) if e.old_status else "—"
        new = titles.get(e.new_status, e.new_status)
        change = new if old == new else f"{old} → {new}"
        line = f"{fmt_dt(e.created_at, tz)}: {change}"
        if e.actor_tg:
            line += f" (ID {e.actor_tg})"
        if e.comment:
            line += f"\n    {h(e.comment)}"
        lines.append(line)
    return "\n".join(lines)


def user_card(user: User, tz: ZoneInfo, is_admin: bool) -> str:
    role = "Администратор" if is_admin else ROLE_TITLES[Role(user.role)]
    return (
        f"<b>Пользователь {h(user.display)}</b>\n"
        f"Telegram ID: <code>{user.tg_id}</code>\n"
        f"Username: {('@' + h(user.username)) if user.username else '—'}\n"
        f"Роль: {role}\n"
        f"Статус: {USER_STATUS_TITLES[UserStatus(user.status)]}\n"
        f"Зарегистрирован: {fmt_dt(user.registered_at, tz)}"
    )
