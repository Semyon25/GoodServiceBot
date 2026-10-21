"""Слой доступа к данным: весь SQL приложения находится здесь.

Сервисы бизнес-логики работают только с методами репозиториев и моделями
из bot.domain.models, поэтому хранилище можно заменить, не трогая логику.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime
from typing import Any, Iterable, TypeVar

from bot.domain.enums import PaymentStatus
from bot.domain.models import (
    Address,
    Order,
    Payment,
    Service,
    StatusHistoryEntry,
    Subscription,
    Tariff,
    User,
    UserAddress,
)

from .connection import Database, from_db_dt, to_db_date, to_db_dt

T = TypeVar("T")


def _convert(cls: type[T], row: dict[str, Any]) -> T:
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):  # type: ignore[arg-type]
        if f.name not in row:
            continue
        value = row[f.name]
        type_str = str(f.type)
        if value is not None:
            if "datetime" in type_str:
                value = from_db_dt(value)
            elif type_str.startswith("date"):
                value = date.fromisoformat(value)
            elif type_str == "bool":
                value = bool(value)
        kwargs[f.name] = value
    return cls(**kwargs)


def _many(cls: type[T], rows: Iterable[dict[str, Any]]) -> list[T]:
    return [_convert(cls, r) for r in rows]


def _placeholders(items: Iterable[Any]) -> str:
    return ", ".join("?" for _ in items)


class Repo:
    def __init__(self, db: Database):
        self.db = db


class UserRepo(Repo):
    async def get(self, user_id: int) -> User | None:
        row = await self.db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))
        return _convert(User, row) if row else None

    async def get_by_tg(self, tg_id: int) -> User | None:
        row = await self.db.fetchone("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        return _convert(User, row) if row else None

    async def get_by_username(self, username: str) -> User | None:
        row = await self.db.fetchone(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username.lstrip("@"),)
        )
        return _convert(User, row) if row else None

    async def upsert(self, tg_id: int, username: str | None, now: datetime) -> User:
        await self.db.execute(
            """
            INSERT INTO users (tg_id, username, registered_at) VALUES (?, ?, ?)
            ON CONFLICT (tg_id) DO UPDATE SET username = excluded.username
            WHERE users.username IS NOT excluded.username
            """,
            (tg_id, username, to_db_dt(now)),
        )
        user = await self.get_by_tg(tg_id)
        assert user is not None
        return user

    async def set_role(self, user_id: int, role: str) -> None:
        await self.db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))

    async def set_status(self, user_id: int, status: str) -> None:
        await self.db.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))

    async def list(self, role: str | None = None, limit: int = 1000) -> list[User]:
        if role:
            rows = await self.db.fetchall(
                "SELECT * FROM users WHERE role = ? ORDER BY id LIMIT ?", (role, limit)
            )
        else:
            rows = await self.db.fetchall("SELECT * FROM users ORDER BY id LIMIT ?", (limit,))
        return _many(User, rows)

    async def get_many(self, ids: Iterable[int]) -> dict[int, User]:
        ids = list(set(ids))
        if not ids:
            return {}
        rows = await self.db.fetchall(f"SELECT * FROM users WHERE id IN ({_placeholders(ids)})", ids)
        return {r["id"]: _convert(User, r) for r in rows}


class AddressRepo(Repo):
    async def list(self, active_only: bool = True) -> list[Address]:
        sql = "SELECT * FROM addresses"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY sort_order, id"
        return _many(Address, await self.db.fetchall(sql))

    async def get(self, address_id: int) -> Address | None:
        row = await self.db.fetchone("SELECT * FROM addresses WHERE id = ?", (address_id,))
        return _convert(Address, row) if row else None

    async def create(self, title: str, sections_count: int) -> int:
        sort = await self.db.fetchval("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM addresses")
        row_id, _ = await self.db.execute(
            "INSERT INTO addresses (title, sections_count, sort_order) VALUES (?, ?, ?)",
            (title, sections_count, sort),
        )
        return int(row_id)

    async def update(self, address_id: int, **fields: Any) -> None:
        allowed = {"title", "sections_count", "is_active"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        assignments = ", ".join(f"{k} = ?" for k in sets)
        await self.db.execute(
            f"UPDATE addresses SET {assignments} WHERE id = ?", (*sets.values(), address_id)
        )


class UserAddressRepo(Repo):
    _SELECT = """
        SELECT ua.*, a.title AS address_title
        FROM user_addresses ua JOIN addresses a ON a.id = ua.address_id
    """

    async def get_current(self, user_id: int) -> UserAddress | None:
        row = await self.db.fetchone(self._SELECT + " WHERE ua.user_id = ? AND ua.is_current = 1", (user_id,))
        return _convert(UserAddress, row) if row else None

    async def get(self, user_address_id: int) -> UserAddress | None:
        row = await self.db.fetchone(self._SELECT + " WHERE ua.id = ?", (user_address_id,))
        return _convert(UserAddress, row) if row else None

    async def replace_current(
        self,
        user_id: int,
        address_id: int,
        section: int,
        floor: int,
        apartment: str,
        intercom_code: str,
        instructions: str | None,
        now: datetime,
    ) -> int:
        """Сохраняет новую версию адреса. Старая версия остаётся для ссылок из заказов."""
        async with self.db.transaction():
            await self.db.execute(
                "UPDATE user_addresses SET is_current = 0 WHERE user_id = ? AND is_current = 1", (user_id,)
            )
            row_id, _ = await self.db.execute(
                """
                INSERT INTO user_addresses
                    (user_id, address_id, section, floor, apartment, intercom_code, instructions, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, address_id, section, floor, apartment, intercom_code, instructions, to_db_dt(now)),
            )
        return int(row_id)

    async def count_with_section_above(self, address_id: int, max_section: int) -> int:
        return int(
            await self.db.fetchval(
                "SELECT COUNT(*) FROM user_addresses WHERE address_id = ? AND is_current = 1 AND section > ?",
                (address_id, max_section),
            )
        )


