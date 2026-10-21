"""Модели предметной области. Не зависят от способа хранения данных."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .enums import OrderStatus, PaymentStatus, Role, SubscriptionStatus, UserStatus


@dataclass
class User:
    id: int
    tg_id: int
    username: str | None
    role: str
    status: str
    registered_at: datetime

    @property
    def is_blocked(self) -> bool:
        return self.status == UserStatus.BLOCKED

    @property
    def is_executor(self) -> bool:
        return self.role == Role.EXECUTOR

    @property
    def display(self) -> str:
        if self.username:
            return f"@{self.username} ({self.tg_id})"
        return f"ID {self.tg_id}"


@dataclass
class Address:
    id: int
    title: str
    sections_count: int
    is_active: bool
    sort_order: int = 0


@dataclass
class UserAddress:
    id: int
    user_id: int
    address_id: int
    address_title: str
    section: int
    floor: int
    apartment: str
    intercom_code: str
    instructions: str | None
    is_current: bool
    created_at: datetime


@dataclass
class Service:
    id: int
    code: str
    title: str
    description: str | None
    is_active: bool
    schedule: dict | None = None  # переопределение глобального расписания, None — общее
    rules: dict = field(default_factory=dict)

    @property
    def max_orders_per_shift(self) -> int:
        return int(self.rules.get("max_orders_per_shift", 1))


@dataclass
class Tariff:
    id: int
    service_id: int
    title: str
    price_rub: int
    duration_days: int
    is_active: bool
    sort_order: int = 0


@dataclass
class Payment:
    id: int
    user_id: int
    service_id: int
    tariff_id: int
    tariff_title: str
    amount_rub: int
    duration_days: int
    status: str
    created_at: datetime
    updated_at: datetime
    paid_marked_at: datetime | None
    confirmed_at: datetime | None
    confirmed_by_tg: int | None
    proof_type: str | None
    proof_file_id: str | None
    proof_caption: str | None
    reject_reason: str | None
    rejected_at: datetime | None
    rejected_by_tg: int | None
    refund_reason: str | None
    refunded_at: datetime | None
    refunded_by_tg: int | None

    @property
    def status_enum(self) -> PaymentStatus:
        return PaymentStatus(self.status)


@dataclass
class Subscription:
    id: int
    user_id: int
    service_id: int
    tariff_id: int
    payment_id: int
    tariff_title: str
    amount_rub: int
    starts_at: datetime
    ends_at: datetime
    status: str
    created_at: datetime
    reminder_sent: bool
    expiry_notified: bool

    def is_active_at(self, moment: datetime) -> bool:
        return self.status != SubscriptionStatus.CANCELLED and self.starts_at <= moment < self.ends_at


@dataclass
class Order:
    id: int
    service_id: int
    user_id: int
    user_address_id: int
    shift_date: date
    attempt: int
    status: str
    executor_id: int | None
    address_id: int
    address_title: str
    section: int
    floor: int
    apartment: str
    intercom_code: str
    instructions: str | None
    created_at: datetime
    assigned_at: datetime | None
    accepted_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    fail_reason: str | None
    comment: str | None
    cancel_reason: str | None
    unassigned_alert_sent: bool

    @property
    def status_enum(self) -> OrderStatus:
        return OrderStatus(self.status)


@dataclass
class StatusHistoryEntry:
    id: int
    entity_id: int
    old_status: str | None
    new_status: str
    actor_tg: int | None
    created_at: datetime
    comment: str | None


@dataclass
class Actor:
    """Тот, кто выполняет действие: пользователь и признак администратора (из конфигурации)."""

    user: User
    is_admin: bool

    @property
    def tg_id(self) -> int:
        return self.user.tg_id
