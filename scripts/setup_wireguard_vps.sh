#!/usr/bin/env bash
# ==============================================================================
# Скрипт автоматической настройки WireGuard VPN моста на облачном VPS
# для защищенной печати на Pantum BP2300NW через роутер Xiaomi AX3000T (OpenWrt)
# ==============================================================================
set -euo pipefail

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}======================================================================${NC}"
echo -e "${BLUE}🚀 НАСТРОЙКА WIREGUARD VPN МОСТА: ОБЛАЧНЫЙ VPS <-> XIAOMI AX3000T (ЦСО-4)${NC}"
echo -e "${BLUE}======================================================================${NC}"

if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}❌ Пожалуйста, запустите этот скрипт с правами root (sudo bash $0)${NC}"
  exit 1
fi

PRINTER_IP="192.168.3.100"
SERVER_WG_IP="10.0.0.1/24"
ROUTER_WG_IP="10.0.0.2/32"
WG_PORT=51820
WG_DIR="/etc/wireguard"

# 1. Установка WireGuard
echo -e "\n${YELLOW}[1/5] Проверка и установка пакетов WireGuard...${NC}"
apt-get update -qq
apt-get install -y -qq wireguard iptables qrencode curl

# 2. Определение публичного IP сервера и основного сетевого интерфейса
echo -e "\n${YELLOW}[2/5] Определение сетевых параметров VPS...${NC}"
SERVER_PUBLIC_IP=$(curl -s4 ifconfig.me || curl -s4 api.ipify.org || echo "YOUR_VPS_PUBLIC_IP")
DEFAULT_IFACE=$(ip route show default | awk '{print $5}' | head -n1)
echo -e "   • Публичный IP VPS: ${GREEN}${SERVER_PUBLIC_IP}${NC}"
echo -e "   • Основной сетевой интерфейс: ${GREEN}${DEFAULT_IFACE}${NC}"

# 3. Включение маршрутизации пакетов (IP Forwarding)
echo -e "\n${YELLOW}[3/5] Включение IP Forwarding в ядре Linux...${NC}"
sysctl -w net.ipv4.ip_forward=1 > /dev/null
if ! grep -q "^net.ipv4.ip_forward=1" /etc/sysctl.conf; then
  echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
fi

# 4. Генерация ключей WireGuard
echo -e "\n${YELLOW}[4/5] Генерация ключей шифрования WireGuard (Curve25519)...${NC}"
mkdir -p "$WG_DIR"
chmod 700 "$WG_DIR"

SERVER_PRIVKEY=$(wg genkey)
SERVER_PUBKEY=$(echo "$SERVER_PRIVKEY" | wg pubkey)

ROUTER_PRIVKEY=$(wg genkey)
ROUTER_PUBKEY=$(echo "$ROUTER_PRIVKEY" | wg pubkey)

# 5. Создание конфигурации сервера /etc/wireguard/wg0.conf
echo -e "\n${YELLOW}[5/5] Создание конфигурации /etc/wireguard/wg0.conf...${NC}"
cat > "${WG_DIR}/wg0.conf" <<EOF
[Interface]
Address = ${SERVER_WG_IP}
ListenPort = ${WG_PORT}
PrivateKey = ${SERVER_PRIVKEY}

# Добавляем маршрут к принтеру через роутер в общаге (10.0.0.2)
PostUp = ip route add ${PRINTER_IP}/32 via 10.0.0.2 dev wg0 2>/dev/null || true
PostDown = ip route del ${PRINTER_IP}/32 via 10.0.0.2 dev wg0 2>/dev/null || true

# Клиент: Роутер Xiaomi AX3000T (ЦСО-4)
[Peer]
PublicKey = ${ROUTER_PUBKEY}
# Разрешаем роутеру иметь IP 10.0.0.2 и маршрутизировать трафик к принтеру
AllowedIPs = 10.0.0.2/32, ${PRINTER_IP}/32
EOF

chmod 600 "${WG_DIR}/wg0.conf"

# Запуск и включение в автозагрузку
systemctl daemon-reload
systemctl enable --now wg-quick@wg0
systemctl restart wg-quick@wg0

echo -e "\n${GREEN}======================================================================${NC}"
echo -e "${GREEN}✅ СЕРВЕР WIREGUARD УСПЕШНО НАСТРОЕН И ЗАПУЩЕН!${NC}"
echo -e "${GREEN}======================================================================${NC}"

# Вывод инструкций для Xiaomi AX3000T (OpenWrt)
echo -e "\n📋 ${YELLOW}СКОПИРУЙТЕ И ВЫПОЛНИТЕ НА РОУТЕРЕ XIAOMI AX3000T (OpenWrt):${NC}"
echo -e "Подключитесь к AX3000T по SSH (ssh root@192.168.3.1 или ваш IP) и выполните команды:\n"

cat <<UCI_COMMANDS
# 1. Установка пакета WireGuard на OpenWrt (если не установлен)
opkg update && opkg install luci-proto-wireguard kmod-wireguard wireguard-tools

# 2. Создание интерфейса 'wg0'
uci set network.wg0=interface
uci set network.wg0.proto='wireguard'
uci set network.wg0.private_key='${ROUTER_PRIVKEY}'
uci add_list network.wg0.addresses='10.0.0.2/24'

# 3. Настройка пира (сервера VPS)
uci set network.wgserver=wireguard_wg0
uci set network.wgserver.public_key='${SERVER_PUBKEY}'
uci set network.wgserver.endpoint_host='${SERVER_PUBLIC_IP}'
uci set network.wgserver.endpoint_port='${WG_PORT}'
uci set network.wgserver.persistent_keepalive='25'
uci add_list network.wgserver.allowed_ips='10.0.0.0/24'

# 4. Привязка к зоне firewall 'lan' (чтобы VPS мог слать печать на принтер)
uci add_list firewall.@zone[0].network='wg0'

# Сохранение и перезапуск сети
uci commit
/etc/init.d/network restart
/etc/init.d/firewall restart

UCI_COMMANDS

echo -e "${BLUE}======================================================================${NC}"
echo -e "После настройки на роутере проверьте статус соединения на VPS командой:"
echo -e "  ${YELLOW}wg show${NC}"
echo -e "И проверьте доступность порта печати Pantum BP2300NW:"
echo -e "  ${YELLOW}nc -zv -w 3 ${PRINTER_IP} 9100${NC}"
echo -e "${BLUE}======================================================================${NC}"
