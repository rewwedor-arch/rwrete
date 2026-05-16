import ccxt.async_support as ccxt
import asyncio

async def main():
    exchange = ccxt.binanceusdm({'enableRateLimit': True})
    
    positions = [
        ('BTC/USDT', 'SHORT', 78025.9, 0.0054),
        ('ETH/USDT', 'SHORT', 2168.4, 0.195),
        ('SOL/USDT', 'SHORT', 85.97, 4.94),
        ('FIL/USDT', 'SHORT', 0.953, 479.6),
    ]
    
    total_pnl = 0
    print("Текущие позиции:\n")
    
    for symbol, side, entry, qty in positions:
        ticker = await exchange.fetch_ticker(symbol)
        current = ticker['last']
        
        if side == 'SHORT':
            pnl = (entry - current) * qty
            pct = ((entry - current) / entry) * 100
        else:
            pnl = (current - entry) * qty
            pct = ((current - entry) / entry) * 100
        
        total_pnl += pnl
        emoji = "📈" if pnl >= 0 else "📉"
        print(f"{emoji} {side} {symbol}")
        print(f"   Вход: ${entry} | Текущая: ${current:.2f}")
        print(f"   PnL: ${pnl:.2f} ({pct:.2f}%)\n")
    
    print(f"{'='*40}")
    print(f"Общий PnL: ${total_pnl:.2f}")
    
    await exchange.close()

asyncio.run(main())
