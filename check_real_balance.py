import ccxt.async_support as ccxt
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

async def main():
    exchange = ccxt.binanceusdm({
        'apiKey': os.getenv('BINANCE_API_KEY', ''),
        'secret': os.getenv('BINANCE_API_SECRET', ''),
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })
    
    balance = await exchange.fetch_balance()
    usdt = balance.get('USDT', {})
    
    print(f"Реальный баланс Binance:")
    print(f"  Total: ${usdt.get('total', 0):.2f}")
    print(f"  Free:  ${usdt.get('free', 0):.2f}")
    print(f"  Used:  ${usdt.get('used', 0):.2f}")
    
    # Открытые позиции
    positions = await exchange.fetch_positions()
    open_pos = [p for p in positions if float(p.get('contracts', 0)) != 0]
    
    print(f"\nОткрытых позиций: {len(open_pos)}")
    total_unrealized = 0
    for p in open_pos:
        unrealized = float(p.get('unrealizedPnl', 0))
        total_unrealized += unrealized
        print(f"  {p['symbol']} {p['side']} qty={p['contracts']} entry={p['entryPrice']} PnL=${unrealized:.2f}")
    
    print(f"\nНереализованный PnL: ${total_unrealized:.2f}")
    
    # История закрытых позиций (последние 10)
    try:
        trades = await exchange.fetch_my_trades('ATOM/USDT', limit=10)
        print(f"\nПоследние сделки ATOM/USDT:")
        for t in trades[-5:]:
            print(f"  {t['datetime']} {t['side']} {t['amount']} @ {t['price']} fee={t['fee']['cost'] if t['fee'] else 0}")
    except Exception as e:
        print(f"Ошибка получения сделок: {e}")
    
    await exchange.close()

asyncio.run(main())
