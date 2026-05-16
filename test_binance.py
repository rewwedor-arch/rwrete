#!/usr/bin/env python3
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

print(f'API Key length: {len(API_KEY)}')
print(f'API Secret length: {len(API_SECRET)}')
print(f'API Key starts: {API_KEY[:20]}...')
print(f'API Secret starts: {API_SECRET[:20]}...')

async def test():
    exchange = ccxt.binanceusdm({
        'apiKey': API_KEY,
        'secret': API_SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'future', 'recvWindow': 60000}
    })
    try:
        await exchange.load_markets()
        balance = await exchange.fetch_balance()
        print('SUCCESS!')
        print(f'USDT Balance: {balance.get("USDT", {})}')
    except Exception as e:
        print(f'ERROR: {e}')
        print(f'Error type: {type(e).__name__}')
    finally:
        await exchange.close()

asyncio.run(test())
