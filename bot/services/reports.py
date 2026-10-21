"""Статистика и выгрузка CSV для администратора."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from bot.db.connection import from_db_dt
from bot.db.repositories import Repositories
from bot.domain.enums import (
    FAIL_REASON_TITLES,
    ORDER_STATUS_TITLES,
    PAYMENT_STATUS_TITLES,
    ROLE_TITLES,
    USER_STATUS_TITLES,
    OrderStatus,
    PaymentStatus,
)
from bot.domain.models import Actor

from .access import require_admin

PERIODS = {
    "today": "Сегодня",
    "7d": "7 дней",
    "30d": "30 дней",
    "all": "Всё время",
}


@dataclass
class Stats:
    period_title: str
    orders_total: int = 0
    orders_by_status: dict[str, int] = field(default_factory=dict)
    by_executor: list[tuple[str, int, int]] = field(default_factory=list)  # (исполнитель, выполнено, всего)
    subscriptions: dict[str, int] = field(default_factory=dict)
    payments: dict[str, tuple[int, int]] = field(default_factory=dict)  # статус → (кол-во, сумма)


class ReportService:
    def __init__(self, repos: Repositories, tz: ZoneInfo, clock: Callable[[], datetime]):
        self.repos = repos
        self.tz = tz
        self._now = clock

    def period_bounds(self, period: str) -> tuple[datetime | None, datetime | None]:
        now = self._now().astimezone(self.tz)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if period == "today":
            return start_of_day, None
        if period == "7d":
            return start_of_day - timedelta(days=6), None
        if period == "30d":
            return start_of_day - timedelta(days=29), None
        return None, None

    async def stats(self, actor: Actor, period: str) -> Stats:
        require_admin(actor)
        since, until = self.period_bounds(period)
        result = Stats(period_title=PERIODS.get(period, period))
        order_stats = await self.repos.orders.stats(since, until)
        result.orders_by_status = order_stats["by_status"]
        result.orders_total = sum(result.orders_by_status.values())

        per_executor: dict[int, list[int]] = {}
        for row in order_stats["by_executor"]:
            done_total = per_executor.setdefault(row["executor_id"], [0, 0])
            done_total[1] += row["cnt"]
            if row["status"] == OrderStatus.DONE:
                done_total[0] += row["cnt"]
        users = await self.repos.users.get_many(per_executor.keys())
        result.by_executor = sorted(
            ((users[eid].display if eid in users else f"#{eid}", d, t) for eid, (d, t) in per_executor.items()),
            key=lambda x: -x[2],
        )
        result.subscriptions = await self.repos.subscriptions.count_by_state(self._now())
        result.payments = await self.repos.payment_stats.stats(since, until)
        return result

    async def export_csv(self, actor: Actor, kind: str) -> tuple[str, bytes]:
        require_admin(actor)
        if kind == "orders":
            rows = await self.repos.export.orders()
            for r in rows:
                r["status"] = ORDER_STATUS_TITLES[OrderStatus(r["status"])]
                if r["fail_reason"]:
                    r["fail_reason"] = FAIL_REASON_TITLES.get(r["fail_reason"], r["fail_reason"])
        elif kind == "payments":
            rows = await self.repos.export.payments()
            for r in rows:
                r["status"] = PAYMENT_STATUS_TITLES[PaymentStatus(r["status"])]
        elif kind == "users":
            rows = await self.repos.export.users()
            for r in rows:
                r["role"] = ROLE_TITLES.get(r["role"], r["role"])
                r["status"] = USER_STATUS_TITLES.get(r["status"], r["status"])
        else:
            raise ValueError(kind)
        for r in rows:
            for key, value in r.items():
                if isinstance(value, str) and key.endswith(("_at", "_start", "_end", "_until")):
                    r[key] = self._local(value)
        stamp = self._now().astimezone(self.tz).strftime("%Y%m%d_%H%M")
        return f"{kind}_{stamp}.csv", self._to_csv(rows)

    def _local(self, value: str) -> str:
        try:
            dt = from_db_dt(value)
        except ValueError:
            return value
        return dt.astimezone(self.tz).strftime("%Y-%m-%d %H:%M:%S") if dt else ""

    @staticmethod
    def _to_csv(rows: list[dict]) -> bytes:
        buf = io.StringIO()
        if rows:
            writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), delimiter=";")
            writer.writeheader()
            writer.writerows(rows)
        else:
            buf.write("нет данных\n")
        # BOM — чтобы Excel корректно открыл кириллицу
        return buf.getvalue().encode("utf-8-sig")
