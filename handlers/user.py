import io
import uuid
import logging
from pathlib import Path
from aiogram import Router, F, Bot
from aiogram.types import (
    Message, CallbackQuery, PreCheckoutQuery, LabeledPrice
)
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from config import settings
from database.models import OrderStatus, TransactionType
from database.repo import Repository
from services.document import DocumentService, DocumentSecurityError
from handlers.states import OrderConfigState, PaymentState
from handlers.keyboards import (
    get_order_config_keyboard,
    get_copies_quick_keyboard,
    get_payment_methods_keyboard,
    get_topup_keyboard,
    get_cancel_queued_order_keyboard,
    get_admin_sbp_approval_keyboard,
    get_admin_deposit_approval_keyboard,
)

logger = logging.getLogger(__name__)
user_router = Router(name="user")


@user_router.message(F.text == "📄 Печать документа")
async def msg_start_print_prompt(message: Message):
    await message.answer(
        "📤 <b>Отправьте документ для печати:</b>\n\n"
        "• Прикрепите <b>PDF-файл</b> (как файл/документ)\n"
        "• Или отправьте <b>фотографию</b> (PNG / JPG)\n\n"
        "⚠️ <i>Совет: файлы Word (.docx) перед отправкой сохраняйте в PDF, чтобы разметка не сместилась!</i>",
        parse_mode="HTML"
    )


@user_router.message(F.document | F.photo)
async def handle_document_upload(message: Message, bot: Bot, session: AsyncSession, state: FSMContext):
    """Безопасная обработка и загрузка документа"""
    # Проверка на наличие уже активного незавершенного заказа
    active_order = await Repository.get_user_active_order(session, message.from_user.id)
    if active_order and active_order.status in (OrderStatus.QUEUED, OrderStatus.PRINTING):
        await message.answer(
            "⏳ У вас уже есть заказ в очереди печати! "
            "Пожалуйста, дождитесь его завершения перед отправкой следующего документа."
        )
        return

    status_msg = await message.answer("⏳ <i>Проверяю файл и считаю страницы...</i>", parse_mode="HTML")

    original_filename = "document.pdf"
    file_id = None
    file_size = 0

    if message.document:
        doc = message.document
        original_filename = doc.file_name or "document.pdf"
        file_size = doc.file_size or 0
        file_id = doc.file_id
    elif message.photo:
        photo = message.photo[-1]
        original_filename = f"photo_{message.from_user.id}.jpg"
        file_size = photo.file_size or 0
        file_id = photo.file_id

    # Проверка размера на уровне Telegram метаданных
    if file_size > settings.MAX_FILE_SIZE_BYTES:
        await status_msg.edit_text(
            f"❌ <b>Файл слишком большой!</b>\n"
            f"Максимальный размер: {settings.MAX_FILE_SIZE_BYTES // (1024*1024)} МБ.",
            parse_mode="HTML"
        )
        return

    order_uuid = str(uuid.uuid4())

    try:
        # Скачиваем файл в память с таймаутом
        file_info = await bot.get_file(file_id)
        file_stream = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=file_stream)
        file_bytes = file_stream.getvalue()

        # Безопасная валидация, санитизация и сохранение в спул
        dest_path, total_pages, sanitized_name = DocumentService.process_and_save_upload(
            file_bytes=file_bytes,
            original_name=original_filename,
            order_uuid=order_uuid
        )

    except DocumentSecurityError as e:
        await status_msg.edit_text(f"❌ <b>Ошибка проверки файла:</b>\n{str(e)}", parse_mode="HTML")
        return
    except Exception as e:
        logger.error(f"Unexpected error processing upload: {e}", exc_info=True)
        await status_msg.edit_text(
            "❌ Не удалось обработать файл. Убедитесь, что это корректный PDF или изображение.",
            parse_mode="HTML"
        )
        return

    # Получаем тариф за страницу
    price_str = await Repository.get_setting(session, "price_per_page", str(settings.PRICE_PER_PAGE_RUB))
    price_per_page = float(price_str)
    cost = round(total_pages * price_per_page, 2)

    # Создаем заказ в БД
    order = await Repository.create_order(
        session=session,
        user_id=message.from_user.id,
        original_filename=sanitized_name,
        file_path=str(dest_path),
        file_size=file_size,
        total_pages=total_pages,
        pages_to_print_count=total_pages,
        cost_rub=cost,
        selected_pages="all",
        copies=1
    )

    card_text = (
        f"📄 <b>Документ готов к печати!</b>\n\n"
        f"📎 Файл: <code>{sanitized_name}</code>\n"
        f"📑 Всего страниц: <b>{total_pages}</b>\n"
        f"🖨 Будет напечатано: <b>Все ({total_pages} стр.)</b>\n"
        f"🔢 Количество копий: <b>1</b>\n"
        f"💵 Стоимость: <b>{cost:.2f} ₽</b> ({price_per_page:.2f} ₽/стр)\n\n"
        f"<i>Вы можете настроить диапазон страниц или сразу перейти к оплате:</i>"
    )

    await status_msg.edit_text(
        card_text,
        reply_markup=get_order_config_keyboard(order.order_uuid, copies=1, pages_str="Все"),
        parse_mode="HTML"
    )


