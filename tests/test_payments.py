from datetime import timedelta

import pytest

from bot.db.connection import IntegrityError
from bot.domain.enums import PaymentStatus, SubscriptionStatus
from bot.domain.errors import AccessDenied, InvalidTransition, NotFound, PaymentPending, ValidationError


async def _tariff(env, title):
    return next(t for t in await env.services.catalog.tariffs() if t.title == title)


async def test_default_tariffs(env):
    tariffs = {t.title: (t.price_rub, t.duration_days) for t in await env.services.catalog.tariffs()}
    assert tariffs == {"Неделя": (200, 7), "Месяц": (600, 30)}


async def test_paid_button_does_not_activate_subscription(env):
    client = await env.client_with_address()
    service = await env.service()
    payment = await env.services.payments.select_tariff(client, (await _tariff(env, "Неделя")).id)
    assert payment.status == PaymentStatus.AWAITING_PAYMENT
    assert payment.amount_rub == 200

    payment, changed = await env.services.payments.submit_proof(client, payment.id, "photo", "f1", "чек")
    assert changed and payment.status == PaymentStatus.AWAITING_CONFIRMATION
    assert await env.services.subscriptions.active(client.user.id, service.id) is None

    # повторная отправка чека ничего не ломает
    _, changed_again = await env.services.payments.submit_proof(client, payment.id, "photo", "f2", None)
    assert not changed_again


async def test_admin_confirmation_activates_subscription(env):
    client = await env.client_with_address()
    service = await env.service()
    payment, sub = await env.subscribe(client, "Месяц")
    assert payment.status == PaymentStatus.CONFIRMED
    assert payment.confirmed_by_tg == (await env.admin()).tg_id
    assert payment.confirmed_at == env.clock()
    assert sub.starts_at == env.clock()
    assert sub.ends_at == env.clock() + timedelta(days=30)
    assert sub.amount_rub == payment.amount_rub == 600
    assert (await env.services.subscriptions.active(client.user.id, service.id)).id == sub.id


async def test_double_confirmation_does_not_create_second_subscription(env):
    client = await env.client_with_address()
    payment, _ = await env.subscribe(client)
    with pytest.raises(InvalidTransition):
        await env.services.payments.confirm(await env.admin(), payment.id)
    count = await env.db.fetchval("SELECT COUNT(*) FROM subscriptions WHERE payment_id = ?", (payment.id,))
    assert count == 1


async def test_db_rejects_second_subscription_for_payment(env):
    client = await env.client_with_address()
    payment, sub = await env.subscribe(client)
    with pytest.raises(IntegrityError):
        await env.repos.subscriptions.create(
            client.user.id, sub.service_id, sub.tariff_id, payment.id, "x", payment.amount_rub,
            sub.starts_at, sub.ends_at, env.clock(),
        )


async def test_db_rejects_subscription_amount_mismatch_and_unconfirmed(env):
    client = await env.client_with_address()
    tariff = await _tariff(env, "Неделя")
    payment = await env.services.payments.select_tariff(client, tariff.id)
    with pytest.raises(Exception, match="payment_not_confirmed"):
        await env.repos.subscriptions.create(
            client.user.id, tariff.service_id, tariff.id, payment.id, "x", 200, env.clock(),
            env.clock() + timedelta(days=7), env.clock(),
        )
    await env.services.payments.confirm(await env.admin(), payment.id)
    await env.db.execute("DELETE FROM subscriptions WHERE payment_id = ?", (payment.id,))
    with pytest.raises(Exception, match="subscription_amount_mismatch"):
        await env.repos.subscriptions.create(
            client.user.id, tariff.service_id, tariff.id, payment.id, "x", 999, env.clock(),
            env.clock() + timedelta(days=7), env.clock(),
        )


async def test_new_subscription_starts_after_current(env):
    client = await env.client_with_address()
    _, first = await env.subscribe(client, "Неделя")
    env.clock.set(2026, 10, 23, 12, 0)
    _, second = await env.subscribe(client, "Месяц")
    assert second.starts_at == first.ends_at
    assert second.ends_at == first.ends_at + timedelta(days=30)


async def test_tariff_change_does_not_affect_paid_subscription(env):
    client = await env.client_with_address()
    admin = await env.admin()
    tariff = await _tariff(env, "Неделя")
    _, sub = await env.subscribe(client, "Неделя")
    await env.services.catalog.update_tariff(admin, tariff.id, price_rub=350, duration_days=10)
    reloaded = await env.repos.subscriptions.get(sub.id)
    assert (reloaded.amount_rub, reloaded.ends_at) == (200, sub.ends_at)


async def test_tariff_change_after_payment_created_keeps_payment_amount(env):
    client = await env.client_with_address()
    admin = await env.admin()
    tariff = await _tariff(env, "Неделя")
    payment = await env.services.payments.select_tariff(client, tariff.id)
    await env.services.catalog.update_tariff(admin, tariff.id, price_rub=350)
    _, sub = await env.services.payments.confirm(admin, payment.id)
    assert sub.amount_rub == 200


