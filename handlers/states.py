from aiogram.fsm.state import State, StatesGroup


class OrderConfigState(StatesGroup):
    waiting_for_pages = State()   # Ввод диапазона страниц (например "1-5, 8")
    waiting_for_copies = State()  # Ввод количества копий


class PaymentState(StatesGroup):
    waiting_for_sbp_receipt = State()     # Ожидание скриншота чека перевода СБП за печать
    waiting_for_deposit_amount = State()  # Ввод кастомной суммы пополнения баланса
    waiting_for_deposit_receipt = State() # Ожидание скриншота чека пополнения баланса


class AdminState(StatesGroup):
    waiting_for_price = State()        # Ввод новой цены за страницу
    waiting_for_balance_user_id = State() # ID пользователя для начисления
    waiting_for_balance_amount = State()  # Сумма для начисления
    waiting_for_broadcast = State()       # Текст рассылки
