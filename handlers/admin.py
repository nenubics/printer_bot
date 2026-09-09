import asyncio
import logging
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from config import settings
from database.models import User, Order, Transaction, OrderStatus, TransactionType, TransactionStatus
from database.repo import Repository
from services.printer import PrinterService
from handlers.states import AdminState
from handlers.keyboards import get_admin_panel_keyboard


logger = logging.getLogger(__name__)
admin_router = Router(name="admin")


def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS


@admin_router.message(F.text == "⚙️ Панель администратора")
@admin_router.message(Command("admin"))
async def cmd_admin_panel(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        await message.answer("⛔️ У вас нет прав администратора.")
        return

    is_paused_str = await Repository.get_setting(session, "is_paused", "0")
    is_paused = is_paused_str == "1"
    price_str = await Repository.get_setting(session, "price_per_page", str(settings.PRICE_PER_PAGE_RUB))

    text = (
        f"⚙️ <b>Панель управления принтером (ЦСО-4)</b>\n\n"
        f"🖨 Принтер: <b>{settings.PRINTER_NAME}</b> (Режим: <code>{settings.PRINTER_MODE}</code>)\n"
        f"🚦 Состояние очереди: {'🔴 <b>ПАУЗА</b>' if is_paused else '🟢 <b>АКТИВНА</b>'}\n"
        f"💵 Тариф: <b>{float(price_str):.2f} ₽/стр</b>\n"
    )

    await message.answer(text, reply_markup=get_admin_panel_keyboard(is_paused), parse_mode="HTML")


@admin_router.callback_query(F.data == "admin_printer_status")
async def cb_admin_printer_status(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    status = await PrinterService.check_status()
    text = (
        f"🖨 <b>Диагностика оборудования:</b>\n\n"
        f"• Готовность: {'✅ ГОТОВ' if status['is_ready'] else '❌ НЕ ГОТОВ'}\n"
        f"• Статус: <code>{status['state']}</code>\n"
        f"• Лог: <i>{status['message']}</i>"
    )
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data.in_(["admin_pause", "admin_resume"]))
async def cb_admin_toggle_pause(callback: CallbackQuery, session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    should_pause = callback.data == "admin_pause"
    await Repository.set_setting(session, "is_paused", "1" if should_pause else "0")

    await callback.message.edit_reply_markup(reply_markup=get_admin_panel_keyboard(should_pause))
    msg = "⏸ Очередь печати приостановлена!" if should_pause else "▶️ Очередь печати возобновлена!"
    await callback.answer(msg, show_alert=True)


@admin_router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery, session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    total_users = await session.scalar(select(func.count(User.id))) or 0
    total_printed_orders = await session.scalar(
        select(func.count(Order.id)).where(Order.status == OrderStatus.COMPLETED)
    ) or 0
    total_printed_pages = await session.scalar(
        select(func.sum(Order.pages_to_print_count * Order.copies)).where(Order.status == OrderStatus.COMPLETED)
    ) or 0
    total_revenue = await session.scalar(
        select(func.sum(Order.cost_rub)).where(Order.status == OrderStatus.COMPLETED)
    ) or 0.0

    queued = await Repository.get_queued_orders_count(session)

    text = (
        f"📊 <b>Статистика сервиса печати ЦСО-4:</b>\n\n"
        f"👥 Пользователей в системе: <b>{total_users}</b>\n"
        f"📑 Успешно распечатано заказов: <b>{total_printed_orders}</b>\n"
        f"📄 Всего отпечатано листов: <b>{total_printed_pages}</b>\n"
        f"💰 Суммарная выручка: <b>{total_revenue:.2f} ₽</b>\n\n"
        f"⏳ Заданий в текущей очереди: <b>{queued}</b>"
    )
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin_approve_sbp:"))
async def cb_admin_approve_sbp(callback: CallbackQuery, bot: Bot, session: AsyncSession):
    """Одобрение администратором чека СБП"""
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    parts = callback.data.split(":")
    order_uuid = parts[1]
    user_id = int(parts[2])
    amount = float(parts[3])

    order = await Repository.get_order_by_uuid(session, order_uuid)
    if not order or order.status != OrderStatus.PENDING_ADMIN_APPROVAL:
        await callback.answer("Заказ уже обработан другим администратором.", show_alert=True)
        return

    # Зачисляем платеж на баланс и списываем за заказ
    await Repository.update_balance(
        session=session,
        user_id=user_id,
        delta=amount,
        trans_type=TransactionType.DEPOSIT,
        payment_method="manual_sbp",
        order_id=order.id
    )
    await Repository.update_balance(
        session=session,
        user_id=user_id,
        delta=-amount,
        trans_type=TransactionType.PRINT_CHARGE,
        payment_method="manual_sbp",
        order_id=order.id
    )

    # Переводим заказ в очередь печати
    await Repository.update_order_status(session, order.id, OrderStatus.QUEUED)

    # Обновляем сообщение у админа
    admin_name = callback.from_user.first_name
    await callback.message.edit_caption(
        caption=f"{callback.message.caption}\n\n✅ <b>ОДОБРЕНО администратором {admin_name}</b>. Отправлено на печать!",
        parse_mode="HTML"
    )
    await callback.answer("Оплата подтверждена!")

    # Уведомляем студента
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"✅ <b>Оплата по СБП ({amount:.2f} ₽) подтверждена!</b>\n\n"
                f"🖨 Документ <code>{order.original_filename}</code> отправлен на принтер <b>Pantum BP2300NW</b>.\n"
                f"Ожидайте уведомления о готовности!"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to notify user {user_id} about SBP approval: {e}")


@admin_router.callback_query(F.data.startswith("admin_reject_sbp:"))
async def cb_admin_reject_sbp(callback: CallbackQuery, bot: Bot, session: AsyncSession):
    """Отклонение администратором чека СБП"""
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    parts = callback.data.split(":")
    order_uuid = parts[1]
    user_id = int(parts[2])

    order = await Repository.get_order_by_uuid(session, order_uuid)
    if not order or order.status != OrderStatus.PENDING_ADMIN_APPROVAL:
        await callback.answer("Заказ уже обработан.", show_alert=True)
        return

    await Repository.update_order_status(session, order.id, OrderStatus.CANCELLED, error_message="Чек отклонен")

    admin_name = callback.from_user.first_name
    await callback.message.edit_caption(
        caption=f"{callback.message.caption}\n\n❌ <b>ОТКЛОНЕНО администратором {admin_name}</b>.",
        parse_mode="HTML"
    )
    await callback.answer("Чек отклонен.")

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"❌ <b>Ваш перевод по СБП не был подтвержден администратором.</b>\n\n"
                f"Возможные причины: средства не поступили, неверная сумма или нечитаемый чек.\n"
                f"Пожалуйста, свяжитесь с администратором комнаты печати в ЦСО-4."
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to notify user {user_id} about rejection: {e}")


@admin_router.callback_query(F.data == "admin_change_price")
async def cb_admin_change_price(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    await state.set_state(AdminState.waiting_for_price)
    await callback.message.answer("Введите новую цену за страницу в рублях (например, <code>6.0</code> или <code>4.5</code>):", parse_mode="HTML")
    await callback.answer()


@admin_router.message(AdminState.waiting_for_price)
async def process_new_price(message: Message, state: FSMContext, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return

    try:
        new_price = float(message.text.replace(",", "."))
        if new_price <= 0 or new_price > 500:
            raise ValueError()
    except ValueError:
        await message.answer("❌ Введите корректное число от 0.1 до 500.")
        return

    await Repository.set_setting(session, "price_per_page", str(new_price))
    await state.clear()
    await message.answer(f"✅ Новый тариф сохранен: <b>{new_price:.2f} ₽/страница</b>", parse_mode="HTML")


@admin_router.message(Command("give"))
async def cmd_give_balance(message: Message, session: AsyncSession):
    """Команда быстрого начисления баланса: /give <user_id> <amount>"""
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Использование: <code>/give &lt;user_id&gt; &lt;сумма&gt;</code>", parse_mode="HTML")
        return

    try:
        target_uid = int(parts[1])
        amount = float(parts[2])
    except ValueError:
        await message.answer("Неверные аргументы. ID и сумма должны быть числами.")
        return

    success, new_bal, msg = await Repository.update_balance(
        session=session,
        user_id=target_uid,
        delta=amount,
        trans_type=TransactionType.ADMIN_ADJUSTMENT,
        payment_method="admin"
    )

    if success:
        await message.answer(
            f"✅ Баланс пользователя <code>{target_uid}</code> пополнен на <b>{amount:.2f} ₽</b>.\n"
            f"Текущий баланс: <b>{new_bal:.2f} ₽</b>",
            parse_mode="HTML"
        )
    else:
        await message.answer(f"❌ Ошибка: {msg}")


@admin_router.callback_query(F.data.startswith("admin_approve_deposit:"))
async def cb_admin_approve_deposit(callback: CallbackQuery, bot: Bot, session: AsyncSession):
    """Подтверждение пополнения баланса студента"""
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    parts = callback.data.split(":")
    tx_id = int(parts[1])
    user_id = int(parts[2])
    amount = float(parts[3])

    tx = await session.get(Transaction, tx_id)
    if not tx or tx.status != TransactionStatus.PENDING:
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return

    tx.status = TransactionStatus.SUCCEEDED
    success, new_bal, msg = await Repository.update_balance(
        session=session,
        user_id=user_id,
        delta=amount,
        trans_type=TransactionType.DEPOSIT,
        payment_method="manual_sbp",
        order_id=None
    )

    admin_name = callback.from_user.first_name
    await callback.message.edit_caption(
        caption=f"{callback.message.caption}\n\n✅ <b>ЗАЧИСЛЕНО администратором {admin_name} (+{amount:.2f} ₽)</b>.",
        parse_mode="HTML"
    )
    await callback.answer("Баланс зачислен!")

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"🎉 <b>Ваш баланс успешно пополнен на {amount:.2f} ₽!</b>\n\n"
                f"💰 Текущий баланс: <b>{new_bal:.2f} ₽</b>\n"
                f"Теперь вы можете быстро отправлять документы на печать без ожидания проверки чеков."
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to notify user {user_id} of deposit: {e}")


@admin_router.callback_query(F.data.startswith("admin_reject_deposit:"))
async def cb_admin_reject_deposit(callback: CallbackQuery, bot: Bot, session: AsyncSession):
    """Отклонение пополнения баланса"""
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    parts = callback.data.split(":")
    tx_id = int(parts[1])
    user_id = int(parts[2])

    tx = await session.get(Transaction, tx_id)
    if tx and tx.status == TransactionStatus.PENDING:
        tx.status = TransactionStatus.FAILED
        await session.commit()

    admin_name = callback.from_user.first_name
    await callback.message.edit_caption(
        caption=f"{callback.message.caption}\n\n❌ <b>ОТКЛОНЕНО администратором {admin_name}</b>.",
        parse_mode="HTML"
    )
    await callback.answer("Отклонено.")

    try:
        await bot.send_message(
            chat_id=user_id,
            text="❌ Ваш чек на пополнение баланса был отклонен администратором. Проверьте платеж.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to notify user {user_id} of deposit rejection: {e}")


@admin_router.callback_query(F.data == "admin_view_queue")
async def cb_admin_view_queue(callback: CallbackQuery, session: AsyncSession):
    """Просмотр текущей очереди печати"""
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    queue = await Repository.get_queued_orders(session)
    if not queue:
        await callback.message.answer("📭 <b>Очередь печати пуста!</b>", parse_mode="HTML")
        await callback.answer()
        return

    lines = ["📑 <b>Текущие задания в очереди:</b>\n"]
    for idx, o in enumerate(queue, 1):
        status_icon = "🔄" if o.status == OrderStatus.PRINTING else "⏳"
        lines.append(
            f"{idx}. {status_icon} <b>{o.original_filename}</b>\n"
            f"   • Листов: {o.pages_to_print_count * o.copies} (копий: {o.copies})\n"
            f"   • ID: <code>{o.order_uuid[:8]}</code>, Студент ID: <code>{o.user_id}</code>\n"
            f"   • Статус: <code>{o.status.value}</code>"
        )

    await callback.message.answer("\n".join(lines), parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data == "admin_reset_paper")
async def cb_admin_reset_paper(callback: CallbackQuery, session: AsyncSession):
    """Сброс счетчика листов бумаги в лотке"""
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    await Repository.reset_paper_counter(session)
    _, max_tray = await Repository.get_paper_counter(session)
    await callback.message.answer(
        f"✅ <b>Счетчик бумаги сброшен!</b>\n"
        f"Лоток Pantum BP2300NW отмечен как полный (вместимость: {max_tray} листов).",
        parse_mode="HTML"
    )
    await callback.answer("Счетчик сброшен!")


@admin_router.callback_query(F.data == "admin_give_balance")
async def cb_admin_give_balance(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    await callback.message.answer(
        "➕ <b>Начисление баланса пользователю:</b>\n\n"
        "Отправьте команду:\n"
        "<code>/give &lt;user_id&gt; &lt;сумма&gt;</code>\n\n"
        "Например:\n"
        "<code>/give 123456789 150</code>",
        parse_mode="HTML"
    )
    await callback.answer()


@admin_router.callback_query(F.data == "admin_broadcast_prompt")
async def cb_admin_broadcast_prompt(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    await state.set_state(AdminState.waiting_for_broadcast)
    await callback.message.answer(
        "📢 <b>Рассылка всем студентам ЦСО-4:</b>\n\n"
        "Отправьте текст сообщения для рассылки всем зарегистрированным пользователям бота "
        "(или отправьте <code>/cancel</code> для отмены):",
        parse_mode="HTML"
    )
    await callback.answer()


@admin_router.message(AdminState.waiting_for_broadcast)
async def process_admin_broadcast(message: Message, bot: Bot, state: FSMContext, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return

    if message.text and message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("Рассылка отменена.")
        return

    broadcast_text = message.text or message.caption or ""
    await state.clear()

    # Извлекаем всех пользователей
    stmt = select(User.id).where(User.is_banned == False)
    result = await session.execute(stmt)
    user_ids = result.scalars().all()

    status_msg = await message.answer(f"⏳ Начинаю рассылку для {len(user_ids)} пользователей...")

    success_count = 0
    fail_count = 0

    for uid in user_ids:
        try:
            await bot.send_message(
                chat_id=uid,
                text=f"📢 <b>Объявление комнаты печати ЦСО-4:</b>\n\n{broadcast_text}",
                parse_mode="HTML"
            )
            success_count += 1
            await asyncio.sleep(0.05) # Защита от лимитов Telegram (30 сообщений/сек)
        except Exception:
            fail_count += 1

    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"• Доставлено: <b>{success_count}</b>\n"
        f"• Ошибок (заблокировали бота): <b>{fail_count}</b>",
        parse_mode="HTML"
    )

