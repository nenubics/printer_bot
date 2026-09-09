import io
import shutil
import tempfile
import unittest
from pathlib import Path
from PIL import Image
import pypdf
from config import settings
from services.document import DocumentService, DocumentSecurityError


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
        self.assertNotIn("..", DocumentService.sanitize_filename("../../../etc/passwd"))
        self.assertNotIn("/", DocumentService.sanitize_filename("folder/document.pdf"))
        self.assertNotIn("\\", DocumentService.sanitize_filename("C:\\Windows\\System32\\cmd.exe"))
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

        with self.assertRaises(DocumentSecurityError):
            DocumentService.parse_page_range("1-10", total_pages)

        with self.assertRaises(DocumentSecurityError):
            DocumentService.parse_page_range("0, 2", total_pages)

        with self.assertRaises(DocumentSecurityError):
            DocumentService.parse_page_range("6", total_pages)

        with self.assertRaises(DocumentSecurityError):
            DocumentService.parse_page_range("5-2", total_pages)

        with self.assertRaises(DocumentSecurityError):
            DocumentService.parse_page_range("1-3; echo pwned", total_pages)

        with self.assertRaises(DocumentSecurityError):
            DocumentService.parse_page_range("`id`", total_pages)

    def test_process_and_save_upload(self):
        temp_dir = Path(tempfile.mkdtemp())
        try:
            original_spool = settings.SPOOL_DIR
            settings.SPOOL_DIR = temp_dir / "spool"

            pdf_bytes = create_dummy_pdf(4)
            dest_path, total_pages, sanitized_name = DocumentService.process_and_save_upload(
                file_bytes=pdf_bytes,
                original_name="lab_work.pdf",
                order_uuid="test-uuid-123"
            )

            self.assertTrue(dest_path.exists())
            self.assertEqual(total_pages, 4)
            self.assertEqual(sanitized_name, "lab_work.pdf")

            DocumentService.cleanup_file(str(dest_path))
            self.assertFalse(dest_path.exists())

            settings.SPOOL_DIR = original_spool
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_sbp_qr_generation(self):
        from services.payment_qr import PaymentQrService
        qr_bytes = PaymentQrService.generate_sbp_qr_bytes(amount_rub=45.0, order_id_or_comment="Заказ #1")
        self.assertTrue(qr_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(qr_bytes), 500)

    def test_docx_handling(self):
        # Corrupt docx with broken zip structure is rejected
        broken_docx = b"PK\x03\x04\x14\x00\x00\x00\x08\x00"
        file_type = DocumentService.detect_file_type(broken_docx, filename="essay.docx")
        self.assertEqual(file_type, "docx")

        with self.assertRaises(DocumentSecurityError):
            DocumentService.process_and_save_upload(broken_docx, "essay.docx", "uuid-docx-broken")

        # Valid docx is processed via pure-python fallback / soffice into A4 PDF
        import docx
        doc = docx.Document()
        doc.add_paragraph("Тестовое эссе для студента ЦСО-4.")
        buf = io.BytesIO()
        doc.save(buf)
        valid_docx = buf.getvalue()

        dest_pdf, pages, name = DocumentService.process_and_save_upload(valid_docx, "essay.docx", "uuid-docx-valid")
        self.assertGreaterEqual(pages, 1)
        self.assertTrue(dest_pdf.exists())
        self.assertTrue(str(dest_pdf).endswith(".pdf"))


if __name__ == "__main__":
    unittest.main()

