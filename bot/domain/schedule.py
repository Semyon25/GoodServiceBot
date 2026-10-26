"""Расписание выноса мусора.

Заказать вынос можно в любое время. Мусор забирают в окно выноса: вынос с датой D
начинается в pickup_start дня D и заканчивается в pickup_end (если это время не больше
времени начала — на следующие сутки, например 19:00 → 02:00). Поэтому 00:30 субботы
относится к выносу пятницы.

Заявка попадает в ближайший вынос, приём заявок на который ещё не закрыт (order_cutoff,
например 01:00) — так исполнитель успевает выполнить заявки до конца окна.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ALL_DAYS = (0, 1, 2, 3, 4, 5, 6)
WEEKDAY_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


@dataclass(frozen=True)
class ScheduleConfig:
    work_days: frozenset[int]
    pickup_start: time
    pickup_end: time
    order_cutoff: time
    tz: ZoneInfo

    @classmethod
    def from_settings(cls, data: dict, tz: ZoneInfo) -> "ScheduleConfig":
        return cls(
            work_days=frozenset(int(d) for d in data.get("work_days", ALL_DAYS)),
            pickup_start=parse_hhmm(data.get("pickup_start", "19:00")),
            pickup_end=parse_hhmm(data.get("pickup_end", "02:00")),
            order_cutoff=parse_hhmm(data.get("order_cutoff", "01:00")),
            tz=tz,
        )


@dataclass(frozen=True)
class Shift:
    """Один вынос (окно забора мусора)."""

    shift_date: date
    starts_at: datetime
    cutoff_at: datetime
    ends_at: datetime


def parse_hhmm(value: str) -> time:
    try:
        hh, mm = value.strip().split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Время должно быть в формате ЧЧ:ММ, получено: {value!r}") from exc


def format_hhmm(value: time) -> str:
    return value.strftime("%H:%M")


def _after_start(d: date, t: time, start: time, tz: ZoneInfo) -> datetime:
    day = d if t > start else d + timedelta(days=1)
    return datetime.combine(day, t, tzinfo=tz)


def shift_bounds(d: date, cfg: ScheduleConfig) -> Shift:
    return Shift(
        shift_date=d,
        starts_at=datetime.combine(d, cfg.pickup_start, tzinfo=cfg.tz),
        cutoff_at=_after_start(d, cfg.order_cutoff, cfg.pickup_start, cfg.tz),
        ends_at=_after_start(d, cfg.pickup_end, cfg.pickup_start, cfg.tz),
    )


def validate_schedule(cfg: ScheduleConfig) -> None:
    if not cfg.work_days:
        raise ValueError("Должен быть выбран хотя бы один день выноса.")
    probe = shift_bounds(date(2024, 1, 1), cfg)
    if probe.ends_at - probe.starts_at > timedelta(hours=24):
        raise ValueError("Окно выноса не может длиться больше суток.")
    if not probe.starts_at < probe.cutoff_at <= probe.ends_at:
        raise ValueError(
            "Время приёма заявок на текущий вынос должно быть после начала и не позже окончания выноса."
        )


def current_shift(now: datetime, cfg: ScheduleConfig) -> Shift | None:
    """Вынос, окно которого идёт сейчас."""
    local = now.astimezone(cfg.tz)
    for d in (local.date(), local.date() - timedelta(days=1)):
        if d.weekday() not in cfg.work_days:
            continue
        shift = shift_bounds(d, cfg)
        if shift.starts_at <= local < shift.ends_at:
            return shift
    return None


def order_shift(now: datetime, cfg: ScheduleConfig) -> Shift | None:
    """Ближайший вынос, в который попадёт заявка, оформленная в момент now."""
    local = now.astimezone(cfg.tz)
    for i in range(-1, 8):
        d = local.date() + timedelta(days=i)
        if d.weekday() not in cfg.work_days:
            continue
        shift = shift_bounds(d, cfg)
        if local < shift.cutoff_at:
            return shift
    return None


def last_shift_date(now: datetime, cfg: ScheduleConfig) -> date | None:
    """Текущий вынос, а если его нет — последний завершившийся."""
    shift = current_shift(now, cfg)
    if shift:
        return shift.shift_date
    local = now.astimezone(cfg.tz)
    for i in range(0, 8):
        d = local.date() - timedelta(days=i)
        if d.weekday() in cfg.work_days and shift_bounds(d, cfg).ends_at <= local:
            return d
    return None


def pickup_days_text(cfg: ScheduleConfig) -> str:
    days = sorted(cfg.work_days)
    if days == list(ALL_DAYS):
        return "ежедневно"
    if days == [0, 1, 2, 3, 4]:
        return "по будням"
    if days == [5, 6]:
        return "по выходным"
    return "по дням: " + ", ".join(WEEKDAY_SHORT[d] for d in days)


def pickup_time_text(cfg: ScheduleConfig) -> str:
    return f"с {format_hhmm(cfg.pickup_start)} до {format_hhmm(cfg.pickup_end)}"


def pickup_when_text(shift_date: date, now: datetime, cfg: ScheduleConfig) -> str:
    """Когда заберут мусор по заявке: «сегодня с 19:00 до 02:00», «этой ночью до 02:00» и т.п."""
    today = now.astimezone(cfg.tz).date()
    start, end = format_hhmm(cfg.pickup_start), format_hhmm(cfg.pickup_end)
    if shift_date == today:
        return f"сегодня с {start} до {end}"
    if shift_date == today - timedelta(days=1) and now < shift_bounds(shift_date, cfg).ends_at:
        return f"этой ночью до {end}"
    if shift_date == today + timedelta(days=1):
        return f"завтра с {start} до {end}"
    return f"{shift_date:%d.%m} ({WEEKDAY_SHORT[shift_date.weekday()]}) с {start} до {end}"
