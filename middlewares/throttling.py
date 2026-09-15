import time
import logging
from typing import Any, Awaitable, Callable, Dict, Optional
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from config import settings

logger = logging.getLogger(__name__)


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(
        self,
        limit_seconds: float = 0.8,
        burst_threshold: int = 5,
        block_duration: float = 30.0
    ):
        self.limit_seconds = limit_seconds
        self.burst_threshold = burst_threshold
        self.block_duration = block_duration
        self.user_timestamps: Dict[int, float] = {}
        self.violation_counts: Dict[int, int] = {}
        self.banned_until: Dict[int, float] = {}
        self._last_cleanup: float = time.monotonic()
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user_id: Optional[int] = None
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id

        if user_id:
            now = time.monotonic()

            # Периодическая очистка устаревших записей (защита от утечки RAM)
            if now - self._last_cleanup > 60.0:
                self._last_cleanup = now
                cutoff = now - 300.0
                self.user_timestamps = {uid: t for uid, t in self.user_timestamps.items() if t > cutoff}
                self.violation_counts = {uid: c for uid, c in self.violation_counts.items() if uid in self.user_timestamps}
                self.banned_until = {uid: t for uid, t in self.banned_until.items() if t > now}

            # 1. Проверяем, находится ли пользователь во временном бане за флуд
            ban_expiry = self.banned_until.get(user_id, 0.0)
            if now < ban_expiry:
                remaining = int(ban_expiry - now) + 1
                if isinstance(event, CallbackQuery):
                    await event.answer(
                        f"⛔️ Вы заблокированы за флуд. Подождите {remaining} сек.",
                        show_alert=True
                    )
                # Сообщения игнорируем, чтобы не расходовать квоту Telegram API
                return None

            last_time = self.user_timestamps.get(user_id, 0.0)
            if now - last_time < self.limit_seconds:
                # Нарушение лимита скорости
                violations = self.violation_counts.get(user_id, 0) + 1
                self.violation_counts[user_id] = violations

                if violations >= self.burst_threshold:
                    # Включаем штрафной бан
                    self.banned_until[user_id] = now + self.block_duration
                    self.violation_counts[user_id] = 0
                    logger.warning(
                        f"Anti-flood activated: user {user_id} banned for {self.block_duration}s "
                        f"(exceeded {self.burst_threshold} rapid requests)."
                    )
                    if isinstance(event, CallbackQuery):
                        await event.answer(
                            f"⛔️ Слишком частые запросы! Вы временно заблокированы на {int(self.block_duration)} сек.",
                            show_alert=True
                        )
                    return None

                if isinstance(event, CallbackQuery):
                    await event.answer("⚠️ Не так часто, пожалуйста!", show_alert=False)
                return None

            # Успешный запрос с допустимым интервалом
            self.user_timestamps[user_id] = now
            if user_id in self.violation_counts:
                # Постепенно уменьшаем счетчик нарушений
                self.violation_counts[user_id] = max(0, self.violation_counts[user_id] - 1)

            # Очистка старых записей для предотвращения утечки памяти
            if len(self.user_timestamps) > 5000:
                cutoff = now - 120.0
                self.user_timestamps = {uid: t for uid, t in self.user_timestamps.items() if t > cutoff}
                self.violation_counts = {uid: c for uid, c in self.violation_counts.items() if uid in self.user_timestamps}
                self.banned_until = {uid: b for uid, b in self.banned_until.items() if b > now}

        return await handler(event, data)

