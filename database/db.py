import logging
from typing import AsyncGenerator
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from config import settings
from database.models import Base, SystemSetting

logger = logging.getLogger(__name__)

# Настройка безопасного асинхронного движка SQLite
engine: AsyncEngine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
)


@event.listens_for(engine.sync_engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """
    Настройка критически важных PRAGMA для SQLite с адаптацией под оборудование:
    1. WAL-режим (Write-Ahead Logging) — обеспечивает параллельное чтение и запись без блокировок.
    2. synchronous = NORMAL — ускорение I/O при сохранении целостности.
    3. foreign_keys = ON — включение целостности связей.
    4. busy_timeout = 10000 (10 секунд) — предотвращение 'database is locked'.
    5. Адаптивное управление RAM:
       - В Low-Memory / OpenWrt режиме: cache 2 МБ, mmap отключен (предотвращает OOM и фрагментацию на 32-bit MIPS/ARM),
         temp_store = FILE, частый autocheckpoint.
       - В High-Performance режиме: cache 64 МБ, mmap 256 МБ, temp_store = MEMORY.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode = WAL;")
    cursor.execute("PRAGMA synchronous = NORMAL;")
    cursor.execute("PRAGMA foreign_keys = ON;")
    cursor.execute("PRAGMA busy_timeout = 10000;")

    if settings.is_low_memory:
        cursor.execute("PRAGMA cache_size = -2000;")       # 2 MB памяти под кэш страниц
        cursor.execute("PRAGMA mmap_size = 0;")           # Отключение mmap для сохранения памяти роутера
        cursor.execute("PRAGMA temp_store = FILE;")       # Временные таблицы на диске/tmpfs
        cursor.execute("PRAGMA wal_autocheckpoint = 100;")  # Частый сброс WAL
    else:
        cursor.execute("PRAGMA cache_size = -64000;")     # 64 MB памяти под кэш страниц
        cursor.execute("PRAGMA mmap_size = 268435456;")   # 256 MB memory-mapped I/O
        cursor.execute("PRAGMA temp_store = MEMORY;")     # Временные таблицы и сортировки в RAM

    cursor.close()


async_session_factory = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def init_db() -> None:
    """Инициализация базы данных: создание таблиц и дефолтных настроек с защитой прав доступа (0700 / 0600)"""
    import os
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    settings.effective_spool_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(settings.DATA_DIR, 0o700)
        os.chmod(settings.effective_spool_dir, 0o700)
    except Exception:
        pass

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    if settings.DB_PATH.exists():
        try:
            os.chmod(settings.DB_PATH, 0o600)
        except Exception:
            pass

    # Инициализация дефолтных настроек
    async with async_session_factory() as session:
        default_settings = {
            "price_per_page": str(settings.PRICE_PER_PAGE_RUB),
            "is_paused": "0",
            "emergency_msg": "",
            "payment_mode": settings.PAYMENT_MODE,
        }
        for key, val in default_settings.items():
            existing = await session.get(SystemSetting, key)
            if not existing:
                session.add(SystemSetting(key=key, value=val))
        await session.commit()
    logger.info("Database initialized successfully with WAL mode.")


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency / context generator for sessions"""
    async with async_session_factory() as session:
        yield session
