import os
import re
import uuid
import logging
from pathlib import Path
from typing import Tuple, Set, Optional
from PIL import Image, ImageOps
import pypdf
from config import settings

logger = logging.getLogger(__name__)

# Защита от декомпрессионных бомб (OOM) в Pillow
Image.MAX_IMAGE_PIXELS = 40_000_000

# Допустимые сигнатуры (Magic Bytes)
MAGIC_PDF = b"%PDF-"
MAGIC_PNG = b"\x89PNG\r\n\x1a\n"
MAGIC_JPEG = b"\xff\xd8\xff"
MAGIC_ZIP_DOCX = b"PK\x03\x04"


class DocumentSecurityError(Exception):
    """Исключение при нарушении безопасности или формата файла"""
    pass


class DocumentService:
    @staticmethod
    def detect_file_type(file_bytes: bytes, filename: str = "") -> str:
        """
        Проверка типа файла по сигнатуре (Magic Bytes).
        Не доверяет слепо расширению файла.
        """
        if file_bytes.startswith(MAGIC_PDF):
            return "pdf"
        elif file_bytes.startswith(MAGIC_PNG):
            return "png"
        elif file_bytes.startswith(MAGIC_JPEG):
            return "jpeg"
        elif file_bytes.startswith(MAGIC_ZIP_DOCX) and filename.lower().endswith(".docx"):
            return "docx"
        else:
            raise DocumentSecurityError(
                "Неподдерживаемый или небезопасный формат файла. "
                "Разрешены документы PDF, изображения (PNG, JPG) и документы Word (.docx)."
            )


    @classmethod
    def process_and_save_upload(
        cls,
        file_bytes: bytes,
        original_name: str,
        order_uuid: str
    ) -> Tuple[Path, int, str]:
        """
        Проверяет файл, при необходимости конвертирует изображение в PDF,
        сохраняет файл под безопасным именем в папку спула и возвращает:
        (путь к файлу, общее количество страниц, очищенное имя)
        """
        if len(file_bytes) > settings.MAX_FILE_SIZE_BYTES:
            raise DocumentSecurityError(
                f"Файл слишком большой! Максимальный размер: {settings.MAX_FILE_SIZE_BYTES // (1024*1024)} МБ."
            )

        file_type = cls.detect_file_type(file_bytes, filename=original_name)
        sanitized_name = cls.sanitize_filename(original_name)

        # Обеспечиваем наличие папки спула
        settings.SPOOL_DIR.mkdir(parents=True, exist_ok=True)
        dest_pdf_path = settings.SPOOL_DIR / f"{order_uuid}.pdf"

        if file_type == "pdf":
            dest_pdf_path.write_bytes(file_bytes)
            os.chmod(dest_pdf_path, 0o600)
            total_pages = cls.verify_pdf(dest_pdf_path)
            return dest_pdf_path, total_pages, sanitized_name

        elif file_type in ("png", "jpeg"):
            total_pages = cls.convert_image_to_a4_pdf(file_bytes, dest_pdf_path)
            os.chmod(dest_pdf_path, 0o600)
            return dest_pdf_path, total_pages, sanitized_name

        elif file_type == "docx":
            # Проверяем наличие LibreOffice для конвертации
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
                    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
                    if proc.returncode == 0 and dest_pdf_path.exists():
                        os.chmod(dest_pdf_path, 0o600)
                        total_pages = cls.verify_pdf(dest_pdf_path)
                        return dest_pdf_path, total_pages, sanitized_name
                    else:
                        raise DocumentSecurityError("Не удалось сконвертировать документ Word в PDF.")
                finally:
                    if temp_docx.exists():
                        temp_docx.unlink()
            else:
                raise DocumentSecurityError(
                    "📝 Документ Word (.docx) получен!\n\n"
                    "Чтобы верстка, таблицы и формулы не съехали при печати на Pantum BP2300NW, "
                    "пожалуйста, сохраните файл как PDF («Файл» -> «Сохранить как PDF» на смартфоне или в Word) "
                    "и отправьте полученный PDF-файл. Это гарантирует 100% точность печати!"
                )

        raise DocumentSecurityError("Неизвестная ошибка обработки документа.")


    @staticmethod
    def verify_pdf(pdf_path: Path) -> int:
        """
        Безопасная валидация PDF: проверка на повреждения, пароли и число страниц.
        """
        try:
            reader = pypdf.PdfReader(str(pdf_path))
            if reader.is_encrypted:
                raise DocumentSecurityError(
                    "Файл защищен паролем. Пожалуйста, снимите пароль перед отправкой на печать."
                )

            total_pages = len(reader.pages)
            if total_pages == 0:
                raise DocumentSecurityError("В документе нет страниц для печати.")

            if total_pages > settings.MAX_PAGES_PER_JOB:
                raise DocumentSecurityError(
                    f"В документе {total_pages} страниц. "
                    f"Максимум за одно задание: {settings.MAX_PAGES_PER_JOB} страниц."
                )

            return total_pages
        except pypdf.errors.PdfReadError as e:
            logger.warning(f"Corrupt PDF upload: {e}")
            raise DocumentSecurityError("Файл поврежден и не может быть прочитан.")
        except DocumentSecurityError:
            raise
        except Exception as e:
            logger.error(f"Unexpected error while reading PDF: {e}", exc_info=True)
            raise DocumentSecurityError("Не удалось разобрать PDF файл.")

    @staticmethod
    def convert_image_to_a4_pdf(image_bytes: bytes, output_pdf_path: Path) -> int:
        """
        Конвертирует изображение в формат A4 PDF с сохранением пропорций и полями.
        """
        import io
        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                # Нормализуем ориентацию EXIF (актуально для фото со смартфонов)
                img = ImageOps.exif_transpose(img)

                # Переводим в RGB (для CMYK или PNG с прозрачностью RGBA)
                if img.mode in ("RGBA", "LA", "P"):
                    rgb_img = Image.new("RGB", img.size, (255, 255, 255))
                    rgb_img.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                else:
                    rgb_img = img.convert("RGB")

                # Стандартный A4 при 300 DPI: 2480 x 3508 пикселей
                a4_width, a4_height = 2480, 3508
                # Если фото горизонтальное, используем альбомную ориентацию
                if rgb_img.width > rgb_img.height:
                    a4_width, a4_height = 3508, 2480

                # Масштабируем с сохранением пропорций
                img_ratio = rgb_img.width / rgb_img.height
                page_ratio = a4_width / a4_height

                margin = 80 # отступ 80px
                target_w = a4_width - 2 * margin
                target_h = a4_height - 2 * margin

                if img_ratio > page_ratio:
                    new_w = target_w
                    new_h = int(target_w / img_ratio)
                else:
                    new_h = target_h
                    new_w = int(target_h * img_ratio)

                resized = rgb_img.resize((new_w, new_h), Image.Resampling.LANCZOS)

                # Создаем белый лист A4 и центрируем
                canvas = Image.new("RGB", (a4_width, a4_height), (255, 255, 255))
                offset_x = (a4_width - new_w) // 2
                offset_y = (a4_height - new_h) // 2
                canvas.paste(resized, (offset_x, offset_y))

                canvas.save(
                    str(output_pdf_path),
                    "PDF",
                    resolution=300.0,
                    quality=95
                )
                return 1
        except Exception as e:
            logger.error(f"Image to PDF conversion error: {e}", exc_info=True)
            raise DocumentSecurityError("Не удалось обработать изображение.")

    @staticmethod
    def parse_page_range(range_str: str, total_pages: int) -> Tuple[str, int]:
        """
        Строгий и безопасный парсинг диапазона страниц (например '1-3, 5, 8-10').
        Возвращает: (нормализованная строка для CUPS, количество страниц к печати).
        """
        cleaned = range_str.strip().lower()
        if cleaned in ("all", "все", "*", ""):
            return "all", total_pages

        # Разрешены только цифры, запятые, дефисы и пробелы
        if not re.match(r"^[\d\s,-]+$", cleaned):
            raise DocumentSecurityError(
                "Неверный формат диапазона страниц. Используйте, например: 1-5, 8, 12"
            )

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

        # Нормализуем для передачи в CUPS (например: '1-3,5,8-10')
        sorted_pages = sorted(list(selected_pages))
        normalized = cls_format_page_ranges(sorted_pages)
        return normalized, len(sorted_pages)

    @staticmethod
    def sanitize_filename(name: str) -> str:
        """Очищает имя файла от опасных символов (path traversal, control chars, null bytes)"""
        if not name:
            return "document.pdf"
        # Унифицируем слэши для кроссплатформенной защиты от Windows путей на POSIX
        name = name.replace("\\", "/").replace("\x00", "")
        name = os.path.basename(name)
        # Устраняем обход каталогов через двойные точки
        while ".." in name:
            name = name.replace("..", "")
        # Оставляем только безопасные символы
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


def cls_format_page_ranges(pages: list[int]) -> str:
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
