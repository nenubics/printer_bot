from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton
)


def get_main_reply_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Главное меню бота"""
    buttons = [
        [KeyboardButton(text="📄 Печать документа"), KeyboardButton(text="💳 Мой баланс")],
        [KeyboardButton(text="ℹ️ Статус принтера"), KeyboardButton(text="❓ Помощь / Тарифы")]
    ]
    if is_admin:
        buttons.append([KeyboardButton(text="⚙️ Панель администратора")])

    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        is_persistent=True
    )


def get_order_config_keyboard(order_uuid: str, copies: int, pages_str: str) -> InlineKeyboardMarkup:
    """Клавиатура настройки заказа перед печатью"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📑 Страницы: {pages_str[:16]}",
                    callback_data=f"cfg_pages:{order_uuid}"
                ),
                InlineKeyboardButton(
                    text=f"🔢 Копий: {copies}",
                    callback_data=f"cfg_copies:{order_uuid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💳 Оплатить и напечатать",
                    callback_data=f"pay_order:{order_uuid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отменить заказ",
                    callback_data=f"cancel_order:{order_uuid}"
                )
            ]
        ]
    )


def get_copies_quick_keyboard(order_uuid: str) -> InlineKeyboardMarkup:
    """Быстрый выбор количества копий"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1", callback_data=f"set_copy:{order_uuid}:1"),
                InlineKeyboardButton(text="2", callback_data=f"set_copy:{order_uuid}:2"),
                InlineKeyboardButton(text="3", callback_data=f"set_copy:{order_uuid}:3"),
                InlineKeyboardButton(text="5", callback_data=f"set_copy:{order_uuid}:5"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data=f"back_to_order:{order_uuid}")
            ]
        ]
    )


def get_payment_methods_keyboard(order_uuid: str, cost: float, balance: float) -> InlineKeyboardMarkup:
    """Выбор способа оплаты"""
    buttons = []
    
    if balance >= cost:
        buttons.append([
            InlineKeyboardButton(
                text=f"💳 Оплатить с баланса ({balance:.2f} ₽)",
                callback_data=f"pay_balance:{order_uuid}"
            )
        ])
    else:
        buttons.append([
            InlineKeyboardButton(
                text=f"📲 Оплатить через СБП ({cost:.2f} ₽)",
                callback_data=f"pay_sbp:{order_uuid}"
            )
        ])
        buttons.append([
            InlineKeyboardButton(
                text=f"⭐️ Оплатить Stars (Telegram)",
                callback_data=f"pay_stars:{order_uuid}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=f"cancel_order:{order_uuid}"
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_topup_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура быстрого пополнения баланса"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="+50 ₽", callback_data="topup_val:50"),
                InlineKeyboardButton(text="+100 ₽", callback_data="topup_val:100"),
                InlineKeyboardButton(text="+200 ₽", callback_data="topup_val:200"),
                InlineKeyboardButton(text="+500 ₽", callback_data="topup_val:500"),
            ],
            [
                InlineKeyboardButton(text="✏️ Другая сумма", callback_data="topup_custom"),
                InlineKeyboardButton(text="⬅️ Назад", callback_data="topup_cancel"),
            ]
        ]
    )


def get_cancel_queued_order_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """Кнопка отмены заказа, пока он в очереди"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отменить заказ и вернуть деньги",
                    callback_data=f"user_cancel_queue:{order_id}"
                )
            ]
        ]
    )


def get_admin_sbp_approval_keyboard(order_uuid: str, user_id: int, amount: float) -> InlineKeyboardMarkup:
    """Кнопки в админском чате для одобрения перевода СБП за печать"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить и напечатать",
                    callback_data=f"admin_approve_sbp:{order_uuid}:{user_id}:{amount}"
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить чек",
                    callback_data=f"admin_reject_sbp:{order_uuid}:{user_id}"
                )
            ]
        ]
    )


def get_admin_deposit_approval_keyboard(tx_id: int, user_id: int, amount: float) -> InlineKeyboardMarkup:
    """Кнопки одобрения пополнения баланса студента по СБП"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"✅ Зачислить {amount:.2f} ₽",
                    callback_data=f"admin_approve_deposit:{tx_id}:{user_id}:{amount}"
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"admin_reject_deposit:{tx_id}:{user_id}"
                )
            ]
        ]
    )


def get_admin_panel_keyboard(is_paused: bool) -> InlineKeyboardMarkup:
    """Кнопки главной админ-панели"""
    pause_text = "▶️ Возобновить печать" if is_paused else "⏸ Приостановить печать"
    pause_data = "admin_resume" if is_paused else "admin_pause"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Статистика и доходы", callback_data="admin_stats"),
                InlineKeyboardButton(text="🖨 Статус Pantum", callback_data="admin_printer_status"),
            ],
            [
                InlineKeyboardButton(text=pause_text, callback_data=pause_data),
                InlineKeyboardButton(text="💰 Изменить тариф", callback_data="admin_change_price"),
            ],
            [
                InlineKeyboardButton(text="➕ Начислить баланс", callback_data="admin_give_balance"),
                InlineKeyboardButton(text="📜 Очередь печати", callback_data="admin_view_queue"),
            ],
            [
                InlineKeyboardButton(text="📄 Бумага пополнена (Сброс)", callback_data="admin_reset_paper"),
                InlineKeyboardButton(text="📢 Рассылка студентам", callback_data="admin_broadcast_prompt"),
            ]
        ]
    )
