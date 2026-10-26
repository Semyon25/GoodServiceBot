"""Заявки: оформление клиентом, выполнение исполнителем, управление администратором."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Iterable

from bot.db.connection import IntegrityError
from bot.db.repositories import Repositories
from bot.domain.enums import (
    FAIL_REASON_TITLES,
    ORDER_STATUS_TITLES,
    DistributionMode,
    FailReason,
    OrderStatus,
    Role,
)
from bot.domain.errors import (
    AccessDenied,
    AlreadyOrdered,
    InvalidTransition,
    NoAddress,
    NoSubscription,
    NotFound,
    OutsideSchedule,
    ValidationError,
)
from bot.domain.models import Actor, Order, Service, StatusHistoryEntry, User
from bot.domain.schedule import last_shift_date, order_shift
from bot.domain.transitions import ORDER_ACTIVE_STATUSES, ORDER_FINAL_STATUSES, order_transition_allowed

from .access import require_active, require_admin, require_executor
from .settings import SettingsService
from .tariffs import CatalogService

O = OrderStatus


@dataclass
class OrderChange:
    """Результат изменения заказа — всё, что нужно для уведомлений."""

    order: Order
    previous_executor_id: int | None = None


class ExecutorView:
    NEW = "new"  # общий пул (только в режиме POOL)
    ASSIGNED = "assigned"
    IN_WORK = "in_work"
    DONE_SHIFT = "done_shift"
    HISTORY = "history"


class OrderService:
    def __init__(
        self,
        repos: Repositories,
        settings: SettingsService,
        catalog: CatalogService,
        clock: Callable[[], datetime],
        admin_ids: Iterable[int] = (),
    ):
        self.repos = repos
        self.admin_ids = tuple(admin_ids)
        self.settings = settings
        self.catalog = catalog
        self._now = clock

    # ------------------------------------------------------------------ клиент

    async def check_can_order(self, actor: Actor, service: Service) -> date:
        """Проверяет условия оформления заявки; возвращает дату выноса, в который она попадёт.

        Заказать можно в любое время: заявка попадает в ближайший вынос.
        """
        require_active(actor)
        if not service.is_active:
            raise ValidationError("Услуга временно недоступна.")
        now = self._now()
        shift = order_shift(now, await self.settings.schedule(service))
        if not shift:
            raise OutsideSchedule("Вынос мусора временно не проводится. Попробуйте позже.")

        existing = await self.repos.orders.list_for_shift(actor.user.id, service.id, shift.shift_date)
        if existing:
            allowed = service.max_orders_per_shift + await self.repos.orders.count_exceptions(
                actor.user.id, service.id, shift.shift_date
            )
            if len(existing) >= allowed:
                raise AlreadyOrdered(existing[-1])

        if not await self.repos.user_addresses.get_current(actor.user.id):
            raise NoAddress()
        if service.rules.get("requires_subscription", True):
            if not await self.repos.subscriptions.get_active_at(actor.user.id, service.id, now):
                raise NoSubscription()
        return shift.shift_date

    async def create_order(self, actor: Actor, service: Service) -> Order:
        """Создаёт заявку. Повторные/одновременные нажатия не создают дубликатов.

        В режиме «назначает администратор» заявка сразу назначается администратору по умолчанию.
        """
        now = self._now()
        async with self.repos.transaction():
            shift_date = await self.check_can_order(actor, service)
            existing = await self.repos.orders.list_for_shift(actor.user.id, service.id, shift_date)
            address = await self.repos.user_addresses.get_current(actor.user.id)
            assert address is not None
            try:
                order_id = await self.repos.orders.create(
                    service.id, actor.user.id, address, shift_date, attempt=len(existing), now=now
                )
            except IntegrityError:
                # одновременное нажатие: заявка с этим номером попытки уже есть
                orders = await self.repos.orders.list_for_shift(actor.user.id, service.id, shift_date)
                raise AlreadyOrdered(orders[-1] if orders else None) from None
            await self.repos.orders.add_history(order_id, None, O.NEW, actor.tg_id, now, "Заявка создана клиентом")

            if await self.settings.distribution_mode() == DistributionMode.MANUAL:
                admin = await self.default_executor()
                if admin:
                    await self.repos.orders.transition(
                        order_id, [O.NEW], O.ASSIGNED, expected_executor=None, executor_id=admin.id, assigned_at=now
                    )
                    await self.repos.orders.add_history(
                        order_id, O.NEW, O.ASSIGNED, None, now, f"Назначена администратору {admin.display}"
                    )
        return await self._reload(order_id)

    async def default_executor(self) -> User | None:
        """Администратор, на которого по умолчанию назначаются заявки (первый в ADMIN_IDS)."""
        if not self.admin_ids:
            return None
        tg_id = self.admin_ids[0]
        return await self.repos.users.get_by_tg(tg_id) or await self.repos.users.upsert(tg_id, None, self._now())

    async def assignees(self) -> list[User]:
        """Кому можно назначить заявку: администраторы и активные исполнители."""
        admins = [u for tg in self.admin_ids if (u := await self.repos.users.get_by_tg(tg))]
        executors = [u for u in await self.repos.users.list(role=Role.EXECUTOR) if not u.is_blocked]
        return admins + [u for u in executors if u.tg_id not in self.admin_ids]

    async def client_orders(self, actor: Actor, limit: int = 10) -> list[Order]:
        return await self.repos.orders.list(user_id=actor.user.id, newest_first=True, limit=limit)

    async def get_for_client(self, actor: Actor, order_id: int) -> Order:
        order = await self.repos.orders.get(order_id)
        if not order or (order.user_id != actor.user.id and not actor.is_admin):
            raise NotFound("Заявка не найдена.")
        return order

    # ------------------------------------------------------------- исполнитель

    async def can_executor_see(self, actor: Actor, order: Order) -> bool:
        if order.executor_id == actor.user.id or actor.is_admin:
            return True
        return (
            order.status == O.NEW
            and order.executor_id is None
            and await self.settings.distribution_mode() == DistributionMode.POOL
        )

    async def get_for_executor(self, actor: Actor, order_id: int) -> Order:
        require_executor(actor)
        order = await self.repos.orders.get(order_id)
        if not order or not await self.can_executor_see(actor, order):
            raise NotFound("Заявка не найдена или недоступна вам.")
        return order

    async def executor_orders(self, actor: Actor, view: str) -> list[Order]:
        require_executor(actor)
        me = actor.user.id
        if view == ExecutorView.NEW:
            if await self.settings.distribution_mode() != DistributionMode.POOL:
                return []
            return await self.repos.orders.list(statuses=[O.NEW], unassigned=True, limit=100)
        if view == ExecutorView.ASSIGNED:
            return await self.repos.orders.list(statuses=[O.ASSIGNED], executor_id=me, limit=100)
        if view == ExecutorView.IN_WORK:
            return await self.repos.orders.list(statuses=[O.ACCEPTED, O.IN_PROGRESS], executor_id=me, limit=100)
        if view == ExecutorView.DONE_SHIFT:
            shift_date = last_shift_date(self._now(), await self.settings.schedule())
            if not shift_date:
                return []
            return await self.repos.orders.list(statuses=[O.DONE], executor_id=me, shift_date=shift_date, limit=100)
        if view == ExecutorView.HISTORY:
            return await self.repos.orders.list(
                statuses=list(ORDER_FINAL_STATUSES), executor_id=me, newest_first=True, limit=30
            )
        raise ValidationError("Неизвестный список заявок.")

    async def executor_accept(self, actor: Actor, order_id: int) -> Order:
        require_executor(actor)
        now = self._now()
        async with self.repos.transaction():
            order = await self.get_for_executor(actor, order_id)
            if order.status == O.NEW:
                ok = await self.repos.orders.transition(
                    order.id, [O.NEW], O.ACCEPTED, expected_executor=None,
                    executor_id=actor.user.id, assigned_at=now, accepted_at=now,
                )
                if not ok:
                    raise InvalidTransition("Заявку уже взял другой исполнитель.")
            else:
                await self._executor_step(actor, order, [O.ASSIGNED], O.ACCEPTED, accepted_at=now)
            await self.repos.orders.add_history(order.id, order.status, O.ACCEPTED, actor.tg_id, now, None)
        return await self._reload(order_id)

    async def executor_start(self, actor: Actor, order_id: int) -> Order:
        require_executor(actor)
        now = self._now()
        async with self.repos.transaction():
            order = await self.get_for_executor(actor, order_id)
            await self._executor_step(actor, order, [O.ACCEPTED], O.IN_PROGRESS, started_at=now)
            await self.repos.orders.add_history(order.id, order.status, O.IN_PROGRESS, actor.tg_id, now, None)
        return await self._reload(order_id)

    async def executor_done(self, actor: Actor, order_id: int) -> Order:
        require_executor(actor)
        now = self._now()
        async with self.repos.transaction():
            order = await self.get_for_executor(actor, order_id)
            if order.status == O.ASSIGNED:
                # «Выполнено» сразу из назначенной заявки: принятие фиксируется автоматически
                await self._executor_step(actor, order, [O.ASSIGNED], O.ACCEPTED, accepted_at=now)
                await self.repos.orders.add_history(order.id, O.ASSIGNED, O.ACCEPTED, actor.tg_id, now, None)
                order = await self._reload(order.id)
            await self._executor_step(actor, order, [O.ACCEPTED, O.IN_PROGRESS], O.DONE, finished_at=now)
            await self.repos.orders.add_history(order.id, order.status, O.DONE, actor.tg_id, now, None)
        return await self._reload(order_id)

    async def executor_fail(self, actor: Actor, order_id: int, reason: FailReason, comment: str | None) -> Order:
        require_executor(actor)
        comment = (comment or "").strip() or None
        if reason == FailReason.OTHER and not comment:
            raise ValidationError("Для причины «Другая причина» обязательно напишите комментарий.")
        if comment and len(comment) > 500:
            raise ValidationError("Комментарий не должен быть длиннее 500 символов.")
        now = self._now()
        async with self.repos.transaction():
            order = await self.get_for_executor(actor, order_id)
            await self._executor_step(
                actor, order, [O.ASSIGNED, O.ACCEPTED, O.IN_PROGRESS], O.FAILED,
                finished_at=now, fail_reason=reason.value, comment=comment,
            )
            text = FAIL_REASON_TITLES[reason] + (f": {comment}" if comment else "")
            await self.repos.orders.add_history(order.id, order.status, O.FAILED, actor.tg_id, now, text)
        return await self._reload(order_id)

    async def _executor_step(self, actor: Actor, order: Order, expected: list[O], new: O, **fields) -> None:
        if order.executor_id != actor.user.id:
            raise AccessDenied("Эта заявка назначена другому исполнителю.")
        if order.status not in expected:
            raise InvalidTransition(
                f"Действие недоступно: заявка в статусе «{ORDER_STATUS_TITLES[order.status_enum]}»."
            )
        ok = await self.repos.orders.transition(
            order.id, [order.status], new, expected_executor=actor.user.id, **fields
        )
        if not ok:
            raise InvalidTransition("Статус заявки уже изменился. Обновите карточку.")

    # ---------------------------------------------------------- администратор

    async def get_for_admin(self, actor: Actor, order_id: int) -> Order:
        require_admin(actor)
        order = await self.repos.orders.get(order_id)
        if not order:
            raise NotFound("Заявка не найдена.")
        return order

    async def admin_list(self, actor: Actor, **filters) -> list[Order]:
        require_admin(actor)
        return await self.repos.orders.list(**filters)

    async def assign(self, actor: Actor, order_id: int, executor_id: int) -> OrderChange:
        require_admin(actor)
        executor = await self.repos.users.get(executor_id)
        is_assignee = executor and (executor.tg_id in self.admin_ids or executor.role == Role.EXECUTOR)
        if not is_assignee or executor.is_blocked:
            raise ValidationError("Выбранный пользователь не является активным исполнителем.")
        now = self._now()
        async with self.repos.transaction():
            order = await self.get_for_admin(actor, order_id)
            if order.executor_id == executor_id and order.status != O.NEW:
                raise ValidationError("Заявка уже назначена этому исполнителю.")
            if not order_transition_allowed(order.status, O.ASSIGNED):
                raise InvalidTransition(
                    f"Нельзя назначить исполнителя: заявка в статусе «{ORDER_STATUS_TITLES[order.status_enum]}»."
                )
            ok = await self.repos.orders.transition(
                order.id, [order.status], O.ASSIGNED, expected_executor=order.executor_id,
                executor_id=executor_id, assigned_at=now, accepted_at=None, started_at=None,
            )
            if not ok:
                raise InvalidTransition("Заявка изменилась. Обновите карточку.")
            if order.executor_id:
                comment = f"Переназначена исполнителю {executor.display}"
            else:
                comment = f"Назначена исполнителю {executor.display}"
            await self.repos.orders.add_history(order.id, order.status, O.ASSIGNED, actor.tg_id, now, comment)
        return OrderChange(await self._reload(order_id), previous_executor_id=order.executor_id)

    async def cancel(self, actor: Actor, order_id: int, reason: str) -> OrderChange:
        require_admin(actor)
        reason = (reason or "").strip()
        if not reason:
            raise ValidationError("Укажите причину отмены.")
        now = self._now()
        async with self.repos.transaction():
            order = await self.get_for_admin(actor, order_id)
            if order.status not in ORDER_ACTIVE_STATUSES:
                raise InvalidTransition("Завершённую заявку отменить нельзя.")
            ok = await self.repos.orders.transition(
                order.id, [order.status], O.CANCELLED, cancel_reason=reason, finished_at=now
            )
            if not ok:
                raise InvalidTransition("Заявка изменилась. Обновите карточку.")
            await self.repos.orders.add_history(order.id, order.status, O.CANCELLED, actor.tg_id, now, reason)
        return OrderChange(await self._reload(order_id), previous_executor_id=order.executor_id)

    async def grant_exception(self, actor: Actor, order_id: int, comment: str | None = None) -> Order:
        """Разрешает клиенту ещё одну заявку в смену этой заявки (исключение по решению администратора)."""
        require_admin(actor)
        order = await self.get_for_admin(actor, order_id)
        if order.status not in ORDER_FINAL_STATUSES:
            raise ValidationError("Исключение можно дать только после завершения текущей заявки.")
        now = self._now()
        async with self.repos.transaction():
            service = await self.catalog.service_by_id(order.service_id)
            orders = await self.repos.orders.list_for_shift(order.user_id, order.service_id, order.shift_date)
            allowed = service.max_orders_per_shift + await self.repos.orders.count_exceptions(
                order.user_id, order.service_id, order.shift_date
            )
            if len(orders) < allowed:
                raise ValidationError("Клиент уже может оформить ещё одну заявку в эту смену.")
            await self.repos.orders.add_exception(
                order.user_id, order.service_id, order.shift_date, actor.tg_id, now, comment
            )
            await self.repos.audit.log(
                actor.tg_id, "order.exception", f"user_id={order.user_id}, shift={order.shift_date}", now
            )
        return order

    async def history(self, actor: Actor, order_id: int) -> list[StatusHistoryEntry]:
        require_admin(actor)
        return await self.repos.orders.history(order_id)

    async def mark_unassigned_alerts(self) -> list[Order]:
        """Заявки без исполнителя дольше заданного времени (уведомление админу отправляется один раз)."""
        minutes = await self.settings.unassigned_alert_minutes()
        if minutes <= 0:
            return []
        orders = await self.repos.orders.unassigned_older_than(self._now() - timedelta(minutes=minutes))
        await self.repos.orders.mark_unassigned_alert(o.id for o in orders)
        return orders

    async def users_by_id(self, ids) -> dict[int, User]:
        return await self.repos.users.get_many(i for i in ids if i)

    async def _reload(self, order_id: int) -> Order:
        order = await self.repos.orders.get(order_id)
        if not order:
            raise NotFound("Заявка не найдена.")
        return order
