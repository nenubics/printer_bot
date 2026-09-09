import asyncio
import io
import logging
import sys
import tempfile
from pathlib import Path

# Добавляем корень проекта в sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pypdf
from config import settings
from database.db import init_db, async_session_factory
from database.models import OrderStatus, TransactionType
from database.repo import Repository
from services.printer import PrinterService
from services.queue_worker import PrintQueueWorker


class MockPantumServer:
    """
    Асинхронный TCP-сервер, эмулирующий принтер Pantum BP2300NW
    на сетевом порту 9100 (RAW / JetDirect), подключенный к роутеру Huawei AX3.
    """
    def __init__(self, host: str = "127.0.0.1", port: int = 9100):
        self.host = host
        self.port = port
        self.server = None
        self.received_jobs = []
        self.should_fail = False

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        client_addr = writer.get_extra_info("peername")
        print(f"   [Pantum BP2300NW] 🟢 Принято входящее соединение от бота: {client_addr}")

        if self.should_fail:
            print("   [Pantum BP2300NW] 🔴 Имитация аппаратного сбоя: разрыв соединения (замятие бумаги/нет связи)!")
            writer.close()
            await writer.wait_closed()
            return

        received_data = bytearray()
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            received_data.extend(chunk)

        if len(received_data) > 0:
            self.received_jobs.append(bytes(received_data))
            print(f"   [Pantum BP2300NW] 🖨 Успешно получен поток печати: {len(received_data)} байт.")
        else:
            print("   [Pantum BP2300NW] ℹ️ Получен ping проверки статуса оборудования (0 байт).")

        writer.close()
        await writer.wait_closed()


    async def start(self):
        self.server = await asyncio.start_server(self.handle_client, self.host, self.port)
        print(f"📡 [Huawei AX3 Wi-Fi] Принтер Pantum BP2300NW запущен на {self.host}:{self.port}")

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            print("📡 [Huawei AX3 Wi-Fi] Симулятор принтера остановлен.")


def create_test_pdf() -> bytes:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=595, height=842)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


