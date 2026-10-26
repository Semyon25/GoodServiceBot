"""Сквозные сценарии через диспетчер aiogram с подменённой сессией Telegram (без сети)."""
from __future__ import annotations

import itertools
import time
from datetime import datetime
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    EditMessageCaption,
    EditMessageReplyMarkup,
    EditMessageText,
    SendDocument,
    SendMessage,
    SendPhoto,
    TelegramMethod,
)
from aiogram.types import Chat, Message

from bot.domain.enums import OrderStatus, PaymentStatus, Role
from bot.telegram import texts as t
from bot.telegram.callbacks import (
    AddrCb,
    AExportCb,
    AOrderCb,
    APayCb,
    AStatsCb,
    AUserCb,
    ExecCb,
    ExecFailCb,
    OrderCb,
    PayCb,
    SupportCb,
    TariffCb,
)
from bot.telegram.handlers import build_router
from bot.telegram.middlewares import ActorMiddleware, PrivateChatMiddleware
from bot.telegram.notifier import Notifier

from .conftest import ADMIN_TG

CLIENT = 10
EXECUTOR = 500
BOT_ID = 42

_ROOT = build_router()


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod] = []
        self._ids = itertools.count(1000)

    async def close(self) -> None:
        pass

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None) -> Any:
        self.requests.append(method)
        if isinstance(method, (SendMessage, SendPhoto, SendDocument, EditMessageText, EditMessageReplyMarkup,
                               EditMessageCaption)):
            chat_id = getattr(method, "chat_id", None) or 0
            return Message(
                message_id=next(self._ids),
                date=datetime.now(),
                chat=Chat(id=int(chat_id), type="private"),
                text=getattr(method, "text", None),
            )
        return True


class Harness:
    def __init__(self, env) -> None:
        self.env = env
        self.session = FakeSession()
        self.bot = Bot(f"{BOT_ID}:TEST", session=self.session, default=DefaultBotProperties(parse_mode="HTML"))
        services = env.services
        self.notifier = Notifier(self.bot, services, frozenset({ADMIN_TG}))
        self.dp = Dispatcher(storage=MemoryStorage(), services=services, notifier=self.notifier)
        for observer in (self.dp.message, self.dp.callback_query):
            observer.outer_middleware(PrivateChatMiddleware())
            observer.outer_middleware(ActorMiddleware(services))
        _ROOT._parent_router = None
        self.dp.include_router(_ROOT)
        self._update_ids = itertools.count(1)

    @staticmethod
    def _user(tg_id: int) -> dict:
        return {"id": tg_id, "is_bot": False, "first_name": "U", "username": f"user{tg_id}"}

    def _message(self, tg_id: int, **content: Any) -> dict:
        return {
            "message_id": next(self._update_ids),
            "date": int(time.time()),
            "chat": {"id": tg_id, "type": "private"},
            "from": self._user(tg_id),
            **content,
        }

    async def _feed(self, update: dict) -> list[TelegramMethod]:
        start = len(self.session.requests)
        await self.dp.feed_raw_update(self.bot, {"update_id": next(self._update_ids), **update})
        return self.session.requests[start:]

    async def text(self, tg_id: int, text: str) -> list[TelegramMethod]:
        return await self._feed({"message": self._message(tg_id, text=text)})

    async def photo(self, tg_id: int) -> list[TelegramMethod]:
        photo = [{"file_id": "photo-file", "file_unique_id": "u1", "width": 10, "height": 10}]
        return await self._feed({"message": self._message(tg_id, photo=photo, caption="чек")})

    async def click(self, tg_id: int, data: str, text: str = "старое сообщение") -> list[TelegramMethod]:
        message = {
            "message_id": next(self._update_ids),
            "date": int(time.time()),
            "chat": {"id": tg_id, "type": "private"},
            "from": {"id": BOT_ID, "is_bot": True, "first_name": "Bot"},
            "text": text,
        }
        return await self._feed(
            {"callback_query": {"id": str(next(self._update_ids)), "from": self._user(tg_id),
                                "chat_instance": "ci", "data": data, "message": message}}
        )

    def all_text(self, methods: list[TelegramMethod], chat_id: int | None = None) -> str:
        parts = []
        for m in methods:
            if chat_id is not None and getattr(m, "chat_id", chat_id) != chat_id:
                continue
            for attr in ("text", "caption"):
                value = getattr(m, attr, None)
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(parts)

    def assert_no_errors(self) -> None:
        texts = self.all_text(self.session.requests)
        assert "Ошибка обработки" not in texts, texts
        assert t.TEMP_ERROR not in texts, texts
        assert not self.notifier._error_sent_at


