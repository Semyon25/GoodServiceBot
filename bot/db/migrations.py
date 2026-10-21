"""Версионирование схемы БД.

Текущая версия схемы хранится в PRAGMA user_version. При запуске применяются
все миграции с номером больше текущего, каждая — в отдельной транзакции.
Чтобы изменить схему, добавьте в MIGRATIONS новую запись со следующим номером;
уже выпущенные миграции не редактируются.
"""
from __future__ import annotations

import json
import logging

from bot.domain.transitions import ORDER_TRANSITIONS, PAYMENT_TRANSITIONS

from .connection import Database

log = logging.getLogger(__name__)

NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')"


def _seed_settings() -> str:
    settings = {
        "schedule.work_days": [0, 1, 2, 3, 4],
        "schedule.accept_start": "19:00",
        "schedule.accept_end": "01:00",
        "schedule.work_end": "02:00",
        "payment.sbp_phone": "+7 900 000-00-00",
        "payment.sbp_bank": "Укажите банк получателя",
        "payment.sbp_recipient": "Укажите имя получателя",
        "payment.instructions": (
            "Откройте приложение своего банка, выберите перевод по номеру телефона (СБП), "
            "укажите номер и банк получателя и переведите точную сумму. "
            "После перевода нажмите «Я оплатил» и пришлите чек или скриншот операции."
        ),
        "orders.distribution_mode": "manual",
        "orders.unassigned_alert_minutes": 15,
        "texts.welcome": (
            "Здравствуйте! Это сервис выноса бытового мусора в ЖК «Заречье Парк».\n\n"
            "Оформите подписку, выставьте пакет у двери квартиры и нажмите кнопку — "
            "исполнитель заберёт его в рабочую смену (по будням с 19:00 до 02:00)."
        ),
        "texts.help": (
            "Как это работает:\n"
            "1. Укажите адрес в разделе «Мой адрес».\n"
            "2. Выберите тариф в разделе «Тарифы и оплата» и оплатите его переводом через СБП.\n"
            "3. После подтверждения оплаты администратором подписка станет активной.\n"
            "4. В рабочую смену выставьте пакет у двери и нажмите «Заказать вынос мусора».\n\n"
            "Одна заявка — одна смена. Заявки принимаются по будням с 19:00 до 01:00."
        ),
    }
    rows = ",\n".join(
        f"('{key}', '{json.dumps(value, ensure_ascii=False).replace(chr(39), chr(39) * 2)}', {NOW_SQL})"
        for key, value in settings.items()
    )
    return f"INSERT INTO system_settings (key, value, updated_at) VALUES\n{rows};"


def _seed_transitions(table: str, transitions) -> str:
    rows = ",\n".join(f"('{a.value}', '{b.value}')" for a, b in sorted(transitions))
    return f"INSERT INTO {table} (from_status, to_status) VALUES\n{rows};"


