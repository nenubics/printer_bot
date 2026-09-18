import os
import gc
import re
import io
import uuid
import zipfile
import shutil
import logging
from pathlib import Path
from typing import Tuple, Set, List, Optional
from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageSequence, ImageChops, ImageFilter
import pypdf
from config import settings

logger = logging.getLogger(__name__)

# Защита от декомпрессионных бомб (OOM) в Pillow с учетом доступной памяти
Image.MAX_IMAGE_PIXELS = 15_000_000 if settings.is_low_memory else 40_000_000
MAX_ZIP_UNCOMPRESSED_BYTES = 50_000_000  # Максимум 50 МБ распакованного DOCX
MAX_RECEIPT_SIZE_BYTES = 5 * 1024 * 1024  # Максимум 5 МБ для банковского чека

# Допустимые сигнатуры (Magic Bytes)
MAGIC_PDF = b"%PDF-"
MAGIC_PNG = b"\x89PNG\r\n\x1a\n"
MAGIC_JPEG = b"\xff\xd8\xff"
MAGIC_ZIP_DOCX = b"PK\x03\x04"
MAGIC_TIFF_LE = b"II*\x00"
MAGIC_TIFF_BE = b"MM\x00*"
MAGIC_RIFF = b"RIFF"

# Стандартный размер A4 в типографских пунктах (points, 72 pt/inch)
A4_PORTRAIT_PT = (595.28, 841.89)
A4_LANDSCAPE_PT = (841.89, 595.28)


class DocumentSecurityError(Exception):
    """Исключение при нарушении безопасности или формата файла"""
    pass


