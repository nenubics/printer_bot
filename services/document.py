import os
import re
import io
import uuid
import zipfile
import shutil
import logging
from pathlib import Path
from typing import Tuple, Set, List, Optional
from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageSequence
import pypdf
from config import settings

logger = logging.getLogger(__name__)

# Защита от декомпрессионных бомб (OOM) в Pillow
Image.MAX_IMAGE_PIXELS = 40_000_000
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
        file_bytes: bytes,
        original_name: str,
        order_uuid: str
    ) -> Tuple[Path, int, str]:
        """
        Проверяет файл, выполняет конвертацию в стандартизированный PDF A4,
        сохраняет файл под безопасным UUID в папку спула и возвращает:
        (путь к файлу, общее количество страниц, очищенное имя)
        """
        # 1. Защита дискового пространства от DoS переполнения
        try:
            free_mb = shutil.disk_usage(settings.DATA_DIR).free // (1024 * 1024)
            if free_mb < settings.MIN_FREE_DISK_MB:
                raise DocumentSecurityError(
                    f"На сервере временно недостаточно места на диске ({free_mb} МБ свободно). "
                    "Попробуйте позже или обратитесь к администратору."
                )
        except OSError as e:
            logger.warning(f"Failed to check disk usage: {e}")

        if len(file_bytes) > settings.MAX_FILE_SIZE_BYTES:
            raise DocumentSecurityError(
                f"Файл слишком большой! Максимальный размер: {settings.MAX_FILE_SIZE_BYTES // (1024*1024)} МБ."
            )

        file_type = cls.detect_file_type(file_bytes, filename=original_name)
        sanitized_name = cls.sanitize_filename(original_name)

        # Создаем папку спула при необходимости
        settings.SPOOL_DIR.mkdir(parents=True, exist_ok=True)
        dest_pdf_path = settings.SPOOL_DIR / f"{order_uuid}.pdf"

        if file_type == "pdf":
            dest_pdf_path.write_bytes(file_bytes)
            os.chmod(dest_pdf_path, 0o600)
            total_pages = cls.verify_pdf(dest_pdf_path)
            return dest_pdf_path, total_pages, sanitized_name

        elif file_type in ("png", "jpeg", "tiff", "webp"):
            total_pages = cls.convert_image_to_a4_pdf(file_bytes, dest_pdf_path)
            os.chmod(dest_pdf_path, 0o600)
            return dest_pdf_path, total_pages, sanitized_name

        elif file_type == "txt":
            total_pages = cls.convert_txt_to_a4_pdf(file_bytes, dest_pdf_path)
            os.chmod(dest_pdf_path, 0o600)
            return dest_pdf_path, total_pages, sanitized_name

        elif file_type == "docx":
            total_pages = cls.convert_docx_to_pdf(file_bytes, dest_pdf_path, order_uuid)
            os.chmod(dest_pdf_path, 0o600)
            return dest_pdf_path, total_pages, sanitized_name

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

            if total_pages > settings.MAX_PAGES_PER_JOB:
                raise DocumentSecurityError(
                    f"В документе {total_pages} страниц. "
                    f"Максимум за одно задание: {settings.MAX_PAGES_PER_JOB} страниц."
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
    def convert_image_to_a4_pdf(cls, image_bytes: bytes, output_pdf_path: Path) -> int:
        """
        Конвертирует изображение (или многостраничный TIFF) в формат A4 PDF
        с автоматическим центрированием, соблюдением полей и 300 DPI.
        """
        try:
            canvases: List[Image.Image] = []
            with Image.open(io.BytesIO(image_bytes)) as img:
                # Обработка всех кадров (для многостраничных TIFF / TIF)
                for frame in ImageSequence.Iterator(img):
                    if len(canvases) >= settings.MAX_PAGES_PER_JOB:
                        raise DocumentSecurityError(
                            f"Количество страниц в изображении превышает лимит ({settings.MAX_PAGES_PER_JOB})."
                        )

                    current = frame.copy()
                    current = ImageOps.exif_transpose(current)

                    # Перевод в RGB (обработка прозрачности PNG / альфа-каналов)
                    if current.mode in ("RGBA", "LA", "P"):
                        rgb_img = Image.new("RGB", current.size, (255, 255, 255))
                        rgb_img.paste(current, mask=current.split()[-1] if current.mode in ("RGBA", "LA") else None)
                    else:
                        rgb_img = current.convert("RGB")

                    # Стандартный A4 при 300 DPI: 2480 x 3508 пикселей
                    a4_w, a4_h = 2480, 3508
                    if rgb_img.width > rgb_img.height:
                        a4_w, a4_h = 3508, 2480  # Альбомная ориентация

                    img_ratio = rgb_img.width / rgb_img.height
                    page_ratio = a4_w / a4_h
                    margin = 80  # Отступ 80px
                    target_w = a4_w - 2 * margin
                    target_h = a4_h - 2 * margin

                    if img_ratio > page_ratio:
                        new_w = target_w
                        new_h = int(target_w / img_ratio)
                    else:
                        new_h = target_h
                        new_w = int(target_h * img_ratio)

                    resized = rgb_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                    canvas = Image.new("RGB", (a4_w, a4_h), (255, 255, 255))
                    offset_x = (a4_w - new_w) // 2
                    offset_y = (a4_h - new_h) // 2
                    canvas.paste(resized, (offset_x, offset_y))
                    canvases.append(canvas)

            if not canvases:
                raise DocumentSecurityError("Не удалось извлечь ни одной страницы из изображения.")

            # Сохраняем все страницы в один PDF
            canvases[0].save(
                str(output_pdf_path),
                "PDF",
                save_all=True,
                append_images=canvases[1:] if len(canvases) > 1 else [],
                resolution=300.0,
                quality=95
            )
            return len(canvases)

        except DocumentSecurityError:
            raise
        except Exception as e:
            logger.error(f"Image to PDF conversion error: {e}", exc_info=True)
            raise DocumentSecurityError("Не удалось обработать изображение.")

    @classmethod
    def convert_txt_to_a4_pdf(cls, text_bytes: bytes, output_pdf_path: Path) -> int:
        """
        Конвертирует текстовый файл (.txt) в стандартизированный A4 PDF
        с автоматической разбивкой на страницы, полями и нумерацией.
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

            if len(pages_chunks) > settings.MAX_PAGES_PER_JOB:
                raise DocumentSecurityError(
                    f"Текстовый документ слишком длинный ({len(pages_chunks)} стр.). "
                    f"Максимум: {settings.MAX_PAGES_PER_JOB} стр."
                )

            font = ImageFont.load_default(size=40)
            header_font = ImageFont.load_default(size=32)

            writer = pypdf.PdfWriter()
            for p_idx, page_lines in enumerate(pages_chunks):
                # Создаем лист A4 (2480 x 3508)
                img = Image.new("RGB", (2480, 3508), (255, 255, 255))
                draw = ImageDraw.Draw(img)

                # Колонтитул: номер страницы
                header_text = f"Страница {p_idx + 1} из {len(pages_chunks)}"
                draw.text((160, 100), header_text, font=header_font, fill=(120, 120, 120))
                draw.line([(160, 150), (2320, 150)], fill=(200, 200, 200), width=2)

                y = 190
                for line in page_lines:
                    draw.text((160, y), line, font=font, fill=(0, 0, 0))
                    y += 65

                buf = io.BytesIO()
                img.save(buf, "PDF", resolution=300.0)
                buf.seek(0)
                reader = pypdf.PdfReader(buf)
                writer.add_page(reader.pages[0])

            with open(str(output_pdf_path), "wb") as f:
                writer.write(f)

            return len(pages_chunks)

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
            temp_docx = settings.SPOOL_DIR / f"{order_uuid}.docx"
            try:
                temp_docx.write_bytes(file_bytes)
                os.chmod(temp_docx, 0o600)
                cmd = [
                    soffice_bin,
                    "--headless",
                    "--convert-to", "pdf",
                    "--outdir", str(settings.SPOOL_DIR),
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
        2. При settings.PAGE_FIT_A4: масштабирует нестандартные страницы под A4.
        3. Для сокетной печати (RAW 9100 на роутер Huawei AX3): дублирует страницы
           при copies > 1, гарантируя печать нужного количества копий.
        4. Создает временный файл со строгими правами 0600.
        """
        reader = pypdf.PdfReader(str(source_pdf))
        total_in_file = len(reader.pages)
        indices = cls.get_page_indices(selected_pages, total_in_file)

        # Если файл не требует нарезки, масштабирования или тиражирования:
        needs_slicing = (selected_pages.lower() not in ("all", "все", "*", "") or len(indices) != total_in_file)
        needs_copies_duplication = (copies > 1)

        if not needs_slicing and not needs_copies_duplication and not settings.PAGE_FIT_A4:
            return source_pdf

        dest_sliced_path = settings.SPOOL_DIR / f"{order_uuid}_prepared.pdf"
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
