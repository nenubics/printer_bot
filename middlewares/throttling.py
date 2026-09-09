import time
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from config import settings


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, limit_seconds: float = 0.8):
        self.limit_seconds = limit_seconds
        self.user_timestamps: Dict[int, float] = {}
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user_id = None
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id

        if user_id:
            now = time.monotonic()
            last_time = self.user_timestamps.get(user_id, 0.0)
            if now - last_time < self.limit_seconds:
                # Слишком частые запросы - антифлуд
                if isinstance(event, Message):
                    # Отвечаем только при повторном спаме или не спамим в ответ
                    pass
                elif isinstance(event, CallbackQuery):
                    await event.answer("⚠️ Не так часто, пожалуйста!", show_alert=False)
                return None

            self.user_timestamps[user_id] = now

            # Периодическая очистка старых таймстемпов, чтобы не раздувать память
            if len(self.user_timestamps) > 10000:
                cutoff = now - 60.0
                self.user_timestamps = {
                    uid: t for uid, t in self.user_timestamps.items() if t > cutoff
                }

        return await handler(event, data)
