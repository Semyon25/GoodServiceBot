"""Расчёт рабочих смен.

Смена с датой D начинается в accept_start дня D. Приём заявок длится до accept_end,
выполнение — до work_end. Если время конца не больше времени начала, оно относится
к следующему календарному дню (19:00 → 01:00 → 02:00). Поэтому 00:30 субботы
относится к пятничной смене.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DEFAULT_WORK_DAYS = (0, 1, 2, 3, 4)


@dataclass(frozen=True)
class ScheduleConfig:
    work_days: frozenset[int]
    accept_start: time
    accept_end: time
    work_end: time
    tz: ZoneInfo

    @classmethod
    def from_settings(cls, data: dict, tz: ZoneInfo) -> "ScheduleConfig":
        return cls(
            work_days=frozenset(int(d) for d in data.get("work_days", DEFAULT_WORK_DAYS)),
            accept_start=parse_hhmm(data.get("accept_start", "19:00")),
            accept_end=parse_hhmm(data.get("accept_end", "01:00")),
            work_end=parse_hhmm(data.get("work_end", "02:00")),
            tz=tz,
        )


@dataclass(frozen=True)
class Shift:
    shift_date: date
    starts_at: datetime
    accept_until: datetime
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
    starts_at = datetime.combine(d, cfg.accept_start, tzinfo=cfg.tz)
    return Shift(
        shift_date=d,
        starts_at=starts_at,
        accept_until=_after_start(d, cfg.accept_end, cfg.accept_start, cfg.tz),
        ends_at=_after_start(d, cfg.work_end, cfg.accept_start, cfg.tz),
    )


def validate_schedule(cfg: ScheduleConfig) -> None:
    """Проверяет, что приём заканчивается не позже выполнения и смены не перекрываются."""
    if not cfg.work_days:
        raise ValueError("Должен быть выбран хотя бы один рабочий день.")
    probe = shift_bounds(date(2024, 1, 1), cfg)
    if probe.accept_until > probe.ends_at:
        raise ValueError("Окончание приёма заявок должно быть не позже окончания выполнения.")
    if probe.ends_at - probe.starts_at > timedelta(hours=24):
        raise ValueError("Смена не может длиться больше суток.")


def current_shift(now: datetime, cfg: ScheduleConfig) -> Shift | None:
    """Смена, в границах которой (от начала приёма до конца выполнения) находится now."""
    local = now.astimezone(cfg.tz)
    for d in (local.date(), local.date() - timedelta(days=1)):
        if d.weekday() not in cfg.work_days:
            continue
        shift = shift_bounds(d, cfg)
        if shift.starts_at <= local < shift.ends_at:
            return shift
    return None


def accepting_shift(now: datetime, cfg: ScheduleConfig) -> Shift | None:
    """Смена, в которую сейчас можно оформить заявку."""
    shift = current_shift(now, cfg)
    if shift and now.astimezone(cfg.tz) < shift.accept_until:
        return shift
    return None


def next_shift(now: datetime, cfg: ScheduleConfig) -> Shift | None:
    """Ближайшая смена, приём заявок в которую ещё не начался."""
    local = now.astimezone(cfg.tz)
    for i in range(0, 8):
        d = local.date() + timedelta(days=i)
        if d.weekday() not in cfg.work_days:
            continue
        shift = shift_bounds(d, cfg)
        if shift.starts_at > local:
            return shift
    return None


def last_shift_date(now: datetime, cfg: ScheduleConfig) -> date | None:
    """Текущая смена, а если её нет — последняя завершившаяся."""
    shift = current_shift(now, cfg)
    if shift:
        return shift.shift_date
    local = now.astimezone(cfg.tz)
    for i in range(0, 8):
        d = local.date() - timedelta(days=i)
        if d.weekday() in cfg.work_days and shift_bounds(d, cfg).ends_at <= local:
            return d
    return None
