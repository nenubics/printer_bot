import asyncio
import logging
import socket
from pathlib import Path
from typing import Tuple, Optional, Dict, Any
from config import settings

logger = logging.getLogger(__name__)


class PrinterService:
    @classmethod
    async def check_status(cls) -> Dict[str, Any]:
        """
        Проверка состояния принтера Pantum BP2300NW.
        Возвращает словарь: {'is_ready': bool, 'state': str, 'message': str}
        """
        mode = settings.PRINTER_MODE.lower()

        if mode == "mock":
            return {
                "is_ready": True,
                "state": "ready (mock)",
                "message": "Принтер в режиме симуляции (готов к тестам)."
            }

        elif mode == "cups":
            return await cls._check_cups_status()

        elif mode == "raw":
            return await cls._check_raw_status()

        return {
            "is_ready": False,
            "state": "unknown_mode",
            "message": f"Неизвестный режим принтера: {mode}"
        }

    @classmethod
    async def _check_cups_status(cls) -> Dict[str, Any]:
        """Проверка статуса очереди в CUPS через команду lpstat"""
        printer = settings.PRINTER_NAME
        try:
            proc = await asyncio.create_subprocess_exec(
                "lpstat", "-p", printer,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            out_str = stdout.decode("utf-8", errors="replace").strip()
            err_str = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                logger.warning(f"CUPS lpstat error: {err_str or out_str}")
                return {
                    "is_ready": False,
                    "state": "error",
                    "message": f"Принтер '{printer}' не найден в CUPS или выключен: {err_str or out_str}"
                }

            out_lower = out_str.lower()
            if "disabled" in out_lower or "stopped" in out_lower:
                return {
                    "is_ready": False,
                    "state": "disabled",
                    "message": f"Принтер остановлен или ошибка: {out_str}"
                }

            if "out of paper" in out_lower or "no paper" in out_lower:
                return {
                    "is_ready": False,
                    "state": "no_paper",
                    "message": "В принтере закончилась бумага! Пожалуйста, добавьте бумагу в лоток."
                }

            return {
                "is_ready": True,
                "state": "idle" if "idle" in out_lower else "busy",
                "message": out_str
            }

        except FileNotFoundError:
            return {
                "is_ready": False,
                "state": "no_cups",
                "message": "Утилита 'lpstat' не установлена в системе (требуется пакет cups)."
            }
        except Exception as e:
            logger.error(f"Error checking CUPS status: {e}", exc_info=True)
            return {
                "is_ready": False,
                "state": "exception",
                "message": f"Ошибка проверки статуса: {str(e)}"
            }

    @classmethod
    async def _check_raw_status(cls) -> Dict[str, Any]:
        """Проверка доступности сетевого сокета принтера (Port 9100)"""
        host = settings.PRINTER_HOST
        port = settings.PRINTER_PORT
        if not host:
            return {
                "is_ready": False,
                "state": "no_host",
                "message": "Не указан IP-адрес принтера (PRINTER_HOST)."
            }

        loop = asyncio.get_running_loop()
        try:
            # Пробуем подключиться к порту с таймаутом 2 секунды
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(False)
            await asyncio.wait_for(loop.sock_connect(sock, (host, port)), timeout=2.5)
            sock.close()
            return {
                "is_ready": True,
                "state": "online",
                "message": f"Сетевой принтер доступен по {host}:{port}"
            }
        except (socket.error, asyncio.TimeoutError) as e:
            return {
                "is_ready": False,
                "state": "offline",
                "message": f"Принтер {host}:{port} недоступен в сети: {e}"
            }

    @classmethod
    async def print_job(
        cls,
        pdf_path: Path,
        copies: int = 1,
        selected_pages: str = "all",
        title: str = "Telegram Print"
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Отправка файла на печать в Pantum BP2300NW.
        Возвращает: (успех, сообщение/ошибка, id задания)
        """
        if not pdf_path.exists():
            return False, "Файл задания отсутствует на диске.", None

        mode = settings.PRINTER_MODE.lower()

        if mode == "mock":
            logger.info(
                f"[MOCK PRINTER] Simulating print for '{pdf_path.name}': "
                f"copies={copies}, pages='{selected_pages}'"
            )
            await asyncio.sleep(1.5) # симуляция времени печати
            return True, "Успешно напечатано (режим симуляции)", "MOCK-JOB-123"

        elif mode == "cups":
            return await cls._print_cups(pdf_path, copies, selected_pages, title)

        elif mode == "raw":
            return await cls._print_raw(pdf_path)

        return False, f"Неизвестный режим печати: {mode}", None

    @classmethod
    async def _print_cups(
        cls,
        pdf_path: Path,
        copies: int,
        selected_pages: str,
        title: str
    ) -> Tuple[bool, str, Optional[str]]:
        """Печать через системную очередь CUPS с безопасными параметрами"""
        printer = settings.PRINTER_NAME
        args = [
            "lp",
            "-d", printer,
            "-n", str(copies),
            "-t", title[:32], # безопасный заголовок
            "-o", "media=A4",
            "-o", "fit-to-page",
        ]

        if selected_pages and selected_pages.lower() not in ("all", "все", "*"):
            args.extend(["-o", f"page-ranges={selected_pages}"])

        args.append(str(pdf_path.resolve()))

        try:
            logger.info(f"Executing CUPS command: {' '.join(args[:-1])} <file>")
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            out_str = stdout.decode("utf-8", errors="replace").strip()
            err_str = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode == 0:
                # Обычно lp возвращает строку вида: "request id is Pantum_BP2300NW-42 (1 file(s))"
                job_id = out_str.split()[3] if len(out_str.split()) > 3 else "CUPS-JOB"
                logger.info(f"Print job queued in CUPS: {out_str}")
                return True, out_str, job_id
            else:
                logger.error(f"CUPS print error: code={proc.returncode}, stderr={err_str}")
                return False, f"Ошибка печати CUPS: {err_str or out_str}", None

        except FileNotFoundError:
            return False, "Команда 'lp' не найдена (не установлен CUPS клиент).", None
        except Exception as e:
            logger.error(f"Unexpected error in _print_cups: {e}", exc_info=True)
            return False, f"Ошибка при отправке в CUPS: {e}", None

    @classmethod
    async def _print_raw(cls, pdf_path: Path) -> Tuple[bool, str, Optional[str]]:
        """Отправка байтов напрямую в сетевой сокет принтера (Port 9100)"""
        host = settings.PRINTER_HOST
        port = settings.PRINTER_PORT
        if not host:
            return False, "Не задан PRINTER_HOST для прямого сокета.", None

        try:
            file_data = pdf_path.read_bytes()
            reader, writer = await asyncio.open_connection(host, port)

            # Проверяем, не разорвал ли принтер соединение сразу
            try:
                probe = await asyncio.wait_for(reader.read(1), timeout=0.05)
                if probe == b"":
                    writer.close()
                    await writer.wait_closed()
                    raise ConnectionResetError("Принтер сбросил сетевое соединение.")
            except asyncio.TimeoutError:
                # Таймаут ожидаем: сетевой принтер слушает порт и не шлет данные первым
                pass

            writer.write(file_data)
            await writer.drain()

            writer.close()
            await writer.wait_closed()
            return True, f"Файл отправлен на сетевой порт {host}:{port}", "RAW-SOCKET-JOB"
        except Exception as e:
            logger.error(f"Raw socket print failed: {e}", exc_info=True)
            return False, f"Сбой отправки на сокет принтера: {e}", None


