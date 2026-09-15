#!/bin/sh
# Setup & Optimization Script for Pantum BP2300NW Printer Bot on OpenWrt
# Target Devices: Netis NX31 (MediaTek MT7981/MT7621), Huawei AX3 (Entware), MIPS / ARM Routers

set -e

echo "========================================================="
echo "   Pantum BP2300NW Telegram Bot - OpenWrt Setup Script"
echo "========================================================="

# 1. Проверка прав root
if [ "$(id -u)" != "0" ]; then
    echo "[-] Ошибка: Этот скрипт должен запускаться от имени root на роутере!"
    exit 1
fi

echo "[1/6] Обновление пакетов opkg..."
opkg update

echo "[2/6] Установка базовых зависимостей OpenWrt..."
# Установка python3, pillow и сопутствующих системных библиотек
opkg install \
    python3 \
    python3-pip \
    python3-asyncio \
    python3-pillow \
    libsqlite3 \
    ca-bundle \
    ca-certificates \
    coreutils-nice

# 3. Настройка ZRAM Swap (Сжатие оперативной памяти в RAM)
# На роутерах с 128-256 МБ RAM zram-swap дает дополнительно 128-200 МБ сжатого виртуального ОЗУ
echo "[3/6] Настройка zram-swap для безопасного расширения RAM..."
if opkg list | grep -q "zram-swap"; then
    opkg install zram-swap || true
    /etc/init.d/zram start || true
    /etc/init.d/zram enable || true
    echo "[+] zram-swap успешно активирован!"
else
    echo "[!] Пакет zram-swap не найден в репозитории, продолжаем без него."
fi

# 4. Проверка и создание папки установки
INSTALL_DIR="/opt/printer_bot"
if [ -d "/mnt/sda1" ]; then
    echo "[+] Обнаружен внешний USB-накопитель (/mnt/sda1). Рекомендуется использовать его для экономии флеш-памяти."
    INSTALL_DIR="/mnt/sda1/printer_bot"
fi

mkdir -p "$INSTALL_DIR"
mkdir -p /tmp/printer_bot_spool
chmod 700 /tmp/printer_bot_spool

echo "[4/6] Установка Python-библиотек (с флагом --no-cache-dir для защиты памяти)..."
pip3 install --no-cache-dir \
    "aiogram>=3.17.0" \
    "pydantic>=2.10.0" \
    "pydantic-settings>=2.7.0" \
    "aiosqlite>=0.21.0" \
    "sqlalchemy>=2.0.38" \
    "pypdf>=5.3.0" \
    "qrcode>=8.0"

# 5. Установка и регистрация procd сервиса
echo "[5/6] Настройка службы автозапуска procd..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/../deploy/openwrt/printer_bot.init" ]; then
    cp "$SCRIPT_DIR/../deploy/openwrt/printer_bot.init" /etc/init.d/printer_bot
    chmod +x /etc/init.d/printer_bot
    /etc/init.d/printer_bot enable
    echo "[+] Служба /etc/init.d/printer_bot установлена и включена в автозагрузку."
fi

echo "[6/6] Проверка сетевой связности с принтером..."
PRINTER_IP="192.168.3.13"
if nc -z -w 3 "$PRINTER_IP" 9100 2>/dev/null; then
    echo "[+] Сетевой порт 9100 на $PRINTER_IP доступен! Прямая печать готова к работе."
else
    echo "[!] Порт 9100 на $PRINTER_IP пока не отвечает. Убедитесь, что принтер включен и находится в одной подсети."
fi

echo "========================================================="
echo "   Установка завершена успешно!"
echo "   Для запуска:   /etc/init.d/printer_bot start"
echo "   Для статуса:   /etc/init.d/printer_bot status"
echo "   Лог спулера:   logread -e printer_bot"
echo "========================================================="
