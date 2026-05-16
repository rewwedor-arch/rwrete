"""Quick test: new LONG+SHORT analyzer"""
import asyncio
import ccxt.async_support as ccxt
import sys
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

async def main():
    exchange = ccxt.binanceusdm({'enableRateLimit': True})
    from smart_money_aggressive import SMCAnalyzer, StrategyConfig
    analyzer = SMCAnalyzer(exchange)
    
    symbols = [
        'BTC/USDT', 'ETH/USDT', 'BNB/USDT', 'SOL/USDT',
        'XRP/USDT', 'ADA/USDT', 'AVAX/USDT', 'DOGE/USDT',
        'DOT/USDT', 'LINK/USDT', 'POL/USDT', 'UNI/USDT',
        'ATOM/USDT', 'LTC/USDT', 'ETC/USDT', 'NEAR/USDT',
        'FIL/USDT', 'AAVE/USDT', 'ARB/USDT', 'OP/USDT',
        'VANA/USDT'
    ]
    
    signals = []
    for symbol in symbols:
        r = await analyzer.analyze_symbol(symbol)
        dir_mark = 'L' if r['direction'] == 'LONG' else 'S'
        sig = 'SIGNAL!' if r['signal'] else '       '
        inds = ', '.join(r['indicators'].keys()) if r['indicators'] else '-'
        print(f"  {symbol:12s} {dir_mark} {r['score']}/7 {sig}  [{inds}]")
        if r['signal']:
            signals.append(r)
    
    print(f"\n  === ИТОГО СИГНАЛОВ: {len(signals)} ===")
    for s in signals:
        print(f"    {s['direction']} {s['symbol']} score={s['score']}/7")
    
    await exchange.close()

asyncio.run(main())
