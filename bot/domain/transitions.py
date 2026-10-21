"""Допустимые переходы статусов заказа и платежа.

Те же таблицы переходов записываются в БД миграцией и проверяются триггерами,
так что нарушить их нельзя ни из кода, ни прямым UPDATE.
"""
from __future__ import annotations

from .enums import OrderStatus as O
from .enums import PaymentStatus as P

ORDER_TRANSITIONS: frozenset[tuple[O, O]] = frozenset(
    {
        (O.NEW, O.ASSIGNED),
        (O.NEW, O.ACCEPTED),
        (O.ASSIGNED, O.ACCEPTED),
        (O.ACCEPTED, O.IN_PROGRESS),
        (O.ACCEPTED, O.DONE),
        (O.IN_PROGRESS, O.DONE),
        (O.ASSIGNED, O.FAILED),
        (O.ACCEPTED, O.FAILED),
        (O.IN_PROGRESS, O.FAILED),
        # переназначение исполнителя администратором
        (O.ASSIGNED, O.ASSIGNED),
        (O.ACCEPTED, O.ASSIGNED),
        # отмена администратором любого незавершённого заказа
        (O.NEW, O.CANCELLED),
        (O.ASSIGNED, O.CANCELLED),
        (O.ACCEPTED, O.CANCELLED),
        (O.IN_PROGRESS, O.CANCELLED),
    }
)

ORDER_FINAL_STATUSES = frozenset({O.DONE, O.FAILED, O.CANCELLED})
ORDER_ACTIVE_STATUSES = frozenset({O.NEW, O.ASSIGNED, O.ACCEPTED, O.IN_PROGRESS})

PAYMENT_TRANSITIONS: frozenset[tuple[P, P]] = frozenset(
    {
        (P.AWAITING_PAYMENT, P.AWAITING_CONFIRMATION),
        # администратор нашёл перевод, хотя клиент не нажал «Я оплатил»
        (P.AWAITING_PAYMENT, P.CONFIRMED),
        (P.AWAITING_PAYMENT, P.REJECTED),
        (P.AWAITING_CONFIRMATION, P.CONFIRMED),
        (P.AWAITING_CONFIRMATION, P.REJECTED),
        # исправление ошибочного отклонения после фактической проверки
        (P.REJECTED, P.CONFIRMED),
        (P.CONFIRMED, P.REFUNDED),
    }
)

PAYMENT_OPEN_STATUSES = frozenset({P.AWAITING_PAYMENT, P.AWAITING_CONFIRMATION})


def order_transition_allowed(old: O | str, new: O | str) -> bool:
    return (O(old), O(new)) in ORDER_TRANSITIONS


def payment_transition_allowed(old: P | str, new: P | str) -> bool:
    return (P(old), P(new)) in PAYMENT_TRANSITIONS
