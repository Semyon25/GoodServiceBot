from datetime import date, timezone

import pytest

from bot.domain.schedule import (
    ScheduleConfig,
    current_shift,
    last_shift_date,
    order_shift,
    pickup_days_text,
    pickup_time_text,
    pickup_when_text,
    validate_schedule,
)
from bot.services.settings import render_text

from .conftest import MSK, msk

CFG = ScheduleConfig.from_settings(
    {"work_days": [0, 1, 2, 3, 4, 5, 6], "pickup_start": "19:00", "pickup_end": "02:00", "order_cutoff": "01:00"},
    MSK,
)
WEEKDAYS = ScheduleConfig.from_settings({**{"work_days": [0, 1, 2, 3, 4]}}, MSK)


@pytest.mark.parametrize(
    "moment, shift_date",
    [
        (msk(2026, 10, 21, 10, 0), date(2026, 10, 21)),  # днём — на сегодняшний вечерний вынос
        (msk(2026, 10, 21, 19, 0), date(2026, 10, 21)),
        (msk(2026, 10, 21, 23, 59), date(2026, 10, 21)),
        (msk(2026, 10, 22, 0, 30), date(2026, 10, 21)),  # после полуночи — ещё вчерашний вынос
        (msk(2026, 10, 22, 1, 0), date(2026, 10, 22)),  # приём на ночной вынос закрыт — следующий
        (msk(2026, 10, 24, 20, 0), date(2026, 10, 24)),  # суббота — тоже вынос
        (msk(2026, 10, 25, 23, 0), date(2026, 10, 25)),  # воскресенье
    ],
)
def test_order_goes_to_nearest_pickup(moment, shift_date):
    assert order_shift(moment, CFG).shift_date == shift_date


def test_weekdays_only_schedule_skips_weekend():
    assert order_shift(msk(2026, 10, 24, 12, 0), WEEKDAYS).shift_date == date(2026, 10, 26)
    assert order_shift(msk(2026, 10, 24, 0, 30), WEEKDAYS).shift_date == date(2026, 10, 23)


def test_current_pickup_window():
    assert current_shift(msk(2026, 10, 21, 18, 59), CFG) is None
    assert current_shift(msk(2026, 10, 22, 1, 59), CFG).shift_date == date(2026, 10, 21)
    assert current_shift(msk(2026, 10, 22, 2, 0), CFG) is None


def test_utc_input_is_converted_to_moscow():
    moment = msk(2026, 10, 21, 20, 0).astimezone(timezone.utc)
    assert order_shift(moment, CFG).shift_date == date(2026, 10, 21)


def test_last_shift_date_outside_window():
    assert last_shift_date(msk(2026, 10, 24, 12, 0), WEEKDAYS) == date(2026, 10, 23)
    assert last_shift_date(msk(2026, 10, 21, 21, 0), CFG) == date(2026, 10, 21)


def test_validation():
    with pytest.raises(ValueError):
        validate_schedule(ScheduleConfig.from_settings({"order_cutoff": "03:00"}, MSK))  # позже конца выноса
    with pytest.raises(ValueError):
        validate_schedule(ScheduleConfig.from_settings({"work_days": []}, MSK))
    validate_schedule(CFG)


def test_texts_follow_schedule():
    assert pickup_days_text(CFG) == "ежедневно"
    assert pickup_days_text(WEEKDAYS) == "по будням"
    assert pickup_time_text(CFG) == "с 19:00 до 02:00"
    other = ScheduleConfig.from_settings({"work_days": [0, 2], "pickup_start": "18:30", "pickup_end": "23:00",
                                          "order_cutoff": "22:00"}, MSK)
    assert render_text("Забираем {дни} {время}.", other) == "Забираем по дням: Пн, Ср с 18:30 до 23:00."


def test_pickup_when_text():
    now = msk(2026, 10, 21, 12, 0)
    assert pickup_when_text(date(2026, 10, 21), now, CFG) == "сегодня с 19:00 до 02:00"
    assert pickup_when_text(date(2026, 10, 22), now, CFG) == "завтра с 19:00 до 02:00"
    assert pickup_when_text(date(2026, 10, 21), msk(2026, 10, 22, 0, 30), CFG) == "этой ночью до 02:00"
