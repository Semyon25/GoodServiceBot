"""Данные inline-кнопок (не длиннее 64 байт)."""
from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


# --- клиент ---
class AddrCb(CallbackData, prefix="ad"):
    action: str  # start | pick | section | skip | save | restart | cancel
    value: int = 0


class TariffCb(CallbackData, prefix="tf"):
    action: str  # list | pick
    tariff_id: int = 0


class PayCb(CallbackData, prefix="pay"):
    action: str  # paid | change | cancel_proof
    payment_id: int


class OrderCb(CallbackData, prefix="co"):
    action: str  # placed | confirm | back | view | list
    order_id: int = 0


class SupportCb(CallbackData, prefix="sp"):
    action: str  # write | reply
    user_tg: int = 0


# --- исполнитель ---
class ExecCb(CallbackData, prefix="ex"):
    action: str  # view | accept | start | done | fail | list
    order_id: int = 0
    view: str = ""


class ExecFailCb(CallbackData, prefix="ef"):
    order_id: int
    reason: str


# --- администратор ---
class AOrderCb(CallbackData, prefix="ao"):
    action: str  # panel | list | view | assign | assign_to | cancel | history | exception | client
    order_id: int = 0
    arg: int = 0


class AFilterCb(CallbackData, prefix="af"):
    field: str  # date | status | address | executor | unassigned | reset
    value: str = ""


class APayCb(CallbackData, prefix="ap"):
    action: str  # panel | list | view | proof | confirm_ask | confirm | reject | refund | history
    payment_id: int = 0
    status: str = ""
    page: int = 0


class AUserCb(CallbackData, prefix="au"):
    action: str  # panel | search | view | role | block | address | orders | payments | executors | blocked | recent
    user_id: int = 0


class ATariffCb(CallbackData, prefix="at"):
    action: str  # list | view | price | duration | toggle | new
    tariff_id: int = 0


class ASchedCb(CallbackData, prefix="sc"):
    action: str  # view | day | time
    value: str = ""


class AAddrCb(CallbackData, prefix="aa"):
    action: str  # list | view | sections | toggle | new
    address_id: int = 0


class ASettingCb(CallbackData, prefix="st"):
    action: str  # view | edit | mode | alert | audit
    key: str = ""


class AStatsCb(CallbackData, prefix="sx"):
    period: str


class AExportCb(CallbackData, prefix="xp"):
    kind: str


class MenuCb(CallbackData, prefix="mn"):
    action: str  # cancel_input
