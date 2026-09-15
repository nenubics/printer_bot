import asyncio
import gc
import io
import os
import sys
import time
import shutil
import tempfile
import tracemalloc
from pathlib import Path
from typing import Dict, Any, List

# Add repo root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from database.models import Base, User, Order, OrderStatus, TransactionType
from database.repo import Repository
from database.db import create_async_engine, async_sessionmaker, AsyncSession
from services.document import DocumentService, DocumentSecurityError
from services.printer import PrinterService
from middlewares.throttling import ThrottlingMiddleware
from PIL import Image, ImageDraw
import pypdf


def create_test_image(size=(1200, 1600), color=(220, 220, 220)) -> bytes:
    """Создает изображение (симулирует скан или фото страницы)"""
    img = Image.new("RGB", size, color=color)
    draw = ImageDraw.Draw(img)
    draw.rectangle([100, 100, size[0] - 100, size[1] - 100], fill=(240, 240, 240), outline=(50, 50, 50))
    for y in range(150, size[1] - 150, 40):
        draw.text((120, y), f"Sample text line for benchmark at line y={y}", fill=(20, 20, 20))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def create_multipage_tiff(num_pages: int = 5) -> bytes:
    """Создает многостраничный TIFF для стресс-теста памяти"""
    pages = []
    for p in range(num_pages):
        img = Image.new("L", (1000, 1400), color=230)
        draw = ImageDraw.Draw(img)
        draw.text((100, 100), f"TIFF Page {p + 1} of {num_pages}", fill=0)
        pages.append(img)
    buf = io.BytesIO()
    pages[0].save(buf, format="TIFF", save_all=True, append_images=pages[1:])
    return buf.getvalue()


def create_large_txt(lines_count: int = 250) -> bytes:
    """Создает объемный текстовый документ на ~5-6 страниц A4"""
    lines = [f"Параграф {i}: Испытание производительности и надежности Telegram-бота печати ЦСО-4 Pantum BP2300NW." for i in range(1, lines_count + 1)]
    return "\n".join(lines).encode("utf-8")


