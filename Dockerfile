FROM python:3.11-slim

# Установка системных утилит CUPS для работы с принтером
RUN apt-get update && apt-get install -y --no-install-recommends \
    cups-client \
    ghostscript \
    && rm -rf /var/lib/apt/lists/*

# Создание непривилегированного пользователя для максимальной безопасности
RUN useradd -m -u 1000 botuser

WORKDIR /app

# Установка Python зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование исходного кода
COPY . .

# Создание папок для БД и спула с нужными правами
RUN mkdir -p /app/data/spool /app/data/logs && \
    chown -R botuser:botuser /app

USER botuser

CMD ["python", "main.py"]
