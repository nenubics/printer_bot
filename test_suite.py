import asyncio
import io
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, AsyncMock, MagicMock
from pathlib import Path
from PIL import Image, ImageDraw
import pypdf

from config import settings
from services.document import DocumentService, DocumentSecurityError
from services.printer import PrinterService
from database.models import Base, User, Order, Transaction, OrderStatus, TransactionType, TransactionStatus
from database.repo import Repository
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession


def create_dummy_pdf(pages_count: int = 3) -> bytes:
    writer = pypdf.PdfWriter()
    for _ in range(pages_count):
        writer.add_blank_page(width=595, height=842)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def create_dummy_image(fmt: str = "PNG", size=(300, 300)) -> bytes:
    img = Image.new("RGB", size, color=(255, 0, 0))
    stream = io.BytesIO()
    img.save(stream, format=fmt)
    return stream.getvalue()


class TestDocumentSecurity(unittest.TestCase):
    def test_magic_bytes_detection(self):
        pdf_bytes = create_dummy_pdf(1)
        self.assertEqual(DocumentService.detect_file_type(pdf_bytes), "pdf")

        png_bytes = create_dummy_image("PNG")
        self.assertEqual(DocumentService.detect_file_type(png_bytes), "png")

        jpeg_bytes = create_dummy_image("JPEG")
        self.assertEqual(DocumentService.detect_file_type(jpeg_bytes), "jpeg")

        # Fake files / Malicious payload detection
        fake_exe = b"MZ\x90\x00\x03\x00\x00\x00"
        with self.assertRaises(DocumentSecurityError):
            DocumentService.detect_file_type(fake_exe)

        fake_html = b"<!DOCTYPE html><html><body>script</body></html>"
        with self.assertRaises(DocumentSecurityError):
            DocumentService.detect_file_type(fake_html)

        fake_sh = b"#!/bin/bash\nrm -rf /"
        with self.assertRaises(DocumentSecurityError):
            DocumentService.detect_file_type(fake_sh)

    def test_sanitize_filename(self):
        # Path traversal prevention
        self.assertNotIn("..", DocumentService.sanitize_filename("../../../etc/passwd"))
        self.assertNotIn("/", DocumentService.sanitize_filename("folder/document.pdf"))
        self.assertNotIn("\\", DocumentService.sanitize_filename("C:\\Windows\\System32\\cmd.exe"))
        # Command injection prevention
        sanitized = DocumentService.sanitize_filename("file; rm -rf.pdf")
        self.assertNotIn(";", sanitized)

    def test_parse_page_range_valid(self):
        total_pages = 10

        norm, count = DocumentService.parse_page_range("all", total_pages)
        self.assertEqual(norm, "all")
        self.assertEqual(count, 10)

        norm, count = DocumentService.parse_page_range("1, 3, 5", total_pages)
        self.assertEqual(norm, "1, 3, 5")
        self.assertEqual(count, 3)

        norm, count = DocumentService.parse_page_range("2-5", total_pages)
        self.assertEqual(norm, "2-5")
        self.assertEqual(count, 4)

        norm, count = DocumentService.parse_page_range("1-3, 5, 7-8", total_pages)
        self.assertEqual(norm, "1-3, 5, 7-8")
        self.assertEqual(count, 6)

        norm, count = DocumentService.parse_page_range("1, 2, 2, 1-3", total_pages)
        self.assertEqual(norm, "1-3")
        self.assertEqual(count, 3)

    def test_parse_page_range_security_and_invalid(self):
        total_pages = 5

        # Out of bounds
        with self.assertRaises(DocumentSecurityError):
            DocumentService.parse_page_range("1-10", total_pages)

        with self.assertRaises(DocumentSecurityError):
            DocumentService.parse_page_range("0, 2", total_pages)

        with self.assertRaises(DocumentSecurityError):
            DocumentService.parse_page_range("6", total_pages)

        # Inverted range
        with self.assertRaises(DocumentSecurityError):
            DocumentService.parse_page_range("5-2", total_pages)

        # Malicious injection
        with self.assertRaises(DocumentSecurityError):
            DocumentService.parse_page_range("1-3; echo pwned", total_pages)

        with self.assertRaises(DocumentSecurityError):
            DocumentService.parse_page_range("`id`", total_pages)

    def test_process_and_save_upload(self):
        temp_dir = Path(tempfile.mkdtemp())
        try:
            original_spool = settings.SPOOL_DIR
            settings.SPOOL_DIR = temp_dir / "spool"

            pdf_bytes = create_dummy_pdf(3)
            dest_path, total_pages, sanitized_name = DocumentService.process_and_save_upload(
                file_bytes=pdf_bytes,
                original_name="homework.pdf",
                order_uuid="uuid-test-123"
            )

            self.assertTrue(dest_path.exists())
            self.assertEqual(total_pages, 3)
            self.assertEqual(sanitized_name, "homework.pdf")

            # Cleanup check
            DocumentService.cleanup_file(str(dest_path))
            self.assertFalse(dest_path.exists())

            settings.SPOOL_DIR = original_spool
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_image_to_pdf_conversion(self):
        temp_dir = Path(tempfile.mkdtemp())
        try:
            original_spool = settings.SPOOL_DIR
            settings.SPOOL_DIR = temp_dir / "spool"

            png_bytes = create_dummy_image("PNG", size=(800, 600))
            dest_path, total_pages, sanitized_name = DocumentService.process_and_save_upload(
                file_bytes=png_bytes,
                original_name="passport_scan.png",
                order_uuid="uuid-test-image"
            )

            self.assertTrue(dest_path.exists())
            self.assertEqual(total_pages, 1)
            self.assertTrue(dest_path.name.endswith(".pdf"))

            # Verify generated PDF is readable by pypdf
            reader = pypdf.PdfReader(str(dest_path))
            self.assertEqual(len(reader.pages), 1)

            DocumentService.cleanup_file(str(dest_path))
            settings.SPOOL_DIR = original_spool
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestDatabaseAndBalance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_user_lifecycle_and_balance(self):
        async with self.session_maker() as session:
            # 1. Create user
            user = await Repository.get_or_create_user(
                session, user_id=999001, username="cso_student", full_name="Алексей Студент"
            )
            self.assertEqual(user.id, 999001)
            self.assertEqual(user.balance, 0.0)

            # 2. Deposit 100.0 RUB
            success, bal, msg = await Repository.update_balance(
                session,
                user_id=999001,
                delta=100.0,
                trans_type=TransactionType.DEPOSIT,
                payment_method="manual_sbp"
            )
            self.assertTrue(success)
            self.assertEqual(bal, 100.0)

            # 3. Deduct 25.0 RUB for print order
            success, bal, msg = await Repository.update_balance(
                session,
                user_id=999001,
                delta=-25.0,
                trans_type=TransactionType.PRINT_CHARGE,
                payment_method="balance"
            )
            self.assertTrue(success)
            self.assertEqual(bal, 75.0)

            # 4. Attempt to overdraw (attempt to deduct 100.0 RUB when balance is 75.0)
            success, bal, msg = await Repository.update_balance(
                session,
                user_id=999001,
                delta=-100.0,
                trans_type=TransactionType.PRINT_CHARGE,
                payment_method="balance"
            )
            self.assertFalse(success)
            self.assertEqual(bal, 75.0) # Balance remains untouched
            self.assertIn("Недостаточно средств", msg)

            # 5. Refund 25.0 RUB
            success, bal, msg = await Repository.update_balance(
                session,
                user_id=999001,
                delta=25.0,
                trans_type=TransactionType.REFUND,
                payment_method="balance"
            )
            self.assertTrue(success)
            self.assertEqual(bal, 100.0)

    async def test_order_creation_and_queue(self):
        async with self.session_maker() as session:
            user = await Repository.get_or_create_user(session, user_id=999002)

            order = await Repository.create_order(
                session=session,
                user_id=999002,
                original_filename="term_paper.pdf",
                file_path="/data/spool/term_paper.pdf",
                file_size=2048,
                total_pages=15,
                pages_to_print_count=15,
                cost_rub=75.0,
                selected_pages="all",
                copies=1
            )
            self.assertEqual(order.status, OrderStatus.PENDING_CONFIG)
            self.assertIsNotNone(order.order_uuid)

            # Transition to QUEUED
            await Repository.update_order_status(session, order.id, OrderStatus.QUEUED)

            # Spooler pulls next job
            next_job = await Repository.get_next_queued_order(session)
            self.assertIsNotNone(next_job)
            self.assertEqual(next_job.id, order.id)

            queued_count = await Repository.get_queued_orders_count(session)
            self.assertEqual(queued_count, 1)

            # Mark completed
            await Repository.update_order_status(session, order.id, OrderStatus.COMPLETED)
            queued_after = await Repository.get_queued_orders_count(session)
            self.assertEqual(queued_after, 0)


