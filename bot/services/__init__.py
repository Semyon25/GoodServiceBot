"""Сборка всех сервисов бизнес-логики поверх репозиториев."""
from __future__ import annotations

from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from bot.db.repositories import Repositories

from .orders import OrderService
from .payments import PaymentService, SubscriptionService
from .reports import ReportService
from .settings import SettingsService
from .tariffs import CatalogService
from .users import AddressService, UserService


class Services:
    def __init__(
        self,
        repos: Repositories,
        admin_ids: frozenset[int],
        tz: ZoneInfo,
        clock: Callable[[], datetime],
    ):
        self.repos = repos
        self.tz = tz
        self.clock = clock
        self.settings = SettingsService(repos, tz, clock)
        self.users = UserService(repos, admin_ids, clock)
        self.addresses = AddressService(repos, clock)
        self.catalog = CatalogService(repos, clock)
        self.payments = PaymentService(repos, self.catalog, clock)
        self.subscriptions = SubscriptionService(repos, clock)
        self.orders = OrderService(repos, self.settings, self.catalog, clock)
        self.reports = ReportService(repos, tz, clock)
