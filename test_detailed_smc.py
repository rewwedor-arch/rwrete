"""Detailed diagnostic: check each of the 7 SMC indicators for all symbols"""
import asyncio
import ccxt.async_support as ccxt
import sys

# Fix encoding
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

async def main():
    exchange = ccxt.binanceusdm({'enableRateLimit': True})
    
    symbols = [
        'BTC/USDT', 'ETH/USDT', 'BNB/USDT', 'SOL/USDT',
        'XRP/USDT', 'ADA/USDT', 'AVAX/USDT', 'DOGE/USDT',
        'DOT/USDT', 'LINK/USDT', 'POL/USDT', 'UNI/USDT',
        'ATOM/USDT', 'LTC/USDT', 'ETC/USDT', 'NEAR/USDT',
        'FIL/USDT', 'AAVE/USDT', 'ARB/USDT', 'OP/USDT',
        'VANA/USDT'
    ]
    
    from smart_money_aggressive import SMCAnalyzer, StrategyConfig
    analyzer = SMCAnalyzer(exchange)
    
    print("=" * 80)
    print("ДЕТАЛЬНАЯ ДИАГНОСТИКА КАЖДОГО ИНДИКАТОРА")
    print("=" * 80)
    
    for symbol in symbols:
        try:
            ohlcv_5m = await analyzer.get_ohlcv(symbol, '5m', limit=100)
            ohlcv_15m = await analyzer.get_ohlcv(symbol, '15m', limit=100)
            
            if not ohlcv_5m or not ohlcv_15m:
                print(f"\n{symbol}: НЕТ ДАННЫХ")
                continue
            
            closes_5m = [c[4] for c in ohlcv_5m]
            highs_5m = [h[2] for h in ohlcv_5m]
            lows_5m = [l[3] for l in ohlcv_5m]
            volumes_5m = [v[5] for v in ohlcv_5m]
            current_price = closes_5m[-1]
            
            print(f"\n{'='*60}")
            print(f"  {symbol} | Цена: {current_price}")
            print(f"{'='*60}")
            
            score = 0
            
            # 1. BOS/CHoCH
            bos = analyzer.detect_bos_choch(ohlcv_15m)
            ok1 = bos in ['BOS_UP', 'CHoCH_BULLISH']
            if ok1: score += 1
            # Debug: show what's happening
            closes_15m = [c[4] for c in ohlcv_15m]
            highs_15m = [h[2] for h in ohlcv_15m]
            if len(ohlcv_15m) >= 20:
                previous_highs = highs_15m[-11:-1]
                highest = max(previous_highs)
                curr = closes_15m[-1]
                print(f"  1. BOS/CHoCH: {bos} {'✅' if ok1 else '❌'}")
                print(f"     Цена закрытия 15m: {curr:.5f} vs Макс предыд. 10 свечей: {highest:.5f}")
                print(f"     Пробой? {curr > highest} (разница: {((curr-highest)/highest*100):.3f}%)")
            
            # 2. FVG
            fvg = analyzer.detect_fvg(ohlcv_5m[-20:])
            ok2 = fvg
            if ok2: score += 1
            print(f"  2. FVG:       {'✅ Есть' if ok2 else '❌ Нет'}")
            # Debug FVG
            fvg_found = False
            for i in range(len(ohlcv_5m[-20:]) - 2):
                candles = ohlcv_5m[-20:]
                c1, c2, c3 = candles[i], candles[i+1], candles[i+2]
                gap = c3[3] - c1[2]  # low3 - high1
                body = c2[2] - c2[3]  # high2 - low2
                if gap > 0 and gap > body * 0.5:
                    fvg_found = True
            if not fvg_found:
                # Show gap sizes
                gaps = []
                for i in range(len(ohlcv_5m[-20:]) - 2):
                    candles = ohlcv_5m[-20:]
                    c1, c2, c3 = candles[i], candles[i+1], candles[i+2]
                    gap = c3[3] - c1[2]
                    gaps.append(gap)
                max_gap = max(gaps) if gaps else 0
                print(f"     Макс. гэп: {max_gap:.6f} (нужен > 0 и > 50% тела свечи)")
            
            # 3. EMA 50
            ema50 = analyzer.calculate_ema(closes_5m, 50)
            ok3 = bool(ema50 and current_price > ema50[-1])
            if ok3: score += 1
            if ema50:
                diff_pct = ((current_price - ema50[-1]) / ema50[-1]) * 100
                print(f"  3. EMA50:     {'✅' if ok3 else '❌'} Цена={current_price:.5f} vs EMA50={ema50[-1]:.5f} ({diff_pct:+.2f}%)")
            else:
                print(f"  3. EMA50:     ❌ Недостаточно данных")
            
            # 4. RSI
            rsi = analyzer.calculate_rsi(closes_5m, 14)
            ok4 = False
            if rsi and len(rsi) >= 2:
                ok4 = 50 <= rsi[-1] <= 80 and rsi[-1] > rsi[-2]
                if ok4: score += 1
                print(f"  4. RSI:       {'✅' if ok4 else '❌'} RSI={rsi[-1]:.1f} (пред: {rsi[-2]:.1f})")
                print(f"     Нужно: 50<=RSI<=80 И RSI растёт. В зоне? {50 <= rsi[-1] <= 80}. Растёт? {rsi[-1] > rsi[-2]}")
            else:
                print(f"  4. RSI:       ❌ Недостаточно данных")
            
            # 5. ADX
            adx = analyzer.calculate_adx(highs_5m, lows_5m, closes_5m, 14)
            ok5 = False
            if adx:
                ok5 = adx[-1] > 20
                if ok5: score += 1
                print(f"  5. ADX:       {'✅' if ok5 else '❌'} ADX={adx[-1]:.1f} (нужно > 20)")
            else:
                print(f"  5. ADX:       ❌ Недостаточно данных")
            
            # 6. MACD
            macd = analyzer.calculate_macd(closes_5m)
            ok6 = macd['histogram'] > 0 and macd['macd'] > macd['signal']
            if ok6: score += 1
            print(f"  6. MACD:      {'✅' if ok6 else '❌'} hist={macd['histogram']:.6f} macd={macd['macd']:.6f} signal={macd['signal']:.6f}")
            
            # 7. Volume
            vol_sma = analyzer.calculate_sma(volumes_5m, 20)
            ok7 = False
            if vol_sma:
                ratio = volumes_5m[-1] / vol_sma[-1] if vol_sma[-1] > 0 else 0
                ok7 = volumes_5m[-1] > vol_sma[-1] * 1.5
                if ok7: score += 1
                print(f"  7. Volume:    {'✅' if ok7 else '❌'} Текущий/SMA20 = {ratio:.2f}x (нужно > 1.5x)")
            else:
                print(f"  7. Volume:    ❌ Недостаточно данных")
            
            emoji = "🟢" if score >= 4 else "🔴"
            print(f"  ────────────")
            print(f"  {emoji} ИТОГО: {score}/7 (минимум для входа: 4)")
            
        except Exception as e:
            print(f"\n{symbol}: ОШИБКА - {e}")
    
    await exchange.close()

if __name__ == '__main__':
    asyncio.run(main())
