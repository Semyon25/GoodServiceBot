from aiogram.fsm.state import State, StatesGroup


class AddressForm(StatesGroup):
    address = State()
    section = State()
    floor = State()
    apartment = State()
    intercom = State()
    instructions = State()
    confirm = State()


class PaymentProof(StatesGroup):
    waiting = State()


class SupportForm(StatesGroup):
    waiting = State()


class FailForm(StatesGroup):
    comment = State()


class AdminInput(StatesGroup):
    """Ввод текста администратором; что именно вводится — в data['input_kind']."""

    value = State()
