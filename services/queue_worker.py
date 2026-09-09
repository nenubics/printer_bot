import asyncio
import logging
from pathlib import Path
from typing import Optional
from aiogram import Bot
from sqlalchemy import select, update
from config import settings
from database.db import async_session_factory
from database.models import Order, OrderStatus, TransactionType
from database.repo import Repository
from services.document import DocumentService
from services.printer import PrinterService

logger = logging.getLogger(__name__)


class PrintQueueWorker:
    def __init__(self, bot: Bot):
        self.bot = bot
        self._is_running = False
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    def start(self) -> None:
        if not self._is_running:
            self._is_running = True
            self._task = asyncio.create_task(self._worker_loop())
            logger.info("Print Queue Worker started.")

    async def stop(self) -> None:
        self._is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info("Print Queue Worker stopped.")

    async def _worker_loop(self) -> None:
        """Бесконечный цикл обработки очереди печати с авто-восстановлением"""
        # Восстановление зависших заданий при аварийном рестарте
        await self._recover_hanging_jobs()

        cleanup_counter = 0
        while self._is_running:
            try:
                async with self._lock:
                    await self._process_next_job()

                cleanup_counter += 1
                if cleanup_counter >= 30:
                    cleanup_counter = 0
                    await self._cleanup_expired_jobs()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in print queue worker loop: {e}", exc_info=True)

            await asyncio.sleep(2.0)

    async def _cleanup_expired_jobs(self) -> None:
        """Периодическая очистка брошенных неоплаченных заказов и файлов спула"""
        try:
            async with async_session_factory() as session:
                max_age = settings.SPOOL_CLEANUP_HOURS * 3600
                cleaned = await Repository.cleanup_expired_pending_orders(session, max_age_seconds=max_age)
                if cleaned > 0:
                    logger.info(f"Auto-cleaned {cleaned} expired abandoned orders and spool files.")
        except Exception as e:
            logger.error(f"Error cleaning expired spool files: {e}", exc_info=True)

    async def _recover_hanging_jobs(self) -> None:
        """Восстановление статусов заданий, которые прервались при перезапуске бота"""
        try:
            async with async_session_factory() as session:
                stmt = select(Order).where(Order.status == OrderStatus.PRINTING)
                result = await session.execute(stmt)
                hanging = result.scalars().all()
                for job in hanging:
                    logger.warning(f"Recovering hanging job {job.order_uuid} -> QUEUED")
                    job.status = OrderStatus.QUEUED
                if hanging:
                    await session.commit()
        except Exception as e:
            logger.error(f"Error recovering hanging jobs: {e}", exc_info=True)

    async def _process_next_job(self) -> None:
        """Извлечение и обработка одного заказа из очереди"""
        async with async_session_factory() as session:
            # Проверка глобальной паузы
            is_paused = await Repository.get_setting(session, "is_paused", "0")
            if is_paused == "1" or settings.EMERGENCY_STOP:
                return

            order = await Repository.get_next_queued_order(session)
            if not order:
                return

            order_id = order.id
            order_uuid = order.order_uuid
            user_id = order.user_id
            file_path = Path(order.file_path)
            copies = order.copies
            selected_pages = order.selected_pages
            cost_rub = order.cost_rub

            # 1. Проверяем статус принтера
            printer_status = await PrinterService.check_status()
            if not printer_status["is_ready"]:
                logger.warning(
                    f"Printer not ready for job {order_uuid}: {printer_status['message']}. "
                    f"Waiting for hardware readiness."
                )
                # Оповещаем админов, если это ошибка (но не спамим каждый тик)
                return

            # 2. Переводим статус в PRINTING
            await Repository.update_order_status(session, order_id, OrderStatus.PRINTING)

        logger.info(f"Started printing job {order_uuid} (copies={copies}, pages={selected_pages})")

        # 3. Физическая нарезка и подготовка страниц (вырезаем только выбранные страницы)
        prepared_pdf_path = file_path
        try:
            prepared_pdf_path = DocumentService.prepare_job_pdf(
                source_pdf=file_path,
                order_uuid=order_uuid,
                selected_pages=selected_pages,
                copies=copies if settings.PRINTER_MODE.lower() == "raw" else 1
            )
        except Exception as prep_err:
            logger.error(f"Failed to prepare/slice PDF for order {order_uuid}: {prep_err}", exc_info=True)
            prepared_pdf_path = file_path

        try:
            # 4. Выполняем печать подготовленного файла
            success, message, job_id = await PrinterService.print_job(
                pdf_path=prepared_pdf_path,
                copies=copies if settings.PRINTER_MODE.lower() != "raw" else 1,
                selected_pages="all",  # В prepared_pdf_path страницы уже физически нарезаны!
                title=f"Order_{order_uuid[:8]}"
            )
        finally:
            # Если создавался отдельный временный нарезанный файл, удаляем его
            if prepared_pdf_path != file_path:
                DocumentService.cleanup_file(str(prepared_pdf_path))

        async with async_session_factory() as session:
            if success:
                await Repository.update_order_status(session, order_id, OrderStatus.COMPLETED)
                # Безопасно удаляем исходный файл с диска
                DocumentService.cleanup_file(str(file_path))

                # Учет расхода бумаги в лотке Pantum BP2300NW
                printed_sheets = order.pages_to_print_count * copies
                new_count = await Repository.increment_paper_counter(session, printed_sheets)
                _, max_tray = await Repository.get_paper_counter(session)

                # Оповещение администраторов о приближении окончания бумаги
                if new_count >= max_tray - 25:
                    for admin_id in settings.ADMIN_IDS:
                        try:
                            await self.bot.send_message(
                                chat_id=admin_id,
                                text=(
                                    f"⚠️ <b>В лотке Pantum BP2300NW заканчивается бумага!</b>\n\n"
                                    f"Отпечатано с последней загрузки: <b>{new_count} / {max_tray}</b> листов.\n"
                                    f"Пожалуйста, проверьте лоток и добавьте бумагу в ЦСО-4.\n\n"
                                    f"<i>После пополнения сбросьте счетчик в панели администратора.</i>"
                                ),
                                parse_mode="HTML"
                            )
                        except Exception as e:
                            logger.error(f"Failed to alert admin {admin_id} about low paper: {e}")

                # Уведомляем пользователя
                try:
                    await self.bot.send_message(
                        chat_id=user_id,
                        text=(
                            f"🎉 <b>Ваш заказ успешно распечатан!</b>\n\n"
                            f"📄 Документ: <code>{order.original_filename}</code>\n"
                            f"📑 Страниц: {order.pages_to_print_count} (копий: {copies})\n\n"
                            f"📍 Заберите готовые листы из лотка принтера <b>Pantum BP2300NW</b> в ЦСО-4!"
                        ),
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error(f"Failed to send success message to user {user_id}: {e}")


            else:
                # В случае ошибки возвращаем деньги на баланс пользователю
                logger.error(f"Print job {order_uuid} failed: {message}")
                await Repository.update_order_status(
                    session, order_id, OrderStatus.FAILED, error_message=message
                )
                # Возврат средств
                await Repository.update_balance(
                    session,
                    user_id=user_id,
                    delta=cost_rub,
                    trans_type=TransactionType.REFUND,
                    payment_method="balance",
                    order_id=order_id
                )

                # Уведомляем пользователя о возврате
                try:
                    await self.bot.send_message(
                        chat_id=user_id,
                        text=(
                            f"⚠️ <b>Произошла ошибка при печати на принтере!</b>\n\n"
                            f"Причина: <i>{message}</i>\n\n"
                            f"💰 Мы автоматически вернули <b>{cost_rub:.2f} ₽</b> на ваш баланс в боте.\n"
                            f"Попробуйте повторить печать позже или напишите администратору."
                        ),
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error(f"Failed to send failure notification to user {user_id}: {e}")

                # Уведомляем администраторов о сбое оборудования
                for admin_id in settings.ADMIN_IDS:
                    try:
                        await self.bot.send_message(
                            chat_id=admin_id,
                            text=(
                                f"🚨 <b>СБОЙ ПРИНТЕРА Pantum BP2300NW!</b>\n\n"
                                f"Заказ ID: <code>{order_uuid}</code>\n"
                                f"Пользователь: <code>{user_id}</code>\n"
                                f"Ошибка: <code>{message}</code>\n"
                                f"Средства возвращены на баланс клиента."
                            ),
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        logger.error(f"Failed to alert admin {admin_id}: {e}")