class ServiceRepo(Repo):
    @staticmethod
    def _from_row(row: dict[str, Any]) -> Service:
        return Service(
            id=row["id"],
            code=row["code"],
            title=row["title"],
            description=row["description"],
            is_active=bool(row["is_active"]),
            schedule=json.loads(row["schedule_json"]) if row["schedule_json"] else None,
            rules=json.loads(row["rules_json"] or "{}"),
        )

    async def get(self, service_id: int) -> Service | None:
        row = await self.db.fetchone("SELECT * FROM services WHERE id = ?", (service_id,))
        return self._from_row(row) if row else None

    async def get_by_code(self, code: str) -> Service | None:
        row = await self.db.fetchone("SELECT * FROM services WHERE code = ?", (code,))
        return self._from_row(row) if row else None

    async def list(self, active_only: bool = True) -> list[Service]:
        sql = "SELECT * FROM services" + (" WHERE is_active = 1" if active_only else "") + " ORDER BY id"
        return [self._from_row(r) for r in await self.db.fetchall(sql)]

    async def create(
        self, code: str, title: str, description: str | None, rules: dict, schedule: dict | None = None
    ) -> int:
        row_id, _ = await self.db.execute(
            "INSERT INTO services (code, title, description, rules_json, schedule_json) VALUES (?, ?, ?, ?, ?)",
            (code, title, description, json.dumps(rules), json.dumps(schedule) if schedule else None),
        )
        return int(row_id)


