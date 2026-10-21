"""Проверки прав. Вызываются в сервисах при каждом действии, а не только скрытием кнопок."""
from __future__ import annotations

from bot.domain.errors import AccessDenied, UserBlocked
from bot.domain.models import Actor


def require_admin(actor: Actor) -> None:
    if not actor.is_admin:
        raise AccessDenied()


def require_active(actor: Actor) -> None:
    if actor.user.is_blocked and not actor.is_admin:
        raise UserBlocked()


def require_executor(actor: Actor) -> None:
    require_active(actor)
    if not actor.user.is_executor:
        raise AccessDenied("Действие доступно только исполнителям.")
