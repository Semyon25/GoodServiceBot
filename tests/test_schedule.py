from datetime import date

import pytest

from bot.domain.schedule import (
    ScheduleConfig,
    accepting_shift,
    current_shift,
    last_shift_date,
    next_shift,
    validate_schedule,
)

from .conftest import MSK, msk

CFG = ScheduleConfig.from_settings(
    {"work_days": [0, 1, 2, 3, 4], "accept_start": "19:00", "accept_end": "01:00", "work_end": "02:00"}, MSK
)


@pytest.mark.parametrize(
    "moment, shift_date, accepting",
    [
        (msk(2026, 10, 21, 18, 59), None, False),  # среда до начала
        (msk(2026, 10, 21, 19, 0), date(2026, 10, 21), True),
        (msk(2026, 10, 21, 23, 59), date(2026, 10, 21), True),
        (msk(2026, 10, 22, 0, 30), date(2026, 10, 21), True),  # после полуночи — смена среды
        (msk(2026, 10, 22, 1, 0), date(2026, 10, 21), False),  # приём закрыт, выполнение идёт
        (msk(2026, 10, 22, 1, 59), date(2026, 10, 21), False),
        (msk(2026, 10, 22, 2, 0), None, False),  # смена закончилась
        (msk(2026, 10, 24, 0, 30), date(2026, 10, 23), True),  # суббота 00:30 — пятничная смена
        (msk(2026, 10, 24, 20, 0), None, False),  # суббота вечером — не работаем
        (msk(2026, 10, 25, 20, 0), None, False),  # воскресенье
        (msk(2026, 10, 26, 0, 30), None, False),  # ночь на понедельник — воскресной смены нет
        (msk(2026, 10, 26, 19, 30), date(2026, 10, 26), True),  # понедельник
    ],
)
def test_shift_detection(moment, shift_date, accepting):
    shift = current_shift(moment, CFG)
    assert (shift.shift_date if shift else None) == shift_date
    assert (accepting_shift(moment, CFG) is not None) == accepting


def test_utc_input_is_converted_to_moscow():
    from datetime import timezone

    moment = msk(2026, 10, 21, 20, 0).astimezone(timezone.utc)  # 17:00 UTC
    assert current_shift(moment, CFG).shift_date == date(2026, 10, 21)


def test_next_shift_skips_weekend():
    upcoming = next_shift(msk(2026, 10, 24, 12, 0), CFG)
    assert upcoming.shift_date == date(2026, 10, 26)
    assert upcoming.starts_at == msk(2026, 10, 26, 19, 0)


def test_last_shift_date_outside_shift():
    assert last_shift_date(msk(2026, 10, 24, 12, 0), CFG) == date(2026, 10, 23)
    assert last_shift_date(msk(2026, 10, 21, 21, 0), CFG) == date(2026, 10, 21)


def test_custom_same_day_schedule():
    cfg = ScheduleConfig.from_settings(
        {"work_days": [5], "accept_start": "10:00", "accept_end": "12:00", "work_end": "13:00"}, MSK
    )
    assert accepting_shift(msk(2026, 10, 24, 11, 0), cfg).shift_date == date(2026, 10, 24)
    assert accepting_shift(msk(2026, 10, 24, 12, 30), cfg) is None
    assert current_shift(msk(2026, 10, 24, 12, 30), cfg) is not None


def test_validation_rejects_accept_after_work_end():
    bad = ScheduleConfig.from_settings(
        {"work_days": [0], "accept_start": "19:00", "accept_end": "03:00", "work_end": "02:00"}, MSK
    )
    with pytest.raises(ValueError):
        validate_schedule(bad)
    validate_schedule(CFG)
