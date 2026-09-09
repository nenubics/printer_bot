import asyncio
import socket
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def probe_ip_port(ip: str, port: int = 9100, timeout: float = 0.5) -> bool:
    """Асинхронная проверка доступности порта на IP-адресе"""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        await asyncio.wait_for(loop.sock_connect(sock, (ip, port)), timeout=timeout)
        sock.close()
        return True
    except (socket.error, asyncio.TimeoutError):
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


async def scan_subnet(prefix: str, port: int = 9100):
    """Параллельное сканирование всей подсети /24 (254 адреса) за ~1-2 секунды"""
    tasks = []
    ips = [f"{prefix}.{i}" for i in range(1, 255)]

    sem = asyncio.Semaphore(50)

    async def worker(target_ip):
        async with sem:
            is_open = await probe_ip_port(target_ip, port=port, timeout=0.8)
            if is_open:
                return target_ip
            return None

    results = await asyncio.gather(*[worker(ip) for ip in ips])
    return [r for r in results if r is not None]


def get_local_ip() -> str:
    """Определение локального IP-адреса хоста"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Не выполняет реального сетевого вызова, только определяет интерфейс маршрутизации
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


async def main():
    print("=" * 70)
    print("🔍 ПОИСК ПРИНТЕРА PANTUM BP2300NW В СЕТИ РОУТЕРА HUAWEI AX3")
    print("=" * 70)

    local_ip = get_local_ip()
    print(f"🖥 Локальный IP текущего устройства: {local_ip}")

    # Стандартные подсети для Huawei AX3:
    # 1. 192.168.3.x (стандартная заводская подсеть Huawei Wi-Fi AX3)
    # 2. 192.168.8.x (подсеть Huawei Mobile WiFi / 4G роутеров)
    # 3. Текущая подсеть хоста
    subnets_to_scan = ["192.168.3"]

    if local_ip != "127.0.0.1" and not local_ip.startswith("198.18."):
        parts = local_ip.split(".")
        current_subnet = f"{parts[0]}.{parts[1]}.{parts[2]}"
        if current_subnet not in subnets_to_scan:
            subnets_to_scan.append(current_subnet)

    if "192.168.1" not in subnets_to_scan:
        subnets_to_scan.append("192.168.1")
    if "192.168.8" not in subnets_to_scan:
        subnets_to_scan.append("192.168.8")


    found_printers = []

    for subnet in subnets_to_scan:
        print(f"\n📡 Сканирование подсети {subnet}.0/24 на наличие порта 9100 (RAW JetDirect)...")
        found = await scan_subnet(subnet, port=9100)
        if found:
            for ip in found:
                print(f"   🟢 НАЙДЕНО УСТРОЙСТВО ПЕЧАТИ: {ip}:9100")
                found_printers.append(ip)
        else:
            print(f"   Устройств с портом 9100 в подсети {subnet}.x не обнаружено.")

    print("\n" + "=" * 70)
    if found_printers:
        chosen_ip = found_printers[0]
        print(f"🎉 ПРИНТЕР ОБНАРУЖЕН ПО АДРЕСУ: {chosen_ip}")
        print("\nДля подключения бота к принтеру через роутер Huawei AX3 пропишите в .env:")
        print("-" * 50)
        print(f"PRINTER_MODE=raw")
        print(f"PRINTER_HOST={chosen_ip}")
        print(f"PRINTER_PORT=9100")
        print("-" * 50)
        print("\nИли для CUPS (Linux / macOS):")
        print(f"lpadmin -p Pantum_BP2300NW -E -v socket://{chosen_ip}:9100 -m everywhere")
    else:
        print("ℹ️ В текущей локальной сети активный принтер Pantum не ответил на порту 9100.")
        print("💡 Подсказка для настройки на роутере Huawei AX3:")
        print("1. Подключите Pantum BP2300NW к Wi-Fi роутера Huawei AX3 (кнопкой WPS или через кабель).")
        print("2. Откройте панель роутера в браузере: http://192.168.3.1")
        print("3. В разделе «Управление устройствами» найдите Pantum и зафиксируйте за ним статический IP (например 192.168.3.50).")
        print("4. Пропишите этот IP в .env: PRINTER_HOST=192.168.3.50 и PRINTER_MODE=raw")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