async def test_reject_payment(env):
    client = await env.client_with_address()
    admin = await env.admin()
    payment = await env.services.payments.select_tariff(client, (await _tariff(env, "Неделя")).id)
    await env.services.payments.submit_proof(client, payment.id, "photo", "f", None)
    rejected = await env.services.payments.reject(admin, payment.id, "Деньги не поступили")
    assert rejected.status == PaymentStatus.REJECTED
    assert rejected.reject_reason == "Деньги не поступили"
    assert await env.services.subscriptions.active(client.user.id, payment.service_id) is None
    # после отклонения клиент может пройти оплату заново
    again = await env.services.payments.select_tariff(client, payment.tariff_id)
    assert again.id != payment.id


async def test_refund_cancels_subscription_and_keeps_history(env):
    client = await env.client_with_address()
    admin = await env.admin()
    payment, sub = await env.subscribe(client)
    with pytest.raises(ValidationError):
        await env.services.payments.refund(admin, payment.id, "")
    refunded = await env.services.payments.refund(admin, payment.id, "Клиент передумал")
    assert refunded.status == PaymentStatus.REFUNDED
    assert (await env.repos.subscriptions.get(sub.id)).status == SubscriptionStatus.CANCELLED
    assert await env.services.subscriptions.active(client.user.id, payment.service_id) is None
    history = [(h.old_status, h.new_status) for h in await env.services.payments.history(admin, payment.id)]
    assert history == [
        (None, "awaiting_payment"),
        ("awaiting_payment", "awaiting_confirmation"),
        ("awaiting_confirmation", "confirmed"),
        ("confirmed", "refunded"),
    ]


async def test_invalid_payment_transitions(env):
    client = await env.client_with_address()
    admin = await env.admin()
    payment, _ = await env.subscribe(client)
    with pytest.raises(InvalidTransition):
        await env.services.payments.reject(admin, payment.id, "x")
    # база данных тоже не позволит запрещённый переход
    with pytest.raises(Exception, match="invalid_payment_transition"):
        await env.db.execute("UPDATE payments SET status = 'awaiting_payment' WHERE id = ?", (payment.id,))


async def test_pending_confirmation_blocks_new_payment(env):
    client = await env.client_with_address()
    tariff = await _tariff(env, "Неделя")
    payment = await env.services.payments.select_tariff(client, tariff.id)
    # пока не оплачен — выбор другого тарифа меняет тот же платёж
    month = await _tariff(env, "Месяц")
    same = await env.services.payments.select_tariff(client, month.id)
    assert same.id == payment.id and same.amount_rub == 600
    await env.services.payments.submit_proof(client, payment.id, "photo", "f", None)
    with pytest.raises(PaymentPending):
        await env.services.payments.select_tariff(client, tariff.id)


async def test_only_admin_can_confirm_and_clients_see_only_own(env):
    client = await env.client_with_address(10)
    other = await env.client_with_address(11)
    payment = await env.services.payments.select_tariff(client, (await _tariff(env, "Неделя")).id)
    with pytest.raises(AccessDenied):
        await env.services.payments.confirm(client, payment.id)
    with pytest.raises(NotFound):
        await env.services.payments.get(other, payment.id)
    with pytest.raises(NotFound):
        await env.services.payments.submit_proof(other, payment.id, "photo", "f", None)
    with pytest.raises(AccessDenied):
        await env.services.payments.list(other, user_id=client.user.id)


async def test_subscription_reminder_and_expiry_notifications(env):
    client = await env.client_with_address()
    await env.subscribe(client, "Неделя")  # 21.10 20:00 → 28.10 20:00
    env.clock.set(2026, 10, 27, 19, 0)
    assert await env.services.subscriptions.process_due() == []
    env.clock.set(2026, 10, 27, 20, 30)
    events = await env.services.subscriptions.process_due()
    assert [e.kind for e in events] == ["reminder"]
    assert await env.services.subscriptions.process_due() == []  # не дублируется
    env.clock.set(2026, 10, 28, 20, 1)
    events = await env.services.subscriptions.process_due()
    assert [e.kind for e in events] == ["expired"]
    assert await env.services.subscriptions.process_due() == []


async def test_no_reminder_when_next_period_paid(env):
    client = await env.client_with_address()
    await env.subscribe(client, "Неделя")
    env.clock.set(2026, 10, 25, 12, 0)
    await env.subscribe(client, "Месяц")
    env.clock.set(2026, 10, 27, 21, 0)
    assert await env.services.subscriptions.process_due() == []
    env.clock.set(2026, 10, 28, 21, 0)
    assert await env.services.subscriptions.process_due() == []
    assert await env.services.subscriptions.active(client.user.id, (await env.service()).id) is not None