@user_router.callback_query(F.data.startswith("cfg_pages:"))
async def cb_config_pages(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    order_uuid = callback.data.split(":")[1]
    order = await Repository.get_order_by_uuid(session, order_uuid)
    if not order or order.status != OrderStatus.PENDING_CONFIG:
        await callback.answer("Заказ уже не доступен для изменения.", show_alert=True)
        return

    await state.set_state(OrderConfigState.waiting_for_pages)
    await state.update_data(order_uuid=order_uuid, total_pages=order.total_pages)

    await callback.message.answer(
        f"📑 <b>Выберите страницы для печати:</b>\n\n"
        f"В вашем документе всего <b>{order.total_pages}</b> страниц.\n\n"
        f"Введите диапазон через запятую, например:\n"
        f"• <code>1-5, 8</code>\n"
        f"• <code>2, 4, 7-10</code>\n"
        f"• Или напишите <code>все</code> для печати всего документа.",
        parse_mode="HTML"
    )
    await callback.answer()


@user_router.message(OrderConfigState.waiting_for_pages)
async def process_custom_pages(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    order_uuid = data.get("order_uuid")
    total_pages = data.get("total_pages", 1)

    order = await Repository.get_order_by_uuid(session, order_uuid)
    if not order or order.status != OrderStatus.PENDING_CONFIG:
        await message.answer("Заказ устарел или был отменен.")
        await state.clear()
        return

    try:
        norm_pages, pages_count = DocumentService.parse_page_range(message.text, total_pages)
    except DocumentSecurityError as e:
        await message.answer(f"❌ {str(e)}\n\nПопробуйте еще раз (например: <code>1-3, 5</code>):", parse_mode="HTML")
        return

    # Контроль вместимости лотка принтера Pantum BP2300NW
    total_sheets = pages_count * order.copies
    if total_sheets > settings.MAX_SHEETS_PER_ORDER:
        await message.answer(
            f"❌ <b>Превышена вместимость лотка бумаги!</b>\n\n"
            f"Выбрано: <b>{pages_count} стр.</b> x <b>{order.copies} экз.</b> = <b>{total_sheets} листов</b>.\n"
            f"Лоток принтера Pantum BP2300NW вмещает не более <b>{settings.MAX_SHEETS_PER_ORDER} листов</b>.\n\n"
            f"Пожалуйста, укажите меньший диапазон страниц (например: <code>1-{min(total_pages, settings.MAX_SHEETS_PER_ORDER)}</code>):",
            parse_mode="HTML"
        )
        return

    price_str = await Repository.get_setting(session, "price_per_page", str(settings.PRICE_PER_PAGE_RUB))
    price_per_page = float(price_str)
    cost = round(pages_count * order.copies * price_per_page, 2)

    order.selected_pages = norm_pages
    order.pages_to_print_count = pages_count
    order.cost_rub = cost
    await session.commit()
    await state.clear()

    display_pages = "Все" if norm_pages == "all" else norm_pages
    card_text = (
        f"📄 <b>Параметры обновлены!</b>\n\n"
        f"📎 Файл: <code>{order.original_filename}</code>\n"
        f"📑 Всего в файле: <b>{order.total_pages}</b> стр.\n"
        f"🖨 К печати: <b>{display_pages}</b> ({pages_count} стр.)\n"
        f"🔢 Количество копий: <b>{order.copies}</b>\n"
        f"📑 Суммарно листов: <b>{total_sheets}</b>\n"
        f"💵 Стоимость: <b>{cost:.2f} ₽</b>\n"
    )

    await message.answer(
        card_text,
        reply_markup=get_order_config_keyboard(order.order_uuid, copies=order.copies, pages_str=display_pages),
        parse_mode="HTML"
    )


@user_router.callback_query(F.data.startswith("cfg_copies:"))
async def cb_config_copies(callback: CallbackQuery, session: AsyncSession):
    order_uuid = callback.data.split(":")[1]
    order = await Repository.get_order_by_uuid(session, order_uuid)
    if not order or order.status != OrderStatus.PENDING_CONFIG:
        await callback.answer("Заказ уже не доступен для изменения.", show_alert=True)
        return

    await callback.message.edit_text(
        f"🔢 <b>Выберите количество копий для файла:</b>\n<code>{order.original_filename}</code>",
        reply_markup=get_copies_quick_keyboard(order_uuid),
        parse_mode="HTML"
    )
    await callback.answer()


@user_router.callback_query(F.data.startswith("set_copy:"))
async def cb_set_copies(callback: CallbackQuery, session: AsyncSession):
    parts = callback.data.split(":")
    order_uuid = parts[1]
    copies = int(parts[2])

    order = await Repository.get_order_by_uuid(session, order_uuid)
    if not order or order.status != OrderStatus.PENDING_CONFIG:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    # Проверка лимитов на копии и лоток
    if copies < 1 or copies > settings.MAX_COPIES_PER_JOB:
        await callback.answer(
            f"Количество копий должно быть от 1 до {settings.MAX_COPIES_PER_JOB}.",
            show_alert=True
        )
        return

    total_sheets = order.pages_to_print_count * copies
    if total_sheets > settings.MAX_SHEETS_PER_ORDER:
        await callback.answer(
            f"Слишком много листов ({total_sheets}). Лоток Pantum вмещает не более {settings.MAX_SHEETS_PER_ORDER} листов.",
            show_alert=True
        )
        return

    price_str = await Repository.get_setting(session, "price_per_page", str(settings.PRICE_PER_PAGE_RUB))
    price_per_page = float(price_str)
    cost = round(order.pages_to_print_count * copies * price_per_page, 2)

    order.copies = copies
    order.cost_rub = cost
    await session.commit()

    display_pages = "Все" if order.selected_pages == "all" else order.selected_pages
    card_text = (
        f"📄 <b>Параметры обновлены!</b>\n\n"
        f"📎 Файл: <code>{order.original_filename}</code>\n"
        f"📑 Всего в файле: <b>{order.total_pages}</b> стр.\n"
        f"🖨 К печати: <b>{display_pages}</b> ({order.pages_to_print_count} стр.)\n"
        f"🔢 Количество копий: <b>{copies}</b>\n"
        f"📑 Суммарно листов: <b>{total_sheets}</b>\n"
        f"💵 Итого к оплате: <b>{cost:.2f} ₽</b>\n"
    )
    await callback.message.edit_text(
        card_text,
        reply_markup=get_order_config_keyboard(order_uuid, copies=copies, pages_str=display_pages),
        parse_mode="HTML"
    )
    await callback.answer()


@user_router.callback_query(F.data.startswith("back_to_order:"))
async def cb_back_to_order(callback: CallbackQuery, session: AsyncSession):
    order_uuid = callback.data.split(":")[1]
    order = await Repository.get_order_by_uuid(session, order_uuid)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    display_pages = "Все" if order.selected_pages == "all" else order.selected_pages
    card_text = (
        f"📄 <b>Параметры печати:</b>\n\n"
        f"📎 Файл: <code>{order.original_filename}</code>\n"
        f"📑 Всего в файле: <b>{order.total_pages}</b> стр.\n"
        f"🖨 К печати: <b>{display_pages}</b> ({order.pages_to_print_count} стр.)\n"
        f"🔢 Количество копий: <b>{order.copies}</b>\n"
        f"💵 Стоимость: <b>{order.cost_rub:.2f} ₽</b>\n"
    )
    await callback.message.edit_text(
        card_text,
        reply_markup=get_order_config_keyboard(order_uuid, copies=order.copies, pages_str=display_pages),
        parse_mode="HTML"
    )
    await callback.answer()


@user_router.callback_query(F.data.startswith("cancel_order:"))
async def cb_cancel_order(callback: CallbackQuery, session: AsyncSession):
    order_uuid = callback.data.split(":")[1]
    order = await Repository.get_order_by_uuid(session, order_uuid)
    if order:
        DocumentService.cleanup_file(order.file_path)
        await Repository.update_order_status(session, order.id, OrderStatus.CANCELLED)
    await callback.message.edit_text("❌ <b>Заказ отменен.</b> Файл удален из очереди.", parse_mode="HTML")
    await callback.answer()


@user_router.callback_query(F.data.startswith("pay_order:"))
async def cb_pay_order(callback: CallbackQuery, session: AsyncSession):
    order_uuid = callback.data.split(":")[1]
    order = await Repository.get_order_by_uuid(session, order_uuid)
    if not order or order.status not in (OrderStatus.PENDING_CONFIG, OrderStatus.PENDING_PAYMENT):
        await callback.answer("Заказ недоступен для оплаты.", show_alert=True)
        return

    # Проверка емкости лотка перед переходом к оплате
    total_sheets = order.pages_to_print_count * order.copies
    if total_sheets > settings.MAX_SHEETS_PER_ORDER:
        await callback.answer(
            f"В заказе {total_sheets} листов. Лоток принтера Pantum вмещает не более {settings.MAX_SHEETS_PER_ORDER} листов. "
            f"Пожалуйста, выберите меньший диапазон страниц.",
            show_alert=True
        )
        return

    user = await Repository.get_or_create_user(session, callback.from_user.id)
    order.status = OrderStatus.PENDING_PAYMENT
    await session.commit()

    text = (
        f"💳 <b>Оплата заказа:</b>\n\n"
        f"📄 Файл: <code>{order.original_filename}</code>\n"
        f"📑 Листов к печати: <b>{order.pages_to_print_count * order.copies}</b>\n"
        f"💵 Сумма к оплате: <b>{order.cost_rub:.2f} ₽</b>\n"
        f"💰 Ваш баланс: <b>{user.balance:.2f} ₽</b>\n\n"
        f"Выберите удобный способ оплаты:"
    )
    await callback.message.edit_text(
        text,
        reply_markup=get_payment_methods_keyboard(order_uuid, order.cost_rub, user.balance),
        parse_mode="HTML"
    )
    await callback.answer()


@user_router.callback_query(F.data.startswith("pay_balance:"))
async def cb_pay_balance(callback: CallbackQuery, session: AsyncSession):
    order_uuid = callback.data.split(":")[1]
    order = await Repository.get_order_by_uuid(session, order_uuid)
    if not order or order.status != OrderStatus.PENDING_PAYMENT:
        await callback.answer("Заказ уже оплачен или недоступен.", show_alert=True)
        return

    # Атомарное списание с проверкой
    success, new_balance, msg = await Repository.update_balance(
        session=session,
        user_id=callback.from_user.id,
        delta=-order.cost_rub,
        trans_type=TransactionType.PRINT_CHARGE,
        payment_method="balance",
        order_id=order.id
    )

    if not success:
        await callback.answer(f"❌ Ошибка оплаты: {msg}", show_alert=True)
        return

    # Заказ переведен в очередь
    await Repository.update_order_status(session, order.id, OrderStatus.QUEUED)

    queued_count = await Repository.get_queued_orders_count(session)

    await callback.message.edit_text(
        f"✅ <b>Заказ успешно оплачен с баланса!</b>\n\n"
        f"💵 Списано: <b>{order.cost_rub:.2f} ₽</b>\n"
        f"💳 Остаток на счете: <b>{new_balance:.2f} ₽</b>\n\n"
        f"🖨 Документ отправлен в очередь на принтер <b>Pantum BP2300NW</b>.\n"
        f"Позиция в очереди: <b>{queued_count}</b>.\n\n"
        f"<i>Как только печать завершится, вы получите уведомление!</i>",
        reply_markup=get_cancel_queued_order_keyboard(order.id),
        parse_mode="HTML"
    )
    await callback.answer("Оплачено!")



@user_router.callback_query(F.data.startswith("pay_sbp:"))
async def cb_pay_sbp(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    from aiogram.types import BufferedInputFile
    from services.payment_qr import PaymentQrService

    order_uuid = callback.data.split(":")[1]
    order = await Repository.get_order_by_uuid(session, order_uuid)
    if not order or order.status != OrderStatus.PENDING_PAYMENT:
        await callback.answer("Заказ не найден или уже оплачен.", show_alert=True)
        return

    await state.set_state(PaymentState.waiting_for_sbp_receipt)
    await state.update_data(order_uuid=order_uuid, cost_rub=order.cost_rub)

    sbp_text = (
        f"📲 <b>Оплата по СБП (Система быстрых платежей):</b>\n\n"
        f"💵 Сумма к переводу: <b>{order.cost_rub:.2f} ₽</b> (ровно эту сумму)\n"
        f"📱 Номер телефона: <code>{settings.SBP_PHONE}</code>\n"
        f"🏦 Банк: <b>{settings.SBP_BANK}</b>\n"
        f"👤 Получатель: <b>{settings.SBP_RECIPIENT_NAME}</b>\n"
        f"💬 Сообщение к переводу: <code>Печать #{order.id}</code>\n\n"
        f"📸 <i>Отсканируйте QR-код в банковском приложении или сделайте перевод по номеру.</i>\n\n"
        f"👇 <b>После перевода отправьте сюда скриншот чека из банка!</b>"
    )

    qr_bytes = PaymentQrService.generate_sbp_qr_bytes(order.cost_rub, f"Заказ #{order.id}")
    qr_file = BufferedInputFile(qr_bytes, filename=f"sbp_qr_{order.id}.png")

    await callback.message.answer_photo(
        photo=qr_file,
        caption=sbp_text,
        parse_mode="HTML"
    )
    await callback.answer()



@user_router.message(PaymentState.waiting_for_sbp_receipt, F.photo | F.document)
async def process_sbp_receipt(message: Message, bot: Bot, state: FSMContext, session: AsyncSession):
    """Прием чека СБП и отправка администраторам на проверку"""
    data = await state.get_data()
    order_uuid = data.get("order_uuid")
    cost_rub = data.get("cost_rub", 0.0)

    order = await Repository.get_order_by_uuid(session, order_uuid)
    if not order or order.status != OrderStatus.PENDING_PAYMENT:
        await message.answer("Заказ уже обработан или отменен.")
        await state.clear()
        return

    # Получаем file_id чека
    proof_file_id = message.photo[-1].file_id if message.photo else message.document.file_id

    # Обновляем статус заказа
    await Repository.update_order_status(session, order.id, OrderStatus.PENDING_ADMIN_APPROVAL)
    await state.clear()

    await message.answer(
        "⏳ <b>Чек получен!</b> Он отправлен администратору на быструю проверку.\n"
        "Как только администратор подтвердит поступление, печать начнется автоматически!",
        parse_mode="HTML"
    )

    # Уведомляем администраторов
    user_info = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
    admin_caption = (
        f"🔔 <b>Новая оплата печати по СБП!</b>\n\n"
        f"👤 Студент: {user_info} (ID: <code>{message.from_user.id}</code>)\n"
        f"📄 Файл: <code>{order.original_filename}</code>\n"
        f"📑 Листов: {order.pages_to_print_count * order.copies}\n"
        f"💰 Сумма: <b>{cost_rub:.2f} ₽</b>\n"
        f"🆔 Заказ: <code>{order_uuid[:8]}</code>\n\n"
        f"<i>Сверьте поступление в банковском приложении и нажмите кнопку:</i>"
    )

    for admin_id in settings.ADMIN_IDS:
        try:
            if message.photo:
                await bot.send_photo(
                    chat_id=admin_id,
                    photo=proof_file_id,
                    caption=admin_caption,
                    reply_markup=get_admin_sbp_approval_keyboard(order_uuid, message.from_user.id, cost_rub),
                    parse_mode="HTML"
                )
            else:
                await bot.send_document(
                    chat_id=admin_id,
                    document=proof_file_id,
                    caption=admin_caption,
                    reply_markup=get_admin_sbp_approval_keyboard(order_uuid, message.from_user.id, cost_rub),
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.error(f"Failed to forward SBP receipt to admin {admin_id}: {e}")


@user_router.callback_query(F.data.startswith("pay_stars:"))
async def cb_pay_stars(callback: CallbackQuery, bot: Bot, session: AsyncSession):
    order_uuid = callback.data.split(":")[1]
    order = await Repository.get_order_by_uuid(session, order_uuid)
    if not order or order.status != OrderStatus.PENDING_PAYMENT:
        await callback.answer("Заказ не найден или уже оплачен.", show_alert=True)
        return

    # Telegram Stars: 1 Star ≈ 1.5 - 2 рубля. Конвертируем по курсу ~ 1 Star = 2 RUB (или минимум 1)
    stars_amount = max(1, int(order.cost_rub / 2.0))

    prices = [LabeledPrice(label=f"Печать: {order.original_filename[:15]}", amount=stars_amount)]

    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Оплата печати в ЦСО-4",
        description=f"Печать {order.pages_to_print_count * order.copies} стр. на Pantum BP2300NW",
        payload=f"stars_order:{order_uuid}",
        currency="XTR", # Telegram Stars
        prices=prices,
        provider_token="" # Для Stars токен пустой
    )
    await callback.answer()


@user_router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery, bot: Bot):
    """Подтверждение готовности принять платеж"""
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@user_router.message(F.successful_payment)
async def process_successful_payment(message: Message, session: AsyncSession):
    """Обработка успешной оплаты через Telegram Payments / Stars"""
    payload = message.successful_payment.invoice_payload
    if payload.startswith("stars_order:"):
        order_uuid = payload.split(":")[1]
        order = await Repository.get_order_by_uuid(session, order_uuid)
        if order and order.status == OrderStatus.PENDING_PAYMENT:
            # Зачисляем транзакцию
            await Repository.update_balance(
                session=session,
                user_id=message.from_user.id,
                delta=order.cost_rub,
                trans_type=TransactionType.DEPOSIT,
                payment_method="stars",
                order_id=order.id,
                provider_payment_id=message.successful_payment.telegram_payment_charge_id
            )
            # Списываем за печать
            await Repository.update_balance(
                session=session,
                user_id=message.from_user.id,
                delta=-order.cost_rub,
                trans_type=TransactionType.PRINT_CHARGE,
                payment_method="stars",
                order_id=order.id
            )
            await Repository.update_order_status(session, order.id, OrderStatus.QUEUED)
            await message.answer(
                "🎉 <b>Оплата звездами Telegram успешно получена!</b>\n"
                "Заказ отправлен в очередь на печать!",
                reply_markup=get_cancel_queued_order_keyboard(order.id),
                parse_mode="HTML"
            )


@user_router.callback_query(F.data.startswith("user_cancel_queue:"))
async def cb_user_cancel_queue(callback: CallbackQuery, session: AsyncSession):
    """Отмена заказа пользователем, пока он ждет очереди печати"""
    order_id = int(callback.data.split(":")[1])
    success, msg = await Repository.cancel_and_refund_order(session, order_id)
    if success:
        order = await Repository.get_order_by_id(session, order_id)
        if order:
            DocumentService.cleanup_file(order.file_path)
        await callback.message.edit_text(
            f"❌ <b>Заказ #{order_id} отменен.</b>\n"
            f"Средства возвращены на ваш баланс в боте.",
            parse_mode="HTML"
        )
        await callback.answer("Заказ отменен, средства возвращены.")
    else:
        await callback.answer(f"Не удалось отменить: {msg}", show_alert=True)


@user_router.callback_query(F.data == "start_topup")
async def cb_start_topup(callback: CallbackQuery):
    await callback.message.answer(
        "💳 <b>Пополнение лицевого счета:</b>\n\n"
        "Выберите желаемую сумму пополнения или введите свою:",
        reply_markup=get_topup_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@user_router.callback_query(F.data == "topup_cancel")
async def cb_topup_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.answer("Отменено")


@user_router.callback_query(F.data.startswith("topup_val:"))
async def cb_topup_val(callback: CallbackQuery, state: FSMContext):
    amount = float(callback.data.split(":")[1])
    await _send_topup_instructions(callback.message, state, amount)
    await callback.answer()


@user_router.callback_query(F.data == "topup_custom")
async def cb_topup_custom(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PaymentState.waiting_for_deposit_amount)
    await callback.message.answer(
        "Введите желаемую сумму пополнения в рублях (например, <code>150</code> или <code>300</code>):",
        parse_mode="HTML"
    )
    await callback.answer()


@user_router.message(PaymentState.waiting_for_deposit_amount)
async def process_topup_custom_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
        if amount < 10 or amount > 10000:
            raise ValueError()
    except ValueError:
        await message.answer("❌ Введите корректную сумму от 10 до 10 000 руб.")
        return

    await _send_topup_instructions(message, state, amount)


async def _send_topup_instructions(target_msg: Message, state: FSMContext, amount: float):
    from aiogram.types import BufferedInputFile
    from services.payment_qr import PaymentQrService

    await state.set_state(PaymentState.waiting_for_deposit_receipt)
    await state.update_data(deposit_amount=amount)

    qr_bytes = PaymentQrService.generate_sbp_qr_bytes(amount, f"Пополнение баланса {amount:.2f}р")
    qr_file = BufferedInputFile(qr_bytes, filename="topup_qr.png")

    text = (
        f"📲 <b>Пополнение баланса на {amount:.2f} ₽:</b>\n\n"
        f"📱 Номер для перевода (СБП): <code>{settings.SBP_PHONE}</code>\n"
        f"🏦 Банк: <b>{settings.SBP_BANK}</b>\n"
        f"👤 Получатель: <b>{settings.SBP_RECIPIENT_NAME}</b>\n"
        f"💵 Сумма к переводу: <b>{amount:.2f} ₽</b> (ровно эту сумму)\n"
        f"💬 Назначение платежа: <code>Баланс ЦСО-4</code>\n\n"
        f"📸 <i>Отсканируйте QR-код в банковском приложении или переведите по номеру.</i>\n\n"
        f"👇 <b>После перевода отправьте сюда скриншот чека из банка!</b>"
    )

    await target_msg.answer_photo(photo=qr_file, caption=text, parse_mode="HTML")


@user_router.message(PaymentState.waiting_for_deposit_receipt, F.photo | F.document)
async def process_topup_receipt(message: Message, bot: Bot, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    amount = data.get("deposit_amount", 0.0)
    await state.clear()

    proof_file_id = message.photo[-1].file_id if message.photo else message.document.file_id

    # Создаем pending транзакцию
    from database.models import Transaction, TransactionType, TransactionStatus
    tx = Transaction(
        user_id=message.from_user.id,
        amount=amount,
        type=TransactionType.DEPOSIT,
        status=TransactionStatus.PENDING,
        payment_method="manual_sbp",
        proof_file_id=proof_file_id
    )
    session.add(tx)
    await session.commit()
    await session.refresh(tx)

    await message.answer(
        "⏳ <b>Чек на пополнение получен!</b>\n"
        "Администратор проверит поступление и подтвердит зачисление на ваш баланс в ближайшее время.",
        parse_mode="HTML"
    )

    user_info = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
    admin_caption = (
        f"💳 <b>Запрос на пополнение баланса!</b>\n\n"
        f"👤 Пользователь: {user_info} (ID: <code>{message.from_user.id}</code>)\n"
        f"💰 Сумма: <b>{amount:.2f} ₽</b>\n"
        f"🆔 ID транзакции: <code>#{tx.id}</code>\n\n"
        f"<i>Сверьте поступление в банковском приложении и нажмите кнопку:</i>"
    )

    for admin_id in settings.ADMIN_IDS:
        try:
            if message.photo:
                await bot.send_photo(
                    chat_id=admin_id,
                    photo=proof_file_id,
                    caption=admin_caption,
                    reply_markup=get_admin_deposit_approval_keyboard(tx.id, message.from_user.id, amount),
                    parse_mode="HTML"
                )
            else:
                await bot.send_document(
                    chat_id=admin_id,
                    document=proof_file_id,
                    caption=admin_caption,
                    reply_markup=get_admin_deposit_approval_keyboard(tx.id, message.from_user.id, amount),
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.error(f"Failed to send deposit receipt to admin {admin_id}: {e}")

