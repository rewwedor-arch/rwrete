import asyncio
import ccxt.async_support as ccxt
import json
from smart_money_aggressive import SMCAnalyzer, StrategyConfig

async def main():
    exchange = ccxt.binanceusdm({'enableRateLimit': True})
    analyzer = SMCAnalyzer(exchange)
    
    symbols = [
        'BTC/USDT', 'ETH/USDT', 'BNB/USDT', 'SOL/USDT',
        'XRP/USDT', 'ADA/USDT', 'AVAX/USDT', 'DOGE/USDT',
        'DOT/USDT', 'LINK/USDT', 'POL/USDT', 'UNI/USDT',
        'ATOM/USDT', 'LTC/USDT', 'ETC/USDT', 'NEAR/USDT',
        'FIL/USDT', 'AAVE/USDT', 'ARB/USDT', 'OP/USDT',
        'VANA/USDT'
    ]
    
    print("Starting analysis...")
    for symbol in symbols:
        try:
            res = await analyzer.analyze_symbol(symbol)
            print(f"{symbol}: Score {res['score']}/7 - Indicators: {res['indicators']}")
        except Exception as e:
            print(f"Error on {symbol}: {e}")
            
    await exchange.close()

if __name__ == '__main__':
    asyncio.run(main())
