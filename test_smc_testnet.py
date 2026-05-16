import asyncio
import ccxt.async_support as ccxt
import json
from smart_money_aggressive import SMCAnalyzer

async def main():
    exchange = ccxt.binanceusdm({'enableRateLimit': True})
    exchange.set_sandbox_mode(True)  # CONNECT TO TESTNET
    analyzer = SMCAnalyzer(exchange)
    
    symbols = ['ATOM/USDT']
    
    print("Starting analysis on testnet...")
    for symbol in symbols:
        try:
            res = await analyzer.analyze_symbol(symbol)
            print(f"{symbol}: Score {res['score']}/7 - Signal: {res['signal']}")
        except Exception as e:
            print(f"Error on {symbol}: {e}")
            
    await exchange.close()

if __name__ == '__main__':
    asyncio.run(main())
