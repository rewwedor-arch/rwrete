import asyncio
import sqlite3
import ccxt.async_support as ccxt
import aiohttp
from datetime import datetime, timedelta, timezone
import logging

from smart_money_aggressive import SMCAnalyzer, config, Database
import smart_money_aggressive

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('backtest_builder')

class BacktestAnalyzer(SMCAnalyzer):
    def __init__(self, exchange):
        super().__init__(exchange)
        self.mock_ohlcv_5m = []
        self.mock_ohlcv_1h = []
        self.mock_imbalance = 1.0
        
    async def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 100):
        if timeframe == config.SCANNER_TIMEFRAME:
            return self.mock_ohlcv_5m[-limit:] if limit else self.mock_ohlcv_5m
        elif timeframe == config.TREND_TIMEFRAME:
            return self.mock_ohlcv_1h[-limit:] if limit else self.mock_ohlcv_1h
        return []
        
    async def analyze_order_book(self, symbol: str, limit: int = 20) -> float:
        return self.mock_imbalance

async def fetch_historical_klines(exchange, symbol, timeframe, since, limit_total):
    all_klines = []
    current_since = since
    clean_symbol = symbol.split(':')[0].replace('/', '')
    url = "https://fapi.binance.com/fapi/v1/klines"
    
    async with aiohttp.ClientSession() as session:
        while len(all_klines) < limit_total:
            limit = min(1000, limit_total - len(all_klines))
            params = {
                'symbol': clean_symbol,
                'interval': timeframe,
                'limit': limit,
                'startTime': current_since
            }
            try:
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        klines = await resp.json()
                        if not klines:
                            break
                        for k in klines:
                            k[0] = int(k[0])
                            for i in range(1, 11):
                                k[i] = float(k[i])
                        all_klines.extend(klines)
                        current_since = klines[-1][0] + 1
                        await asyncio.sleep(0.1)
                    else:
                        logger.error(f"API Error {resp.status} for {symbol}")
                        break
            except Exception as e:
                logger.error(f"Error fetching klines for {symbol}: {e}")
                break
    return all_klines

async def get_fear_and_greed():
    url = "https://api.alternative.me/fng/?limit=40"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            fg_dict = {}
            for item in data['data']:
                date_str = datetime.fromtimestamp(int(item['timestamp']), tz=timezone.utc).strftime('%Y-%m-%d')
                fg_dict[date_str] = int(item['value'])
            return fg_dict

def simulate_trade(direction, entry_price, atr, future_candles):
    max_safe_dist = entry_price * 0.015
    if atr > 0:
        sl_dist = atr * 2.5
        sl_dist = min(sl_dist, max_safe_dist)
        tp1_dist = sl_dist * 1.5
        tp2_dist = sl_dist * 2.8
        tp3_dist = sl_dist * 4.0
    else:
        sl_dist = entry_price * (config.STOP_LOSS_PCT / 100)
        tp1_dist = entry_price * (config.TAKE_PROFIT_PCT / 100)
        tp2_dist = entry_price * (config.TP2_PCT / 100)
        tp3_dist = entry_price * (config.TP3_PCT / 100)

    if direction == 'LONG':
        sl = entry_price - sl_dist
        tp1 = entry_price + tp1_dist
        tp2 = entry_price + tp2_dist
        tp3 = entry_price + tp3_dist
    else:
        sl = entry_price + sl_dist
        tp1 = entry_price - tp1_dist
        tp2 = entry_price - tp2_dist
        tp3 = entry_price - tp3_dist

    qty = 1.0
    realized_pnl = 0.0
    
    for f_candle in future_candles:
        high = f_candle[2]
        low = f_candle[3]
        
        if direction == 'LONG':
            if low <= sl:
                realized_pnl += (sl - entry_price) / entry_price * qty
                qty = 0
                break
            if qty == 1.0 and high >= tp1:
                realized_pnl += (tp1 - entry_price) / entry_price * 0.4
                qty -= 0.4
                sl = entry_price * 1.003
            if qty > 0.5 and high >= tp2:
                realized_pnl += (tp2 - entry_price) / entry_price * 0.3
                qty -= 0.3
                sl = entry_price * 1.009
            if qty > 0.2 and high >= tp3:
                realized_pnl += (tp3 - entry_price) / entry_price * qty
                qty = 0
                break
        else: # SHORT
            if high >= sl:
                realized_pnl += (entry_price - sl) / entry_price * qty
                qty = 0
                break
            if qty == 1.0 and low <= tp1:
                realized_pnl += (entry_price - tp1) / entry_price * 0.4
                qty -= 0.4
                sl = entry_price * 0.997
            if qty > 0.5 and low <= tp2:
                realized_pnl += (entry_price - tp2) / entry_price * 0.3
                qty -= 0.3
                sl = entry_price * 0.991
            if qty > 0.2 and low <= tp3:
                realized_pnl += (entry_price - tp3) / entry_price * qty
                qty = 0
                break

    # If timeout or end of data reached without fully closing
    if qty > 0:
        last_close = future_candles[-1][4]
        if direction == 'LONG':
            realized_pnl += (last_close - entry_price) / entry_price * qty
        else:
            realized_pnl += (entry_price - last_close) / entry_price * qty

    fees = config.TAKER_FEE * 2 * 1.0 # Approximate fees for full position
    realized_pnl -= fees
    
    return realized_pnl * config.LEVERAGE * 100.0

