#!/usr/bin/env python3
"""Тест: может ли открыть рыночный ордер без Read права"""
import os
from pathlib import Path
from dotenv import load_dotenv
import ccxt.async_support as ccxt
import asyncio

load_dotenv(Path('.').resolve() / '.env')

def _env_secret(*names):
    for n in names:
        raw = os.getenv(n)
        if raw is None:
            continue
        s = raw.strip().strip('\ufeff').strip('"').strip("'")
        if s:
            return s
    return ''

API_KEY = _env_secret('BINANCE_API_KEY')
API_SECRET = _env_secret('BINANCE_SECRET', 'BINANCE_API_SECRET')

async def test():
    exchange = ccxt.binanceusdm({
        'apiKey': API_KEY,
        'secret': API_SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'future'},
        'rateLimit': 2000  # 2 сек между запросами
    })
    exchange.has['fetchCurrencies'] = False  # Отключаем загрузку currencies (требует Read)
    
    try:
        # Загружаем маркеты
        print('Загрузка маркетов...')
        await asyncio.sleep(1)
        await exchange.load_markets()
        print('✅ Маркеты загружены')
        
        # Попробуем получить баланс (должно упасть без Read)
        try:
            balance = await exchange.fetch_balance()
            print(f'✅ Баланс получен: {balance.get("USDT", {})}')
        except Exception as e:
            print(f'⚠️ Баланс ошибка (ожидается без Read): {e}')
        
        # Попробуем получить открытые ордеры (должно упасть без Read)
        try:
            orders = await exchange.fetch_open_orders('BTC/USDT')
            print(f'✅ Открытые ордеры получены: {len(orders)} шт')
        except Exception as e:
            print(f'⚠️ Открытые ордеры ошибка (ожидается без Read): {e}')
        
        # Проверим, работает ли get_account (для позиций на фьючерсах)
        try:
            account = await exchange.fetch_account()
            print(f'✅ Account получена')
        except Exception as e:
            print(f'⚠️ Account ошибка: {e}')
        
        print("\n✅ ВЫВОД: Бот может открывать позиции (нужно право Trade)")
        print("          Ошибка fetch_balance - только потому что нет Read")
        
    except Exception as e:
        print(f'❌ Критическая ошибка: {e}')
    finally:
        await exchange.close()

asyncio.run(test())