async def run_simulation():
    print("=" * 70)
    print("🚀 СИМУЛЯЦИЯ ПЕЧАТИ ЧЕРЕЗ БОТА НА РОУТЕР HUAWEI AX3 (PANTUM BP2300NW)")
    print("=" * 70)

    # 1. Запуск виртуального принтера Pantum на порту 9100
    pantum_mock = MockPantumServer(host="127.0.0.1", port=9199)
    await pantum_mock.start()

    # Временно перенаправляем настройки на наш тестовый сокет
    original_mode = settings.PRINTER_MODE
    original_host = settings.PRINTER_HOST
    original_port = settings.PRINTER_PORT

    settings.PRINTER_MODE = "raw"
    settings.PRINTER_HOST = "127.0.0.1"
    settings.PRINTER_PORT = 9199

    try:
        # 2. Инициализация базы данных
        await init_db()

        # 3. Проверка статуса доступности через роутер
        print("\n[ШАГ 1] Проверка статуса принтера в сети Huawei AX3...")
        status = await PrinterService.check_status()
        print(f"   Результат проверки: {status['state']} - {status['message']}")
        assert status["is_ready"] is True, "Принтер должен быть доступен!"

        # 4. Создание пользователя и пополнение баланса
        import random
        student_id = random.randint(100000, 999999)
        print(f"\n[ШАГ 2] Регистрация студента (ID: {student_id}) и пополнение баланса...")
        async with async_session_factory() as session:
            student = await Repository.get_or_create_user(
                session, user_id=student_id, username="cso4_student", full_name="Иван Студент"
            )
            await Repository.update_balance(
                session, user_id=student_id, delta=100.0, trans_type=TransactionType.DEPOSIT
            )
            print(f"   Студент {student.full_name} зарегистрирован. Баланс: 100.00 ₽")


        # 5. Генерация файла и создание заказа печати
        print("\n[ШАГ 3] Загрузка документа студентом...")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            pdf_path = Path(f.name)
            pdf_data = create_test_pdf()
            pdf_path.write_bytes(pdf_data)

        async with async_session_factory() as session:
            order = await Repository.create_order(
                session=session,
                user_id=student_id,
                original_filename="referat_cso4.pdf",
                file_path=str(pdf_path),
                file_size=len(pdf_data),
                total_pages=1,
                pages_to_print_count=1,
                cost_rub=5.0,
                selected_pages="all",
                copies=1
            )
            print(f"   Заказ #{order.id} создан (UUID: {order.order_uuid[:8]}). Стоимость: 5.00 ₽")

            # 6. Оплата с баланса
            print("\n[ШАГ 4] Оплата заказа с лицевого счета...")
            ok, new_bal, msg = await Repository.update_balance(
                session=session,
                user_id=student_id,
                delta=-5.0,
                trans_type=TransactionType.PRINT_CHARGE,
                payment_method="balance",
                order_id=order.id
            )
            assert ok is True
            await Repository.update_order_status(session, order.id, OrderStatus.QUEUED)
            print(f"   Заказ оплачен! Новый баланс студента: {new_bal:.2f} ₽. Заказ в очереди (QUEUED).")

        # 7. Запуск процесса печати через фоновый воркер
        print("\n[ШАГ 5] Отправка задания из очереди через Huawei AX3 на Pantum BP2300NW...")
        # Создаем мок-бота для проверки уведомлений
        class MockBot:
            async def send_message(self, chat_id, text, parse_mode=None):
                print(f"   📨 [Telegram -> Пользователь {chat_id}]: {text[:60]}...")

        bot_mock = MockBot()
        worker = PrintQueueWorker(bot_mock)
        # Обрабатываем 1 заказ из очереди
        await worker._process_next_job()

        # 8. Проверка получения на стороне Pantum
        assert len(pantum_mock.received_jobs) == 1, "Принтер не получил задание!"
        received_bytes = pantum_mock.received_jobs[0]
        assert received_bytes.startswith(b"%PDF-"), "Полученные данные повреждены или не являются PDF!"
        print(f"   🎉 УСПЕХ: Принтер Pantum BP2300NW получил задание ({len(received_bytes)} байт) через сокет!")

        # 9. Проверка итогового статуса заказа
        async with async_session_factory() as session:
            final_order = await Repository.get_order_by_id(session, order.id)
            print(f"   Статус заказа в базе данных: {final_order.status.value.upper()}")
            assert final_order.status == OrderStatus.COMPLETED

        # ---------------------------------------------------------------------
        # СИМУЛЯЦИЯ 2: Аппаратный сбой принтера во время передачи (замятие бумаги / обрыв)
        # ---------------------------------------------------------------------
        print("\n" + "-" * 70)
        print("⚡️ СИМУЛЯЦИЯ СБОЯ: Аппаратный сбой Pantum BP2300NW при печати (замятие)...")
        print("-" * 70)

        # Включаем режим сбоя соединения на принтере
        pantum_mock.should_fail = True

        # Создаем новый заказ
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            pdf2_path = Path(f.name)
            pdf2_path.write_bytes(create_test_pdf())

        async with async_session_factory() as session:
            order2 = await Repository.create_order(
                session=session,
                user_id=student_id,
                original_filename="lab2.pdf",
                file_path=str(pdf2_path),
                file_size=1024,
                total_pages=2,
                pages_to_print_count=2,
                cost_rub=10.0
            )
            # Списываем 10 руб
            await Repository.update_balance(session, student_id, -10.0, TransactionType.PRINT_CHARGE, order_id=order2.id)
            await Repository.update_order_status(session, order2.id, OrderStatus.QUEUED)
            print("   Заказ #2 оплачен (10.00 ₽) и поставлен в очередь.")

        # Воркер пытается отправить на принтер со сбоем
        print("   Воркер отправляет задание на принтер, принтер разрывает связь...")
        await worker._process_next_job()


        # Проверяем статус второго заказа и автоматический возврат денег
        async with async_session_factory() as session:
            chk_order2 = await Repository.get_order_by_id(session, order2.id)
            print(f"   Статус заказа при недоступности принтера: {chk_order2.status.value}")
            user_after = await Repository.get_user(session, student_id)
            print(f"   💰 Баланс студента после сбоя принтера: {user_after.balance:.2f} ₽")
            # Проверяем, что деньги НЕ сгорели! (было 95, списали 10 = 85, при сбое вернулось = 95)
            assert user_after.balance == 95.0, "Деньги должны автоматически вернуться на баланс!"
            print("   ✅ ЗАЩИТА СРАБОТАЛА: Средства автоматически возвращены студенту!")


    finally:
        settings.PRINTER_MODE = original_mode
        settings.PRINTER_HOST = original_host
        settings.PRINTER_PORT = original_port
        await pantum_mock.stop()

    print("\n" + "=" * 70)
    print("🏆 ВСЕ ТЕСТЫ И СИМУЛЯЦИИ ПЕЧАТИ ЧЕРЕЗ РОУТЕР УСПЕШНО ПРОЙДЕНЫ!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_simulation())
