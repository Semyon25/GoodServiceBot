"""Фоновые задачи: напоминания о подписке и заявки без исполнителя."""
from __future__ import annotations

import asyncio
import logging

from bot.services import Services
from bot.telegram.notifier import Notifier

log = logging.getLogger(__name__)


async def run_once(services: Services, notifier: Notifier) -> None:
    for event in await services.subscriptions.process_due():
        await notifier.subscription_event(event)
    await notifier.unassigned_orders(await services.orders.mark_unassigned_alerts())


async def run_forever(services: Services, notifier: Notifier, interval: int) -> None:
    while True:
        try:
            await run_once(services, notifier)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — фоновая задача не должна останавливаться
            log.exception("Ошибка фоновой задачи")
            await notifier.internal_error(f"Фоновая задача: {type(exc).__name__}: {exc}", key="jobs")
        await asyncio.sleep(interval)
