"""Ошибки предметной области. Текст ошибки предназначен для показа пользователю."""
from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Ожидаемая ошибка бизнес-правила; message показывается пользователю."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class AccessDenied(DomainError):
    def __init__(self, message: str = "Недостаточно прав для этого действия."):
        super().__init__(message)


class NotFound(DomainError):
    def __init__(self, message: str = "Запись не найдена."):
        super().__init__(message)


class InvalidTransition(DomainError):
    pass


class UserBlocked(DomainError):
    def __init__(self, message: str = "Ваш доступ к сервису ограничен. Обратитесь к администратору."):
        super().__init__(message)


class NoAddress(DomainError):
    def __init__(self, message: str = "Сначала укажите адрес в разделе «Мой адрес»."):
        super().__init__(message)


class NoSubscription(DomainError):
    def __init__(self, message: str = "У вас нет действующей подписки. Оформите её в разделе «Тарифы и оплата»."):
        super().__init__(message)


class OutsideSchedule(DomainError):
    pass


class AlreadyOrdered(DomainError):
    def __init__(self, order: Any, message: str = "Вы уже заказали вынос мусора — повторно заказывать не нужно."):
        super().__init__(message)
        self.order = order


class PaymentPending(DomainError):
    def __init__(self, payment: Any, message: str):
        super().__init__(message)
        self.payment = payment


class ValidationError(DomainError):
    pass


class InternalStateError(Exception):
    """Нарушение внутренней согласованности данных — сообщается администраторам."""