async def main():
    exchange = ccxt.binanceusdm({
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })
    
    db = Database('smart_money.db')
    
    logger.info("Fetching markets and top 10 pairs by volume...")
    await exchange.load_markets()
    tickers = await exchange.fetch_tickers()
    
    usdt_pairs = [s for s in exchange.markets.keys() if ':USDT' in s and 'USDC' not in s and 'BUSD' not in s]
    usdt_pairs.sort(key=lambda s: float(tickers.get(s, {}).get('quoteVolume', 0)), reverse=True)
    top_pairs = usdt_pairs[:10]
    
    logger.info(f"Top pairs: {[s.split(':')[0] for s in top_pairs]}")
    
    fg_data = await get_fear_and_greed()
    logger.info("Fear and greed data loaded.")

    analyzer = BacktestAnalyzer(exchange)
    
    days = 180
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    limit_5m = days * 24 * 12
    limit_1h = days * 24
    
    total_signals = 0

    for symbol in top_pairs:
        logger.info(f"Downloading data for {symbol}...")
        klines_5m = await fetch_historical_klines(exchange, symbol, '5m', since_ms, limit_5m)
        klines_1h = await fetch_historical_klines(exchange, symbol, '1h', since_ms, limit_1h)
        
        if len(klines_5m) < 1000 or len(klines_1h) < 200:
            logger.warning(f"Not enough data for {symbol}, skipping.")
            continue
            
        logger.info(f"Processing {symbol} ({len(klines_5m)} 5m candles)")
        
        # Build a timestamp index for 1h candles
        idx_1h = 0
        
        # Start from index 200 so we have enough 5m data to form indicators
        for i in range(200, len(klines_5m) - 100):
            current_5m = klines_5m[:i]
            timestamp = current_5m[-1][0]
            date_str = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
            
            # Find appropriate 1h candles up to this timestamp
            while idx_1h < len(klines_1h) and klines_1h[idx_1h][0] <= timestamp:
                idx_1h += 1
            
            current_1h = klines_1h[:idx_1h]
            if len(current_1h) < 200:
                continue
                
            analyzer.mock_ohlcv_5m = current_5m
            analyzer.mock_ohlcv_1h = current_1h
            
            # Calculate volume imbalance
            last_candle = current_5m[-1]
            total_volume = float(last_candle[5])
            taker_buy_volume = float(last_candle[9])
            taker_sell_volume = total_volume - taker_buy_volume
            imbalance = taker_buy_volume / max(taker_sell_volume, 1e-9)
            analyzer.mock_imbalance = imbalance
            
            fg_val = fg_data.get(date_str, 50)
            smart_money_aggressive.FEAR_GREED_VALUE = fg_val
            
            result = await analyzer.analyze_symbol(symbol.split(':')[0])
            
            if result['signal']:
                total_signals += 1
                entry_price = last_candle[4]
                direction = result['direction']
                atr = result.get('atr', 0)
                
                # Future 100 candles (approx 8 hours) for simulation
                future_candles = klines_5m[i:i+100]
                
                pnl_pct = simulate_trade(direction, entry_price, atr, future_candles)
                
                features = result.get('features_dict', {})
                features['side'] = direction
                features['symbol'] = symbol.split(':')[0]
                features['rsi'] = result.get('rsi', 0.0)
                features['adx'] = result.get('adx', 0.0)
                features['order_book_imbalance'] = imbalance
                features['fear_greed_index'] = fg_val
                
                if 'ema200' in result and result['ema200'] > 0:
                    features['ema200_dist_pct'] = ((entry_price - result['ema200']) / result['ema200']) * 100.0
                
                # Insert into DB
                conn = sqlite3.connect('smart_money.db')
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO ml_training_data
                        (symbol, side, rsi, adx, ema200_dist_pct,
                         order_book_imbalance, fear_greed_index, result_pnl_pct)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    features['symbol'],
                    features['side'],
                    features['rsi'],
                    features['adx'],
                    features.get('ema200_dist_pct', 0.0),
                    features['order_book_imbalance'],
                    features['fear_greed_index'],
                    pnl_pct
                ))
                conn.commit()
                conn.close()
                
                if total_signals % 50 == 0:
                    logger.info(f"Saved {total_signals} signals to dataset...")

    await exchange.close()
    logger.info(f"Dataset building completed. Total signals collected: {total_signals}")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
    finally:
        input("\nНажмите Enter для выхода...")
