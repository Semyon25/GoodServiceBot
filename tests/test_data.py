"""Схема БД, адреса, настройки, расширяемость услуг и сохранность данных."""
import pytest

from bot.db.connection import Database
from bot.db.migrations import MIGRATIONS, get_schema_version, migrate
from bot.db.repositories import Repositories
from bot.domain.errors import ValidationError
from bot.services import Services

from .conftest import ADMIN_TG, MSK, FakeClock, msk


async def test_migration_2_defaults(env):
    settings = await env.repos.settings.get_all()
    assert settings["schedule.work_days"] == [0, 1, 2, 3, 4, 5, 6]
    assert (settings["schedule.pickup_start"], settings["schedule.pickup_end"], settings["schedule.order_cutoff"]) == (
        "19:00", "02:00", "01:00",
    )
    assert "schedule.accept_start" not in settings
    welcome = await env.services.settings.text("texts.welcome")
    assert "Здравствуйте" not in welcome and "ежедневно с 19:00 до 02:00" in welcome
    assert "{дни}" in settings["texts.help"]


async def test_initial_addresses(env):
    addresses = {a.title: a.sections_count for a in await env.services.addresses.list_addresses()}
    assert addresses == {"Торговая, 6": 8, "Луговая, 6к1": 10, "Луговая, 3": 11}


@pytest.mark.parametrize("title, max_section", [("Торговая, 6", 8), ("Луговая, 6к1", 10), ("Луговая, 3", 11)])
async def test_section_limits(env, title, max_section):
    client = await env.actor(10)
    address = next(a for a in await env.services.addresses.list_addresses() if a.title == title)
    saved = await env.services.addresses.save_user_address(
        client, client.user.id, address.id, max_section, 1, "1", "1", None
    )
    assert saved.section == max_section
    with pytest.raises(ValidationError):
        await env.services.addresses.save_user_address(
            client, client.user.id, address.id, max_section + 1, 1, "1", "1", None
        )
    # ограничение действует и на уровне БД
    with pytest.raises(Exception, match="section_out_of_range"):
        await env.db.execute(
            "INSERT INTO user_addresses (user_id, address_id, section, floor, apartment, intercom_code, created_at)"
            " VALUES (?, ?, ?, 1, '1', '1', '2026-01-01T00:00:00+00:00')",
            (client.user.id, address.id, max_section + 1),
        )


async def test_client_cannot_edit_foreign_address(env):
    from bot.domain.errors import AccessDenied

    client = await env.actor(10)
    other = await env.actor(11)
    address = (await env.services.addresses.list_addresses())[0]
    with pytest.raises(AccessDenied):
        await env.services.addresses.save_user_address(other, client.user.id, address.id, 1, 1, "1", "1", None)


async def test_user_without_name_and_username(env):
    actor = await env.services.users.actor(777, None)
    assert actor.user.username is None and actor.user.role == "client"
    assert actor.user.display == "ID 777"


async def test_admin_from_config_only(env):
    assert (await env.services.users.actor(ADMIN_TG, None)).is_admin
    assert not (await env.services.users.actor(5, None)).is_admin


async def test_schedule_settings_validation(env):
    admin = await env.admin()
    with pytest.raises(ValidationError):
        await env.services.settings.set_schedule_time(admin, "order_cutoff", "25:00")
    with pytest.raises(ValidationError):
        await env.services.settings.set_schedule_time(admin, "order_cutoff", "03:00")  # позже конца выноса
    await env.services.settings.set_schedule_time(admin, "pickup_start", "18:00")
    assert (await env.services.settings.schedule()).pickup_start.hour == 18
    await env.services.settings.toggle_work_day(admin, 5)
    assert 5 not in (await env.services.settings.schedule()).work_days
    assert "18:00" in await env.services.settings.text("texts.help")


async def test_upgrade_from_v1_keeps_data_and_admin_texts(tmp_path, monkeypatch):
    """База, созданная прошлой версией бота, обновляется до текущей без потери данных."""
    import bot.db.migrations as migrations

    db = Database(tmp_path / "old.sqlite3")
    await db.connect()
    monkeypatch.setattr(migrations, "MIGRATIONS", MIGRATIONS[:1])
    assert await migrations.migrate(db) == 1
    await db.execute("UPDATE system_settings SET value = '\"Свой текст помощи\"' WHERE key = 'texts.help'")
    await db.execute("UPDATE system_settings SET value = '\"20:00\"' WHERE key = 'schedule.accept_start'")
    await db.execute(
        "INSERT INTO users (tg_id, username, registered_at) VALUES (5, 'old', '2026-10-20T10:00:00+00:00')"
    )

    monkeypatch.setattr(migrations, "MIGRATIONS", MIGRATIONS)
    assert await migrations.migrate(db) == MIGRATIONS[-1][0]
    settings = await Repositories(db).settings.get_all()
    assert settings["schedule.pickup_start"] == "20:00"  # настройка администратора перенесена
    assert settings["texts.help"] == "Свой текст помощи"  # отредактированный текст не тронут
    assert "Здравствуйте" not in settings["texts.welcome"]
    prices = {r["title"]: r["price_rub"] for r in await db.fetchall("SELECT title, price_rub FROM tariffs")}
    assert prices == {"Неделя": 299, "Месяц": 999}
    assert await db.fetchval("SELECT username FROM users WHERE tg_id = 5") == "old"
    await db.close()


async def test_migrations_are_idempotent_and_data_persists(tmp_path):
    path = tmp_path / "persist.sqlite3"
    clock = FakeClock(msk(2026, 10, 21, 20, 0))

    db = Database(path)
    await db.connect()
    assert await migrate(db) == MIGRATIONS[-1][0]
    services = Services(Repositories(db), frozenset({ADMIN_TG}), MSK, clock)
    actor = await services.users.actor(42, "persist")
    address = (await services.addresses.list_addresses())[0]
    await services.addresses.save_user_address(actor, actor.user.id, address.id, 1, 2, "3", "4", "у лифта")
    await db.close()

    db = Database(path)
    await db.connect()
    assert await migrate(db) == MIGRATIONS[-1][0]  # повторный запуск ничего не ломает
    assert await get_schema_version(db) == MIGRATIONS[-1][0]
    services = Services(Repositories(db), frozenset({ADMIN_TG}), MSK, clock)
    user = await services.repos.users.get_by_tg(42)
    assert user.username == "persist"
    saved = await services.addresses.current(user.id)
    assert saved.instructions == "у лифта"
    assert await db.fetchval("SELECT COUNT(*) FROM addresses") == 3
    await db.close()


async def test_new_service_can_be_added_without_code_changes(env):
    """Новая услуга добавляется записью в БД и сразу работает через общие механизмы."""
    repos = env.repos
    service_id = await repos.services.create(
        "dog_walking", "Выгул собак", "Прогулка с собакой", {"max_orders_per_shift": 2}, None
    )
    tariff_id = await repos.tariffs.create(service_id, "Неделя прогулок", 1500, 7)
    service = await env.services.catalog.service("dog_walking")
    client = await env.client_with_address()

    payment = await env.services.payments.select_tariff(client, tariff_id)
    assert payment.service_id == service_id
    _, sub = await env.services.payments.confirm(await env.admin(), payment.id)
    assert sub.service_id == service_id

    # подписка на одну услугу не даёт права на другую
    from bot.domain.errors import NoSubscription

    with pytest.raises(NoSubscription):
        await env.services.orders.create_order(client, await env.service())

    first = await env.services.orders.create_order(client, service)
    second = await env.services.orders.create_order(client, service)  # правило услуги: 2 заявки в смену
    assert first.service_id == second.service_id == service_id
