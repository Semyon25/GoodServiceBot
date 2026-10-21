"""Оплата через СБП с ручным подтверждением администратором и подписки."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from bot.db.connection import IntegrityError
from bot.db.repositories import Repositories
from bot.domain.enums import PAYMENT_STATUS_TITLES, PaymentStatus
from bot.domain.errors import (
    AccessDenied,
    InternalStateError,
    InvalidTransition,
    NotFound,
    PaymentPending,
    ValidationError,
)
from bot.domain.models import Actor, Payment, StatusHistoryEntry, Subscription, User
from bot.domain.transitions import payment_transition_allowed

from .access import require_active, require_admin
from .tariffs import CatalogService

REMINDER_BEFORE = timedelta(days=1)


class PaymentService:
    def __init__(self, repos: Repositories, catalog: CatalogService, clock: Callable[[], datetime]):
        self.repos = repos
        self.catalog = catalog
        self._now = clock

    async def get(self, actor: Actor, payment_id: int) -> Payment:
        payment = await self.repos.payments.get(payment_id)
        if not payment or (payment.user_id != actor.user.id and not actor.is_admin):
            raise NotFound("Платёж не найден.")
        return payment

    async def open_payment(self, user_id: int, service_id: int) -> Payment | None:
        return await self.repos.payments.get_open_for_user(user_id, service_id)

    async def select_tariff(self, actor: Actor, tariff_id: int) -> Payment:
        """Создаёт платёж «Ожидает оплаты» (или меняет тариф у ещё не оплаченного)."""
        require_active(actor)
        tariff = await self.catalog.tariff(tariff_id)
        service = await self.catalog.service_by_id(tariff.service_id)
        if not tariff.is_active or not service.is_active:
            raise ValidationError("Этот тариф сейчас недоступен для покупки.")
        now = self._now()
        async with self.repos.transaction():
            open_payment = await self.repos.payments.get_open_for_user(actor.user.id, service.id)
            if open_payment and open_payment.status == PaymentStatus.AWAITING_CONFIRMATION:
                raise PaymentPending(
                    open_payment,
                    f"Ваш платёж №{open_payment.id} на {open_payment.amount_rub} ₽ уже на проверке у администратора. "
                    "Дождитесь подтверждения — повторно оплачивать не нужно.",
                )
            if open_payment:
                if (open_payment.tariff_id, open_payment.amount_rub, open_payment.duration_days) != (
                    tariff.id,
                    tariff.price_rub,
                    tariff.duration_days,
                ):
                    await self.repos.payments.change_tariff(open_payment.id, tariff, now)
                    await self.repos.payments.add_history(
                        open_payment.id,
                        open_payment.status,
                        open_payment.status,
                        actor.tg_id,
                        now,
                        f"Выбран тариф «{tariff.title}», {tariff.price_rub} ₽",
                    )
                payment_id = open_payment.id
            else:
                payment_id = await self.repos.payments.create(actor.user.id, tariff, now)
                await self.repos.payments.add_history(
                    payment_id, None, PaymentStatus.AWAITING_PAYMENT, actor.tg_id, now, f"Тариф «{tariff.title}»"
                )
        payment = await self.repos.payments.get(payment_id)
        assert payment is not None
        return payment

    async def submit_proof(
        self, actor: Actor, payment_id: int, proof_type: str, file_id: str, caption: str | None
    ) -> tuple[Payment, bool]:
        """Клиент сообщает об оплате и прикладывает чек. Подписку это НЕ активирует.

        Возвращает (платёж, изменился ли статус) — повторная отправка ничего не ломает.
        """
        require_active(actor)
        payment = await self.repos.payments.get(payment_id)
        if not payment or payment.user_id != actor.user.id:
            raise NotFound("Платёж не найден.")
        if payment.status == PaymentStatus.AWAITING_CONFIRMATION:
            return payment, False
        if payment.status != PaymentStatus.AWAITING_PAYMENT:
            raise InvalidTransition(
                f"Платёж №{payment.id} уже обработан (статус: {PAYMENT_STATUS_TITLES[payment.status_enum]})."
            )
        now = self._now()
        async with self.repos.transaction():
            changed = await self.repos.payments.transition(
                payment.id,
                [PaymentStatus.AWAITING_PAYMENT],
                PaymentStatus.AWAITING_CONFIRMATION,
                now,
                paid_marked_at=now,
                proof_type=proof_type,
                proof_file_id=file_id,
                proof_caption=caption,
            )
            if changed:
                await self.repos.payments.add_history(
                    payment.id,
                    PaymentStatus.AWAITING_PAYMENT,
                    PaymentStatus.AWAITING_CONFIRMATION,
                    actor.tg_id,
                    now,
                    "Клиент сообщил об оплате и приложил подтверждение",
                )
        payment = await self.repos.payments.get(payment_id)
        assert payment is not None
        return payment, changed

    async def confirm(self, actor: Actor, payment_id: int) -> tuple[Payment, Subscription]:
        """Администратор подтверждает фактическое поступление денег — активируется подписка."""
        require_admin(actor)
        now = self._now()
        async with self.repos.transaction():
            payment = await self.repos.payments.get(payment_id)
            if not payment:
                raise NotFound("Платёж не найден.")
            if payment.status == PaymentStatus.CONFIRMED:
                raise InvalidTransition(f"Платёж №{payment.id} уже подтверждён, подписка уже создана.")
            if not payment_transition_allowed(payment.status, PaymentStatus.CONFIRMED):
                raise InvalidTransition(
                    f"Платёж в статусе «{PAYMENT_STATUS_TITLES[payment.status_enum]}» нельзя подтвердить."
                )
            changed = await self.repos.payments.transition(
                payment.id, [payment.status], PaymentStatus.CONFIRMED, now, confirmed_at=now, confirmed_by_tg=actor.tg_id
            )
            if not changed:
                raise InvalidTransition("Статус платежа уже изменился. Обновите карточку платежа.")
            if await self.repos.subscriptions.get_by_payment(payment.id):
                raise InternalStateError(f"Для платежа №{payment.id} уже существует подписка")

            latest_end = await self.repos.subscriptions.latest_end(payment.user_id, payment.service_id)
            starts_at = max(now, latest_end) if latest_end else now
            ends_at = starts_at + timedelta(days=payment.duration_days)
            try:
                subscription_id = await self.repos.subscriptions.create(
                    user_id=payment.user_id,
                    service_id=payment.service_id,
                    tariff_id=payment.tariff_id,
                    payment_id=payment.id,
                    tariff_title=payment.tariff_title,
                    amount_rub=payment.amount_rub,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    now=now,
                )
            except IntegrityError as exc:
                raise InternalStateError(f"Не удалось создать подписку по платежу №{payment.id}: {exc}") from exc
            await self.repos.payments.add_history(
                payment.id, payment.status, PaymentStatus.CONFIRMED, actor.tg_id, now, "Поступление денег подтверждено"
            )
            await self.repos.audit.log(actor.tg_id, "payment.confirm", f"payment_id={payment.id}", now)

        payment = await self.repos.payments.get(payment_id)
        subscription = await self.repos.subscriptions.get(subscription_id)
        assert payment is not None and subscription is not None
        return payment, subscription

    async def reject(self, actor: Actor, payment_id: int, reason: str | None) -> Payment:
        require_admin(actor)
        reason = (reason or "").strip() or None
        return await self._admin_transition(
            actor,
            payment_id,
            PaymentStatus.REJECTED,
            reason or "Поступление не подтверждено",
            reject_reason=reason,
            rejected_at=self._now(),
            rejected_by_tg=actor.tg_id,
        )

    async def refund(self, actor: Actor, payment_id: int, reason: str) -> Payment:
        """Фиксирует возврат денег: платёж «Возвращён», связанная подписка аннулируется."""
        require_admin(actor)
        reason = (reason or "").strip()
        if not reason:
            raise ValidationError("Укажите причину возврата.")
        return await self._admin_transition(
            actor,
            payment_id,
            PaymentStatus.REFUNDED,
            reason,
            refund_reason=reason,
            refunded_at=self._now(),
            refunded_by_tg=actor.tg_id,
        )

    async def _admin_transition(
        self, actor: Actor, payment_id: int, new_status: PaymentStatus, comment: str, **fields
    ) -> Payment:
        now = self._now()
        async with self.repos.transaction():
            payment = await self.repos.payments.get(payment_id)
            if not payment:
                raise NotFound("Платёж не найден.")
            if not payment_transition_allowed(payment.status, new_status):
                raise InvalidTransition(
                    f"Нельзя перевести платёж из статуса «{PAYMENT_STATUS_TITLES[payment.status_enum]}» "
                    f"в «{PAYMENT_STATUS_TITLES[new_status]}»."
                )
            if not await self.repos.payments.transition(payment.id, [payment.status], new_status, now, **fields):
                raise InvalidTransition("Статус платежа уже изменился. Обновите карточку платежа.")
            if new_status == PaymentStatus.REFUNDED:
                await self.repos.subscriptions.cancel_by_payment(payment.id)
            await self.repos.payments.add_history(payment.id, payment.status, new_status, actor.tg_id, now, comment)
            await self.repos.audit.log(actor.tg_id, f"payment.{new_status.value}", f"payment_id={payment.id}: {comment}", now)
        result = await self.repos.payments.get(payment_id)
        assert result is not None
        return result

    async def list(self, actor: Actor, status: str | None = None, user_id: int | None = None, limit: int = 20,
                   offset: int = 0) -> list[Payment]:
        if user_id != actor.user.id:
            require_admin(actor)
        return await self.repos.payments.list(status=status, user_id=user_id, limit=limit, offset=offset)

    async def history(self, actor: Actor, payment_id: int) -> list[StatusHistoryEntry]:
        require_admin(actor)
        return await self.repos.payments.history(payment_id)


@dataclass
class SubscriptionEvent:
    kind: str  # "reminder" | "expired"
    user: User
    subscription: Subscription


class SubscriptionService:
    def __init__(self, repos: Repositories, clock: Callable[[], datetime]):
        self.repos = repos
        self._now = clock

    async def active(self, user_id: int, service_id: int) -> Subscription | None:
        return await self.repos.subscriptions.get_active_at(user_id, service_id, self._now())

    async def current_and_future(self, actor: Actor, user_id: int) -> list[Subscription]:
        if user_id != actor.user.id:
            require_admin(actor)
        return await self.repos.subscriptions.list_current_and_future(user_id, self._now())

    async def history(self, actor: Actor, user_id: int) -> list[Subscription]:
        if user_id != actor.user.id and not actor.is_admin:
            raise AccessDenied()
        return await self.repos.subscriptions.list_for_user(user_id)

    async def process_due(self) -> list[SubscriptionEvent]:
        """Напоминания за сутки до окончания и уведомления об окончании подписки.

        Если следующий период уже оплачен, клиент не беспокоится.
        """
        now = self._now()
        events: list[SubscriptionEvent] = []
        for sub in await self.repos.subscriptions.due_for_reminder(now, now + REMINDER_BEFORE):
            if not await self.repos.subscriptions.mark_reminder_sent(sub.id):
                continue
            if await self.repos.subscriptions.has_successor(sub):
                continue
            user = await self.repos.users.get(sub.user_id)
            if user:
                events.append(SubscriptionEvent("reminder", user, sub))
        for sub in await self.repos.subscriptions.due_for_expiry(now):
            has_successor = await self.repos.subscriptions.has_successor(sub)
            if not await self.repos.subscriptions.mark_expired(sub.id, notified=not has_successor):
                continue
            if has_successor:
                continue
            user = await self.repos.users.get(sub.user_id)
            if user:
                events.append(SubscriptionEvent("expired", user, sub))
        return events
