#!/usr/bin/env python3
import os
from pathlib import Path
from dotenv import load_dotenv
import ccxt.async_support as ccxt
import asyncio
import logging

logging.basicConfig(level=logging.DEBUG)

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

print(f'API Key length: {len(API_KEY)}')
print(f'API Secret length: {len(API_SECRET)}')
print(f'API Key: {API_KEY}')
print(f'API Secret: {API_SECRET}')

async def test():
    # Попробуем разные конфигурации
    configs = [
        {
            'apiKey': API_KEY,
            'secret': API_SECRET,
            'enableRateLimit': True,
            'options': {'defaultType': 'future', 'recvWindow': 60000}
        },
        {
            'apiKey': API_KEY,
            'secret': API_SECRET,
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}
        },
        {
            'apiKey': API_KEY,
            'secret': API_SECRET,
        },
    ]
    
    for i, config in enumerate(configs):
        print(f'\n--- Config {i+1} ---')
        print(f'Config: {config}')
        exchange = ccxt.binanceusdm(config)
        try:
            await exchange.load_markets()
            print(f'load_markets() SUCCESS')
            balance = await exchange.fetch_balance()
            print(f'fetch_balance() SUCCESS')
            print(f'USDT Balance: {balance.get("USDT", {})}')
            break
        except Exception as e:
            print(f'ERROR: {e}')
            import traceback
            traceback.print_exc()
        finally:
            try:
                await exchange.close()
            except:
                pass

asyncio.run(test())