class TariffRepo(Repo):
    async def list(self, service_id: int | None = None, active_only: bool = True) -> list[Tariff]:
        where, params = [], []
        if service_id is not None:
            where.append("service_id = ?")
            params.append(service_id)
        if active_only:
            where.append("is_active = 1")
        sql = "SELECT * FROM tariffs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY sort_order, id"
        return _many(Tariff, await self.db.fetchall(sql, params))

    async def get(self, tariff_id: int) -> Tariff | None:
        row = await self.db.fetchone("SELECT * FROM tariffs WHERE id = ?", (tariff_id,))
        return _convert(Tariff, row) if row else None

    async def create(self, service_id: int, title: str, price_rub: int, duration_days: int) -> int:
        sort = await self.db.fetchval("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM tariffs")
        row_id, _ = await self.db.execute(
            "INSERT INTO tariffs (service_id, title, price_rub, duration_days, sort_order) VALUES (?, ?, ?, ?, ?)",
            (service_id, title, price_rub, duration_days, sort),
        )
        return int(row_id)

    async def update(self, tariff_id: int, **fields: Any) -> None:
        allowed = {"title", "price_rub", "duration_days", "is_active"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        assignments = ", ".join(f"{k} = ?" for k in sets)
        await self.db.execute(f"UPDATE tariffs SET {assignments} WHERE id = ?", (*sets.values(), tariff_id))


class PaymentRepo(Repo):
    async def get(self, payment_id: int) -> Payment | None:
        row = await self.db.fetchone("SELECT * FROM payments WHERE id = ?", (payment_id,))
        return _convert(Payment, row) if row else None

    async def get_open_for_user(self, user_id: int, service_id: int) -> Payment | None:
        row = await self.db.fetchone(
            """
            SELECT * FROM payments
            WHERE user_id = ? AND service_id = ? AND status IN ('awaiting_payment', 'awaiting_confirmation')
            ORDER BY id DESC LIMIT 1
            """,
            (user_id, service_id),
        )
        return _convert(Payment, row) if row else None

    async def create(self, user_id: int, tariff: Tariff, now: datetime) -> int:
        row_id, _ = await self.db.execute(
            """
            INSERT INTO payments (user_id, service_id, tariff_id, tariff_title, amount_rub, duration_days,
                                  status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                tariff.service_id,
                tariff.id,
                tariff.title,
                tariff.price_rub,
                tariff.duration_days,
                PaymentStatus.AWAITING_PAYMENT.value,
                to_db_dt(now),
                to_db_dt(now),
            ),
        )
        return int(row_id)

    async def change_tariff(self, payment_id: int, tariff: Tariff, now: datetime) -> bool:
        _, count = await self.db.execute(
            """
            UPDATE payments SET tariff_id = ?, tariff_title = ?, amount_rub = ?, duration_days = ?, updated_at = ?
            WHERE id = ? AND status = 'awaiting_payment'
            """,
            (tariff.id, tariff.title, tariff.price_rub, tariff.duration_days, to_db_dt(now), payment_id),
        )
        return count == 1

    async def transition(
        self, payment_id: int, expected: Iterable[str], new_status: str, now: datetime, **fields: Any
    ) -> bool:
        """Атомарно меняет статус, только если текущий статус входит в expected."""
        expected = [str(s) for s in expected]
        sets = {"status": str(new_status), "updated_at": to_db_dt(now)}
        for key, value in fields.items():
            sets[key] = to_db_dt(value) if isinstance(value, datetime) else value
        assignments = ", ".join(f"{k} = ?" for k in sets)
        _, count = await self.db.execute(
            f"UPDATE payments SET {assignments} WHERE id = ? AND status IN ({_placeholders(expected)})",
            (*sets.values(), payment_id, *expected),
        )
        return count == 1

    async def add_history(
        self, payment_id: int, old: str | None, new: str, actor_tg: int | None, now: datetime, comment: str | None
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO payment_status_history (payment_id, old_status, new_status, actor_tg, created_at, comment)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (payment_id, old, new, actor_tg, to_db_dt(now), comment),
        )

    async def history(self, payment_id: int) -> list[StatusHistoryEntry]:
        rows = await self.db.fetchall(
            """
            SELECT id, payment_id AS entity_id, old_status, new_status, actor_tg, created_at, comment
            FROM payment_status_history WHERE payment_id = ? ORDER BY id
            """,
            (payment_id,),
        )
        return _many(StatusHistoryEntry, rows)

    async def list(
        self,
        status: str | None = None,
        user_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Payment]:
        where, params = self._filters(status, user_id, since, until)
        rows = await self.db.fetchall(
            f"SELECT * FROM payments {where} ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset)
        )
        return _many(Payment, rows)

    async def count(self, status: str | None = None, user_id: int | None = None) -> int:
        where, params = self._filters(status, user_id, None, None)
        return int(await self.db.fetchval(f"SELECT COUNT(*) FROM payments {where}", params))

    @staticmethod
    def _filters(status, user_id, since, until) -> tuple[str, list[Any]]:
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(str(status))
        if user_id:
            where.append("user_id = ?")
            params.append(user_id)
        if since:
            where.append("created_at >= ?")
            params.append(to_db_dt(since))
        if until:
            where.append("created_at < ?")
            params.append(to_db_dt(until))
        return ("WHERE " + " AND ".join(where)) if where else "", params


class SubscriptionRepo(Repo):
    async def get(self, subscription_id: int) -> Subscription | None:
        row = await self.db.fetchone("SELECT * FROM subscriptions WHERE id = ?", (subscription_id,))
        return _convert(Subscription, row) if row else None

    async def get_by_payment(self, payment_id: int) -> Subscription | None:
        row = await self.db.fetchone("SELECT * FROM subscriptions WHERE payment_id = ?", (payment_id,))
        return _convert(Subscription, row) if row else None

    async def create(
        self,
        user_id: int,
        service_id: int,
        tariff_id: int,
        payment_id: int,
        tariff_title: str,
        amount_rub: int,
        starts_at: datetime,
        ends_at: datetime,
        now: datetime,
    ) -> int:
        row_id, _ = await self.db.execute(
            """
            INSERT INTO subscriptions (user_id, service_id, tariff_id, payment_id, tariff_title, amount_rub,
                                       starts_at, ends_at, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                user_id,
                service_id,
                tariff_id,
                payment_id,
                tariff_title,
                amount_rub,
                to_db_dt(starts_at),
                to_db_dt(ends_at),
                to_db_dt(now),
            ),
        )
        return int(row_id)

    async def get_active_at(self, user_id: int, service_id: int, moment: datetime) -> Subscription | None:
        row = await self.db.fetchone(
            """
            SELECT * FROM subscriptions
            WHERE user_id = ? AND service_id = ? AND status != 'cancelled' AND starts_at <= ? AND ends_at > ?
            ORDER BY starts_at LIMIT 1
            """,
            (user_id, service_id, to_db_dt(moment), to_db_dt(moment)),
        )
        return _convert(Subscription, row) if row else None

    async def latest_end(self, user_id: int, service_id: int) -> datetime | None:
        value = await self.db.fetchval(
            "SELECT MAX(ends_at) FROM subscriptions WHERE user_id = ? AND service_id = ? AND status != 'cancelled'",
            (user_id, service_id),
        )
        return from_db_dt(value)

    async def list_current_and_future(self, user_id: int, moment: datetime) -> list[Subscription]:
        rows = await self.db.fetchall(
            """
            SELECT * FROM subscriptions
            WHERE user_id = ? AND status != 'cancelled' AND ends_at > ?
            ORDER BY starts_at
            """,
            (user_id, to_db_dt(moment)),
        )
        return _many(Subscription, rows)

    async def list_for_user(self, user_id: int, limit: int = 20) -> list[Subscription]:
        rows = await self.db.fetchall(
            "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY starts_at DESC LIMIT ?", (user_id, limit)
        )
        return _many(Subscription, rows)

    async def has_successor(self, sub: Subscription) -> bool:
        value = await self.db.fetchval(
            """
            SELECT 1 FROM subscriptions
            WHERE user_id = ? AND service_id = ? AND status != 'cancelled' AND id != ? AND ends_at > ?
            LIMIT 1
            """,
            (sub.user_id, sub.service_id, sub.id, to_db_dt(sub.ends_at)),
        )
        return bool(value)

    async def due_for_reminder(self, moment: datetime, ahead_until: datetime) -> list[Subscription]:
        rows = await self.db.fetchall(
            """
            SELECT * FROM subscriptions
            WHERE status = 'active' AND reminder_sent = 0 AND ends_at > ? AND ends_at <= ?
            """,
            (to_db_dt(moment), to_db_dt(ahead_until)),
        )
        return _many(Subscription, rows)

    async def due_for_expiry(self, moment: datetime) -> list[Subscription]:
        rows = await self.db.fetchall(
            "SELECT * FROM subscriptions WHERE status = 'active' AND ends_at <= ?", (to_db_dt(moment),)
        )
        return _many(Subscription, rows)

    async def mark_reminder_sent(self, subscription_id: int) -> bool:
        _, count = await self.db.execute(
            "UPDATE subscriptions SET reminder_sent = 1 WHERE id = ? AND reminder_sent = 0", (subscription_id,)
        )
        return count == 1

    async def mark_expired(self, subscription_id: int, notified: bool) -> bool:
        _, count = await self.db.execute(
            "UPDATE subscriptions SET status = 'expired', expiry_notified = ? WHERE id = ? AND status = 'active'",
            (int(notified), subscription_id),
        )
        return count == 1

    async def cancel_by_payment(self, payment_id: int) -> bool:
        _, count = await self.db.execute(
            "UPDATE subscriptions SET status = 'cancelled' WHERE payment_id = ? AND status != 'cancelled'",
            (payment_id,),
        )
        return count == 1

    async def count_by_state(self, moment: datetime) -> dict[str, int]:
        row = await self.db.fetchone(
            """
            SELECT
                SUM(CASE WHEN status != 'cancelled' AND starts_at <= :m AND ends_at > :m THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN status != 'cancelled' AND starts_at > :m THEN 1 ELSE 0 END) AS future,
                SUM(CASE WHEN status != 'cancelled' AND ends_at <= :m THEN 1 ELSE 0 END) AS expired,
                SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled
            FROM subscriptions
            """,
            {"m": to_db_dt(moment)},
        )
        return {k: int(v or 0) for k, v in (row or {}).items()}

    async def all_for_export(self) -> list[Subscription]:
        return _many(Subscription, await self.db.fetchall("SELECT * FROM subscriptions ORDER BY id"))


class OrderRepo(Repo):
    async def get(self, order_id: int) -> Order | None:
        row = await self.db.fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))
        return _convert(Order, row) if row else None

    async def list_for_shift(self, user_id: int, service_id: int, shift_date: date) -> list[Order]:
        rows = await self.db.fetchall(
            "SELECT * FROM orders WHERE user_id = ? AND service_id = ? AND shift_date = ? ORDER BY attempt",
            (user_id, service_id, to_db_date(shift_date)),
        )
        return _many(Order, rows)

    async def create(
        self,
        service_id: int,
        user_id: int,
        user_address: UserAddress,
        shift_date: date,
        attempt: int,
        now: datetime,
    ) -> int:
        row_id, _ = await self.db.execute(
            """
            INSERT INTO orders (service_id, user_id, user_address_id, shift_date, attempt, status,
                                address_id, address_title, section, floor, apartment, intercom_code,
                                instructions, created_at)
            VALUES (?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                service_id,
                user_id,
                user_address.id,
                to_db_date(shift_date),
                attempt,
                user_address.address_id,
                user_address.address_title,
                user_address.section,
                user_address.floor,
                user_address.apartment,
                user_address.intercom_code,
                user_address.instructions,
                to_db_dt(now),
            ),
        )
        return int(row_id)

    async def transition(
        self,
        order_id: int,
        expected: Iterable[str],
        new_status: str,
        expected_executor: int | None | bool = False,
        **fields: Any,
    ) -> bool:
        """Атомарно меняет статус (и поля), если статус и при необходимости исполнитель совпадают.

        expected_executor=False — исполнитель не проверяется, None — должен отсутствовать.
        """
        expected = [str(s) for s in expected]
        sets: dict[str, Any] = {"status": str(new_status)}
        for key, value in fields.items():
            sets[key] = to_db_dt(value) if isinstance(value, datetime) else value
        assignments = ", ".join(f"{k} = ?" for k in sets)
        sql = f"UPDATE orders SET {assignments} WHERE id = ? AND status IN ({_placeholders(expected)})"
        params: list[Any] = [*sets.values(), order_id, *expected]
        if expected_executor is None:
            sql += " AND executor_id IS NULL"
        elif expected_executor is not False:
            sql += " AND executor_id = ?"
            params.append(expected_executor)
        _, count = await self.db.execute(sql, params)
        return count == 1

    async def add_history(
        self, order_id: int, old: str | None, new: str, actor_tg: int | None, now: datetime, comment: str | None
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO order_status_history (order_id, old_status, new_status, actor_tg, created_at, comment)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (order_id, old, new, actor_tg, to_db_dt(now), comment),
        )

    async def history(self, order_id: int) -> list[StatusHistoryEntry]:
        rows = await self.db.fetchall(
            """
            SELECT id, order_id AS entity_id, old_status, new_status, actor_tg, created_at, comment
            FROM order_status_history WHERE order_id = ? ORDER BY id
            """,
            (order_id,),
        )
        return _many(StatusHistoryEntry, rows)

    async def list(
        self,
        *,
        statuses: Iterable[str] | None = None,
        shift_date: date | None = None,
        user_id: int | None = None,
        executor_id: int | None = None,
        unassigned: bool = False,
        address_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        newest_first: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Order]:
        where, params = self._filters(statuses, shift_date, user_id, executor_id, unassigned, address_id, since, until)
        order = "DESC" if newest_first else "ASC"
        rows = await self.db.fetchall(
            f"SELECT * FROM orders {where} ORDER BY created_at {order}, id {order} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return _many(Order, rows)

    async def count(self, **filters: Any) -> int:
        where, params = self._filters(
            filters.get("statuses"),
            filters.get("shift_date"),
            filters.get("user_id"),
            filters.get("executor_id"),
            filters.get("unassigned", False),
            filters.get("address_id"),
            filters.get("since"),
            filters.get("until"),
        )
        return int(await self.db.fetchval(f"SELECT COUNT(*) FROM orders {where}", params))

    @staticmethod
    def _filters(statuses, shift_date, user_id, executor_id, unassigned, address_id, since, until):
        where, params = [], []
        if statuses:
            statuses = [str(s) for s in statuses]
            where.append(f"status IN ({_placeholders(statuses)})")
            params.extend(statuses)
        if shift_date:
            where.append("shift_date = ?")
            params.append(to_db_date(shift_date))
        if user_id:
            where.append("user_id = ?")
            params.append(user_id)
        if executor_id:
            where.append("executor_id = ?")
            params.append(executor_id)
        if unassigned:
            where.append("executor_id IS NULL")
        if address_id:
            where.append("address_id = ?")
            params.append(address_id)
        if since:
            where.append("created_at >= ?")
            params.append(to_db_dt(since))
        if until:
            where.append("created_at < ?")
            params.append(to_db_dt(until))
        return ("WHERE " + " AND ".join(where)) if where else "", params

    async def unassigned_older_than(self, moment: datetime) -> list[Order]:
        rows = await self.db.fetchall(
            """
            SELECT * FROM orders
            WHERE status = 'new' AND executor_id IS NULL AND unassigned_alert_sent = 0 AND created_at <= ?
            ORDER BY id
            """,
            (to_db_dt(moment),),
        )
        return _many(Order, rows)

    async def mark_unassigned_alert(self, order_ids: Iterable[int]) -> None:
        ids = list(order_ids)
        if ids:
            await self.db.execute(
                f"UPDATE orders SET unassigned_alert_sent = 1 WHERE id IN ({_placeholders(ids)})", ids
            )

    async def add_exception(
        self, user_id: int, service_id: int, shift_date: date, granted_by_tg: int, now: datetime, comment: str | None
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO order_exceptions (user_id, service_id, shift_date, granted_by_tg, created_at, comment)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, service_id, to_db_date(shift_date), granted_by_tg, to_db_dt(now), comment),
        )

    async def count_exceptions(self, user_id: int, service_id: int, shift_date: date) -> int:
        return int(
            await self.db.fetchval(
                "SELECT COUNT(*) FROM order_exceptions WHERE user_id = ? AND service_id = ? AND shift_date = ?",
                (user_id, service_id, to_db_date(shift_date)),
            )
        )

    async def stats(self, since: datetime | None, until: datetime | None) -> dict[str, Any]:
        where, params = self._filters(None, None, None, None, False, None, since, until)
        rows = await self.db.fetchall(f"SELECT status, COUNT(*) AS cnt FROM orders {where} GROUP BY status", params)
        by_status = {r["status"]: int(r["cnt"]) for r in rows}
        by_executor = await self.db.fetchall(
            f"""
            SELECT executor_id, status, COUNT(*) AS cnt FROM orders
            {where + (' AND' if where else 'WHERE')} executor_id IS NOT NULL
            GROUP BY executor_id, status
            """,
            params,
        )
        return {"by_status": by_status, "by_executor": by_executor}


class SettingsRepo(Repo):
    async def get_all(self) -> dict[str, Any]:
        rows = await self.db.fetchall("SELECT key, value FROM system_settings")
        return {r["key"]: json.loads(r["value"]) for r in rows}

    async def set(self, key: str, value: Any, actor_tg: int | None, now: datetime) -> None:
        await self.db.execute(
            """
            INSERT INTO system_settings (key, value, updated_at, updated_by_tg) VALUES (?, ?, ?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at,
                                            updated_by_tg = excluded.updated_by_tg
            """,
            (key, json.dumps(value, ensure_ascii=False), to_db_dt(now), actor_tg),
        )


class AuditRepo(Repo):
    async def log(self, actor_tg: int, action: str, details: str | None, now: datetime) -> None:
        await self.db.execute(
            "INSERT INTO admin_actions (actor_tg, action, details, created_at) VALUES (?, ?, ?, ?)",
            (actor_tg, action, details, to_db_dt(now)),
        )

    async def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        return await self.db.fetchall("SELECT * FROM admin_actions ORDER BY id DESC LIMIT ?", (limit,))


class ExportRepo(Repo):
    async def orders(self) -> list[dict[str, Any]]:
        return await self.db.fetchall(
            """
            SELECT o.id, s.title AS service, o.shift_date, o.status, u.tg_id AS client_tg, u.username AS client_username,
                   o.address_title, o.section, o.floor, o.apartment, o.instructions,
                   e.tg_id AS executor_tg, e.username AS executor_username,
                   o.created_at, o.accepted_at, o.started_at, o.finished_at, o.fail_reason, o.comment, o.cancel_reason
            FROM orders o
            JOIN services s ON s.id = o.service_id
            JOIN users u ON u.id = o.user_id
            LEFT JOIN users e ON e.id = o.executor_id
            ORDER BY o.id
            """
        )

    async def payments(self) -> list[dict[str, Any]]:
        return await self.db.fetchall(
            """
            SELECT p.id, u.tg_id AS client_tg, u.username AS client_username, p.tariff_title, p.amount_rub,
                   p.duration_days, p.status, p.created_at, p.paid_marked_at, p.confirmed_at, p.confirmed_by_tg,
                   p.reject_reason, p.rejected_at, p.refund_reason, p.refunded_at, sub.starts_at AS subscription_start,
                   sub.ends_at AS subscription_end
            FROM payments p
            JOIN users u ON u.id = p.user_id
            LEFT JOIN subscriptions sub ON sub.payment_id = p.id
            ORDER BY p.id
            """
        )

    async def users(self) -> list[dict[str, Any]]:
        return await self.db.fetchall(
            """
            SELECT u.id, u.tg_id, u.username, u.role, u.status, u.registered_at,
                   a.title AS address, ua.section, ua.floor, ua.apartment,
                   (SELECT MAX(ends_at) FROM subscriptions s WHERE s.user_id = u.id AND s.status != 'cancelled')
                       AS subscription_until,
                   (SELECT COUNT(*) FROM orders o WHERE o.user_id = u.id) AS orders_total
            FROM users u
            LEFT JOIN user_addresses ua ON ua.user_id = u.id AND ua.is_current = 1
            LEFT JOIN addresses a ON a.id = ua.address_id
            ORDER BY u.id
            """
        )


class PaymentStatsRepo(Repo):
    async def stats(self, since: datetime | None, until: datetime | None) -> dict[str, Any]:
        where, params = [], []
        if since:
            where.append("COALESCE(confirmed_at, rejected_at, created_at) >= ?")
            params.append(to_db_dt(since))
        if until:
            where.append("COALESCE(confirmed_at, rejected_at, created_at) < ?")
            params.append(to_db_dt(until))
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = await self.db.fetchall(
            f"SELECT status, COUNT(*) AS cnt, SUM(amount_rub) AS total FROM payments {clause} GROUP BY status",
            params,
        )
        return {r["status"]: (int(r["cnt"]), int(r["total"] or 0)) for r in rows}


class Repositories:
    """Набор всех репозиториев поверх одной БД."""

    def __init__(self, db: Database):
        self.db = db
        self.users = UserRepo(db)
        self.addresses = AddressRepo(db)
        self.user_addresses = UserAddressRepo(db)
        self.services = ServiceRepo(db)
        self.tariffs = TariffRepo(db)
        self.payments = PaymentRepo(db)
        self.subscriptions = SubscriptionRepo(db)
        self.orders = OrderRepo(db)
        self.settings = SettingsRepo(db)
        self.audit = AuditRepo(db)
        self.export = ExportRepo(db)
        self.payment_stats = PaymentStatsRepo(db)

    def transaction(self):
        return self.db.transaction()

