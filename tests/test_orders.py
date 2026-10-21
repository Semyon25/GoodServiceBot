import asyncio

import pytest

from bot.db.connection import IntegrityError
from bot.domain.enums import DistributionMode, FailReason, OrderStatus, Role, UserStatus
from bot.domain.errors import (
    AccessDenied,
    AlreadyOrdered,
    InvalidTransition,
    NoAddress,
    NoSubscription,
    NotFound,
    OutsideSchedule,
    UserBlocked,
    ValidationError,
)


async def _ready_client(env, tg_id=10):
    client = await env.client_with_address(tg_id)
    await env.subscribe(client)
    return client


async def _set_mode(env, mode: DistributionMode):
    await env.services.settings.set(await env.admin(), "orders.distribution_mode", mode.value)


async def test_order_requires_subscription(env):
    client = await env.client_with_address()
    with pytest.raises(NoSubscription):
        await env.services.orders.create_order(client, await env.service())


async def test_order_requires_address(env):
    client = await env.actor(10)
    with pytest.raises(NoAddress):
        await env.services.orders.create_order(client, await env.service())


async def test_order_created_with_address_snapshot(env):
    client = await _ready_client(env)
    service = await env.service()
    order = await env.services.orders.create_order(client, service)
    assert order.status == OrderStatus.NEW
    assert str(order.shift_date) == "2026-10-21"
    assert (order.address_title, order.section, order.floor, order.apartment, order.intercom_code) == (
        "Торговая, 6", 3, 5, "45", "45K1234",
    )
    # изменение адреса не меняет созданный заказ
    address = (await env.services.addresses.list_addresses())[1]
    await env.services.addresses.save_user_address(client, client.user.id, address.id, 9, 2, "7", "7", None)
    reloaded = await env.repos.orders.get(order.id)
    assert (reloaded.address_title, reloaded.section, reloaded.apartment) == ("Торговая, 6", 3, "45")


@pytest.mark.parametrize(
    "moment",
    [
        (2026, 10, 22, 18, 0),  # будний день до начала приёма
        (2026, 10, 22, 1, 30),  # приём закрыт, идёт выполнение
        (2026, 10, 24, 20, 0),  # суббота
        (2026, 10, 25, 21, 0),  # воскресенье
    ],
)
async def test_order_outside_schedule(env, moment):
    client = await _ready_client(env)
    env.clock.set(*moment)
    with pytest.raises(OutsideSchedule):
        await env.services.orders.create_order(client, await env.service())


async def test_after_midnight_belongs_to_previous_shift(env):
    client = await _ready_client(env)
    env.clock.set(2026, 10, 24, 0, 30)  # суббота 00:30 — пятничная смена
    order = await env.services.orders.create_order(client, await env.service())
    assert str(order.shift_date) == "2026-10-23"


async def test_one_order_per_shift(env):
    client = await _ready_client(env)
    service = await env.service()
    first = await env.services.orders.create_order(client, service)
    env.clock.set(2026, 10, 22, 0, 30)  # та же смена после полуночи
    with pytest.raises(AlreadyOrdered) as exc:
        await env.services.orders.create_order(client, service)
    assert exc.value.order.id == first.id
    env.clock.set(2026, 10, 22, 19, 30)  # следующая смена — можно
    second = await env.services.orders.create_order(client, service)
    assert str(second.shift_date) == "2026-10-22"


async def test_concurrent_clicks_create_single_order(env):
    client = await _ready_client(env)
    service = await env.service()
    results = await asyncio.gather(
        *(env.services.orders.create_order(client, service) for _ in range(5)), return_exceptions=True
    )
    created = [r for r in results if not isinstance(r, Exception)]
    assert len(created) == 1
    assert all(isinstance(r, AlreadyOrdered) for r in results if isinstance(r, Exception))
    assert await env.db.fetchval("SELECT COUNT(*) FROM orders") == 1


async def test_db_unique_order_per_shift(env):
    client = await _ready_client(env)
    service = await env.service()
    order = await env.services.orders.create_order(client, service)
    address = await env.repos.user_addresses.get_current(client.user.id)
    with pytest.raises(IntegrityError):
        await env.repos.orders.create(service.id, client.user.id, address, order.shift_date, 0, env.clock())


async def test_blocked_client_cannot_order(env):
    client = await _ready_client(env)
    await env.services.users.set_status(await env.admin(), client.user.id, UserStatus.BLOCKED)
    client = await env.actor(10)
    with pytest.raises(UserBlocked):
        await env.services.orders.create_order(client, await env.service())


async def test_cancelled_order_does_not_restore_right_but_exception_does(env):
    client = await _ready_client(env)
    admin = await env.admin()
    service = await env.service()
    order = await env.services.orders.create_order(client, service)
    with pytest.raises(ValidationError):
        await env.services.orders.cancel(admin, order.id, "")
    await env.services.orders.cancel(admin, order.id, "Ошибка клиента")
    with pytest.raises(AlreadyOrdered):
        await env.services.orders.create_order(client, service)
    await env.services.orders.grant_exception(admin, order.id)
    second = await env.services.orders.create_order(client, service)
    assert second.attempt == 1
    with pytest.raises(AlreadyOrdered):
        await env.services.orders.create_order(client, service)


