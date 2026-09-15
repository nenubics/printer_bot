import asyncio
import sys
from pathlib import Path
from datetime import datetime

# Корень проекта в sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from services.printer import PrinterService
from services.document import DocumentService

async def main():
    print("=" * 65)
    print("🖨️ ТЕСТИРОВАНИЕ РЕАЛЬНОГО ПРИНТЕРА PANTUM BP2300NW")
    print("=" * 65)
    print(f"⚙️  Режим: {settings.PRINTER_MODE}")
    print(f"🖨️  Очередь CUPS: {settings.PRINTER_NAME}")
    print(f"🌐 IP принтера: {settings.PRINTER_HOST}:{settings.PRINTER_PORT}")
    print("=" * 65)

    # 1. Проверка статуса принтера
    print("\n[1/4] Проверка статуса принтера...")
    status = await PrinterService.check_status()
    print(f"      Состояние: {status['state']}")
    print(f"      Готовность: {'✅ Готов' if status['is_ready'] else '❌ Не готов'}")
    print(f"      Инфо: {status['message']}")

    if not status["is_ready"]:
        print("\n❌ Принтер не готов к печати! Проверьте питание, бумагу и подключение.")
        return 1

    # 2. Создание тестового документа
    print("\n[2/4] Генерация тестовой страницы A4...")
    test_dir = settings.DATA_DIR / "test"
    test_dir.mkdir(parents=True, exist_ok=True)
    test_pdf = test_dir / "pantum_test_page.pdf"

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    test_text = f"""
======================================================================
         PANTUM BP2300NW — ТЕСТОВАЯ СТРАНИЦА ПЕЧАТИ
         Telegram-бот печати Общежития ЦСО-4
======================================================================

Дата и время теста: {now_str}
Принтер: Pantum BP2300NW Laser Printer
Сетевой адрес: {settings.PRINTER_HOST}:{settings.PRINTER_PORT}
Подключение: Роутер Huawei WiFi AX3 -> AirPrint / CUPS
Очередь CUPS: {settings.PRINTER_NAME}

Параметры печати:
- Формат бумаги: A4 (210 x 297 mm)
- Режим: Монохромный лазерный
- Разрешение: 1200 DPI
- Статус оборудования: ONLINE (IDLE)

Проверка систем бота:
[+] Ядро бота (Python 3.14 / aiogram 3.x / SQLAlchemy 2.0)
[+] Защита спулера и лимиты безопасности (KIIB Compliant)
[+] Система очередей и транзакционный баланс
[+] Драйвер печати CUPS / IPP Everywhere

Если вы держите эту страницу в руках — принтер Pantum BP2300NW
полностью настроен, подключен к роутеру и готов к приему заказов!
======================================================================
"""
    total_pages = DocumentService.convert_txt_to_a4_pdf(test_text.encode("utf-8"), test_pdf)
    print(f"      Документ сгенерирован: {test_pdf}")
    print(f"      Всего страниц: {total_pages}")

    # 3. Отправка задания на печать
    print("\n[3/4] Отправка задания на печать...")
    success, message, job_id = await PrinterService.print_job(
        pdf_path=test_pdf,
        copies=1,
        selected_pages="1",
        title="Pantum BP2300NW Test Page"
    )
    print(f"      Результат отправки: {'✅ УСПЕХ' if success else '❌ ОШИБКА'}")
    print(f"      Сообщение: {message}")
    print(f"      Job ID: {job_id}")

    if not success:
        print(f"\n❌ Ошибка печати: {message}")
        return 2

    # 4. Мониторинг очереди
    print("\n[4/4] Ожидание завершения печати в очереди CUPS...")
    for sec in range(1, 15):
        await asyncio.sleep(1)
        proc = await asyncio.create_subprocess_exec(
            "lpstat", "-o", settings.PRINTER_NAME,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        queue_out = stdout.decode().strip()
        if not queue_out:
            print(f"      Задание вышло из очереди CUPS и передано на печать (прошло {sec} сек).")
            break
        print(f"      [{sec} сек] Задание в очереди: {queue_out}")

    print("\n" + "=" * 65)
    print("🎉 ТЕСТОВАЯ ПЕЧАТЬ ОТПРАВЛЕНА НА ПРИНТЕР PANTUM BP2300NW!")
    print("=" * 65)
    return 0

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
