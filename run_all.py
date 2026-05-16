#!/usr/bin/env python3
"""
SMART MONEY AGGRESSIVE - ALL-IN-ONE LAUNCHER
Запускает всё одной командой:
1. Проверяет зависимости
2. Создает .env с ключами
3. Запускает Торгового бота + Telegram + Веб-дашборд
"""

import os
import sys

# Fix Unicode encoding for Windows console (cp1251 -> utf-8)
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
elif sys.stdout:
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer)

import subprocess
import asyncio
import threading
import time
from pathlib import Path

# === Шаблон .env (секреты только в файле .env, не в коде) ===
CONFIG = {
    "BINANCE_API_KEY": "",
    "BINANCE_SECRET": "",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "USER_CHAT_ID": "",
    "DEPOSIT": "140",
    "ENTRY_AMOUNT": "140",
    "LEVERAGE": "75",
    "STOP_LOSS": "3.5",
    "REINVEST_PROFITS": "True",
    "ENTRY_PERCENT": "100.0",
    "DRAWDOWN_ALERT": "12.0",
}

REQUIRED_PACKAGES = [
    "ccxt",
    "python-telegram-bot",
    "flask",
    "flask-socketio",
    "python-dotenv",
    "websockets",
    "requests",
    "pandas",
    "numpy"
]

def print_header():
    print("="*60)
    print("  SMART MONEY AGGRESSIVE - AUTO LAUNCHER")
    print("  Запуск всех модулей в одном окне")
    print("="*60)

def install_dependencies():
    print("\n[1/4] Проверка зависимостей...")
    missing = []
    for pkg in REQUIRED_PACKAGES:
        try:
            __import__(pkg.replace("-", "_"))
        except ImportError:
            missing.append(pkg)
    
    if missing:
        print(f"   Отсутствуют пакеты: {', '.join(missing)}")
        print("   Установка... (это может занять минуту)")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", *missing, "--quiet"])
            print("   ✅ Зависимости установлены!")
        except Exception as e:
            print(f"   ❌ Ошибка установки: {e}")
            print("   Попробуйте вручную: pip install " + " ".join(missing))
            return False
    else:
        print("   ✅ Все зависимости найдены.")
    return True

def create_env_file():
    print("\n[2/4] Файл конфигурации .env...")
    root = Path(__file__).resolve().parent
    env_path = root / ".env"
    
    if env_path.exists():
        print("   ✅ .env уже есть — не перезаписываем (ключи правьте вручную в этом файле).")
        return True
    
    content = "\n".join(f"{k}={v}" for k, v in CONFIG.items())
    content += "\nRUN_MODE=ALL_IN_ONE\n"
    
    try:
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(content)
        print("   ✅ Создан пустой шаблон .env — вставьте BINANCE_* и TELEGRAM_* ключи.")
        print("   ⚠️ Не передавайте .env никому; он в .gitignore.")
    except Exception as e:
        print(f"   ❌ Ошибка записи .env: {e}")
        return False
    return True

def run_trading_bot():
    """Запускает основной торговый цикл"""
    print("\n[3/4] Запуск ТОРГОВОГО БОТА...")
    try:
        # Импортируем логику из основного файла, если он существует, 
        # или запускаем его как процесс. 
        # Для надежности в рамках одного скрипта мы эмулируем запуск модулей.
        
        # Если файл smart_money_aggressive.py существует, запускаем его логику
        if Path("smart_money_aggressive.py").exists():
            print("   🚀 Запуск smart_money_aggressive.py...")
            # В реальном сценарии мы бы импортировали main(), 
            # но чтобы избежать конфликтов имен и глобальных переменных,
            # мы запустим это в отдельном потоке или просто выполним код.
            # Для простоты "одного файла" мы предположим, что пользователь 
            # хочет видеть вывод всех систем.
            
            # Эмуляция: здесь должен быть вызов основной функции бота
            # Поскольку мы не можем гарантировать чистый импорт без рефакторинга всех файлов,
            # мы используем subprocess для изоляции, но в том же окне.
            # НО! subprocess заблокирует вывод. 
            # Лучший вариант для "одного файла" - это собрать всю логику здесь.
            
            # Однако, чтобы не дублировать код, мы просто скажем пользователю,
            # что сейчас будет запуск.
            pass
        else:
            print("   ⚠️ Файл smart_money_aggressive.py не найден. Пропуск.")
    except Exception as e:
        print(f"   ❌ Ошибка запуска бота: {e}")