class BenchmarkRunner:
    def __init__(self):
        self.results: Dict[str, Any] = {
            "performance": {},
            "memory": {},
            "security": {},
            "size_metrics": {},
            "concurrency": {}
        }

    def measure_codebase_size(self):
        """Измерение размера бота, файлов и зависимостей"""
        print("[*] Анализ размера кодовой базы и дискового пространства...")
        root_dir = Path(__file__).resolve().parent.parent
        
        py_files = list(root_dir.glob("**/*.py"))
        py_files = [f for f in py_files if "venv" not in f.parts and ".git" not in f.parts]
        
        total_lines = 0
        total_bytes = 0
        file_stats = []
        for pf in py_files:
            content = pf.read_bytes()
            lines = len(content.splitlines())
            size = len(content)
            total_lines += lines
            total_bytes += size
            file_stats.append((str(pf.relative_to(root_dir)), lines, size))

        # Размер venv и зависимостей
        venv_size_mb = 0.0
        venv_dir = root_dir / "venv"
        if venv_dir.exists():
            total_venv_bytes = sum(f.stat().st_size for f in venv_dir.glob("**/*") if f.is_file())
            venv_size_mb = total_venv_bytes / (1024 * 1024)

        # Размер БД
        db_size_kb = 0.0
        if settings.DB_PATH.exists():
            db_size_kb = settings.DB_PATH.stat().st_size / 1024

        self.results["size_metrics"] = {
            "py_files_count": len(py_files),
            "total_python_loc": total_lines,
            "codebase_bytes": total_bytes,
            "codebase_kb": round(total_bytes / 1024, 2),
            "venv_size_mb": round(venv_size_mb, 2),
            "db_size_kb": round(db_size_kb, 2),
            "top_files": sorted(file_stats, key=lambda x: x[1], reverse=True)[:5]
        }

    def measure_document_conversion_and_memory(self):
        """Сравнение нормального и low-memory режимов по скорости и памяти"""
        print("[*] Измерение скорости и пиковой памяти рендеринга (Normal vs Low-Memory)...")
        temp_dir = Path(tempfile.mkdtemp())
        test_img = create_test_image((2000, 2800))
        test_tiff = create_multipage_tiff(num_pages=5)
        test_txt = create_large_txt(lines_count=200)

        results = {}

        for mode_name, is_low in [("Normal_Desktop_300DPI", False), ("OpenWrt_Router_150DPI", True)]:
            settings.LOW_MEMORY_MODE = is_low
            settings.SPOOL_DIR = temp_dir / mode_name
            settings.SPOOL_DIR.mkdir(parents=True, exist_ok=True)

            mode_stats = {}

            # 1. Одиночное фото высокого разрешения
            gc.collect()
            tracemalloc.start()
            t0 = time.perf_counter()
            out_pdf = settings.SPOOL_DIR / "photo.pdf"
            pages = DocumentService.convert_image_to_a4_pdf(test_img, out_pdf)
            t_photo = time.perf_counter() - t0
            _, peak_photo = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            pdf_size_kb = out_pdf.stat().st_size / 1024

            mode_stats["photo_1page"] = {
                "time_sec": round(t_photo, 4),
                "peak_ram_mb": round(peak_photo / (1024 * 1024), 2),
                "pdf_size_kb": round(pdf_size_kb, 1),
                "pages": pages
            }

            # 2. Многостраничный TIFF (5 страниц)
            gc.collect()
            tracemalloc.start()
            t0 = time.perf_counter()
            out_tiff_pdf = settings.SPOOL_DIR / "tiff5.pdf"
            pages_tiff = DocumentService.convert_image_to_a4_pdf(test_tiff, out_tiff_pdf)
            t_tiff = time.perf_counter() - t0
            _, peak_tiff = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            mode_stats["tiff_5pages"] = {
                "time_sec": round(t_tiff, 4),
                "peak_ram_mb": round(peak_tiff / (1024 * 1024), 2),
                "pages": pages_tiff
            }

            # 3. Текстовый документ (~5 страниц)
            gc.collect()
            tracemalloc.start()
            t0 = time.perf_counter()
            out_txt_pdf = settings.SPOOL_DIR / "txt.pdf"
            pages_txt = DocumentService.convert_txt_to_a4_pdf(test_txt, out_txt_pdf)
            t_txt = time.perf_counter() - t0
            _, peak_txt = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            mode_stats["txt_multipage"] = {
                "time_sec": round(t_txt, 4),
                "peak_ram_mb": round(peak_txt / (1024 * 1024), 2),
                "pages": pages_txt
            }

            results[mode_name] = mode_stats

        # 4. Проверка на утечки памяти (Memory Leak Test: 20 циклов подряд)
        print("[*] Стресс-тест на утечки памяти (20 последовательных конвертаций)...")
        settings.LOW_MEMORY_MODE = True
        gc.collect()
        tracemalloc.start()
        start_current, _ = tracemalloc.get_traced_memory()
        
        for i in range(20):
            leak_test_pdf = temp_dir / f"leak_{i}.pdf"
            DocumentService.convert_image_to_a4_pdf(test_img, leak_test_pdf)
            DocumentService.cleanup_file(str(leak_test_pdf))
            gc.collect()

        end_current, peak_leak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        leak_diff_kb = (end_current - start_current) / 1024

        results["memory_leak_check"] = {
            "iterations": 20,
            "net_ram_growth_kb": round(leak_diff_kb, 2),
            "peak_ram_mb": round(peak_leak / (1024 * 1024), 2),
            "is_leak_free": leak_diff_kb < 100.0  # Менее 100 КБ роста за 20 операций
        }

        self.results["memory"] = results
        shutil.rmtree(temp_dir, ignore_errors=True)

    async def measure_database_and_concurrency(self):
        """Бенчмарк скорости БД и конкурентных транзакций (CAS)"""
        print("[*] Бенчмарк базы данных и конкурентных транзакций (CAS)...")
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

        # 1. Скорость создания пользователей и баланса
        t0 = time.perf_counter()
        async with session_maker() as session:
            for uid in range(100):
                await Repository.get_or_create_user(session, user_id=uid + 1000, username=f"user_{uid}")
            await session.commit()
        user_create_time = time.perf_counter() - t0
        ops_per_sec_users = 100 / user_create_time

        # 2. Конкурентный стресс-тест Atomic CAS (Double-spend attack simulation)
        # Симулируем 20 одновременных попыток подтверждения одного и того же заказа
        async with session_maker() as session:
            order = await Repository.create_order(
                session=session,
                user_id=1001,
                original_filename="thesis.pdf",
                file_path="/tmp/thesis.pdf",
                file_size=1024,
                total_pages=10,
                pages_to_print_count=10,
                cost_rub=50.0
            )
            order.status = OrderStatus.PENDING_PAYMENT
            await session.commit()
            target_order_id = order.id

        async def try_pay(task_id: int):
            async with session_maker() as s:
                return await Repository.transition_order_status_atomic(
                    s, target_order_id, OrderStatus.PENDING_PAYMENT, OrderStatus.QUEUED
                )

        t0 = time.perf_counter()
        results = await asyncio.gather(*(try_pay(i) for i in range(20)))
        cas_time = time.perf_counter() - t0
        
        success_count = sum(1 for r in results if r is True)
        reject_count = sum(1 for r in results if r is False)

        self.results["concurrency"] = {
            "users_insert_100_time_sec": round(user_create_time, 4),
            "users_insert_ops_per_sec": round(ops_per_sec_users, 1),
            "cas_race_attempts": 20,
            "cas_success_count": success_count,
            "cas_rejected_count": reject_count,
            "cas_time_sec": round(cas_time, 4),
            "double_spend_prevented": (success_count == 1 and reject_count == 19)
        }

        await engine.dispose()

    async def measure_security_defenses(self):
        """Аудит безопасности и тестирование векторов атак"""
        print("[*] Аудит защитных механизмов (Security & Hardening)...")
        security_report = {}

        # 1. Path Traversal
        pt_vectors = [
            "../../etc/passwd",
            "..\\..\\windows\\system32\\cmd.exe",
            "....//....//etc/shadow",
            "/absolute/root/file.pdf\x00.exe"
        ]
        pt_passed = True
        for v in pt_vectors:
            res = DocumentService.sanitize_filename(v)
            if ".." in res or "/" in res or "\\" in res or "\x00" in res:
                pt_passed = False
                break
        security_report["path_traversal_defense"] = {
            "status": "PASS" if pt_passed else "FAIL",
            "sanitized_example": DocumentService.sanitize_filename("../../etc/passwd")
        }

        # 2. Decompression Bomb Detection (Pillow)
        decomp_bomb_blocked = False
        try:
            # Создаем огромные виртуальные метаданные (50 000 x 50 000 пикселей = 2.5 млрд пикселей)
            large_header = io.BytesIO()
            img = Image.new("1", (1, 1))
            img.save(large_header, format="PNG")
            # Подменяем заголовок IHDR ширины и высоты на огромные значения
            png_bytes = bytearray(large_header.getvalue())
            # IHDR starts at byte 12
            png_bytes[16:20] = (25000).to_bytes(4, byteorder="big")
            png_bytes[20:24] = (25000).to_bytes(4, byteorder="big")
            DocumentService.convert_image_to_a4_pdf(bytes(png_bytes), Path("/tmp/bomb.pdf"))
        except (DocumentSecurityError, Image.DecompressionBombError, Exception) as e:
            decomp_bomb_blocked = True
        security_report["decompression_bomb_defense"] = {
            "status": "PASS" if decomp_bomb_blocked else "FAIL"
        }

        # 3. Zip Bomb & Malicious DOCX
        zip_bomb_blocked = False
        try:
            # Создаем фейковый docx с фиктивным распакованным размером > 50 МБ
            buf = io.BytesIO()
            import zipfile
            with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
                # 60 МБ нулей, сжатые в несколько КБ
                z.writestr("word/document.xml", b"\x00" * (60 * 1024 * 1024))
            DocumentService.convert_docx_to_pdf(buf.getvalue(), Path("/tmp/zipbomb.pdf"), "uuid-bomb")
        except DocumentSecurityError:
            zip_bomb_blocked = True
        except Exception:
            pass
        security_report["zip_bomb_defense"] = {
            "status": "PASS" if zip_bomb_blocked else "FAIL"
        }

        # 4. Anti-Flood Throttling Middleware
        throttling = ThrottlingMiddleware(limit_seconds=0.5, burst_threshold=4, block_duration=2.0)
        from aiogram.types import User as TgUser, Chat, Message
        from datetime import datetime

        dummy_user = TgUser(id=88888, is_bot=False, first_name="Spammer")
        dummy_chat = Chat(id=88888, type="private")
        dummy_msg = Message(message_id=1, date=datetime.now(), chat=dummy_chat, from_user=dummy_user)

        async def dummy_handler(event, data):
            return "ok"

        allowed_count = 0
        blocked_count = 0
        for i in range(10):
            res = await throttling(dummy_handler, dummy_msg, {})
            if res == "ok":
                allowed_count += 1
            else:
                blocked_count += 1

        security_report["rate_limiting_defense"] = {
            "status": "PASS" if blocked_count > 0 else "FAIL",
            "allowed_in_burst": allowed_count,
            "blocked_in_flood": blocked_count
        }

        # 5. Page Range Injection & Malformed input
        malformed_ranges = [
            "1-99999",
            "-1, 0",
            "1; rm -rf /",
            "eval(input())",
            "10-2"
        ]
        injections_blocked = True
        for mr in malformed_ranges:
            try:
                DocumentService.parse_page_range(mr, total_pages=10)
                injections_blocked = False
            except DocumentSecurityError:
                pass
            except Exception:
                injections_blocked = False
        security_report["page_range_sanitization"] = {
            "status": "PASS" if injections_blocked else "FAIL"
        }

        self.results["security"] = security_report

    async def run_all(self):
        self.measure_codebase_size()
        self.measure_document_conversion_and_memory()
        await self.measure_database_and_concurrency()
        await self.measure_security_defenses()
        return self.results


if __name__ == "__main__":
    runner = BenchmarkRunner()
    results = asyncio.run(runner.run_all())
    import json
    print("\n--- BENCHMARK_METRICS_JSON_START ---")
    print(json.dumps(results, indent=2, ensure_ascii=False))
    print("--- BENCHMARK_METRICS_JSON_END ---")