@pytest.fixture
async def h(env):
    harness = Harness(env)
    yield harness
    harness.assert_no_errors()
    _ROOT._parent_router = None


async def _register_address(h: Harness, tg_id: int = CLIENT) -> None:
    await h.text(tg_id, t.BTN_ADDRESS)
    address = (await h.env.services.addresses.list_addresses())[0]
    await h.click(tg_id, AddrCb(action="pick", value=address.id).pack())
    await h.click(tg_id, AddrCb(action="section", value=3).pack())
    await h.text(tg_id, "5")
    sent = await h.text(tg_id, "45")
    assert "код домофона:" in h.all_text(sent) and "как его набирать" not in h.all_text(sent)
    await h.text(tg_id, "45K1234")
    await h.click(tg_id, AddrCb(action="skip").pack())
    sent = await h.click(tg_id, AddrCb(action="save").pack())
    assert "Адрес сохранён" in h.all_text(sent)


async def _pay_and_confirm(h: Harness, tg_id: int = CLIENT) -> None:
    week = next(tr for tr in await h.env.services.catalog.tariffs() if tr.title == "Неделя")
    sent = await h.click(tg_id, TariffCb(action="pick", tariff_id=week.id).pack())
    text = h.all_text(sent)
    assert "299 ₽" in text and "+7 900 000-00-00" in text
    assert "ничего указывать не нужно" in text and "№" not in text
    payment = await h.env.repos.payments.list(limit=1)
    payment_id = payment[0].id
    await h.click(tg_id, PayCb(action="paid", payment_id=payment_id).pack())
    sent = await h.photo(tg_id)
    assert "№" not in h.all_text(sent, tg_id)
    assert any(isinstance(m, SendPhoto) and m.chat_id == ADMIN_TG for m in sent)
    assert (await h.env.repos.payments.get(payment_id)).status == PaymentStatus.AWAITING_CONFIRMATION
    service = await h.env.service()
    assert await h.env.services.subscriptions.active((await h.env.actor(tg_id)).user.id, service.id) is None

    await h.click(ADMIN_TG, APayCb(action="confirm_ask", payment_id=payment_id).pack())
    sent = await h.click(ADMIN_TG, APayCb(action="confirm", payment_id=payment_id).pack())
    client_text = h.all_text(sent, tg_id)
    assert "Оплата подтверждена" in client_text
    # даты без времени: неделя с 21.10 по 27.10 включительно
    assert "Начало: 21.10.2026" in client_text and "Окончание: 27.10.2026" in client_text
    assert "20:00" not in client_text
    # повторное нажатие не создаёт вторую подписку
    await h.click(ADMIN_TG, APayCb(action="confirm", payment_id=payment_id).pack())
    assert await h.env.db.fetchval("SELECT COUNT(*) FROM subscriptions") == 1


async def test_start_without_name(h):
    sent = await h.text(CLIENT, "/start")
    text = h.all_text(sent)
    assert "Здравствуйте" not in text
    assert "ежедневно с 19:00 до 02:00" in text
    assert "укажите адрес" in text


async def test_menu_button_interrupts_form(h):
    await h.text(CLIENT, t.BTN_ADDRESS)
    address = (await h.env.services.addresses.list_addresses())[2]
    sent = await h.click(CLIENT, AddrCb(action="pick", value=address.id).pack())
    # для «Луговая, 3» — 11 секций
    markup = sent[-1].reply_markup
    section_buttons = [b for row in markup.inline_keyboard for b in row if b.text.isdigit()]
    assert len(section_buttons) == 11
    await h.click(CLIENT, AddrCb(action="section", value=1).pack())
    sent = await h.text(CLIENT, t.BTN_TARIFFS)
    assert "Тарифы на вынос мусора" in h.all_text(sent)
    assert "999 ₽" in h.all_text(sent)