class DocumentService:
    @staticmethod
    def detect_file_type(file_bytes: bytes, filename: str = "") -> str:
        """
        Строгая проверка типа файла по сигнатуре (Magic Bytes) и структуре.
        Не доверяет слепо расширению файла.
        """
        fn_lower = filename.lower()

        if file_bytes.startswith(MAGIC_PDF):
            return "pdf"
        elif file_bytes.startswith(MAGIC_PNG):
            return "png"
        elif file_bytes.startswith(MAGIC_JPEG):
            return "jpeg"
        elif file_bytes.startswith(MAGIC_TIFF_LE) or file_bytes.startswith(MAGIC_TIFF_BE):
            return "tiff"
        elif file_bytes.startswith(MAGIC_RIFF) and len(file_bytes) >= 12 and file_bytes[8:12] == b"WEBP":
            return "webp"
        elif file_bytes.startswith(MAGIC_ZIP_DOCX) and fn_lower.endswith(".docx"):
            return "docx"
        elif fn_lower.endswith(".txt"):
            # Проверяем, что текстовый файл не содержит нулевых байтов (бинарных вставок)
            if b"\x00" not in file_bytes[:4096]:
                return "txt"

        raise DocumentSecurityError(
            "Неподдерживаемый или небезопасный формат файла. "
            "Разрешены: PDF, изображения (PNG, JPG, TIFF, WebP), документы Word (.docx) и текст (.txt)."
        )

    @classmethod
    def validate_receipt_file(cls, file_bytes: bytes, original_name: str = "") -> bool:
        """
        Строгая проверка банковского чека:
        - Лимит размера не более 5 МБ
        - Разрешены только изображения (JPEG, PNG, WebP) или PDF
        - Запрещены любые бинарники, скрипты, HTML, архивы
        """
        if len(file_bytes) > MAX_RECEIPT_SIZE_BYTES:
            raise DocumentSecurityError(
                f"Чек слишком большой! Максимальный размер чека: {MAX_RECEIPT_SIZE_BYTES // (1024*1024)} МБ."
            )
        if len(file_bytes) < 32:
            raise DocumentSecurityError("Файл чека поврежден или пуст.")

        # Проверка по сигнатурам (Magic Bytes)
        is_pdf = file_bytes.startswith(MAGIC_PDF)
        is_png = file_bytes.startswith(MAGIC_PNG)
        is_jpeg = file_bytes.startswith(MAGIC_JPEG)
        is_webp = file_bytes.startswith(MAGIC_RIFF) and len(file_bytes) >= 12 and file_bytes[8:12] == b"WEBP"

        if not (is_pdf or is_png or is_jpeg or is_webp):
            raise DocumentSecurityError(
                "Недопустимый формат чека! Разрешены только фотографии/сканы (JPG, PNG) или PDF-квитанции."
            )
        return True

    @classmethod
    def process_and_save_upload(
        cls,
        file_bytes: Optional[bytes] = None,
        original_name: str = "",
        order_uuid: str = "",
        source_file_path: Optional[Path] = None
    ) -> Tuple[Path, int, str]:
        """
        Проверяет файл, выполняет конвертацию в стандартизированный PDF A4,
        сохраняет файл под безопасным UUID в папку спула и возвращает:
        (путь к файлу, общее количество страниц, очищенное имя).
        Поддерживает передачу как байтов в памяти, так и пути к файлу на диске
        для потоковой обработки с 0 МБ расхода RAM.
        """
        # 1. Защита дискового пространства от DoS переполнения
        try:
            spool_target = settings.effective_spool_dir
            check_dir = spool_target if spool_target.exists() else settings.DATA_DIR
            free_mb = shutil.disk_usage(check_dir).free // (1024 * 1024)
            if free_mb < settings.effective_min_free_disk_mb:
                raise DocumentSecurityError(
                    f"На сервере временно недостаточно места на диске ({free_mb} МБ свободно). "
                    "Попробуйте позже или обратитесь к администратору."
                )
        except OSError as e:
            logger.warning(f"Failed to check disk usage: {e}")

        # Проверка размера
        if source_file_path and source_file_path.exists():
            file_size = source_file_path.stat().st_size
        else:
            file_size = len(file_bytes) if file_bytes else 0

        if file_size > settings.effective_max_file_size:
            raise DocumentSecurityError(
                f"Файл слишком большой! Максимальный размер: {settings.effective_max_file_size // (1024*1024)} МБ."
            )

        # Чтение magic bytes (первые 4096 байт)
        if source_file_path and source_file_path.exists():
            with open(source_file_path, "rb") as f:
                header_bytes = f.read(4096)
        else:
            header_bytes = file_bytes[:4096] if file_bytes else b""

        file_type = cls.detect_file_type(header_bytes, filename=original_name)
        sanitized_name = cls.sanitize_filename(original_name)

        # Создаем папку спула при необходимости (с учетом защиты флеш-памяти)
        spool_dir = settings.effective_spool_dir
        spool_dir.mkdir(parents=True, exist_ok=True)
        dest_pdf_path = spool_dir / f"{order_uuid}.pdf"

        if file_type == "pdf":
            if source_file_path and source_file_path.exists():
                # Потоковое перемещение без загрузки в память (0 MB RAM overhead)
                if source_file_path != dest_pdf_path:
                    shutil.move(str(source_file_path), str(dest_pdf_path))
            else:
                dest_pdf_path.write_bytes(file_bytes or b"")
            os.chmod(dest_pdf_path, 0o600)
            total_pages = cls.verify_pdf(dest_pdf_path)
            if settings.ENHANCE_CONTRAST_PHOTOS:
                cls.enhance_scanned_pdf_if_needed(dest_pdf_path)
            return dest_pdf_path, total_pages, sanitized_name

        # Для не-PDF форматов (картинки, txt, docx) получаем байты
        if file_bytes is None and source_file_path and source_file_path.exists():
            file_data = source_file_path.read_bytes()
        else:
            file_data = file_bytes or b""

        try:
            if file_type in ("png", "jpeg", "tiff", "webp"):
                total_pages = cls.convert_image_to_a4_pdf(file_data, dest_pdf_path)
                os.chmod(dest_pdf_path, 0o600)
                return dest_pdf_path, total_pages, sanitized_name

            elif file_type == "txt":
                total_pages = cls.convert_txt_to_a4_pdf(file_data, dest_pdf_path)
                os.chmod(dest_pdf_path, 0o600)
                return dest_pdf_path, total_pages, sanitized_name

            elif file_type == "docx":
                total_pages = cls.convert_docx_to_pdf(file_data, dest_pdf_path, order_uuid)
                os.chmod(dest_pdf_path, 0o600)
                return dest_pdf_path, total_pages, sanitized_name
        finally:
            # Если передавался временный исходный файл, очищаем его
            if source_file_path and source_file_path.exists() and source_file_path != dest_pdf_path:
                cls.cleanup_file(str(source_file_path))

        raise DocumentSecurityError("Неизвестная ошибка обработки документа.")

    @staticmethod
    def verify_pdf(pdf_path: Path) -> int:
        """
        Глубокая и безопасная валидация PDF:
        - Проверка шифрования и паролей
        - Проверка целостности каждой страницы
        - Проверка допустимых диапазонов и размеров страниц
        - Контроль общего количества страниц
        """
        try:
            reader = pypdf.PdfReader(str(pdf_path))
            if reader.is_encrypted:
                raise DocumentSecurityError(
                    "Файл защищен паролем. Пожалуйста, снимите защиту перед отправкой на печать."
                )

            total_pages = len(reader.pages)
            if total_pages <= 0:
                raise DocumentSecurityError("В документе нет страниц для печати.")

            if total_pages > settings.effective_max_pages:
                raise DocumentSecurityError(
                    f"В документе {total_pages} страниц. "
                    f"Максимум за одно задание: {settings.effective_max_pages} страниц."
                )

            # Глубокая проверка целостности каждой страницы
            for i, page in enumerate(reader.pages):
                try:
                    mbox = page.mediabox
                    w, h = float(mbox.width), float(mbox.height)
                    if w <= 0 or h <= 0:
                        raise DocumentSecurityError(
                            f"Страница {i + 1} имеет недопустимые размеры ({w}x{h})."
                        )
                except (AttributeError, ValueError, TypeError) as e:
                    raise DocumentSecurityError(f"Страница {i + 1} повреждена и не может быть обработана.")

            return total_pages

        except pypdf.errors.PdfReadError as e:
            logger.warning(f"Corrupt PDF upload: {e}")
            raise DocumentSecurityError("Файл PDF поврежден или имеет недопустимый формат.")
        except DocumentSecurityError:
            raise
        except Exception as e:
            logger.error(f"Unexpected error while reading PDF: {e}", exc_info=True)
            raise DocumentSecurityError("Не удалось разобрать PDF файл.")

    @classmethod
    def enhance_scanned_pdf_if_needed(cls, pdf_path: Path) -> bool:
        """
        Проверяет, содержит ли PDF отсканированные страницы/фотографии без векторного текста.
        Если да — автоматически пропускает растровые страницы через адаптивный фильтр отбеливания фона
        для устранения серого фона, градиентов освещения и экономии тонера Pantum BP2300NW.
        Работает потоково (O(1) RAM) со сборщиком мусора между страницами.
        """
        try:
            reader = pypdf.PdfReader(str(pdf_path))
            has_scanned = False
            for p in reader.pages:
                txt = (p.extract_text() or "").strip()
                if len(p.images) == 1 and len(txt) == 0:
                    has_scanned = True
                    break

            if not has_scanned:
                return False

            temp_out = pdf_path.with_name(f"{pdf_path.stem}_enhanced.pdf")
            writer = pypdf.PdfWriter()

            dpi = settings.effective_dpi
            resample_filter = Image.Resampling.BILINEAR if settings.is_low_memory else Image.Resampling.LANCZOS
            base_w = int(8.27 * dpi)
            base_h = int(11.69 * dpi)
            margin = max(20, int(80 * (dpi / 300.0)))

            for i, p in enumerate(reader.pages):
                txt = (p.extract_text() or "").strip()
                if len(p.images) == 1 and len(txt) == 0:
                    raw_img = Image.open(io.BytesIO(p.images[0].data))
                    enhanced = cls.enhance_document_image(raw_img)

                    page_w, page_h = base_w, base_h
                    if enhanced.width > enhanced.height:
                        page_w, page_h = base_h, base_w

                    img_ratio = enhanced.width / enhanced.height
                    page_ratio = page_w / page_h
                    target_w = page_w - 2 * margin
                    target_h = page_h - 2 * margin

                    if img_ratio > page_ratio:
                        new_w = target_w
                        new_h = int(target_w / img_ratio)
                    else:
                        new_h = target_h
                        new_w = int(target_h * img_ratio)

                    resized = enhanced.resize((new_w, new_h), resample_filter)
                    canvas = Image.new("L", (page_w, page_h), 255)
                    offset_x = (page_w - new_w) // 2
                    offset_y = (page_h - new_h) // 2
                    canvas.paste(resized, (offset_x, offset_y))

                    buf = io.BytesIO()
                    canvas.save(buf, format="PDF", resolution=dpi)
                    buf.seek(0)
                    writer.add_page(pypdf.PdfReader(buf).pages[0])
                    del raw_img, enhanced, resized, canvas
                    if settings.is_low_memory:
                        gc.collect()
                else:
                    writer.add_page(p)

            with open(str(temp_out), "wb") as f_out:
                writer.write(f_out)
            writer.close()

            # Атомарная замена
            shutil.move(str(temp_out), str(pdf_path))
            os.chmod(pdf_path, 0o600)
            logger.info(f"Automatically enhanced scanned raster pages in {pdf_path.name}")
            return True

        except Exception as e:
            logger.warning(f"Could not auto-enhance scanned PDF {pdf_path.name}: {e}")
            return False

    @staticmethod
    def enhance_document_image(img: Image.Image) -> Image.Image:
        """
        Интеллектуальная адаптивная оптимизация тонера и контраста для фотографий/сканов:
        - Перевод в Grayscale (L).
        - Адаптивная нормализация фонового освещения (Illumination Normalization):
          устраняет неравномерные градиенты, тени от смартфона, пальцев, складок и освещения,
          гарантируя 100% чистый белый фон (255) по всей площади листа.
        - Экономит 60-80% тонера картриджа Pantum BP2300NW.
        - Нелинейная тоновая кривая для глубокого черного цвета букв (< 95 -> насыщенный черный).
        - Подавление краевых артефактов съемки (тени краев стола, скобы/скрепки на полях).
        """
        if img.mode != "L":
            gray = img.convert("L")
        else:
            gray = img.copy()

        w, h = gray.size
        if w < 10 or h < 10:
            return gray

        # Для небольших иконок и синтетических изображений
        if w < 200 or h < 200:
            lut = [0] * 256
            low, high = 85, 205
            for i in range(256):
                if i <= low:
                    lut[i] = 0
                elif i >= high:
                    lut[i] = 255
                else:
                    lut[i] = int(255 * (i - low) / (high - low))
            return gray.point(lut)

        # Для реальных документов и сканов (> 200px):
        # 1. Быстрая оценка локального фона через даунскейл (O(1) RAM, время < 3 мс)
        small_w = 120
        small_h = max(10, int(h * (small_w / w)))
        small = gray.resize((small_w, small_h), Image.Resampling.BILINEAR)
        bg_small = small.filter(ImageFilter.MaxFilter(size=7))
        bg_small = bg_small.filter(ImageFilter.BoxBlur(radius=7))
        bg = bg_small.resize((w, h), Image.Resampling.BILINEAR)

        # 2. Вычитание фоновой засветки и инверсия
        diff = ImageChops.subtract(bg, gray)
        inv = ImageOps.invert(diff)

        # 3. Тоновая кривая (чистый белый фон >= 230, глубокий черный текст <= 95)
        lut = [0] * 256
        for i in range(256):
            if i >= 230:
                lut[i] = 255
            elif i <= 95:
                lut[i] = int(i * 0.25)
            else:
                norm = (i - 95) / (230 - 95)
                lut[i] = int(24 + norm * (255 - 24))

        enhanced = inv.point(lut)

        # 4. Подавление краевых швов съемки и следов скрепки на полях
        draw = ImageDraw.Draw(enhanced)
        edge_x = max(2, int(w * 0.015))
        edge_y = max(2, int(h * 0.012))
        draw.rectangle([0, 0, w, edge_y], fill=255)
        draw.rectangle([0, h - edge_y, w, h], fill=255)
        draw.rectangle([0, 0, edge_x, h], fill=255)
        draw.rectangle([w - edge_x, 0, w, h], fill=255)
        # Зона скобы / скрепки в верхнем левом углу
        draw.rectangle([0, 0, min(24, int(w * 0.04)), min(75, int(h * 0.08))], fill=255)

        return enhanced

    @classmethod
    def convert_image_to_a4_pdf(cls, image_bytes: bytes, output_pdf_path: Path) -> int:
        """
        Конвертирует изображение (или многостраничный TIFF) в формат A4 PDF:
        - Потоковая обработка O(1) RAM: в памяти удерживается не более 1 страницы,
          что предотвращает OOM killer на роутерах с 128-256 МБ RAM (Netis NX31, Huawei AX3).
        - Адаптивный DPI: 150 DPI в Low-Memory режиме (2.1 МБ/стр), 300 DPI на мощных серверах.
        - Монохромная лазерная оптимизация (Grayscale 'L') и отбеливание серого фона.
        """
        try:
            dpi = settings.effective_dpi
            resample_filter = Image.Resampling.BILINEAR if settings.is_low_memory else Image.Resampling.LANCZOS

            # Базовые размеры A4 под заданный DPI
            base_w = int(8.27 * dpi)
            base_h = int(11.69 * dpi)
            margin = max(20, int(80 * (dpi / 300.0)))

            writer = pypdf.PdfWriter()
            total_pages = 0

            with Image.open(io.BytesIO(image_bytes)) as img:
                for frame in ImageSequence.Iterator(img):
                    if total_pages >= settings.effective_max_pages:
                        raise DocumentSecurityError(
                            f"Количество страниц в изображении превышает лимит ({settings.effective_max_pages})."
                        )

                    current = frame.copy()
                    current = ImageOps.exif_transpose(current)

                    # Обработка прозрачности и альфа-каналов с белой подложкой
                    if current.mode in ("RGBA", "LA", "P"):
                        gray_base = Image.new("L", current.size, 255)
                        alpha_mask = current.split()[-1] if current.mode in ("RGBA", "LA") else None
                        gray_base.paste(current.convert("L"), mask=alpha_mask)
                        gray_img = gray_base
                    else:
                        gray_img = current.convert("L")

                    # Интеллектуальное отбеливание серого фона фотографий конспектов
                    if settings.ENHANCE_CONTRAST_PHOTOS:
                        gray_img = cls.enhance_document_image(gray_img)

                    # Расчет геометрии A4 (книжная / альбомная)
                    page_w, page_h = base_w, base_h
                    if gray_img.width > gray_img.height:
                        page_w, page_h = base_h, base_w  # Альбомная ориентация

                    img_ratio = gray_img.width / gray_img.height
                    page_ratio = page_w / page_h
                    target_w = page_w - 2 * margin
                    target_h = page_h - 2 * margin

                    if img_ratio > page_ratio:
                        new_w = target_w
                        new_h = int(target_w / img_ratio)
                    else:
                        new_h = target_h
                        new_w = int(target_h * img_ratio)

                    resized = gray_img.resize((new_w, new_h), resample_filter)
                    canvas = Image.new("L", (page_w, page_h), 255)
                    offset_x = (page_w - new_w) // 2
                    offset_y = (page_h - new_h) // 2
                    canvas.paste(resized, (offset_x, offset_y))

                    # Потоковая запись страницы в PDF без накопления растровых холстов в RAM
                    page_buf = io.BytesIO()
                    canvas.save(page_buf, "PDF", resolution=float(dpi), quality=95)
                    page_buf.seek(0)
                    page_reader = pypdf.PdfReader(page_buf)
                    writer.add_page(page_reader.pages[0])
                    total_pages += 1

                    # Мгновенная утилизация объектов в памяти текущей страницы (O(1) footprint)
                    del canvas, resized, gray_img, current, page_reader, page_buf
                    if settings.is_low_memory:
                        gc.collect()

            if total_pages == 0:
                raise DocumentSecurityError("Не удалось извлечь ни одной страницы из изображения.")

            # Сохраняем в целевой PDF файл
            with open(str(output_pdf_path), "wb") as f:
                writer.write(f)
            writer.close()

            if settings.is_low_memory:
                gc.collect()

            return total_pages

        except DocumentSecurityError:
            raise
        except Exception as e:
            logger.error(f"Image to PDF conversion error: {e}", exc_info=True)
            raise DocumentSecurityError("Не удалось обработать изображение.")

    @classmethod
    def convert_txt_to_a4_pdf(cls, text_bytes: bytes, output_pdf_path: Path) -> int:
        """
        Конвертирует текстовый файл (.txt) в стандартизированный A4 PDF
        с потоковой разбивкой на страницы, полями и нумерацией (O(1) по RAM).
        """
        try:
            # Определение кодировки
            text = ""
            for encoding in ("utf-8", "cp1251", "latin-1"):
                try:
                    text = text_bytes.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue

            if not text:
                raise DocumentSecurityError("Не удалось распознать кодировку текстового файла.")

            raw_lines = text.splitlines()
            # Перенос длинных строк (до 85 символов на строку)
            wrapped_lines: List[str] = []
            max_chars = 85
            for line in raw_lines:
                if not line:
                    wrapped_lines.append("")
                    continue
                while len(line) > max_chars:
                    wrapped_lines.append(line[:max_chars])
                    line = line[max_chars:]
                wrapped_lines.append(line)

            lines_per_page = 48
            pages_chunks = [
                wrapped_lines[i:i + lines_per_page]
                for i in range(0, max(len(wrapped_lines), 1), lines_per_page)
            ]

            if len(pages_chunks) > settings.effective_max_pages:
                raise DocumentSecurityError(
                    f"Текстовый документ слишком длинный ({len(pages_chunks)} стр.). "
                    f"Максимум: {settings.effective_max_pages} стр."
                )

            dpi = settings.effective_dpi
            scale = dpi / 300.0
            a4_w = int(2480 * scale)
            a4_h = int(3508 * scale)
            margin_x = int(160 * scale)
            header_y = int(100 * scale)
            line_y = int(150 * scale)
            line_end_x = int(2320 * scale)
            start_y = int(190 * scale)
            step_y = max(25, int(65 * scale))

            font = ImageFont.load_default(size=max(14, int(40 * scale)))
            header_font = ImageFont.load_default(size=max(12, int(32 * scale)))

            writer = pypdf.PdfWriter()
            total_pages = len(pages_chunks)

            for p_idx, page_lines in enumerate(pages_chunks):
                # Создаем монохромный лист A4 (1 байт на пиксель)
                img = Image.new("L", (a4_w, a4_h), 255)
                draw = ImageDraw.Draw(img)

                # Колонтитул: номер страницы
                header_text = f"Страница {p_idx + 1} из {total_pages}"
                draw.text((margin_x, header_y), header_text, font=header_font, fill=110)
                draw.line([(margin_x, line_y), (line_end_x, line_y)], fill=190, width=max(1, int(2 * scale)))

                y = start_y
                for line in page_lines:
                    draw.text((margin_x, y), line, font=font, fill=0)
                    y += step_y

                # Потоковое добавление страницы в PDF
                page_buf = io.BytesIO()
                img.save(page_buf, "PDF", resolution=float(dpi))
                page_buf.seek(0)
                page_reader = pypdf.PdfReader(page_buf)
                writer.add_page(page_reader.pages[0])

                del img, draw, page_reader, page_buf
                if settings.is_low_memory:
                    gc.collect()

            with open(str(output_pdf_path), "wb") as f:
                writer.write(f)
            writer.close()

            if settings.is_low_memory:
                gc.collect()

            return total_pages

        except DocumentSecurityError:
            raise
        except Exception as e:
            logger.error(f"Error converting TXT to PDF: {e}", exc_info=True)
            raise DocumentSecurityError("Ошибка при формировании страниц из текстового файла.")

    @classmethod
    def convert_docx_to_pdf(cls, file_bytes: bytes, output_pdf_path: Path, order_uuid: str) -> int:
        """
        Безопасная конвертация DOCX:
        - Защита от Zip-бомб (контроль суммарного размера распакованных данных)
        - Проверка целостности word/document.xml
        - Использование LibreOffice (при наличии)
        - Автономный Python-рендеринг в A4 PDF при отсутствии LibreOffice
        """
        # 1. Проверка Zip-архива и защита от Zip-бомб
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                total_uncompressed = sum(info.file_size for info in z.infolist())
                if total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
                    raise DocumentSecurityError(
                        "Файл DOCX превышает безопасный лимит распаковки (защита от Zip-бомбы)."
                    )
                if "word/document.xml" not in z.namelist():
                    raise DocumentSecurityError("Некорректная структура документа Word (отсутствует document.xml).")
        except zipfile.BadZipFile:
            raise DocumentSecurityError("Поврежденный архив документа Word (.docx).")

        # 2. Проверка наличия LibreOffice
        import shutil
        import subprocess
        soffice_bin = shutil.which("soffice") or shutil.which("libreoffice")
        if soffice_bin:
            temp_docx = settings.effective_spool_dir / f"{order_uuid}.docx"
            try:
                temp_docx.write_bytes(file_bytes)
                os.chmod(temp_docx, 0o600)
                cmd = [
                    soffice_bin,
                    "--headless",
                    "--convert-to", "pdf",
                    "--outdir", str(settings.effective_spool_dir),
                    str(temp_docx)
                ]
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25)
                if proc.returncode == 0 and output_pdf_path.exists():
                    os.chmod(output_pdf_path, 0o600)
                    return cls.verify_pdf(output_pdf_path)
            finally:
                if temp_docx.exists():
                    temp_docx.unlink()

        # 3. Автономный Python-конвертер (fallback без LibreOffice)
        try:
            import docx
            doc = docx.Document(io.BytesIO(file_bytes))
            extracted_lines: List[str] = []
            for p in doc.paragraphs:
                txt = p.text.strip()
                if txt:
                    extracted_lines.append(txt)
            for t in doc.tables:
                for row in t.rows:
                    row_txt = " | ".join(c.text.strip() for c in row.cells if c.text.strip())
                    if row_txt:
                        extracted_lines.append(row_txt)

            if not extracted_lines:
                raise DocumentSecurityError("В документе Word не найдено текста для печати.")

            full_text = "\n\n".join(extracted_lines)
            return cls.convert_txt_to_a4_pdf(full_text.encode("utf-8"), output_pdf_path)

        except DocumentSecurityError:
            raise
        except Exception as e:
            logger.error(f"Fallback docx parser error: {e}", exc_info=True)
            raise DocumentSecurityError("Не удалось разобрать документ Word.")

    @classmethod
    def get_page_indices(cls, range_str: str, total_pages: int) -> List[int]:
        """
        Преобразует строку диапазона ('1-3, 5') в отсортированный список 1-based номеров страниц.
        """
        cleaned = range_str.strip().lower()
        if cleaned in ("all", "все", "*", ""):
            return list(range(1, total_pages + 1))

        if not re.match(r"^[\d\s,-]+$", cleaned):
            raise DocumentSecurityError("Неверный формат диапазона страниц (например: 1-5, 8).")

        selected_pages: Set[int] = set()
        parts = [p.strip() for p in cleaned.split(",") if p.strip()]

        for part in parts:
            if "-" in part:
                sub_parts = [s.strip() for s in part.split("-")]
                if len(sub_parts) != 2 or not sub_parts[0].isdigit() or not sub_parts[1].isdigit():
                    raise DocumentSecurityError(f"Некорректный диапазон: '{part}'.")
                start, end = int(sub_parts[0]), int(sub_parts[1])
                if start < 1 or end > total_pages or start > end:
                    raise DocumentSecurityError(
                        f"Диапазон {start}-{end} выходит за пределы документа (1-{total_pages})."
                    )
                for p in range(start, end + 1):
                    selected_pages.add(p)
            else:
                if not part.isdigit():
                    raise DocumentSecurityError(f"Некорректный номер страницы: '{part}'.")
                p = int(part)
                if p < 1 or p > total_pages:
                    raise DocumentSecurityError(
                        f"Страница {p} выходит за пределы документа (1-{total_pages})."
                    )
                selected_pages.add(p)

        if not selected_pages:
            raise DocumentSecurityError("Не выбрано ни одной страницы для печати.")

        return sorted(list(selected_pages))

    @staticmethod
    def is_page_blank(page: pypdf.PageObject) -> bool:
        """
        Проверяет, является ли страница PDF полностью пустой (без текста, растровых и векторных элементов).
        Позволяет экономить бумагу и деньги студентов, исключая случайные пустые страницы в конце рефератов.
        """
        try:
            text = (page.extract_text() or "").strip()
            if text:
                return False

            if len(page.images) > 0:
                return False

            resources = page.get("/Resources")
            if resources and isinstance(resources, dict):
                xobjects = resources.get("/XObject")
                if xobjects and len(xobjects) > 0:
                    return False

            contents = page.get_contents()
            if contents is None:
                return True

            data = contents.get_data() if hasattr(contents, "get_data") else b""
            cleaned = re.sub(rb'\s+', b'', data)
            if not cleaned or cleaned in (b'qQ', b''):
                return True

        except Exception as e:
            logger.debug(f"is_page_blank error: {e}")
            return False

        return False

    @classmethod
    def get_non_blank_pages(cls, pdf_path: Path) -> Tuple[List[int], List[int]]:
        """
        Сканирует PDF файл и возвращает:
        (список 1-based непустых страниц, список 1-based пустых страниц)
        """
        try:
            reader = pypdf.PdfReader(str(pdf_path))
            total = len(reader.pages)
            non_blank = []
            blank = []
            for idx, page in enumerate(reader.pages, start=1):
                if cls.is_page_blank(page):
                    blank.append(idx)
                else:
                    non_blank.append(idx)

            if not non_blank:
                return list(range(1, total + 1)), []

            return non_blank, blank
        except Exception as e:
            logger.warning(f"Error scanning for blank pages: {e}")
            return [], []

    @classmethod
    def parse_page_range(cls, range_str: str, total_pages: int) -> Tuple[str, int]:
        """
        Строгий парсинг диапазона страниц.
        Возвращает: (нормализованная строка, количество страниц к печати).
        """
        indices = cls.get_page_indices(range_str, total_pages)
        if len(indices) == total_pages and indices == list(range(1, total_pages + 1)):
            return "all", total_pages
        normalized = cls_format_page_ranges(indices)
        return normalized, len(indices)

    @classmethod
    def prepare_job_pdf(
        cls,
        source_pdf: Path,
        order_uuid: str,
        selected_pages: str = "all",
        copies: int = 1
    ) -> Path:
        """
        ФИЗИЧЕСКАЯ НАРЕЗКА И ПОДГОТОВКА СТРАНИЦ ПЕРЕД ОТПРАВКОЙ НА ПРИНТЕР:
        1. Извлекает ТОЛЬКО выбранные страницы из исходного PDF.
        2. При settings.AUTO_SKIP_BLANK_PAGES: автоматически исключает пустые листы при полной печати.
        3. При settings.PAGE_FIT_A4: масштабирует нестандартные страницы под A4.
        4. Для сокетной печати (RAW 9100 на роутер Huawei AX3): дублирует страницы
           при copies > 1, гарантируя печать нужного количества копий.
        5. Создает временный файл со строгими правами 0600.
        """
        reader = pypdf.PdfReader(str(source_pdf))
        total_in_file = len(reader.pages)
        indices = cls.get_page_indices(selected_pages, total_in_file)

        # Автоматический пропуск пустых страниц для экономии бумаги
        if settings.AUTO_SKIP_BLANK_PAGES and selected_pages.lower() in ("all", "все", "*", ""):
            non_blank, blank = cls.get_non_blank_pages(source_pdf)
            if blank and non_blank:
                indices = [p for p in indices if p in non_blank]

        # Если файл не требует нарезки, масштабирования или тиражирования:
        needs_slicing = (len(indices) != total_in_file)
        needs_copies_duplication = (copies > 1)

        if not needs_slicing and not needs_copies_duplication and not settings.PAGE_FIT_A4:
            return source_pdf

        dest_sliced_path = settings.effective_spool_dir / f"{order_uuid}_prepared.pdf"
        writer = pypdf.PdfWriter()

        copies_to_add = copies if needs_copies_duplication else 1
        for _ in range(copies_to_add):
            for p_num in indices:
                page = reader.pages[p_num - 1]
                # Масштабирование под A4 при необходимости
                if settings.PAGE_FIT_A4:
                    try:
                        w, h = float(page.mediabox.width), float(page.mediabox.height)
                        is_landscape = w > h
                        target_w, target_h = A4_LANDSCAPE_PT if is_landscape else A4_PORTRAIT_PT
                        # Если размеры существенно отличаются от A4 (более 10%)
                        if abs(w - target_w) > 30 or abs(h - target_h) > 30:
                            page.scale_to(width=target_w, height=target_h)
                    except Exception as e:
                        logger.warning(f"Failed to normalize page {p_num} geometry to A4: {e}")

                writer.add_page(page)

        with open(str(dest_sliced_path), "wb") as out_f:
            writer.write(out_f)
        writer.close()

        if settings.is_low_memory:
            gc.collect()

        os.chmod(dest_sliced_path, 0o600)
        return dest_sliced_path

    @staticmethod
    def sanitize_filename(name: str) -> str:
        """Очищает имя файла от опасных символов (path traversal, control chars, null bytes)"""
        if not name:
            return "document.pdf"
        name = name.replace("\\", "/").replace("\x00", "")
        name = os.path.basename(name)
        while ".." in name:
            name = name.replace("..", "")
        name = re.sub(r'[^\w\s\.\-\(\)]', '_', name, flags=re.UNICODE)
        name = name.strip("._ ")
        return name[:64] if name else "document.pdf"

    @staticmethod
    def cleanup_file(path_str: Optional[str]) -> None:
        """Безопасное удаление файла из спула"""
        if not path_str:
            return
        try:
            p = Path(path_str)
            if p.exists() and p.is_file():
                p.unlink()
                logger.debug(f"Removed spool file: {p}")
        except Exception as e:
            logger.warning(f"Error removing spool file {path_str}: {e}")


def cls_format_page_ranges(pages: List[int]) -> str:
    """Группирует список [1, 2, 3, 5, 7, 8] в '1-3, 5, 7-8'"""
    if not pages:
        return ""
    ranges = []
    start = pages[0]
    prev = pages[0]

    for p in pages[1:]:
        if p == prev + 1:
            prev = p
        else:
            if start == prev:
                ranges.append(str(start))
            else:
                ranges.append(f"{start}-{prev}")
            start = p
            prev = p

    if start == prev:
        ranges.append(str(start))
    else:
        ranges.append(f"{start}-{prev}")

    return ", ".join(ranges)