def run_dashboard():
    """Запускает веб-сервер"""
    print("\n[4/4] Запуск ВЕБ-ДАШБОРДА...")
    if Path("dashboard_aggressive.py").exists():
        print("   🚀 Запуск dashboard_aggressive.py...")
    else:
        print("   ⚠️ Файл dashboard_aggressive.py не найден. Пропуск.")

def main():
    print_header()
    root = Path(__file__).resolve().parent
    os.chdir(root)
    print(f"   Рабочая папка: {root}")
    
    # 1. Зависимости
    if not install_dependencies():
        print("\n❌ Не удалось установить зависимости. Выход.")
        sys.exit(1)
    
    # 2. ENV
    if not create_env_file():
        print("\n❌ Не удалось создать .env. Выход.")
        sys.exit(1)
    
    # Загрузка параметров стратегии из .env
    from dotenv import load_dotenv
    load_dotenv(root / '.env')
    
    print("\n" + "="*60)
    print("  ЗАПУСК СИСТЕМЫ...")
    print("="*60)
    print("  📡 Торговый бот: ACTIVE")
    print("  💬 Telegram бот: ACTIVE")
    print("  🖥️  Веб-дашборд: http://127.0.0.1:5000")
    print("  🛑 Нажмите Ctrl+C для остановки")
    print("="*60 + "\n")
    
    # Теперь нам нужно запустить три асинхронные задачи параллельно.
    # Поскольку оригинальные скрипты могут иметь свои циклы событий,
    # самый надежный способ "в одном файле" без переписывания всего кода -
    # использовать subprocess для каждого скрипта, но перенаправить вывод сюда.
    
    processes = []
    
    # Запуск торгового бота
    bot_py = root / "smart_money_aggressive.py"
    if bot_py.exists():
        p_bot = subprocess.Popen([sys.executable, str(bot_py)], cwd=str(root))
        processes.append(("Trading Bot", p_bot))
    
    # Telegram менеджер НЕ запускаем отдельно — он уже встроен в smart_money_aggressive.py
    # Два процесса с одним токеном вызывают конфликт getUpdates
    # if Path("telegram_manager.py").exists():
    #     p_tg = subprocess.Popen([sys.executable, "telegram_manager.py"])
    #     processes.append(("Telegram Manager", p_tg))
        
    dash_py = root / "dashboard_aggressive.py"
    if dash_py.exists():
        p_dash = subprocess.Popen([sys.executable, str(dash_py)], cwd=str(root))
        processes.append(("Dashboard", p_dash))
    
    if not processes:
        print("❌ Не найдено ни одного файла для запуска (.py).")
        print("Убедитесь, что smart_money_aggressive.py и dashboard_aggressive.py лежат в одной папке с run_all.py.")
        sys.exit(1)

    print(f"\n✅ Запущено процессов: {len(processes)}")
    print("Ожидание работы... (Нажмите Ctrl+C для выхода)\n")

    try:
        # Ждем завершения любого из процессов: бот мог упасть по API — дашборд не гасим
        while True:
            time.sleep(1)
            for name, proc in list(processes):
                if proc.poll() is not None:
                    rc = proc.returncode
                    print(f"\n⚠️ Процесс «{name}» завершился (код выхода {rc}).")
                    processes = [(n, p) for n, p in processes if p is not proc]
                    if name == "Trading Bot" and rc == 2:
                        print(
                            "   Частая причина: Binance отклонил ключ (-2015). "
                            "Проверьте .env, Futures API и что в коде указан тот же режим (testnet / mainnet), что и ключ."
                        )
                    if not processes:
                        print("Все процессы завершены.")
                        sys.exit(rc if rc is not None else 0)
                    else:
                        alive = ", ".join(n for n, _ in processes)
                        print(f"   Оставляем работать: {alive}")
    except KeyboardInterrupt:
        print("\n\n🛑 Получен сигнал остановки. Завершение работы...")
        for name, proc in processes:
            if proc.poll() is None:
                proc.terminate()
                print(f"   ✅ {name} остановлен.")
        print("Все процессы остановлены. До свидания!")

if __name__ == "__main__":
    main()
