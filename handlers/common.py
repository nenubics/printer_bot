import logging
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession
from config import settings
from database.repo import Repository
from services.printer import PrinterService
from handlers.keyboards import get_main_reply_keyboard

logger = logging.getLogger(__name__)
common_router = Router(name="common")


@common_router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession):
    user = await Repository.get_or_create_user(
        session=session,
        user_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name or "Студент"
    )

    is_admin = message.from_user.id in settings.ADMIN_IDS
    price_str = await Repository.get_setting(session, "price_per_page", str(settings.PRICE_PER_PAGE_RUB))
    price = float(price_str)

    text = (
        f"👋 <b>Привет, {message.from_user.first_name}!</b>\n\n"
        f"Я автоматический бот для печати документов на принтере <b>Pantum BP2300NW</b> в ЦСО-4.\n\n"
        f"📌 <b>Как это работает:</b>\n"
        f"1. Просто отправьте мне <b>PDF-файл</b> или <b>фотографию</b>.\n"
        f"2. Выберите нужные страницы и количество копий.\n"
        f"3. Оплатите заказ (СБП, баланс или Stars).\n"
        f"4. Заберите готовые распечатки из лотка принтера!\n\n"
        f"💵 <b>Текущий тариф:</b> {price:.2f} ₽ / страница\n"
        f"💳 <b>Ваш баланс:</b> {user.balance:.2f} ₽\n\n"
        f"Отправьте документ прямо сейчас, чтобы начать печать! 👇"
    )
    await message.answer(
        text,
        reply_markup=get_main_reply_keyboard(is_admin=is_admin),
        parse_mode="HTML"
    )


@common_router.message(F.text == "❓ Помощь / Тарифы")
@common_router.message(Command("help"))
async def cmd_help(message: Message, session: AsyncSession):
    price_str = await Repository.get_setting(session, "price_per_page", str(settings.PRICE_PER_PAGE_RUB))
    price = float(price_str)

    text = (
        f"📖 <b>Справка и правила печати (ЦСО-4)</b>\n\n"
        f"🖨 <b>Принтер:</b> Pantum BP2300NW (лазерный, ч/б, формат А4)\n"
        f"💰 <b>Стоимость:</b> {price:.2f} ₽ за 1 страницу А4\n\n"
        f"🔒 <b>Требования безопасности к файлам:</b>\n"
        f"• Поддерживаются: <b>PDF</b> и изображения (<b>PNG, JPG</b>).\n"
        f"• Максимальный размер файла: <b>{settings.MAX_FILE_SIZE_BYTES // (1024*1024)} МБ</b>.\n"
        f"• Лимит страниц за раз: <b>{settings.MAX_PAGES_PER_JOB}</b>.\n"
        f"• Не принимаются запароленные или поврежденные PDF.\n\n"
        f"💡 <b>Совет:</b> Если у вас документ Word (.docx) или презентация (.pptx), "
        f"сохраните его в PDF на телефоне/ПК перед отправкой. Это гарантирует, "
        f"что верстка и шрифты не съедут!\n\n"
        f"💬 <b>По всем вопросам и сбоям:</b> обратитесь к администратору комнаты печати."
    )
    await message.answer(text, parse_mode="HTML")


@common_router.message(F.text == "💳 Мой баланс")
async def cmd_balance(message: Message, session: AsyncSession):
    user = await Repository.get_or_create_user(session, message.from_user.id)
    text = (
        f"💳 <b>Ваш лицевой счет:</b>\n\n"
        f"👤 Пользователь: <code>{message.from_user.full_name}</code> (ID: <code>{user.id}</code>)\n"
        f"💰 Текущий баланс: <b>{user.balance:.2f} ₽</b>\n\n"
        f"Баланс списывается автоматически при подтверждении заказа на печать. "
        f"В случае сбоя оборудования или отсутствия бумаги средства немедленно возвращаются на баланс!"
    )
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Пополнить баланс", callback_data="start_topup")]
        ]
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")



@common_router.message(F.text == "ℹ️ Статус принтера")
@common_router.message(Command("status"))
async def cmd_status(message: Message, session: AsyncSession):
    is_paused = await Repository.get_setting(session, "is_paused", "0")
    queued_count = await Repository.get_queued_orders_count(session)
    printer_status = await PrinterService.check_status()

    if is_paused == "1":
        status_line = "⏸ <b>Печать временно приостановлена администратором</b>"
    elif printer_status["is_ready"]:
        status_line = "🟢 <b>Принтер онлайн и готов к печати</b>"
    else:
        status_line = f"🔴 <b>Принтер временно недоступен:</b> {printer_status['message']}"

    text = (
        f"🖨 <b>Статус оборудования Pantum BP2300NW:</b>\n\n"
        f"{status_line}\n\n"
        f"📑 Заказов в очереди печати: <b>{queued_count}</b>\n"
        f"⚙️ Режим интеграции: <code>{settings.PRINTER_MODE}</code>\n"
    )
    await message.answer(text, parse_mode="HTML")
