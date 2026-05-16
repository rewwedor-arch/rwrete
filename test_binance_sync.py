#!/usr/bin/env python3
import os
from pathlib import Path
from dotenv import load_dotenv
import ccxt  # Синхронная версия
import time

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

print(f'Testing with sync ccxt version...')
print(f'API Key: {API_KEY}')
print(f'API Secret: {API_SECRET}')

try:
    exchange = ccxt.binanceusdm({
        'apiKey': API_KEY,
        'secret': API_SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'future', 'testnet': True},
        'urls': {
            'api': {
                'public': 'https://testnet.binancefuture.com/fapi',
                'private': 'https://testnet.binancefuture.com/fapi'
            }
        }
    })
    
    print(f'\nTesting ping...')
    result = exchange.public_get_ping()
    print(f'Ping result: {result}')
    
    print(f'\nFetching balance...')
    balance = exchange.fetch_balance()
    print(f'SUCCESS!')
    print(f'USDT Balance: {balance.get("USDT", {})}')
    
except Exception as e:
    print(f'ERROR: {e}')
    print(f'Error type: {type(e).__name__}')