MIGRATION_1 = f"""
CREATE TABLE users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id           INTEGER NOT NULL UNIQUE,
    username        TEXT,
    role            TEXT NOT NULL DEFAULT 'client' CHECK (role IN ('client', 'executor')),
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'blocked')),
    registered_at   TEXT NOT NULL
);
CREATE INDEX ix_users_username ON users (username COLLATE NOCASE);

CREATE TABLE addresses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL UNIQUE,
    sections_count  INTEGER NOT NULL CHECK (sections_count > 0),
    is_active       INTEGER NOT NULL DEFAULT 1,
    sort_order      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE user_addresses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users (id),
    address_id      INTEGER NOT NULL REFERENCES addresses (id),
    section         INTEGER NOT NULL CHECK (section > 0),
    floor           INTEGER NOT NULL,
    apartment       TEXT NOT NULL,
    intercom_code   TEXT NOT NULL,
    instructions    TEXT,
    is_current      INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_user_addresses_current ON user_addresses (user_id) WHERE is_current = 1;

CREATE TRIGGER trg_user_addresses_section_ins BEFORE INSERT ON user_addresses
BEGIN
    SELECT RAISE(ABORT, 'section_out_of_range')
    WHERE NEW.section > (SELECT sections_count FROM addresses WHERE id = NEW.address_id);
END;
CREATE TRIGGER trg_user_addresses_section_upd BEFORE UPDATE OF section, address_id ON user_addresses
BEGIN
    SELECT RAISE(ABORT, 'section_out_of_range')
    WHERE NEW.section > (SELECT sections_count FROM addresses WHERE id = NEW.address_id);
END;

CREATE TABLE services (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    description     TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    schedule_json   TEXT,
    rules_json      TEXT NOT NULL DEFAULT '{{}}'
);

CREATE TABLE tariffs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id      INTEGER NOT NULL REFERENCES services (id),
    title           TEXT NOT NULL,
    price_rub       INTEGER NOT NULL CHECK (price_rub > 0),
    duration_days   INTEGER NOT NULL CHECK (duration_days > 0),
    is_active       INTEGER NOT NULL DEFAULT 1,
    sort_order      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE payment_transitions (
    from_status TEXT NOT NULL,
    to_status   TEXT NOT NULL,
    PRIMARY KEY (from_status, to_status)
);

CREATE TABLE payments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users (id),
    service_id       INTEGER NOT NULL REFERENCES services (id),
    tariff_id        INTEGER NOT NULL REFERENCES tariffs (id),
    tariff_title     TEXT NOT NULL,
    amount_rub       INTEGER NOT NULL CHECK (amount_rub > 0),
    duration_days    INTEGER NOT NULL CHECK (duration_days > 0),
    status           TEXT NOT NULL CHECK (status IN
                        ('awaiting_payment', 'awaiting_confirmation', 'confirmed', 'rejected', 'refunded')),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    paid_marked_at   TEXT,
    confirmed_at     TEXT,
    confirmed_by_tg  INTEGER,
    proof_type       TEXT,
    proof_file_id    TEXT,
    proof_caption    TEXT,
    reject_reason    TEXT,
    rejected_at      TEXT,
    rejected_by_tg   INTEGER,
    refund_reason    TEXT,
    refunded_at      TEXT,
    refunded_by_tg   INTEGER
);
CREATE INDEX ix_payments_user ON payments (user_id, status);
CREATE INDEX ix_payments_status ON payments (status, created_at);

CREATE TRIGGER trg_payments_status_transition BEFORE UPDATE OF status ON payments
WHEN OLD.status != NEW.status AND NOT EXISTS (
    SELECT 1 FROM payment_transitions WHERE from_status = OLD.status AND to_status = NEW.status)
BEGIN
    SELECT RAISE(ABORT, 'invalid_payment_transition');
END;

CREATE TABLE payment_status_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id  INTEGER NOT NULL REFERENCES payments (id),
    old_status  TEXT,
    new_status  TEXT NOT NULL,
    actor_tg    INTEGER,
    created_at  TEXT NOT NULL,
    comment     TEXT
);
CREATE INDEX ix_payment_history ON payment_status_history (payment_id);

CREATE TABLE subscriptions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users (id),
    service_id       INTEGER NOT NULL REFERENCES services (id),
    tariff_id        INTEGER NOT NULL REFERENCES tariffs (id),
    payment_id       INTEGER NOT NULL UNIQUE REFERENCES payments (id),
    tariff_title     TEXT NOT NULL,
    amount_rub       INTEGER NOT NULL,
    starts_at        TEXT NOT NULL,
    ends_at          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'expired', 'cancelled')),
    created_at       TEXT NOT NULL,
    reminder_sent    INTEGER NOT NULL DEFAULT 0,
    expiry_notified  INTEGER NOT NULL DEFAULT 0,
    CHECK (ends_at > starts_at)
);
CREATE INDEX ix_subscriptions_user ON subscriptions (user_id, service_id, ends_at);

CREATE TRIGGER trg_subscriptions_payment_check BEFORE INSERT ON subscriptions
BEGIN
    SELECT RAISE(ABORT, 'payment_not_confirmed')
    WHERE (SELECT status FROM payments WHERE id = NEW.payment_id) IS NOT 'confirmed';
    SELECT RAISE(ABORT, 'subscription_amount_mismatch')
    WHERE NEW.amount_rub != (SELECT amount_rub FROM payments WHERE id = NEW.payment_id);
END;

CREATE TABLE order_transitions (
    from_status TEXT NOT NULL,
    to_status   TEXT NOT NULL,
    PRIMARY KEY (from_status, to_status)
);

CREATE TABLE orders (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id             INTEGER NOT NULL REFERENCES services (id),
    user_id                INTEGER NOT NULL REFERENCES users (id),
    user_address_id        INTEGER NOT NULL REFERENCES user_addresses (id),
    shift_date             TEXT NOT NULL,
    attempt                INTEGER NOT NULL DEFAULT 0,
    status                 TEXT NOT NULL CHECK (status IN
                              ('new', 'assigned', 'accepted', 'in_progress', 'done', 'failed', 'cancelled')),
    executor_id            INTEGER REFERENCES users (id),
    address_id             INTEGER NOT NULL REFERENCES addresses (id),
    address_title          TEXT NOT NULL,
    section                INTEGER NOT NULL,
    floor                  INTEGER NOT NULL,
    apartment              TEXT NOT NULL,
    intercom_code          TEXT NOT NULL,
    instructions           TEXT,
    created_at             TEXT NOT NULL,
    assigned_at            TEXT,
    accepted_at            TEXT,
    started_at             TEXT,
    finished_at            TEXT,
    fail_reason            TEXT,
    comment                TEXT,
    cancel_reason          TEXT,
    unassigned_alert_sent  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, service_id, shift_date, attempt)
);
CREATE INDEX ix_orders_shift ON orders (shift_date, status);
CREATE INDEX ix_orders_executor ON orders (executor_id, status);

CREATE TRIGGER trg_orders_status_transition BEFORE UPDATE OF status ON orders
WHEN OLD.status != NEW.status AND NOT EXISTS (
    SELECT 1 FROM order_transitions WHERE from_status = OLD.status AND to_status = NEW.status)
BEGIN
    SELECT RAISE(ABORT, 'invalid_order_transition');
END;

CREATE TABLE order_status_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL REFERENCES orders (id),
    old_status  TEXT,
    new_status  TEXT NOT NULL,
    actor_tg    INTEGER,
    created_at  TEXT NOT NULL,
    comment     TEXT
);
CREATE INDEX ix_order_history ON order_status_history (order_id);

CREATE TABLE order_exceptions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users (id),
    service_id     INTEGER NOT NULL REFERENCES services (id),
    shift_date     TEXT NOT NULL,
    granted_by_tg  INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    comment        TEXT
);
CREATE INDEX ix_order_exceptions ON order_exceptions (user_id, service_id, shift_date);

CREATE TABLE system_settings (
    key            TEXT PRIMARY KEY,
    value          TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    updated_by_tg  INTEGER
);

CREATE TABLE admin_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_tg    INTEGER NOT NULL,
    action      TEXT NOT NULL,
    details     TEXT,
    created_at  TEXT NOT NULL
);

INSERT INTO addresses (title, sections_count, sort_order) VALUES
    ('Торговая, 6', 8, 1),
    ('Луговая, 6к1', 10, 2),
    ('Луговая, 3', 11, 3);

INSERT INTO services (code, title, description, is_active, rules_json) VALUES
    ('trash', 'Вынос мусора', 'Вынос бытового мусора от двери квартиры', 1,
     '{{"max_orders_per_shift": 1, "requires_subscription": true}}');

INSERT INTO tariffs (service_id, title, price_rub, duration_days, sort_order) VALUES
    ((SELECT id FROM services WHERE code = 'trash'), 'Неделя', 200, 7, 1),
    ((SELECT id FROM services WHERE code = 'trash'), 'Месяц', 600, 30, 2);

{_seed_transitions("order_transitions", ORDER_TRANSITIONS)}
{_seed_transitions("payment_transitions", PAYMENT_TRANSITIONS)}
{_seed_settings()}
"""

# (версия, SQL). Новые миграции добавляются в конец списка.
MIGRATIONS: list[tuple[int, str]] = [
    (1, MIGRATION_1),
]


async def get_schema_version(db: Database) -> int:
    return int(await db.fetchval("PRAGMA user_version") or 0)


async def migrate(db: Database) -> int:
    current = await get_schema_version(db)
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        log.info("Применяется миграция схемы БД v%s", version)
        script = f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {int(version)};\nCOMMIT;"
        try:
            await db.executescript(script)
        except Exception:
            try:
                await db.execute("ROLLBACK")
            except Exception:  # транзакция могла уже завершиться
                pass
            raise
        current = version
    return current
