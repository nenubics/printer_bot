import unittest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from database.models import Base, OrderStatus, TransactionType
from database.repo import Repository


class TestDatabaseAndBalance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_user_creation_and_idempotency(self):
        async with self.session_maker() as session:
            user = await Repository.get_or_create_user(
                session,
                user_id=1001,
                username="student1",
                full_name="Ivan Ivanov"
            )
            self.assertEqual(user.id, 1001)
            self.assertEqual(user.balance, 0.0)

            user2 = await Repository.get_or_create_user(
                session,
                user_id=1001,
                username="student1_new",
                full_name="Ivan Ivanov Updated"
            )
            self.assertEqual(user2.id, 1001)
            self.assertEqual(user2.username, "student1_new")

    async def test_atomic_balance_deposit_and_charges(self):
        async with self.session_maker() as session:
            user = await Repository.get_or_create_user(session, user_id=2002)

            # 1. Deposit 50.0 RUB
            success, bal, msg = await Repository.update_balance(
                session,
                user_id=2002,
                delta=50.0,
                trans_type=TransactionType.DEPOSIT,
                payment_method="manual_sbp"
            )
            self.assertTrue(success)
            self.assertEqual(bal, 50.0)

            # 2. Charge 35.0 RUB for printing
            success, bal, msg = await Repository.update_balance(
                session,
                user_id=2002,
                delta=-35.0,
                trans_type=TransactionType.PRINT_CHARGE,
                payment_method="balance"
            )
            self.assertTrue(success)
            self.assertEqual(bal, 15.0)

            # 3. Try to charge 20.0 RUB when only 15.0 RUB available (MUST FAIL, no overdraft)
            success, bal, msg = await Repository.update_balance(
                session,
                user_id=2002,
                delta=-20.0,
                trans_type=TransactionType.PRINT_CHARGE,
                payment_method="balance"
            )
            self.assertFalse(success)
            self.assertEqual(bal, 15.0)
            self.assertIn("Недостаточно средств", msg)

            # 4. Refund 10.0 RUB on printer error
            success, bal, msg = await Repository.update_balance(
                session,
                user_id=2002,
                delta=10.0,
                trans_type=TransactionType.REFUND,
                payment_method="balance"
            )
            self.assertTrue(success)
            self.assertEqual(bal, 25.0)

    async def test_order_lifecycle(self):
        async with self.session_maker() as session:
            user = await Repository.get_or_create_user(session, user_id=3003)

            order = await Repository.create_order(
                session=session,
                user_id=3003,
                original_filename="essay.pdf",
                file_path="/tmp/fake_spool.pdf",
                file_size=1024,
                total_pages=5,
                pages_to_print_count=5,
                cost_rub=25.0
            )
            self.assertEqual(order.status, OrderStatus.PENDING_CONFIG)
            self.assertIsNotNone(order.order_uuid)

            # Update to queued
            updated = await Repository.update_order_status(session, order.id, OrderStatus.QUEUED)
            self.assertEqual(updated.status, OrderStatus.QUEUED)

            # Queue worker fetch
            next_order = await Repository.get_next_queued_order(session)
            self.assertIsNotNone(next_order)
            self.assertEqual(next_order.id, order.id)

    async def test_paper_counter(self):
        async with self.session_maker() as session:
            # Check initial
            current, max_tray = await Repository.get_paper_counter(session)
            self.assertEqual(current, 0)
            self.assertEqual(max_tray, 150)

            # Increment after printing 25 sheets
            new_val = await Repository.increment_paper_counter(session, 25)
            self.assertEqual(new_val, 25)

            # Increment another 30 sheets
            new_val = await Repository.increment_paper_counter(session, 30)
            self.assertEqual(new_val, 55)

            # Reset counter
            await Repository.reset_paper_counter(session)
            current, _ = await Repository.get_paper_counter(session)
            self.assertEqual(current, 0)

    async def test_cancel_and_refund_order(self):
        async with self.session_maker() as session:
            user = await Repository.get_or_create_user(session, user_id=4004)
            # Give 50 RUB
            await Repository.update_balance(session, 4004, 50.0, TransactionType.DEPOSIT)

            # Deduct 30 RUB for print
            await Repository.update_balance(session, 4004, -30.0, TransactionType.PRINT_CHARGE)

            order = await Repository.create_order(
                session=session,
                user_id=4004,
                original_filename="thesis.pdf",
                file_path="/tmp/thesis.pdf",
                file_size=5000,
                total_pages=6,
                pages_to_print_count=6,
                cost_rub=30.0
            )
            await Repository.update_order_status(session, order.id, OrderStatus.QUEUED)

            # Cancel while queued -> must refund 30 RUB
            success, msg = await Repository.cancel_and_refund_order(session, order.id)
            self.assertTrue(success)

            user_updated = await Repository.get_user(session, 4004)
            self.assertEqual(user_updated.balance, 50.0) # Fully refunded


if __name__ == "__main__":
    unittest.main()

