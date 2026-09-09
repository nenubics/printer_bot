import asyncio
import uuid
from collections import defaultdict
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
    async def get_next_queued_order(session: AsyncSession) -> Optional[Order]:
        """Получить следующий заказ из очереди печати"""
        stmt = select(Order).where(
            Order.status == OrderStatus.QUEUED
        ).order_by(Order.id.asc()).limit(1)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_queued_orders_count(session: AsyncSession) -> int:
        stmt = select(Order).where(Order.status.in_([OrderStatus.QUEUED, OrderStatus.PRINTING]))
        result = await session.execute(stmt)
        return len(result.scalars().all())

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

