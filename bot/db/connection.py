"""Подключение к SQLite.

Используется одно соединение в режиме autocommit. Все обращения сериализуются
через asyncio.Lock, а транзакция (BEGIN IMMEDIATE) удерживает блокировку до
COMMIT — поэтому чужие корутины не вклиниваются в середину транзакции.
"""
from __future__ import annotations

import asyncio
import contextvars
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Mapping

import aiosqlite

IntegrityError = sqlite3.IntegrityError


def to_db_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("naive datetime cannot be stored")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_db_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def to_db_date(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _params(params: Iterable[Any] | Mapping[str, Any]) -> tuple | Mapping[str, Any]:
    return params if isinstance(params, Mapping) else tuple(params)


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._in_tx: contextvars.ContextVar[bool] = contextvars.ContextVar(f"db_tx_{id(self)}", default=False)

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            await self._conn.execute("PRAGMA journal_mode = WAL")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    @asynccontextmanager
    async def _guard(self) -> AsyncIterator[None]:
        if self._in_tx.get():
            yield
        else:
            async with self._lock:
                yield

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        if self._in_tx.get():
            # вложенная транзакция — просто часть внешней
            yield
            return
        async with self._lock:
            token = self._in_tx.set(True)
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            else:
                await self.conn.execute("COMMIT")
            finally:
                self._in_tx.reset(token)

    async def execute(self, sql: str, params: Iterable[Any] | Mapping[str, Any] = ()) -> tuple[int | None, int]:
        """Выполняет запрос, возвращает (lastrowid, rowcount)."""
        async with self._guard():
            cur = await self.conn.execute(sql, _params(params))
            try:
                return cur.lastrowid, cur.rowcount
            finally:
                await cur.close()

    async def executescript(self, sql: str) -> None:
        async with self._guard():
            await self.conn.executescript(sql)

    async def fetchone(self, sql: str, params: Iterable[Any] | Mapping[str, Any] = ()) -> dict[str, Any] | None:
        async with self._guard():
            cur = await self.conn.execute(sql, _params(params))
            try:
                row = await cur.fetchone()
            finally:
                await cur.close()
        return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: Iterable[Any] | Mapping[str, Any] = ()) -> list[dict[str, Any]]:
        async with self._guard():
            cur = await self.conn.execute(sql, _params(params))
            try:
                rows = await cur.fetchall()
            finally:
                await cur.close()
        return [dict(r) for r in rows]

    async def fetchval(self, sql: str, params: Iterable[Any] | Mapping[str, Any] = ()) -> Any:
        row = await self.fetchone(sql, params)
        if row is None:
            return None
        return next(iter(row.values()))
