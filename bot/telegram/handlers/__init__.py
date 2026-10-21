from aiogram import Router

from . import admin_common, admin_orders, admin_payments, admin_settings, admin_users, client, common, executor


def build_router() -> Router:
    """Порядок важен: общие команды → клиент → исполнитель → администратор → ввод значений → прочее."""
    root = Router(name="root")
    root.include_routers(
        common.errors_router,
        common.router,
        client.router,
        executor.router,
        admin_orders.router,
        admin_payments.router,
        admin_users.router,
        admin_settings.router,
        admin_common.router,
        common.fallback_router,
    )
    return root