async def test_manual_mode_full_flow(env):
    client = await _ready_client(env)
    admin = await env.admin()
    executor = await env.actor(500, Role.EXECUTOR, "worker")
    other_executor = await env.actor(501, Role.EXECUTOR, "worker2")
    order = await env.services.orders.create_order(client, await env.service())

    # в ручном режиме неназначенная заявка исполнителю не видна
    assert await env.services.orders.executor_orders(executor, "new") == []
    with pytest.raises(NotFound):
        await env.services.orders.executor_accept(executor, order.id)

    change = await env.services.orders.assign(admin, order.id, executor.user.id)
    assert change.order.status == OrderStatus.ASSIGNED and change.previous_executor_id is None
    assert [o.id for o in await env.services.orders.executor_orders(executor, "assigned")] == [order.id]
    with pytest.raises(NotFound):
        await env.services.orders.get_for_executor(other_executor, order.id)

    # нельзя завершить, не приняв
    with pytest.raises(InvalidTransition):
        await env.services.orders.executor_done(executor, order.id)

    await env.services.orders.executor_accept(executor, order.id)
    await env.services.orders.executor_start(executor, order.id)
    done = await env.services.orders.executor_done(executor, order.id)
    assert done.status == OrderStatus.DONE
    assert done.executor_id == executor.user.id and done.finished_at == env.clock()

    # из конечного статуса вернуться нельзя
    with pytest.raises(InvalidTransition):
        await env.services.orders.cancel(admin, order.id, "поздно")
    with pytest.raises(Exception, match="invalid_order_transition"):
        await env.db.execute("UPDATE orders SET status = 'in_progress' WHERE id = ?", (order.id,))

    history = [(h.old_status, h.new_status) for h in await env.services.orders.history(admin, order.id)]
    assert history == [
        (None, "new"), ("new", "assigned"), ("assigned", "accepted"), ("accepted", "in_progress"),
        ("in_progress", "done"),
    ]
    assert [o.id for o in await env.services.orders.executor_orders(executor, "done_shift")] == [order.id]


async def test_pool_mode_first_executor_wins(env):
    await _set_mode(env, DistributionMode.POOL)
    client = await _ready_client(env)
    e1 = await env.actor(500, Role.EXECUTOR)
    e2 = await env.actor(501, Role.EXECUTOR)
    order = await env.services.orders.create_order(client, await env.service())
    assert [o.id for o in await env.services.orders.executor_orders(e1, "new")] == [order.id]
    accepted = await env.services.orders.executor_accept(e1, order.id)
    assert accepted.status == OrderStatus.ACCEPTED and accepted.executor_id == e1.user.id
    with pytest.raises(NotFound):
        await env.services.orders.executor_accept(e2, order.id)
    # принятый заказ можно сразу отметить выполненным
    assert (await env.services.orders.executor_done(e1, order.id)).status == OrderStatus.DONE


async def test_fail_requires_comment_for_other(env):
    client = await _ready_client(env)
    executor = await env.actor(500, Role.EXECUTOR)
    order = await env.services.orders.create_order(client, await env.service())
    await env.services.orders.assign(await env.admin(), order.id, executor.user.id)
    with pytest.raises(ValidationError):
        await env.services.orders.executor_fail(executor, order.id, FailReason.OTHER, "  ")
    failed = await env.services.orders.executor_fail(executor, order.id, FailReason.WRONG_CODE, None)
    assert failed.status == OrderStatus.FAILED and failed.fail_reason == "wrong_code"


async def test_reassign_notifies_previous_executor(env):
    client = await _ready_client(env)
    admin = await env.admin()
    e1 = await env.actor(500, Role.EXECUTOR)
    e2 = await env.actor(501, Role.EXECUTOR)
    order = await env.services.orders.create_order(client, await env.service())
    await env.services.orders.assign(admin, order.id, e1.user.id)
    await env.services.orders.executor_accept(e1, order.id)
    change = await env.services.orders.assign(admin, order.id, e2.user.id)
    assert change.previous_executor_id == e1.user.id
    assert change.order.status == OrderStatus.ASSIGNED and change.order.accepted_at is None
    with pytest.raises(NotFound):
        await env.services.orders.executor_start(e1, order.id)


async def test_access_rights(env):
    client = await _ready_client(env, 10)
    other = await env.client_with_address(11)
    executor = await env.actor(500, Role.EXECUTOR)
    order = await env.services.orders.create_order(client, await env.service())

    with pytest.raises(NotFound):
        await env.services.orders.get_for_client(other, order.id)
    assert await env.services.orders.client_orders(other) == []
    with pytest.raises(AccessDenied):
        await env.services.orders.assign(client, order.id, executor.user.id)
    with pytest.raises(AccessDenied):
        await env.services.orders.executor_accept(client, order.id)
    with pytest.raises(AccessDenied):
        await env.services.orders.cancel(executor, order.id, "x")
    with pytest.raises(AccessDenied):
        await env.services.payments.confirm(executor, 1)
    tariff = (await env.services.catalog.tariffs())[0]
    with pytest.raises(AccessDenied):
        await env.services.catalog.update_tariff(executor, tariff.id, price_rub=1)
    with pytest.raises(AccessDenied):
        await env.services.users.set_role(executor, client.user.id, Role.EXECUTOR)
    with pytest.raises(AccessDenied):
        await env.services.settings.set(executor, "orders.distribution_mode", "pool")


async def test_unassigned_alert_once(env):
    client = await _ready_client(env)
    order = await env.services.orders.create_order(client, await env.service())
    assert await env.services.orders.mark_unassigned_alerts() == []
    env.clock.set(2026, 10, 21, 20, 20)
    assert [o.id for o in await env.services.orders.mark_unassigned_alerts()] == [order.id]
    assert await env.services.orders.mark_unassigned_alerts() == []
