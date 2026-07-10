import asyncio
import sqlite3
import ccxt.async_support as ccxt
import aiohttp
from datetime import datetime, timedelta, timezone
import logging
import os
import json

from smart_money_aggressive import SMCAnalyzer, config, Database
import smart_money_aggressive

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('backtest_builder')

class BacktestAnalyzer(SMCAnalyzer):
    def __init__(self, exchange):
        super().__init__(exchange)
        self.mock_ohlcv_15m = []
        self.mock_ohlcv_1h = []
        self.mock_imbalance = 1.0
        
    async def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 100):
        if timeframe == config.SCANNER_TIMEFRAME:
            return self.mock_ohlcv_15m[-limit:] if limit else self.mock_ohlcv_15m
        elif timeframe == config.TREND_TIMEFRAME:
            return self.mock_ohlcv_1h[-limit:] if limit else self.mock_ohlcv_1h
        return []
        
    async def analyze_order_book(self, symbol: str, limit: int = 20) -> float:
        return self.mock_imbalance

async def fetch_historical_klines(exchange, symbol, timeframe, since, limit_total):
    clean_symbol = symbol.split(':')[0].replace('/', '')
    
    # Система кэширования
    cache_dir = "cache"
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)
        
    cache_file = os.path.join(cache_dir, f"klines_{clean_symbol}_{timeframe}_{since}_{limit_total}.json")
    
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r') as f:
                cached_data = json.load(f)
                if len(cached_data) >= limit_total * 0.9: # Если скачано почти все
                    return cached_data
        except Exception as e:
            logger.warning(f"Cache read error for {cache_file}: {e}")

    all_klines = []
    current_since = since
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
                
    # Сохраняем в кэш
    if all_klines:
        try:
            with open(cache_file, 'w') as f:
                json.dump(all_klines, f)
        except Exception as e:
            logger.warning(f"Cache write error for {cache_file}: {e}")
            
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
    # ДИНАМИЧЕСКИЙ СТОП НА ОСНОВЕ ATR (как в реальном боте)
    # FIX #2: Ликвидация считается ПЕРВОЙ, SL не может быть дальше неё
    liq_dist_pct = (100.0 / config.LEVERAGE) * 0.95 
    liq_dist = entry_price * (liq_dist_pct / 100.0)
    # Максимально допустимый SL = 80% дистанции до ликвидации (страховка)
    max_sl_dist = liq_dist * 0.80

    if atr > 0 and entry_price > 0:
        sl_dist_atr = atr * 1.5
        sl_dist_pct = (sl_dist_atr / entry_price) * 100
        sl_dist_pct = max(1.5, min(sl_dist_pct, 3.0))  # Floor & Ceiling
        sl_dist = entry_price * (sl_dist_pct / 100.0)
    else:
        sl_dist = entry_price * (config.STOP_LOSS_PCT / 100.0)
    
    # FIX #2: Ограничиваем SL, чтобы он ВСЕГДА срабатывал до ликвидации
    sl_dist = min(sl_dist, max_sl_dist)
    
    if config.PARTIAL_TP_ENABLED:
        tp1_dist = entry_price * (config.PARTIAL_TP1_PCT / config.LEVERAGE / 100.0)
        tp2_dist = entry_price * (config.PARTIAL_TP2_PCT / config.LEVERAGE / 100.0)
        tp3_dist = entry_price * (config.PARTIAL_TP3_PCT / config.LEVERAGE / 100.0)
    else:
        tp1_dist = entry_price * (config.TAKE_PROFIT_PCT / 100.0)
        tp2_dist = tp1_dist * 2
        tp3_dist = tp1_dist * 3

    if direction == 'LONG':
        sl = entry_price - sl_dist
        liq_price = entry_price - liq_dist
        tp1 = entry_price + tp1_dist
        tp2 = entry_price + tp2_dist
        tp3 = entry_price + tp3_dist
    else:
        sl = entry_price + sl_dist
        liq_price = entry_price + liq_dist
        tp1 = entry_price - tp1_dist
        tp2 = entry_price - tp2_dist
        tp3 = entry_price - tp3_dist

    qty = 1.0
    realized_pnl = 0.0
    trailing_active = False
    trailing_peak = 0.0
    # FIX #1: Отслеживаем текущий уровень TP (только один TP за свечу)
    tp_stage = 0  # 0=ожидаем TP1, 1=ожидаем TP2, 2=ожидаем TP3, 3=все TP закрыты
    # FIX #3: Считаем количество ордеров для корректных комиссий
    num_fills = 1  # Вход = 1 fill
    
    for idx, f_candle in enumerate(future_candles):
        high = f_candle[2]
        low = f_candle[3]
        close = f_candle[4]
        duration_minutes = (idx + 1) * 15
        
        if direction == 'LONG':
            hit_sl = low <= sl
            open_p = f_candle[1]
            
            # FIX #1: Определяем какой TP может сработать на этой свече
            if tp_stage == 0:
                hit_tp = qty > 0 and high >= tp1
            elif tp_stage == 1:
                hit_tp = qty > 0 and high >= tp2
            elif tp_stage == 2:
                hit_tp = qty > 0 and high >= tp3
            else:
                hit_tp = False
            
            # Проверка одновременного хита SL и TP
            if hit_sl and hit_tp:
                current_tp = [tp1, tp2, tp3][tp_stage] if tp_stage < 3 else sl
                if abs(current_tp - open_p) <= abs(sl - open_p):
                    hit_sl = False
                else:
                    hit_tp = False

            if hit_sl:
                realized_pnl += (sl - entry_price) / entry_price * qty
                num_fills += 1
                qty = 0
                break
                
            if low <= liq_price:
                realized_pnl -= (1.0 / config.LEVERAGE) * qty
                num_fills += 1
                qty = 0
                break
                
            # FIX #1: Только ОДИН тейк-профит за свечу
            if hit_tp and config.PARTIAL_TP_ENABLED:
                if tp_stage == 0 and high >= tp1:
                    realized_pnl += (tp1 - entry_price) / entry_price * 0.25
                    qty -= 0.25
                    sl = entry_price  # Breakeven
                    tp_stage = 1
                    num_fills += 1
                    # НЕ проверяем TP2/TP3 на этой же свече!
                elif tp_stage == 1 and high >= tp2:
                    realized_pnl += (tp2 - entry_price) / entry_price * 0.25
                    qty -= 0.25
                    sl = tp1  # Trail to TP1
                    tp_stage = 2
                    num_fills += 1
                elif tp_stage == 2 and high >= tp3:
                    realized_pnl += (tp3 - entry_price) / entry_price * 0.40
                    qty -= 0.40
                    sl = tp2  # Trail to TP2
                    tp_stage = 3  # RUNNER STAGE
                    num_fills += 1
            elif hit_tp and not config.PARTIAL_TP_ENABLED:
                realized_pnl += (tp1 - entry_price) / entry_price * qty
                num_fills += 1
                qty = 0
                break

            # Trailing Stop
            if trailing_active:
                current_floating_pnl_pct = (low - entry_price) / entry_price * config.LEVERAGE * 100.0
                trailing_drawdown = trailing_peak - current_floating_pnl_pct
                
                # Используем обычный трейлинг, или широкий трейлинг для раннера
                active_drawdown_limit = config.RUNNER_TRAILING_DRAWDOWN_PCT if tp_stage == 3 else config.TRAILING_DRAWDOWN_CLOSE_PCT
                
                if trailing_drawdown >= active_drawdown_limit:
                    exit_price = entry_price * (1 + (trailing_peak - active_drawdown_limit) / 100.0 / config.LEVERAGE)
                    realized_pnl += (exit_price - entry_price) / entry_price * qty
                    num_fills += 1
                    qty = 0
                    break

            peak_floating_pnl_pct = (high - entry_price) / entry_price * config.LEVERAGE * 100.0
            if peak_floating_pnl_pct >= config.TRAILING_ACTIVATE_PCT:
                trailing_active = True
                if peak_floating_pnl_pct > trailing_peak:
                    trailing_peak = peak_floating_pnl_pct

            floating_pnl_pct = (close - entry_price) / entry_price * config.LEVERAGE * 100.0
            
            if duration_minutes >= config.POSITION_TIMEOUT_HOURS * 60 and floating_pnl_pct < 10.0:
                realized_pnl += (close - entry_price) / entry_price * qty
                num_fills += 1
                qty = 0
                break
            if duration_minutes >= config.MOMENTUM_EXIT_MINUTES and floating_pnl_pct < config.MOMENTUM_MIN_PROFIT:
                realized_pnl += (close - entry_price) / entry_price * qty
                num_fills += 1
                qty = 0
                break
            if duration_minutes >= config.BAD_POSITION_TIMEOUT_MINUTES and floating_pnl_pct <= config.MAX_POSITION_LOSS_PCT:
                realized_pnl += (close - entry_price) / entry_price * qty
                num_fills += 1
                qty = 0
                break
                
        else: # SHORT
            hit_sl = high >= sl
            open_p = f_candle[1]
            
            # FIX #1: Определяем какой TP может сработать
            if tp_stage == 0:
                hit_tp = qty > 0 and low <= tp1
            elif tp_stage == 1:
                hit_tp = qty > 0 and low <= tp2
            elif tp_stage == 2:
                hit_tp = qty > 0 and low <= tp3
            else:
                hit_tp = False
            
            if hit_sl and hit_tp:
                current_tp = [tp1, tp2, tp3][tp_stage] if tp_stage < 3 else sl
                if abs(current_tp - open_p) <= abs(sl - open_p):
                    hit_sl = False
                else:
                    hit_tp = False

            if hit_sl:
                realized_pnl += (entry_price - sl) / entry_price * qty
                num_fills += 1
                qty = 0
                break
                
            if high >= liq_price:
                realized_pnl -= (1.0 / config.LEVERAGE) * qty
                num_fills += 1
                qty = 0
                break
                
            # FIX #1: Только ОДИН тейк-профит за свечу
            if hit_tp and config.PARTIAL_TP_ENABLED:
                if tp_stage == 0 and low <= tp1:
                    realized_pnl += (entry_price - tp1) / entry_price * 0.25
                    qty -= 0.25
                    sl = entry_price  # Breakeven
                    tp_stage = 1
                    num_fills += 1
                elif tp_stage == 1 and low <= tp2:
                    realized_pnl += (entry_price - tp2) / entry_price * 0.25
                    qty -= 0.25
                    sl = tp1  # Trail to TP1
                    tp_stage = 2
                    num_fills += 1
                elif tp_stage == 2 and low <= tp3:
                    realized_pnl += (entry_price - tp3) / entry_price * 0.40
                    qty -= 0.40
                    sl = tp2  # Trail to TP2
                    tp_stage = 3  # RUNNER STAGE
                    num_fills += 1
            elif hit_tp and not config.PARTIAL_TP_ENABLED:
                realized_pnl += (entry_price - tp1) / entry_price * qty
                num_fills += 1
                qty = 0
                break

            # Trailing Stop SHORT
            if trailing_active:
                current_floating_pnl_pct = (entry_price - high) / entry_price * config.LEVERAGE * 100.0
                trailing_drawdown = trailing_peak - current_floating_pnl_pct
                
                # Используем обычный трейлинг, или широкий трейлинг для раннера
                active_drawdown_limit = config.RUNNER_TRAILING_DRAWDOWN_PCT if tp_stage == 3 else config.TRAILING_DRAWDOWN_CLOSE_PCT
                
                if trailing_drawdown >= active_drawdown_limit:
                    exit_price = entry_price * (1 - (trailing_peak - active_drawdown_limit) / 100.0 / config.LEVERAGE)
                    realized_pnl += (entry_price - exit_price) / entry_price * qty
                    num_fills += 1
                    qty = 0
                    break

            peak_floating_pnl_pct = (entry_price - low) / entry_price * config.LEVERAGE * 100.0
            if peak_floating_pnl_pct >= config.TRAILING_ACTIVATE_PCT:
                trailing_active = True
                if peak_floating_pnl_pct > trailing_peak:
                    trailing_peak = peak_floating_pnl_pct

            floating_pnl_pct = (entry_price - close) / entry_price * config.LEVERAGE * 100.0
            
            if duration_minutes >= config.POSITION_TIMEOUT_HOURS * 60 and floating_pnl_pct < 10.0:
                realized_pnl += (entry_price - close) / entry_price * qty
                num_fills += 1
                qty = 0
                break
            if duration_minutes >= config.MOMENTUM_EXIT_MINUTES and floating_pnl_pct < config.MOMENTUM_MIN_PROFIT:
                realized_pnl += (entry_price - close) / entry_price * qty
                num_fills += 1
                qty = 0
                break
            if duration_minutes >= config.BAD_POSITION_TIMEOUT_MINUTES and floating_pnl_pct <= config.MAX_POSITION_LOSS_PCT:
                realized_pnl += (entry_price - close) / entry_price * qty
                num_fills += 1
                qty = 0
                break

    # Закрытие по таймауту
    if qty > 0:
        last_close = future_candles[-1][4]
        if direction == 'LONG':
            realized_pnl += (last_close - entry_price) / entry_price * qty
        else:
            realized_pnl += (entry_price - last_close) / entry_price * qty
        num_fills += 1

    # FIX #3: Комиссия за КАЖДЫЙ fill (вход + все частичные выходы)
    fee_per_fill = config.TAKER_FEE  # 0.04% за каждый fill
    total_fees = fee_per_fill * num_fills
    
    # Slippage — реалистичный (0.05% на вход + 0.03% на каждый выход)
    slippage = 0.0005 + 0.0003 * (num_fills - 1)
    
    # FIX #4: Funding rate — ~0.01% каждые 8 часов на notional
    funding_rate_per_8h = 0.0001  # 0.01% (средний funding rate на Binance)
    hours_held = duration_minutes / 60.0
    funding_periods = hours_held / 8.0
    funding_cost = funding_rate_per_8h * funding_periods
    
    realized_pnl -= (total_fees + slippage + funding_cost)
    
    # Возвращаем ROE (%) и длительность сделки в минутах
    return realized_pnl * config.LEVERAGE * 100.0, duration_minutes

