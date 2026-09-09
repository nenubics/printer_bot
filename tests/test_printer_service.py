import io
import shutil
import tempfile
import unittest
from pathlib import Path
from services.printer import PrinterService
import pypdf


def create_dummy_pdf(pages_count: int = 1) -> bytes:
    writer = pypdf.PdfWriter()
    for _ in range(pages_count):
        writer.add_blank_page(width=595, height=842)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


class TestPrinterService(unittest.IsolatedAsyncioTestCase):
    async def test_mock_printer_status(self):
        status = await PrinterService.check_status()
        self.assertTrue(status["is_ready"])
        self.assertIn("mock", status["state"])

    async def test_mock_printer_print_job(self):
        temp_dir = Path(tempfile.mkdtemp())
        try:
            dummy_pdf = temp_dir / "test.pdf"
            dummy_pdf.write_bytes(create_dummy_pdf(1))

            success, msg, job_id = await PrinterService.print_job(
                pdf_path=dummy_pdf,
                copies=2,
                selected_pages="1-2",
                title="TestJob"
            )
            self.assertTrue(success)
            self.assertIsNotNone(job_id)
            self.assertIn("MOCK", job_id)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def test_print_job_missing_file(self):
        missing = Path("/nonexistent/path/document.pdf")
        success, msg, job_id = await PrinterService.print_job(pdf_path=missing)
        self.assertFalse(success)
        self.assertIn("отсутствует", msg)


if __name__ == "__main__":
    unittest.main()
