import asyncio
import logging
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Optional, List, Tuple
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession
from database.models import (
    User, Order, Transaction, SystemSetting,
    OrderStatus, TransactionType, TransactionStatus, utc_now
)

_user_balance_locks = defaultdict(asyncio.Lock)


class Repository:

    @staticmethod
    async def get_or_create_user(
        session: AsyncSession,
        user_id: int,
        username: Optional[str] = None,
        full_name: str = ""
    ) -> User:
        user = await session.get(User, user_id)
        if not user:
            user = User(
                id=user_id,
                username=username,
                full_name=full_name,
                balance=0.0,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
        else:
            # Обновляем профиль при необходимости
            changed = False
            if user.username != username:
                user.username = username
                changed = True
            if user.full_name != full_name:
                user.full_name = full_name
                changed = True
            if changed:
                await session.commit()
                await session.refresh(user)
        return user

    @staticmethod
    async def get_user(session: AsyncSession, user_id: int) -> Optional[User]:
        user = await session.get(User, user_id)
        if user:
            await session.refresh(user)
        return user


    @staticmethod
    async def update_balance(
        session: AsyncSession,
        user_id: int,
        delta: float,
        trans_type: TransactionType,
        payment_method: str = "balance",
        order_id: Optional[int] = None,
        provider_payment_id: Optional[str] = None,
        proof_file_id: Optional[str] = None
    ) -> Tuple[bool, float, str]:
        """
        Атомарное изменение баланса пользователя с защитой от Race Condition (Double-Spending)
        и ухода баланса в минус.
        """
        async with _user_balance_locks[user_id]:
            rounded_delta = round(delta, 2)
            # Проверяем, существует ли пользователь
            user = await session.get(User, user_id)
            if not user:
                return False, 0.0, "Пользователь не найден"

            # Атомарный SQL UPDATE с проверкой условия достаточности баланса
            stmt = (
                update(User)
                .where(User.id == user_id)
                .where((User.balance + rounded_delta >= -0.00001) | (rounded_delta > 0))
                .values(balance=func.round(User.balance + rounded_delta, 2))
                .returning(User.balance)
            )
            result = await session.execute(stmt)
            new_balance = result.scalar_one_or_none()

            if new_balance is None:
                # Обновление не прошло, значит баланс стал бы отрицательным
                await session.refresh(user)
                return False, user.balance, "Недостаточно средств на балансе"

            tx = Transaction(
                user_id=user_id,
                order_id=order_id,
                amount=rounded_delta,
                type=trans_type,
                status=TransactionStatus.SUCCEEDED,
                payment_method=payment_method,
                provider_payment_id=provider_payment_id,
                proof_file_id=proof_file_id,
            )
            session.add(tx)
            await session.commit()
            return True, round(new_balance, 2), "Успешно"


    @staticmethod
    async def create_order(
        session: AsyncSession,
        user_id: int,
        original_filename: str,
        file_path: str,
        file_size: int,
        total_pages: int,
        pages_to_print_count: int,
        cost_rub: float,
        selected_pages: str = "all",
        copies: int = 1,
    ) -> Order:
        order = Order(
            order_uuid=str(uuid.uuid4()),
            user_id=user_id,
            original_filename=original_filename,
            file_path=file_path,
            file_size=file_size,
            total_pages=total_pages,
            pages_to_print_count=pages_to_print_count,
            cost_rub=cost_rub,
            selected_pages=selected_pages,
            copies=copies,
            status=OrderStatus.PENDING_CONFIG,
        )
        session.add(order)
        await session.commit()
        await session.refresh(order)
        return order

    @staticmethod
    async def get_order_by_uuid(session: AsyncSession, order_uuid: str) -> Optional[Order]:
        stmt = select(Order).where(Order.order_uuid == order_uuid)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_order_by_id(session: AsyncSession, order_id: int) -> Optional[Order]:
        return await session.get(Order, order_id)

    @staticmethod
    async def get_user_active_order(session: AsyncSession, user_id: int) -> Optional[Order]:
        """Возвращает текущий незавершенный заказ пользователя"""
        active_statuses = [
            OrderStatus.PENDING_CONFIG,
            OrderStatus.PENDING_PAYMENT,
            OrderStatus.PENDING_ADMIN_APPROVAL,
            OrderStatus.QUEUED,
            OrderStatus.PRINTING,
        ]
        stmt = select(Order).where(
            Order.user_id == user_id,
            Order.status.in_(active_statuses)
        ).order_by(Order.id.desc()).limit(1)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def update_order_status(
        session: AsyncSession,
        order_id: int,
        status: OrderStatus,
        error_message: Optional[str] = None
    ) -> Optional[Order]:
        order = await session.get(Order, order_id)
        if not order:
            return None
        order.status = status
        if error_message:
            order.error_message = error_message
        if status == OrderStatus.COMPLETED:
            order.printed_at = utc_now()
        await session.commit()
        await session.refresh(order)
        return order

    @staticmethod
    async def transition_order_status_atomic(
        session: AsyncSession,
        order_id: int,
        from_status: OrderStatus,
        to_status: OrderStatus,
        error_message: Optional[str] = None
    ) -> bool:
        """
        Атомарный CAS (Compare-And-Swap) переход статуса заказа.
        Возвращает True только если заказ действительно находился в статусе from_status.
        Исключает race condition при параллельных запросах (защита от повторной оплаты).
        """
        values = {"status": to_status}
        if error_message:
            values["error_message"] = error_message
        if to_status == OrderStatus.COMPLETED:
            values["printed_at"] = utc_now()

        stmt = (
            update(Order)
            .where(Order.id == order_id, Order.status == from_status)
            .values(**values)
            .returning(Order.id)
        )
        result = await session.execute(stmt)
        updated_id = result.scalar_one_or_none()
        await session.commit()
        return updated_id is not None

    @staticmethod
    async def get_next_queued_order(session: AsyncSession) -> Optional[Order]:
        """Получить следующий заказ из очереди печати"""
        stmt = select(Order).where(
            Order.status == OrderStatus.QUEUED
        ).order_by(Order.id.asc()).limit(1)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_queued_orders_count(session: AsyncSession) -> int:
        """Быстрый подсчет заказов в очереди без загрузки полных объектов в память"""
        stmt = select(func.count(Order.id)).where(Order.status.in_([OrderStatus.QUEUED, OrderStatus.PRINTING]))
        return (await session.scalar(stmt)) or 0

    @staticmethod
    async def get_queued_orders(session: AsyncSession) -> List[Order]:
        """Список заказов в очереди печати"""
        stmt = select(Order).where(
            Order.status.in_([OrderStatus.QUEUED, OrderStatus.PRINTING])
        ).order_by(Order.id.asc())
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def get_paper_counter(session: AsyncSession) -> Tuple[int, int]:
        """Возвращает (текущее число листов, лимит лотка)"""
        current_str = await Repository.get_setting(session, "paper_counter", "0")
        max_str = await Repository.get_setting(session, "paper_max_tray", "150")
        try:
            return int(current_str), int(max_str)
        except ValueError:
            return 0, 150

    @staticmethod
    async def increment_paper_counter(session: AsyncSession, delta: int) -> int:
        current, max_tray = await Repository.get_paper_counter(session)
        new_val = current + delta
        await Repository.set_setting(session, "paper_counter", str(new_val))
        return new_val

    @staticmethod
    async def reset_paper_counter(session: AsyncSession) -> None:
        await Repository.set_setting(session, "paper_counter", "0")

    @staticmethod
    async def cancel_and_refund_order(
        session: AsyncSession,
        order_id: int,
        user_id: Optional[int] = None
    ) -> Tuple[bool, str]:
        """Отмена заказа из очереди с автоматическим возвратом средств"""
        order = await session.get(Order, order_id)
        if not order:
            return False, "Заказ не найден."
        if user_id is not None and order.user_id != user_id:
            return False, "У вас нет прав на отмену этого заказа."
        if order.status not in (OrderStatus.QUEUED, OrderStatus.PENDING_PAYMENT, OrderStatus.PENDING_ADMIN_APPROVAL):
            return False, f"Заказ уже в статусе '{order.status.value}', отмена невозможна."

        prev_status = order.status
        order.status = OrderStatus.CANCELLED
        order.error_message = "Отменено пользователем"

        # Если был оплачен с баланса или СБП, возвращаем средства
        if prev_status == OrderStatus.QUEUED and order.cost_rub > 0:
            await Repository.update_balance(
                session=session,
                user_id=order.user_id,
                delta=order.cost_rub,
                trans_type=TransactionType.REFUND,
                payment_method="balance",
                order_id=order.id
            )

        await session.commit()
        return True, "Заказ отменен, средства возвращены на баланс."

    @staticmethod
    async def get_user_pending_orders_count(session: AsyncSession, user_id: int) -> int:
        """Подсчет активных неоплаченных заказов пользователя (защита от переполнения спула)"""
        stmt = select(func.count(Order.id)).where(
            Order.user_id == user_id,
            Order.status.in_([
                OrderStatus.PENDING_CONFIG,
                OrderStatus.PENDING_PAYMENT,
                OrderStatus.PENDING_ADMIN_APPROVAL
            ])
        )
        return (await session.scalar(stmt)) or 0

    @staticmethod
    async def get_user_pending_deposits_count(session: AsyncSession, user_id: int) -> int:
        """Подсчет неподтвержденных заявок на пополнение баланса"""
        stmt = select(func.count(Transaction.id)).where(
            Transaction.user_id == user_id,
            Transaction.type == TransactionType.DEPOSIT,
            Transaction.status == TransactionStatus.PENDING
        )
        return (await session.scalar(stmt)) or 0

    @staticmethod
    async def cleanup_expired_pending_orders(session: AsyncSession, max_age_seconds: int = 7200) -> int:
        """Очистка брошенных неоплаченных заказов и их файлов на диске"""
        from datetime import datetime, timezone, timedelta
        from pathlib import Path
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        stmt = select(Order).where(
            Order.status.in_([OrderStatus.PENDING_CONFIG, OrderStatus.PENDING_PAYMENT]),
            Order.created_at < cutoff
        )
        result = await session.execute(stmt)
        expired_orders = result.scalars().all()
        cleaned_count = 0
        for order in expired_orders:
            # Удаляем файл с диска
            try:
                p = Path(order.file_path)
                if p.exists() and p.is_file():
                    p.unlink()
            except Exception:
                pass
            order.status = OrderStatus.CANCELLED
            order.error_message = "Истек срок ожидания оплаты (автоочистка)"
            cleaned_count += 1

        if cleaned_count > 0:
            await session.commit()
        return cleaned_count

    @staticmethod
    async def get_setting(session: AsyncSession, key: str, default: str = "") -> str:
        s = await session.get(SystemSetting, key)
        return s.value if s else default

    @staticmethod
    async def set_setting(session: AsyncSession, key: str, value: str) -> None:
        s = await session.get(SystemSetting, key)
        if s:
            s.value = value
        else:
            s = SystemSetting(key=key, value=value)
            session.add(s)
        await session.commit()

    @classmethod
    async def backup_database(
        cls,
        backup_dir: Optional[Path] = None,
        session: Optional[AsyncSession] = None
    ) -> Optional[Path]:
        """
        Создает онлайн-бэкап базы данных без остановки сервиса:
        - Использует SQLite VACUUM INTO для безопасной репликации активной БД (включая WAL).
        - Сжимает дамп с помощью gzip (уменьшение размера до ~85%).
        - Ограничивает количество резервных копий (хранит 3 последних) для защиты диска роутера.
        - Устанавливает права 0600 на архив.
        """
        import gzip
        import shutil
        import os
        from datetime import datetime
        from config import settings
        from sqlalchemy import text

        _logger = logging.getLogger(__name__)

        if session is None and not settings.DB_PATH.exists():
            return None

        target_dir = backup_dir or (settings.DATA_DIR / "backups")
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(target_dir, 0o700)
        except Exception:
            pass

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        temp_bak = target_dir / f"backup_{timestamp}.db"
        gz_bak = target_dir / f"printer_bot_backup_{timestamp}.db.gz"

        try:
            if session is not None:
                await session.execute(text(f"VACUUM INTO '{temp_bak.resolve()}';"))
            else:
                from database.db import async_session_factory
                async with async_session_factory() as s:
                    await s.execute(text(f"VACUUM INTO '{temp_bak.resolve()}';"))

            if temp_bak.exists():
                with open(temp_bak, "rb") as f_in, gzip.open(gz_bak, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
                temp_bak.unlink()
                try:
                    os.chmod(gz_bak, 0o600)
                except Exception:
                    pass

                # Ротация старых бэкапов (храним 3 последних)
                existing = sorted(list(target_dir.glob("printer_bot_backup_*.db.gz")), key=lambda p: p.stat().st_mtime)
                while len(existing) > 3:
                    oldest = existing.pop(0)
                    oldest.unlink(missing_ok=True)

                _logger.info(f"Database online backup created successfully: {gz_bak.name} ({gz_bak.stat().st_size // 1024} KB)")
                return gz_bak
        except Exception as e:
            _logger.error(f"Database online backup error: {e}", exc_info=True)
            if temp_bak.exists():
                temp_bak.unlink(missing_ok=True)
            return None