async def main():
    exchange = ccxt.binanceusdm({
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })
    
    # Используем ОТДЕЛЬНУЮ базу для обучения, чтобы не удалять статистику реального бота!
    db = Database('smart_money_train.db')
    
    # === ОПТИМИЗАЦИЯ БД: Делаем проверку структуры ОДИН раз при запуске ===
    conn = sqlite3.connect('smart_money_train.db')
    cursor = conn.cursor()
    for col, col_type in [
        ('rsi_slope', 'REAL DEFAULT 0'), ('adx_slope', 'REAL DEFAULT 0'),
        ('atr_pct', 'REAL DEFAULT 0'), ('volume_ratio', 'REAL DEFAULT 0'),
        ('ema50_dist_pct', 'REAL DEFAULT 0'), ('bullish_candles_ratio', 'REAL DEFAULT 0'),
        ('price_vs_equilibrium', 'REAL DEFAULT 0'), ('macd_histogram', 'REAL DEFAULT 0'),
    ]:
        try:
            cursor.execute(f'ALTER TABLE ml_training_data ADD COLUMN {col} {col_type}')
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()
    # =====================================================================
    
    logger.info("Fetching markets and top 10 pairs by volume...")
    await exchange.load_markets()
    tickers = await exchange.fetch_tickers()
    
    usdt_pairs = [s for s in exchange.markets.keys() if ':USDT' in s and 'USDC' not in s and 'BUSD' not in s]
    # Синхронизируем с run_backtest.py — берем ТОП монеты по объему + фильтр
    from smart_money_aggressive import TRADFI_SYMBOLS_BLACKLIST
    usdt_pairs_filtered = []
    for s in usdt_pairs:
        base_asset = s.split(':')[0].replace('/USDT', '')
        if base_asset in TRADFI_SYMBOLS_BLACKLIST:
            continue
        vol = float(tickers.get(s, {}).get('quoteVolume', 0))
        if vol >= config.MIN_VOLUME_USDT:
            usdt_pairs_filtered.append(s)
    # 🎯 СПИСОК МОНЕТ ДЛЯ АГРЕССИВНОГО ФЛИППИНГА
    # Мемкоины (высокая волатильность, быстрые импульсы)
    MEMECOINS = ['PEPE', 'WIF', 'BONK', 'FLOKI', 'DOGE', 'SHIB', 'MEME', 'MYRO', 'POPCAT']

    # AI-токены (тренд 2024-2025, сильные движения)
    AI_TOKENS = ['FET', 'RNDR', 'AGIX', 'OCEAN', 'ARKM', 'NFP', 'AI16Z']

    # Gaming / Metaverse (высокая бета, реагируют на новости)
    GAMING = ['IMX', 'GALA', 'SAND', 'MANA', 'AXS', 'ENJ', 'PIXEL']

    # Layer 1/Layer 2 альткоины (средняя волатильность, ликвидные)
    L1_L2 = ['SOL', 'AVAX', 'NEAR', 'APT', 'SUI', 'SEI', 'TIA', 'INJ', 'OP', 'ARB']

    # Новые листинги и хайповые монеты (добавь свои, если знаешь какие сейчас в тренде)
    TRENDING = ['JTO', 'JUP', 'STRK', 'DYM', 'PYTH', 'W', 'ENA', 'ETHFI']

    # Теперь тестируем ВЕСЬ РЫНОК (как в реальном боте), а не только 40 монет.
    # Это займет больше времени, но даст абсолютно точную картину реальности.
    target_pairs = usdt_pairs_filtered

    logger.info(f"✅ Тестируем ВЕСЬ РЫНОК: {len(target_pairs)} монет (всё, что прошло фильтр объема).")
    
    top_pairs = target_pairs
    
    fg_data = await get_fear_and_greed()
    logger.info("Fear and greed data loaded.")

    analyzer = BacktestAnalyzer(exchange)
    
    days = 90  # 90 дней достаточно для статистики, 180 = 2x медленнее без пользы
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    limit_15m = days * 24 * 4
    limit_1h = days * 24
    
    total_signals = 0

    for symbol in top_pairs:
        symbol_signals = []
        logger.info(f"Downloading data for {symbol}...")
        klines_15m = await fetch_historical_klines(exchange, symbol, '15m', since_ms, limit_15m)
        klines_1h = await fetch_historical_klines(exchange, symbol, '1h', since_ms, limit_1h)
        
        if len(klines_15m) < 330 or len(klines_1h) < 200:
            logger.warning(f"Not enough data for {symbol}, skipping.")
            continue
            
        logger.info(f"Processing {symbol} ({len(klines_15m)} 15m candles)")
        
        # Build a timestamp index for 1h candles
        idx_1h = 0
        
        # ОПТИМИЗАЦИЯ: шаг 3 свечи вместо 1 — сигналы не меняются каждые 15 сек
        # Ускорение в 3 раза. Качество датасета не теряется.
        for i in range(200, len(klines_15m) - 100, 3):
            # ОПТИМИЗАЦИЯ: передаём только последние 300 свечей вместо полного среза klines[:i]
            # Это убирает O(n²) рост памяти. Индикаторы считают по 200 свечам — хватит с запасом.
            current_15m = klines_15m[max(0, i-300):i]
            timestamp = klines_15m[i-1][0]
            date_str = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
            
            # Find appropriate 1h candles up to this timestamp
            while idx_1h < len(klines_1h) and klines_1h[idx_1h][0] <= timestamp:
                idx_1h += 1
            
            current_1h = klines_1h[:idx_1h]
            if len(current_1h) < 200:
                continue
                
            analyzer.mock_ohlcv_15m = current_15m
            analyzer.mock_ohlcv_1h = current_1h
            
            # Calculate volume imbalance
            last_candle = current_15m[-1]
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
                future_candles = klines_15m[i:i+100]
                
                pnl_pct, duration_mins = simulate_trade(direction, entry_price, atr, future_candles)
                
                features = result.get('features_dict', {})
                features['side'] = direction
                features['symbol'] = symbol.split(':')[0]
                features['rsi'] = result.get('rsi', 0.0)
                features['adx'] = result.get('adx', 0.0)
                features['order_book_imbalance'] = imbalance
                features['fear_greed_index'] = fg_val
                
                if 'ema200' in result and result['ema200'] > 0:
                    features['ema200_dist_pct'] = ((entry_price - result['ema200']) / result['ema200']) * 100.0
                
                # Вместо коннекта к БД просто добавляем кортеж в список:
                symbol_signals.append((
                    features['symbol'],
                    features['side'],
                    features['rsi'],
                    features['adx'],
                    features.get('ema200_dist_pct', 0.0),
                    features['order_book_imbalance'],
                    features['fear_greed_index'],
                    features.get('rsi_slope', 0.0),
                    features.get('adx_slope', 0.0),
                    features.get('atr_pct', 0.0),
                    features.get('volume_ratio', 0.0),
                    features.get('ema50_dist_pct', 0.0),
                    features.get('bullish_candles_ratio', 0.0),
                    features.get('price_vs_equilibrium', 0.0),
                    features.get('macd_histogram', 0.0),
                    pnl_pct
                ))
                
                if total_signals % 50 == 0:
                    logger.info(f"Found {total_signals} signals...")

        # === СОХРАНЯЕМ ВСЕ СИГНАЛЫ МОНЕТЫ ОДНИМ МАХОМ ===
        if symbol_signals:
            conn = sqlite3.connect('smart_money_train.db')
            cursor = conn.cursor()
            cursor.executemany('''
                INSERT INTO ml_training_data
                    (symbol, side, rsi, adx, ema200_dist_pct, order_book_imbalance, fear_greed_index,
                     rsi_slope, adx_slope, atr_pct, volume_ratio, ema50_dist_pct, 
                     bullish_candles_ratio, price_vs_equilibrium, macd_histogram, result_pnl_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', symbol_signals)
            conn.commit()
            conn.close()
            logger.info(f"💾 Сохранено {len(symbol_signals)} сигналов для {symbol}")

    await exchange.close()
    logger.info(f"Dataset building completed. Total signals collected: {total_signals}")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
    finally:
        input("\nНажмите Enter для выхода...")