async def test_full_client_executor_admin_flow(h):
    env = h.env
    await h.text(CLIENT, "/start")
    await h.text(EXECUTOR, "/start")
    await h.text(ADMIN_TG, "/start")
    await _register_address(h)

    # без подписки заказать нельзя
    sent = await h.text(CLIENT, t.BTN_ORDER)
    assert "нет действующей подписки" in h.all_text(sent)

    await _pay_and_confirm(h)

    sent = await h.text(CLIENT, t.BTN_SUBSCRIPTION)
    assert "Подписка активна" in h.all_text(sent) and "27.10.2026" in h.all_text(sent)

    sent = await h.text(CLIENT, t.BTN_ORDER)
    assert "Выставьте пакет" in h.all_text(sent) and "сегодня с 19:00 до 02:00" in h.all_text(sent)
    sent = await h.click(CLIENT, OrderCb(action="placed").pack())
    assert "Пакет уже стоит у двери. Создать заявку на вынос?" in h.all_text(sent)
    sent = await h.click(CLIENT, OrderCb(action="confirm").pack())
    # клиенту — ровно одно сообщение: статус и когда заберут, без номера, смены, адреса
    client_msgs = [m for m in sent if isinstance(m, (SendMessage, EditMessageText)) and m.chat_id in (CLIENT, None)]
    assert len(client_msgs) == 1, client_msgs
    card = client_msgs[0].text
    assert "Ожидает выноса" in card and "Заберём: сегодня с 19:00 до 02:00" in card
    for word in ("№", "Смена", "смен", "Создана", "Адрес", "Торговая"):
        assert word not in card, word
    # заявка сразу на администраторе, у него — кнопки исполнителя
    admin_msgs = [m for m in sent if isinstance(m, SendMessage) and m.chat_id == ADMIN_TG]
    assert "Новая заявка" in admin_msgs[0].text and "Назначен" in admin_msgs[0].text
    assert "Выполнено" in str(admin_msgs[0].reply_markup)
    order = (await env.repos.orders.list())[0]
    admin_user = await env.repos.users.get_by_tg(ADMIN_TG)
    assert order.executor_id == admin_user.id

    # повторное подтверждение — дубля нет, показывается существующая заявка без слов про смену
    sent = await h.click(CLIENT, OrderCb(action="confirm").pack())
    assert "уже заказали" in h.all_text(sent) and "смен" not in h.all_text(sent)
    assert await env.db.fetchval("SELECT COUNT(*) FROM orders") == 1

    # администратор назначает роль исполнителя и передаёт ему заявку
    executor = await env.repos.users.get_by_tg(EXECUTOR)
    sent = await h.click(ADMIN_TG, AUserCb(action="role", user_id=executor.id).pack())
    assert "роль исполнителя" in h.all_text(sent, EXECUTOR)
    await h.click(ADMIN_TG, AOrderCb(action="assign", order_id=order.id).pack())
    sent = await h.click(ADMIN_TG, AOrderCb(action="assign_to", order_id=order.id, arg=executor.id).pack())
    executor_text = h.all_text(sent, EXECUTOR)
    assert "Вам назначена заявка" in executor_text and "45K1234" in executor_text and "№" not in executor_text
    # администратор остаётся в списке исполнителей и может забрать заявку обратно
    sent = await h.click(ADMIN_TG, AOrderCb(action="assign", order_id=order.id).pack())
    assert "👑" in str(sent[-1].reply_markup)

    # исполнитель: статусы «принят»/«в работе» клиенту не сообщаются
    sent = await h.text(EXECUTOR, t.BTN_EX_ASSIGNED)
    assert "Торговая" in str(sent[-1].reply_markup) and "№" not in str(sent[-1].reply_markup)
    sent = await h.click(EXECUTOR, ExecCb(action="accept", order_id=order.id).pack())
    assert not [m for m in sent if isinstance(m, SendMessage) and m.chat_id == CLIENT]
    sent = await h.click(EXECUTOR, ExecCb(action="start", order_id=order.id).pack())
    assert not [m for m in sent if isinstance(m, SendMessage) and m.chat_id == CLIENT]
    sent = await h.click(EXECUTOR, ExecCb(action="done", order_id=order.id).pack())
    assert "Мусор вынесен" in h.all_text(sent, CLIENT)
    assert (await env.repos.orders.get(order.id)).status == OrderStatus.DONE

    # клиент видит свои заказы — без номеров
    sent = await h.text(CLIENT, t.BTN_MY_ORDERS)
    assert "№" not in h.all_text(sent) and "Мусор вынесен" in h.all_text(sent)
    sent = await h.click(CLIENT, OrderCb(action="view", order_id=order.id).pack())
    assert "Забрали: 21.10.2026" in h.all_text(sent)

    # следующий вынос: администратор сам выносит мусор по своей заявке
    env.clock.set(2026, 10, 22, 19, 30)
    await h.text(CLIENT, t.BTN_ORDER)
    await h.click(CLIENT, OrderCb(action="confirm").pack())
    second = (await env.repos.orders.list(newest_first=True))[0]
    assert second.id != order.id and second.executor_id == admin_user.id
    sent = await h.text(ADMIN_TG, t.BTN_A_MY_ORDERS)
    assert "Назначенные" in str(sent[-1].reply_markup)
    sent = await h.click(ADMIN_TG, ExecCb(action="done", order_id=second.id).pack())
    assert "Мусор вынесен" in h.all_text(sent, CLIENT)

    # ещё один вынос: исполнитель не смог выполнить, комментарий обязателен
    env.clock.set(2026, 10, 23, 19, 30)
    await h.text(CLIENT, t.BTN_ORDER)
    await h.click(CLIENT, OrderCb(action="confirm").pack())
    third = (await env.repos.orders.list(newest_first=True))[0]
    await h.click(ADMIN_TG, AOrderCb(action="assign_to", order_id=third.id, arg=executor.id).pack())
    await h.click(EXECUTOR, ExecCb(action="fail", order_id=third.id).pack())
    await h.click(EXECUTOR, ExecFailCb(order_id=third.id, reason="other").pack())
    sent = await h.text(EXECUTOR, "Пакет порван")
    client_text = h.all_text(sent, CLIENT)
    assert "Не удалось вынести" in client_text and "Пакет порван" in client_text
    assert "не удалось выполнить" in h.all_text(sent, ADMIN_TG)


