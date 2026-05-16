import ccxt.async_support as ccxt
import asyncio

async def main():
    exchange = ccxt.binanceusdm({'enableRateLimit': True})
    ticker = await exchange.fetch_ticker('ATOM/USDT')
    print(f'ATOM/USDT текущая цена: {ticker["last"]}')
    print(f'24ч изменение: {ticker["percentage"]:.2f}%')
    
    # История свечей за последние 2 часа
    ohlcv = await exchange.fetch_ohlcv('ATOM/USDT', '5m', limit=24)
    print(f'\nПоследние 2 часа (5m свечи):')
    for c in ohlcv:
        from datetime import datetime
        dt = datetime.fromtimestamp(c[0]/1000)
        print(f'  {dt.strftime("%H:%M")} O={c[1]:.3f} H={c[2]:.3f} L={c[3]:.3f} C={c[4]:.3f}')
    
    await exchange.close()

asyncio.run(main())
