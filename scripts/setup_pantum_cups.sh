#!/usr/bin/env bash
# ==============================================================================
# Скрипт автоматической настройки принтера Pantum BP2300NW в CUPS (Linux / macOS)
# Для работы в общежитии ЦСО-4
# ==============================================================================

set -e

PRINTER_NAME="Pantum_BP2300NW"
DEFAULT_IP="192.168.1.100"

echo "=========================================================="
echo " Настройка принтера $PRINTER_NAME для Telegram-бота печати"
echo "=========================================================="

# 1. Проверка наличия CUPS
if ! command -v lpadmin &> /dev/null; then
    echo "⚠️ CUPS утилиты не найдены. Установка необходимых пакетов..."
    if command -v apt-get &> /dev/null; then
        sudo apt-get update
        sudo apt-get install -y cups cups-client
        sudo systemctl enable --now cups
    elif command -v dnf &> /dev/null; then
        sudo dnf install -y cups
        sudo systemctl enable --now cups
    else
        echo "❌ Пожалуйста, установите пакет cups вручную для вашей ОС."
        exit 1
    fi
fi

echo "✅ Подсистема CUPS активна."

# 2. Выбор способа подключения
echo ""
echo "Выберите тип подключения Pantum BP2300NW:"
echo "1) Сетевое подключение (Wi-Fi или кабель Ethernet в роутер)"
echo "2) Прямое USB-подключение к компьютеру/серверу"
read -r -p "Введите 1 или 2 [по умолчанию 1]: " CONN_TYPE
CONN_TYPE=${CONN_TYPE:-1}

DEVICE_URI=""

if [ "$CONN_TYPE" == "1" ]; then
    read -r -p "Введите IP-адрес принтера в сети ЦСО-4 [по умолчанию $DEFAULT_IP]: " PRINTER_IP
    PRINTER_IP=${PRINTER_IP:-$DEFAULT_IP}
    
    echo "🔍 Проверка доступности принтера по IP: $PRINTER_IP (порт 9100 / JetDirect)..."
    if nc -z -w 3 "$PRINTER_IP" 9100 2>/dev/null; then
        echo "✅ Порт 9100 доступен!"
    else
        echo "⚠️ Предупреждение: порт 9100 не ответил за 3 сек. Убедитесь, что принтер включен и IP верен."
    fi
    DEVICE_URI="socket://${PRINTER_IP}:9100"
else
    echo "🔍 Поиск подключенных USB принтеров через lpinfo..."
    USB_FOUND=$(lpinfo -v 2>/dev/null | grep -i "usb://" | grep -i "pantum" | head -n 1 | awk '{print $2}')
    if [ -n "$USB_FOUND" ]; then
        echo "✅ Найден USB принтер: $USB_FOUND"
        DEVICE_URI="$USB_FOUND"
    else
        echo "⚠️ Автопоиск по слову 'pantum' не нашел устройство. Доступные USB-устройства:"
        lpinfo -v 2>/dev/null | grep "usb://" || true
        read -r -p "Введите полный Device URI (например usb://Pantum/...): " DEVICE_URI
    fi
fi

if [ -z "$DEVICE_URI" ]; then
    echo "❌ Ошибка: не указан Device URI принтера."
    exit 1
fi

echo ""
echo "⚙️ Настройка очереди печати: $PRINTER_NAME ($DEVICE_URI)..."

# Удаление старой очереди, если есть
lpadmin -x "$PRINTER_NAME" 2>/dev/null || true

# Добавление очереди с драйвером IPP Everywhere или Generic PCL
lpadmin -p "$PRINTER_NAME" -E -v "$DEVICE_URI" -m everywhere 2>/dev/null || \
lpadmin -p "$PRINTER_NAME" -E -v "$DEVICE_URI" -m "raw" 2>/dev/null || \
lpadmin -p "$PRINTER_NAME" -E -v "$DEVICE_URI"

# Настройка формата листа A4 по умолчанию
lpoptions -p "$PRINTER_NAME" -o media=A4 -o fit-to-page
cupsenable "$PRINTER_NAME" 2>/dev/null || true
cupsaccept "$PRINTER_NAME" 2>/dev/null || true

echo "✅ Очередь $PRINTER_NAME успешно создана и включена!"
echo ""
echo "Текущее состояние принтера:"
lpstat -p "$PRINTER_NAME"

echo ""
read -r -p "Хотите напечатать тестовую страницу прямо сейчас? (y/n) [n]: " DO_TEST
if [[ "$DO_TEST" =~ ^[Yy]$ ]]; then
    echo "Печать тестовой страницы..."
    printf "\n\n==============================\n  ЦСО-4 Pantum BP2300NW Test  \n  Принтер успешно подключен!  \n==============================\n" | lp -d "$PRINTER_NAME" -o media=A4
    echo "Задание отправлено в принтер!"
fi

echo ""
echo "🎉 Настройка завершена! Не забудьте указать в файле .env:"
echo "PRINTER_MODE=cups"
echo "PRINTER_NAME=$PRINTER_NAME"
