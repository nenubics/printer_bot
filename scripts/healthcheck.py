import asyncio
import sys
import shutil
from pathlib import Path

# Добавляем корень проекта в sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
import aiosqlite
from services.printer import PrinterService



async def run_healthcheck() -> int:
    # 1. Проверка доступности папки спула и свободного места на диске
    settings.SPOOL_DIR.mkdir(parents=True, exist_ok=True)
    disk_usage = shutil.disk_usage(settings.DATA_DIR)
    free_mb = disk_usage.free / (1024 * 1024)
    if free_mb < 200:
        print(f"CRITICAL: Low disk space! Free: {free_mb:.1f} MB")
        return 1

    # 2. Проверка базы данных SQLite и WAL-файлов
    if not settings.DB_PATH.exists():
        print("CRITICAL: Database file does not exist!")
        return 2

    try:
        async with aiosqlite.connect(str(settings.DB_PATH)) as db:
            cursor = await db.execute("PRAGMA integrity_check;")
            row = await cursor.fetchone()
            if not row or row[0] != "ok":
                print(f"CRITICAL: Database integrity check failed: {row}")
                return 3
            # Проверка записи
            await db.execute("SELECT count(*) FROM system_settings;")
    except Exception as e:
        print(f"CRITICAL: Database access exception: {e}")
        return 4

    # 3. Проверка статуса принтера
    try:
        status = await PrinterService.check_status()
        print(f"Printer status: {status['state']} (ready={status['is_ready']})")
    except Exception as e:
        print(f"WARNING: Printer check error: {e}")

    print("HEALTHCHECK: OK")
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(run_healthcheck())
    sys.exit(exit_code)
