import enum
from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy import (
    BigInteger, Integer, Float, String, Boolean, DateTime, Text, ForeignKey, Enum as SQLEnum
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class OrderStatus(str, enum.Enum):
    PENDING_CONFIG = "pending_config"              # Файл загружен, настраиваются страницы/копии
    PENDING_PAYMENT = "pending_payment"            # Ожидание оплаты
    PENDING_ADMIN_APPROVAL = "pending_admin_approval" # Ожидание подтверждения чека СБП админом
    QUEUED = "queued"                              # В очереди на печать
    PRINTING = "printing"                          # Отправлено на принтер
    COMPLETED = "completed"                        # Успешно напечатано
    FAILED = "failed"                              # Ошибка печати
    CANCELLED = "cancelled"                        # Отменено пользователем или админом


class TransactionType(str, enum.Enum):
    DEPOSIT = "deposit"                            # Пополнение баланса
    PRINT_CHARGE = "print_charge"                  # Списание за печать
    REFUND = "refund"                              # Возврат при сбое печати
    ADMIN_ADJUSTMENT = "admin_adjustment"          # Корректировка админом


class TransactionStatus(str, enum.Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False) # Telegram User ID
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    balance: Mapped[float] = mapped_column(Float, default=0.0)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    orders: Mapped[List["Order"]] = relationship("Order", back_populates="user", cascade="all, delete-orphan")
    transactions: Mapped[List["Transaction"]] = relationship("Transaction", back_populates="user", cascade="all, delete-orphan")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    
    original_filename: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(512))
    file_size: Mapped[int] = mapped_column(Integer)
    
    total_pages: Mapped[int] = mapped_column(Integer, default=1)
    selected_pages: Mapped[str] = mapped_column(String(128), default="all") # "all" или "1-4, 7"
    copies: Mapped[int] = mapped_column(Integer, default=1)
    pages_to_print_count: Mapped[int] = mapped_column(Integer, default=1)
    cost_rub: Mapped[float] = mapped_column(Float, default=0.0)
    
    status: Mapped[OrderStatus] = mapped_column(
        SQLEnum(OrderStatus, native_enum=False), default=OrderStatus.PENDING_CONFIG, index=True
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    printed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="orders")
    transactions: Mapped[List["Transaction"]] = relationship("Transaction", back_populates="order")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    order_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    
    amount: Mapped[float] = mapped_column(Float) # положительное для пополнения, отрицательное для списания
    type: Mapped[TransactionType] = mapped_column(SQLEnum(TransactionType, native_enum=False), index=True)
    status: Mapped[TransactionStatus] = mapped_column(SQLEnum(TransactionStatus, native_enum=False), default=TransactionStatus.PENDING)
    payment_method: Mapped[str] = mapped_column(String(32), default="manual_sbp") # manual_sbp, balance, stars, yookassa, admin
    
    provider_payment_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    proof_file_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True) # file_id скриншота чека
    
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    user: Mapped["User"] = relationship("User", back_populates="transactions")
    order: Mapped[Optional["Order"]] = relationship("Order", back_populates="transactions")


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