async def test_admin_screens(h):
    env = h.env
    await h.text(ADMIN_TG, "/start")
    for button in (t.BTN_A_MY_ORDERS, t.BTN_A_ORDERS, t.BTN_A_PAYMENTS, t.BTN_A_USERS, t.BTN_A_TARIFFS,
                   t.BTN_A_SCHEDULE, t.BTN_A_ADDRESSES, t.BTN_A_SBP, t.BTN_A_SETTINGS, t.BTN_A_STATS,
                   t.BTN_A_EXPORT):
        sent = await h.text(ADMIN_TG, button)
        assert sent, button
    await h.click(ADMIN_TG, AOrderCb(action="list").pack())
    sent = await h.click(ADMIN_TG, AStatsCb(period="30d").pack())
    assert "Статистика" in h.all_text(sent)
    for kind in ("orders", "payments", "users"):
        sent = await h.click(ADMIN_TG, AExportCb(kind=kind).pack())
        assert any(isinstance(m, SendDocument) for m in sent)

    # смена тарифа через ввод
    tariff = (await env.services.catalog.tariffs())[0]
    from bot.telegram.callbacks import ASchedCb, ATariffCb

    await h.click(ADMIN_TG, ATariffCb(action="price", tariff_id=tariff.id).pack())
    sent = await h.text(ADMIN_TG, "250")
    assert "Сохранено" in h.all_text(sent)
    assert (await env.services.catalog.tariff(tariff.id)).price_rub == 250

    # неверное время — остаёмся в режиме ввода
    await h.click(ADMIN_TG, ASchedCb(action="time", value="order_cutoff").pack())
    sent = await h.text(ADMIN_TG, "99:99")
    assert "ЧЧ:ММ" in h.all_text(sent)
    sent = await h.text(ADMIN_TG, "00:30")
    assert "Сохранено" in h.all_text(sent)

    # новое время и дни выноса сразу видны клиенту
    await h.click(ADMIN_TG, ASchedCb(action="time", value="pickup_start").pack())
    await h.text(ADMIN_TG, "18:00")
    await h.click(ADMIN_TG, ASchedCb(action="day", value="6").pack())
    sent = await h.text(CLIENT, "/start")
    assert "с 18:00 до 02:00" in h.all_text(sent) and "Пн, Вт, Ср, Чт, Пт, Сб" in h.all_text(sent)
    sent = await h.text(CLIENT, t.BTN_HELP)
    assert "с 18:00 до 02:00" in h.all_text(sent)


async def test_non_admin_cannot_use_admin_callbacks(h):
    env = h.env
    await h.text(CLIENT, "/start")
    client = await env.repos.users.get_by_tg(CLIENT)
    await h.click(CLIENT, AUserCb(action="role", user_id=client.id).pack())
    assert (await env.repos.users.get(client.id)).role == Role.CLIENT
    sent = await h.text(CLIENT, t.BTN_A_PAYMENTS)
    assert "Платежи" not in h.all_text(sent)


async def test_blocked_user_and_support(h):
    env = h.env
    await h.text(CLIENT, "/start")
    await h.click(CLIENT, SupportCb(action="write").pack())
    sent = await h.text(CLIENT, "Где мой пакет?")
    assert "Где мой пакет?" in h.all_text(sent, ADMIN_TG)
    await h.click(ADMIN_TG, SupportCb(action="reply", user_tg=CLIENT).pack())
    sent = await h.text(ADMIN_TG, "Уже несём")
    assert "Уже несём" in h.all_text(sent, CLIENT)

    client = await env.repos.users.get_by_tg(CLIENT)
    await h.click(ADMIN_TG, AUserCb(action="block", user_id=client.id).pack())
    sent = await h.text(CLIENT, t.BTN_ORDER)
    assert "ограничен" in h.all_text(sent)
