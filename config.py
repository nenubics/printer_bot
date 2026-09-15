import os
import json
from pathlib import Path
from typing import List, Optional, Union, Any
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def detect_low_memory_hardware() -> bool:
    """
    Автоматическое определение работы на маломощном оборудовании (< 512 МБ RAM):
    роутеры с OpenWrt (Netis NX31, Huawei AX3 / Entware, MIPS MT7621, MediaTek Filogic, ARM).
    """
    try:
        # Проверка /proc/meminfo (стандартно для Linux и OpenWrt)
        meminfo = Path("/proc/meminfo")
        if meminfo.exists():
            for line in meminfo.read_text().splitlines():
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        total_kb = int(parts[1])
                        return total_kb < 512 * 1024  # Меньше 512 МБ
    except Exception:
        pass

    try:
        # Fallback через os.sysconf
        if hasattr(os, "sysconf"):
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            if pages > 0 and page_size > 0:
                total_bytes = pages * page_size
                return total_bytes < 512 * 1024 * 1024
    except Exception:
        pass

    return False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Telegram Bot
    BOT_TOKEN: str = Field(default="YOUR_BOT_TOKEN_HERE", description="Токен Telegram бота от @BotFather")
    ADMIN_IDS: Union[List[int], str, Any] = Field(default_factory=list, description="Список Telegram ID администраторов")

    # Embedded / Low-Memory Hardware Optimization (OpenWrt, MIPS, ARM, Routers)
    LOW_MEMORY_MODE: Optional[bool] = Field(
        default=None,
        description="Режим для слабых устройств и роутеров (< 256MB RAM). Если None - определяется автоматически"
    )
    RENDER_DPI: int = Field(
        default=0,
        description="Разрешение рендеринга A4 (0 = авто: 150 DPI в low-memory, 300 DPI в обычном)"
    )
    FLASH_PROTECTION_MODE: bool = Field(
        default=True,
        description="Защита флеш-памяти роутера от износа (перенос спула в tmpfs /tmp)"
    )

    # Storage & Paths
    DATA_DIR: Path = Field(default=Path("data"), description="Папка для хранения данных")
    SPOOL_DIR: Path = Field(default=Path("data/spool"), description="Папка для временных файлов печати")
    DB_PATH: Path = Field(default=Path("data/printer_bot.db"), description="Путь к SQLite БД")
    MIN_FREE_DISK_MB: int = Field(default=500, description="Минимальный свободный объем диска для приема файлов (МБ)")
    MAX_PENDING_ORDERS_PER_USER: int = Field(default=3, description="Максимум неоплаченных заказов на одного пользователя (защита от переполнения спула)")
    MAX_PENDING_DEPOSITS_PER_USER: int = Field(default=2, description="Максимум неподтвержденных чеков на пополнение на пользователя")
    SPOOL_CLEANUP_HOURS: int = Field(default=2, description="Время жизни неоплаченных файлов в спуле (часы)")

    # Printing & Limits (Security / Hardware Protection)
    MAX_FILE_SIZE_BYTES: int = Field(default=35 * 1024 * 1024, description="Максимальный размер файла (35 МБ)")
    MAX_PAGES_PER_JOB: int = Field(default=200, description="Максимальное количество страниц за одну печать")
    MAX_COPIES_PER_JOB: int = Field(default=20, description="Максимальное количество копий за один заказ")
    MAX_SHEETS_PER_ORDER: int = Field(default=150, description="Максимальное число листов в заказе (емкость лотка Pantum BP2300NW)")
    PAGE_FIT_A4: bool = Field(default=True, description="Автоматическое масштабирование страниц под формат A4")
    AUTO_SKIP_BLANK_PAGES: bool = Field(default=True, description="Автоматически пропускать пустые страницы в документах для экономии бумаги")
    ENHANCE_CONTRAST_PHOTOS: bool = Field(default=True, description="Интеллектуальное отбеливание серого фона на фото/сканах для экономии тонера")
    TONER_SAVE_MODE: bool = Field(default=False, description="Режим экономии тонера (draft)")
    PRICE_PER_PAGE_RUB: float = Field(default=5.0, description="Базовая цена за страницу (руб)")
    
    # Printer Settings (Pantum BP2300NW)
    # Режимы: 'mock' (симуляция для тестов), 'cups' (печать через CUPS/lp), 'raw' (прямой сокет TCP:9100)
    PRINTER_MODE: str = Field(default="mock", description="Режим печати: 'cups', 'mock', 'raw'")
    PRINTER_NAME: str = Field(default="Pantum_BP2300NW", description="Имя очереди принтера в CUPS")
    PRINTER_HOST: Optional[str] = Field(default="192.168.1.100", description="IP-адрес принтера в локальной сети ЦСО-4")
    PRINTER_PORT: int = Field(default=9100, description="Порт принтера (9100 для RAW / JetDirect)")

    # Payment settings
    # 'balance' (внутренний баланс), 'manual_sbp' (перевод по номеру с чеком), 'stars' (Telegram Stars), 'yookassa'
    PAYMENT_MODE: str = Field(default="manual_sbp", description="Основной способ оплаты")
    YOOKASSA_PROVIDER_TOKEN: str = Field(default="", description="Токен ЮKassa для Telegram Payments")
    SBP_PHONE: str = Field(default="+7 (999) 000-00-00", description="Номер телефона для СБП переводов")
    SBP_BANK: str = Field(default="Т-Банк / Сбербанк", description="Банк получателя СБП")
    SBP_RECIPIENT_NAME: str = Field(default="Иван И.", description="Имя получателя для сверки")

    # Anti-flood & Safety
    RATE_LIMIT_DELAY: float = Field(default=0.8, description="Минимальный интервал между сообщениями (сек)")
    RATE_LIMIT_BURST_COUNT: int = Field(default=5, description="Число быстрых запросов до включения временного бана")
    RATE_LIMIT_BLOCK_SECONDS: float = Field(default=30.0, description="Длительность временного бана при флуде (сек)")
    EMERGENCY_STOP: bool = Field(default=False, description="Экстренная остановка очереди печати")

    @field_validator("ADMIN_IDS", mode="before")
    @classmethod
    def parse_admin_ids(cls, v):
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("[") and v.endswith("]"):
                try:
                    return json.loads(v)
                except Exception:
                    pass
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v

    @property
    def is_low_memory(self) -> bool:
        if self.LOW_MEMORY_MODE is not None:
            return self.LOW_MEMORY_MODE
        return detect_low_memory_hardware()

    @property
    def effective_dpi(self) -> int:
        if self.RENDER_DPI > 0:
            return self.RENDER_DPI
        return 150 if self.is_low_memory else 300

    @property
    def effective_max_file_size(self) -> int:
        if self.is_low_memory and self.MAX_FILE_SIZE_BYTES > 15 * 1024 * 1024:
            return 15 * 1024 * 1024
        return self.MAX_FILE_SIZE_BYTES

    @property
    def effective_max_pages(self) -> int:
        if self.is_low_memory and self.MAX_PAGES_PER_JOB > 40:
            return 40
        return self.MAX_PAGES_PER_JOB

    @property
    def effective_min_free_disk_mb(self) -> int:
        if self.is_low_memory and self.MIN_FREE_DISK_MB > 25:
            return 25
        return self.MIN_FREE_DISK_MB

    @property
    def effective_spool_dir(self) -> Path:
        if self.is_low_memory and self.FLASH_PROTECTION_MODE and self.SPOOL_DIR == Path("data/spool"):
            tmp_spool = Path("/tmp/printer_bot_spool")
            try:
                tmp_spool.mkdir(parents=True, exist_ok=True)
                return tmp_spool
            except Exception:
                pass
        return self.SPOOL_DIR

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.DB_PATH.resolve()}"


settings = Settings()
