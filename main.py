import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from config import settings
from database.db import init_db, engine
from middlewares.throttling import ThrottlingMiddleware
from middlewares.db_session import DbSessionMiddleware
from handlers.common import common_router
from handlers.admin import admin_router
from handlers.user import user_router
from services.queue_worker import PrintQueueWorker


def setup_logging() -> None:
    """Настройка надежного логирования с ротацией файлов"""
    log_dir = settings.DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "printer_bot.log"

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Консольный лог
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    # Файловый лог с ротацией (макс 10 МБ, храним 5 архивов)
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Снижаем шум от aiogram и sqlalchemy
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def main() -> None:
    setup_logging()
    logger = logging.getLogger("main")
    logger.info("Initializing CSO-4 Pantum BP2300NW Printer Bot...")

    if settings.BOT_TOKEN == "YOUR_BOT_TOKEN_HERE" or not settings.BOT_TOKEN:
        logger.warning(
            "⚠️ ВНИМАНИЕ: BOT_TOKEN не установлен в файле .env! "
            "Укажите ваш токен бота в .env перед запуском боевого режима."
        )

    # 1. Инициализация базы данных (WAL-mode SQLite)
    await init_db()

    # 2. Инициализация бота и диспетчера
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher(storage=MemoryStorage())

    # 3. Регистрация Middleware (безопасность, троттлинг и сессии БД)
    throttling = ThrottlingMiddleware(
        limit_seconds=settings.RATE_LIMIT_DELAY,
        burst_threshold=settings.RATE_LIMIT_BURST_COUNT,
        block_duration=settings.RATE_LIMIT_BLOCK_SECONDS
    )
    dp.message.middleware(throttling)
    dp.callback_query.middleware(throttling)

    dp.update.middleware(DbSessionMiddleware())

    # 4. Регистрация роутеров
    dp.include_router(admin_router)
    dp.include_router(common_router)
    dp.include_router(user_router)

    # 5. Запуск фонового спулера печати
    queue_worker = PrintQueueWorker(bot)
    queue_worker.start()

    logger.info("Starting polling...")

    # 6. Обработка сигналов завершения (Graceful Shutdown)
    stop_event = asyncio.Event()

    def signal_handler():
        logger.info("Received termination signal, shutting down...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # На некоторых ОС (например Windows) add_signal_handler может быть не реализован
            pass

    try:
        polling_task = asyncio.create_task(
            dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        )
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Graceful shutdown in progress...")
        await queue_worker.stop()
        await dp.stop_polling()
        await bot.session.close()
        await engine.dispose()
        logger.info("Bot stopped cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