class TestPrinterService(unittest.IsolatedAsyncioTestCase):
    async def test_mock_status_and_print(self):
        orig_mode = settings.PRINTER_MODE
        settings.PRINTER_MODE = "mock"
        try:
            status = await PrinterService.check_status()
            self.assertTrue(status["is_ready"])
            self.assertIn("mock", status["state"])

            temp_dir = Path(tempfile.mkdtemp())
            try:
                dummy_pdf = temp_dir / "print_test.pdf"
                dummy_pdf.write_bytes(create_dummy_pdf(1))

                success, msg, job_id = await PrinterService.print_job(
                    pdf_path=dummy_pdf,
                    copies=1,
                    selected_pages="1",
                    title="TestPrint"
                )
                self.assertTrue(success)
                self.assertIsNotNone(job_id)

                # Test missing file error handling
                missing_pdf = temp_dir / "nonexistent.pdf"
                success, msg, job_id = await PrinterService.print_job(pdf_path=missing_pdf)
                self.assertFalse(success)
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
        finally:
            settings.PRINTER_MODE = orig_mode


class TestEcoAndPerformanceOptimizations(unittest.IsolatedAsyncioTestCase):
    def test_document_image_enhancement(self):
        # Тест отбеливания фона (серый фон -> 255, темный текст -> 0)
        img = Image.new("L", (100, 100), color=215) # Серый фон мобильного фото
        # Рисуем темный символ
        for y in range(40, 60):
            for x in range(40, 60):
                img.putpixel((x, y), 50)

        enhanced = DocumentService.enhance_document_image(img)
        # Фон должен стать идеально белым (экономия тонера)
        self.assertEqual(enhanced.getpixel((10, 10)), 255)
        # Текст должен стать глубоким черным
        self.assertEqual(enhanced.getpixel((50, 50)), 0)

    def test_blank_page_detection(self):
        temp_dir = Path(tempfile.mkdtemp())
        try:
            pdf_path = temp_dir / "test_blank.pdf"
            writer = pypdf.PdfWriter()
            # 1. Страница с текстом
            text_img = Image.new("L", (595, 842), 255)
            draw = ImageDraw.Draw(text_img)
            draw.text((100, 100), "Hello Student", fill=0)
            buf1 = io.BytesIO()
            text_img.save(buf1, "PDF")
            buf1.seek(0)
            writer.add_page(pypdf.PdfReader(buf1).pages[0])

            # 2. Полностью пустая страница
            writer.add_blank_page(width=595, height=842)

            with open(str(pdf_path), "wb") as f:
                writer.write(f)

            non_blank, blank = DocumentService.get_non_blank_pages(pdf_path)
            self.assertEqual(non_blank, [1])
            self.assertEqual(blank, [2])

            # Проверка, что prepare_job_pdf пропускает пустую страницу при AUTO_SKIP_BLANK_PAGES
            prepared = DocumentService.prepare_job_pdf(pdf_path, "test_order", "all", copies=1)
            prep_reader = pypdf.PdfReader(str(prepared))
            self.assertEqual(len(prep_reader.pages), 1)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def test_atomic_cas_transition(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with session_maker() as session:
            user = User(id=777, username="student_test", full_name="Student Test", balance=100.0)
            session.add(user)
            await session.commit()

            order = await Repository.create_order(
                session=session,
                user_id=777,
                original_filename="doc.pdf",
                file_path="/tmp/doc.pdf",
                file_size=1024,
                total_pages=2,
                pages_to_print_count=2,
                cost_rub=10.0
            )
            order.status = OrderStatus.PENDING_PAYMENT
            await session.commit()

            # Первая попытка CAS (успех)
            ok1 = await Repository.transition_order_status_atomic(
                session, order.id, OrderStatus.PENDING_PAYMENT, OrderStatus.QUEUED
            )
            self.assertTrue(ok1)

            # Вторая попытка CAS от того же статуса (должна вернуть False - защита от race condition)
            ok2 = await Repository.transition_order_status_atomic(
                session, order.id, OrderStatus.PENDING_PAYMENT, OrderStatus.QUEUED
            )
            self.assertFalse(ok2)

        await engine.dispose()


class TestLowMemoryAndRouterOptimization(unittest.IsolatedAsyncioTestCase):
    async def test_low_memory_config_and_autodetect(self):
        orig_low = settings.LOW_MEMORY_MODE
        orig_dpi = settings.RENDER_DPI
        try:
            settings.LOW_MEMORY_MODE = True
            settings.RENDER_DPI = 0
            self.assertTrue(settings.is_low_memory)
            self.assertEqual(settings.effective_dpi, 150)
            self.assertEqual(settings.effective_max_pages, 40)
            self.assertEqual(settings.effective_max_file_size, 15 * 1024 * 1024)
            self.assertEqual(settings.effective_min_free_disk_mb, 25)
            self.assertEqual(settings.effective_spool_dir, Path("/tmp/printer_bot_spool"))

            # Проверка возврата в desktop режим
            settings.LOW_MEMORY_MODE = False
            self.assertFalse(settings.is_low_memory)
            self.assertEqual(settings.effective_dpi, 300)
            self.assertEqual(settings.effective_max_pages, 200)
        finally:
            settings.LOW_MEMORY_MODE = orig_low
            settings.RENDER_DPI = orig_dpi

    async def test_streaming_image_conversion_low_memory(self):
        orig_low = settings.LOW_MEMORY_MODE
        orig_spool = settings.SPOOL_DIR
        temp_dir = Path(tempfile.mkdtemp())
        try:
            settings.LOW_MEMORY_MODE = True
            settings.SPOOL_DIR = temp_dir / "spool"

            # Создаем тестовое изображение
            img_bytes = create_dummy_image("PNG", size=(600, 800))
            dest_pdf = temp_dir / "test_stream_img.pdf"

            pages = DocumentService.convert_image_to_a4_pdf(img_bytes, dest_pdf)
            self.assertEqual(pages, 1)
            self.assertTrue(dest_pdf.exists())

            # Проверяем геометрию созданного PDF
            reader = pypdf.PdfReader(str(dest_pdf))
            self.assertEqual(len(reader.pages), 1)
            mbox = reader.pages[0].mediabox
            # При 150 DPI A4 = 595.2 x 841.92 pt
            self.assertAlmostEqual(float(mbox.width), 595.2, delta=1.0)
            self.assertAlmostEqual(float(mbox.height), 841.92, delta=1.0)
        finally:
            settings.LOW_MEMORY_MODE = orig_low
            settings.SPOOL_DIR = orig_spool
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def test_streaming_txt_conversion_low_memory(self):
        orig_low = settings.LOW_MEMORY_MODE
        orig_spool = settings.SPOOL_DIR
        temp_dir = Path(tempfile.mkdtemp())
        try:
            settings.LOW_MEMORY_MODE = True
            settings.SPOOL_DIR = temp_dir / "spool"

            # Генерируем текст на 2 страницы (60 строк)
            text_content = "\n".join([f"Строка отчета {i}: Тестирование OpenWrt Netis NX31" for i in range(1, 65)])
            dest_pdf = temp_dir / "test_stream_txt.pdf"

            pages = DocumentService.convert_txt_to_a4_pdf(text_content.encode("utf-8"), dest_pdf)
            self.assertEqual(pages, 2)
            self.assertTrue(dest_pdf.exists())

            reader = pypdf.PdfReader(str(dest_pdf))
            self.assertEqual(len(reader.pages), 2)
        finally:
            settings.LOW_MEMORY_MODE = orig_low
            settings.SPOOL_DIR = orig_spool
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def test_adaptive_sqlite_pragmas(self):
        orig_low = settings.LOW_MEMORY_MODE
        temp_dir = Path(tempfile.mkdtemp())
        try:
            settings.LOW_MEMORY_MODE = True
            from database.db import set_sqlite_pragma
            import sqlite3

            test_db = temp_dir / "test_pragma.db"
            conn = sqlite3.connect(str(test_db))
            set_sqlite_pragma(conn, None)
            cursor = conn.cursor()

            # В Low-Memory режиме:
            cache_size = cursor.execute("PRAGMA cache_size;").fetchone()[0]
            mmap_size = cursor.execute("PRAGMA mmap_size;").fetchone()[0]
            temp_store = cursor.execute("PRAGMA temp_store;").fetchone()[0]

            self.assertEqual(cache_size, -2000)  # 2 МБ кэш
            self.assertEqual(mmap_size, 0)      # mmap отключен для роутеров
            self.assertEqual(temp_store, 1)    # FILE temp store (0=DEFAULT, 1=FILE, 2=MEMORY)
            cursor.close()
            conn.close()
        finally:
            settings.LOW_MEMORY_MODE = orig_low
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def test_chunked_raw_socket_streaming(self):
        # Проверка потоковой чанковой передачи по 64 КБ без открытия внешних сокетов
        temp_dir = Path(tempfile.mkdtemp())
        orig_host = settings.PRINTER_HOST
        orig_port = settings.PRINTER_PORT
        try:
            settings.PRINTER_HOST = "192.168.3.13"
            settings.PRINTER_PORT = 9100

            # Создаем тестовый файл размером ~130 КБ (требует 3 чанка: 64КБ + 64КБ + остаток)
            test_pdf = temp_dir / "mock_stream.pdf"
            sample_content = b"%PDF-1.4 " + (b"X" * 131072) + b" %%EOF"
            test_pdf.write_bytes(sample_content)

            mock_reader = AsyncMock()
            # Имитация отсутствия раннего сброса соединения (read возвращает таймаут)
            mock_reader.read = AsyncMock(side_effect=asyncio.TimeoutError())

            sent_chunks = []
            mock_writer = MagicMock()
            mock_writer.write = MagicMock(side_effect=lambda data: sent_chunks.append(data))
            mock_writer.drain = AsyncMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()

            with patch("asyncio.open_connection", new_callable=AsyncMock) as mock_connect:
                mock_connect.return_value = (mock_reader, mock_writer)
                success, msg, job_id = await PrinterService._print_raw(test_pdf)

                self.assertTrue(success)
                self.assertIn("RAW-SOCKET-JOB", job_id)

                # Проверяем целостность переданных данных
                full_sent = b"".join(sent_chunks)
                self.assertEqual(full_sent, sample_content)

                # Проверяем, что файл передавался блоками по 64 КБ (защита RAM роутера)
                self.assertGreaterEqual(len(sent_chunks), 2)
                for chunk in sent_chunks[:-1]:
                    self.assertEqual(len(chunk), 65536)
        finally:
            settings.PRINTER_HOST = orig_host
            settings.PRINTER_PORT = orig_port
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def test_process_and_save_upload_streaming(self):
        # Проверка прямого перемещения файла на диск без буферизации в RAM
        temp_dir = Path(tempfile.mkdtemp())
        orig_spool = settings.SPOOL_DIR
        try:
            settings.SPOOL_DIR = temp_dir / "spool"
            raw_file = temp_dir / "test_raw_upload.pdf"
            # Минимальный валидный PDF
            writer = pypdf.PdfWriter()
            writer.add_blank_page(width=595.2, height=841.92)
            with open(raw_file, "wb") as f:
                writer.write(f)

            dest_path, total_pages, sanitized_name = DocumentService.process_and_save_upload(
                original_name="MyDocument.pdf",
                order_uuid="test-uuid-streaming",
                source_file_path=raw_file
            )

            self.assertEqual(total_pages, 1)
            self.assertEqual(sanitized_name, "MyDocument.pdf")
            self.assertTrue(dest_path.exists())
            self.assertFalse(raw_file.exists())  # Перемещен, исходного нет
        finally:
            settings.SPOOL_DIR = orig_spool
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def test_raw_socket_deep_sleep_retry(self):
        # Проверка повторных попыток пробуждения принтера из энергосберегающего сна (Deep Sleep)
        temp_dir = Path(tempfile.mkdtemp())
        orig_host = settings.PRINTER_HOST
        try:
            settings.PRINTER_HOST = "192.168.3.13"
            test_pdf = temp_dir / "retry_test.pdf"
            test_pdf.write_bytes(b"%PDF-1.4 mock content %%EOF")

            mock_reader = AsyncMock()
            mock_reader.read = AsyncMock(side_effect=asyncio.TimeoutError())
            mock_writer = MagicMock()
            mock_writer.write = MagicMock()
            mock_writer.drain = AsyncMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()

            # 1-я попытка таймаут (сон), 2-я успешная
            attempts = 0
            async def fake_open_connection(h, p):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise asyncio.TimeoutError("Deep sleep ASIC timeout")
                return mock_reader, mock_writer

            with patch("asyncio.open_connection", side_effect=fake_open_connection):
                with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                    success, msg, job_id = await PrinterService._print_raw(test_pdf)
                    self.assertTrue(success)
                    self.assertEqual(attempts, 2)
                    mock_sleep.assert_awaited()
        finally:
            settings.PRINTER_HOST = orig_host
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def test_database_backup_online_gzip(self):
        # Проверка онлайн бэкапа SQLite с gzip и ротацией до 3 копий
        temp_dir = Path(tempfile.mkdtemp())
        try:
            import gzip
            test_db = temp_dir / "test_backup.db"
            test_engine = create_async_engine(f"sqlite+aiosqlite:///{test_db.resolve()}")
            async with test_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            test_session_maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
            backup_dir = temp_dir / "backups"

            async with test_session_maker() as session:
                backup_file = await Repository.backup_database(backup_dir=backup_dir, session=session)

            self.assertIsNotNone(backup_file)
            self.assertTrue(backup_file.exists())
            self.assertTrue(backup_file.name.endswith(".db.gz"))

            # Проверяем, что бэкап распаковывается и данные являются валидной SQLite базой
            with gzip.open(backup_file, "rb") as gz_in:
                decompressed = gz_in.read()
            self.assertTrue(decompressed.startswith(b"SQLite format 3"))

            # Проверяем ротацию: создаем еще 4 фиктивных бэкапа и проверяем, что хранится максимум 3
            for i in range(4):
                dummy_bak = backup_dir / f"printer_bot_backup_20260916_00000{i}.db.gz"
                dummy_bak.write_bytes(b"dummy")

            async with test_session_maker() as session:
                await Repository.backup_database(backup_dir=backup_dir, session=session)

            remaining_backups = list(backup_dir.glob("printer_bot_backup_*.db.gz"))
            self.assertLessEqual(len(remaining_backups), 3)

            await test_engine.dispose()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_enhance_scanned_pdf(self):
        temp_dir = Path(tempfile.mkdtemp())
        try:
            # Создаем тестовый PDF с одной растровой страницей (серый фон 215, темный текст)
            scan_img = Image.new("L", (300, 400), color=215)
            draw = ImageDraw.Draw(scan_img)
            draw.rectangle([50, 50, 150, 100], fill=40)
            
            buf = io.BytesIO()
            scan_img.save(buf, format="PDF")
            buf.seek(0)
            
            test_pdf = temp_dir / "test_scan.pdf"
            with open(test_pdf, "wb") as f:
                f.write(buf.getvalue())
                
            enhanced = DocumentService.enhance_scanned_pdf_if_needed(test_pdf)
            self.assertTrue(enhanced)
            self.assertTrue(test_pdf.exists())
            
            # Проверяем, что в результирующем файле фон стал белым
            reader = pypdf.PdfReader(str(test_pdf))
            self.assertEqual(len(reader.pages), 1)
            out_img = Image.open(io.BytesIO(reader.pages[0].images[0].data))
            # Фон (10, 10) должен быть 255
            self.assertEqual(out_img.getpixel((10, 10)), 255)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


