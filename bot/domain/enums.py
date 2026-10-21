"""Перечисления предметной области: роли, статусы, причины."""
from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    CLIENT = "client"
    EXECUTOR = "executor"


class UserStatus(StrEnum):
    ACTIVE = "active"
    BLOCKED = "blocked"


class PaymentStatus(StrEnum):
    AWAITING_PAYMENT = "awaiting_payment"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    REFUNDED = "refunded"


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class OrderStatus(StrEnum):
    NEW = "new"
    ASSIGNED = "assigned"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FailReason(StrEnum):
    NO_BAG = "no_bag"
    TOO_BIG = "too_big"
    NO_ACCESS = "no_access"
    WRONG_CODE = "wrong_code"
    OTHER = "other"


class DistributionMode(StrEnum):
    MANUAL = "manual"  # заявки распределяет администратор
    POOL = "pool"  # новые заявки видят все исполнители, первый принявший забирает


ROLE_TITLES = {
    Role.CLIENT: "Клиент",
    Role.EXECUTOR: "Исполнитель",
}

USER_STATUS_TITLES = {
    UserStatus.ACTIVE: "Активен",
    UserStatus.BLOCKED: "Заблокирован",
}

PAYMENT_STATUS_TITLES = {
    PaymentStatus.AWAITING_PAYMENT: "Ожидает оплаты",
    PaymentStatus.AWAITING_CONFIRMATION: "Ожидает подтверждения",
    PaymentStatus.CONFIRMED: "Подтверждён",
    PaymentStatus.REJECTED: "Отклонён",
    PaymentStatus.REFUNDED: "Возвращён",
}

SUBSCRIPTION_STATUS_TITLES = {
    SubscriptionStatus.ACTIVE: "Действует",
    SubscriptionStatus.EXPIRED: "Истекла",
    SubscriptionStatus.CANCELLED: "Аннулирована",
}

ORDER_STATUS_TITLES = {
    OrderStatus.NEW: "Новый",
    OrderStatus.ASSIGNED: "Назначен",
    OrderStatus.ACCEPTED: "Принят",
    OrderStatus.IN_PROGRESS: "В работе",
    OrderStatus.DONE: "Выполнен",
    OrderStatus.FAILED: "Не удалось выполнить",
    OrderStatus.CANCELLED: "Отменён",
}

FAIL_REASON_TITLES = {
    FailReason.NO_BAG: "Пакет отсутствует у двери",
    FailReason.TOO_BIG: "Пакет превышает допустимые ограничения",
    FailReason.NO_ACCESS: "Нет доступа в подъезд",
    FailReason.WRONG_CODE: "Неверный код домофона",
    FailReason.OTHER: "Другая причина",
}

DISTRIBUTION_MODE_TITLES = {
    DistributionMode.MANUAL: "Назначает администратор",
    DistributionMode.POOL: "Общий пул (первый принявший)",
}

WEEKDAY_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
