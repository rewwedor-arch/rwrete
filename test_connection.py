import asyncio
import ccxt.async_support as ccxt
import os
from dotenv import load_dotenv

load_dotenv()

async def test():
    exchange = ccxt.binanceusdm({
        'apiKey': os.getenv('BINANCE_API_KEY'),
        'secret': os.getenv('BINANCE_SECRET'),
        'enableRateLimit': True,
        'options': {
            'defaultType': 'future',
            'recvWindow': 60000
        }
    })
    try:
        await exchange.load_markets()
        print("✅ Маркеты загружены успешно!")

        balance = await exchange.fetch_balance()
        usdt = balance.get('total', {}).get('USDT', 0)
        print(f"✅ Баланс USDT: {usdt}")

        # Проверим фьючерсный баланс
        if hasattr(exchange, 'fetch_positions'):
            positions = await exchange.fetch_positions()
            print(f"✅ Позиции: {len(positions)}")

        print("\n🎉 API ключ работает! Бот может открывать позиции.")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
    finally:
        await exchange.close()

asyncio.run(test())