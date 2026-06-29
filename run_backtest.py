import asyncio
import ccxt.async_support as ccxt
import aiohttp
from datetime import datetime, timedelta, timezone
import logging
import joblib
import pandas as pd
import numpy as np

from smart_money_aggressive import SMCAnalyzer, config
from backtest_dataset_builder import fetch_historical_klines, get_fear_and_greed, simulate_trade
import smart_money_aggressive

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger('backtest_runner')

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

async def main():
    logger.info("Загрузка ML модели...")
    try:
        model = joblib.load('trade_model.pkl')
    except Exception as e:
        logger.error(f"Не удалось загрузить trade_model.pkl: {e}")
        return

    exchange = ccxt.binanceusdm({'enableRateLimit': True, 'options': {'defaultType': 'future'}})
    
    logger.info("Загрузка рынков...")
    await exchange.load_markets()
    tickers = await exchange.fetch_tickers()
    
    usdt_pairs = [s for s in exchange.markets.keys() if ':USDT' in s and 'USDC' not in s]
    usdt_pairs.sort(key=lambda s: float(tickers.get(s, {}).get('quoteVolume', 0)), reverse=True)
    
    # 📝 ЗДЕСЬ МОЖНО УКАЗАТЬ СВОИ МОНЕТЫ
    # Удали комментарий ниже и впиши свои монеты, если хочешь тестить конкретные:
    # my_custom_pairs = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'PEPE/USDT']
    # top_pairs = [p + ':USDT' if not p.endswith(':USDT') else p for p in my_custom_pairs]
    
    # Берем ВСЕ пары, НО только с адекватным объемом (как в реальном боте)
    # Иначе бэктестер будет сливать деньги на мертвых щиткоинах с нулевой ликвидностью
    from smart_money_aggressive import TRADFI_SYMBOLS_BLACKLIST
    valid_pairs = []
    for s in usdt_pairs:
        base_asset = s.split(':')[0].replace('/USDT', '')
        if base_asset in TRADFI_SYMBOLS_BLACKLIST:
            continue
        vol = float(tickers.get(s, {}).get('quoteVolume', 0))
        if vol >= config.MIN_VOLUME_USDT:
            valid_pairs.append(s)
            
    # Тестируем ВСЕ монеты, как в реальном боте!
    target_pairs = valid_pairs
    
    top_pairs = target_pairs
    logger.info(f"✅ Бэктест на {len(top_pairs)} волатильных альтах (ВЕСЬ РЫНОК)")
    
    fg_data = await get_fear_and_greed()
    analyzer = BacktestAnalyzer(exchange)
    
    days = 30 # Длительность бэктеста в днях
    offset_days = 0 # Сдвиг в прошлое (0 = последний месяц), 30 = предыдущий месяц, 60 = два месяца назад)
    
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=days + offset_days)).timestamp() * 1000)
    limit_15m = days * 24 * 4
    limit_1h = days * 24
    
    # Симуляция баланса
    initial_balance = 100.0
    current_balance = initial_balance
    peak_balance = initial_balance
    max_drawdown_pct = 0.0
    
    total_trades = 0
    winning_trades = 0
    ml_rejected = 0

    logger.info(f"\n{'='*50}\nСБОР СИГНАЛОВ И ПОСТРОЕНИЕ ХРОНОЛОГИИ (Депозит ${initial_balance:.2f})\n{'='*50}")
    
    all_signals = []

    for symbol in top_pairs:
        logger.info(f"Скачивание {days} дней истории для {symbol}...")
        klines_15m = await fetch_historical_klines(exchange, symbol, '15m', since_ms, limit_15m)
        klines_1h = await fetch_historical_klines(exchange, symbol, '1h', since_ms, limit_1h)
        
        if len(klines_15m) < 330 or len(klines_1h) < 100:
            continue
            
        idx_1h = 0
        skip_until_index = 0
        try:
            for i in range(200, len(klines_15m) - 100, 1):
                if i < skip_until_index:
                    continue
                    
                current_15m = klines_15m[max(0, i - 250):i]
                timestamp = current_15m[-1][0]
                date_str = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
                
                while idx_1h < len(klines_1h) and klines_1h[idx_1h][0] <= timestamp:
                    idx_1h += 1
                
                current_1h = klines_1h[max(0, idx_1h - 250):idx_1h]
                if len(current_1h) < 200:
                    continue
                    
                hour_utc = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).hour
                if config.RESTRICT_HOURS:
                    if config.TRADE_START_HOUR_UTC <= config.TRADE_END_HOUR_UTC:
                        is_allowed = config.TRADE_START_HOUR_UTC <= hour_utc < config.TRADE_END_HOUR_UTC
                    else:
                        is_allowed = hour_utc >= config.TRADE_START_HOUR_UTC or hour_utc < config.TRADE_END_HOUR_UTC
                    if not is_allowed:
                        continue
                        
                analyzer.mock_ohlcv_15m = current_15m
                analyzer.mock_ohlcv_1h = current_1h
                
                last_candle = current_15m[-1]
                taker_buy = float(last_candle[9])
                taker_sell = float(last_candle[5]) - taker_buy
                imbalance = taker_buy / max(taker_sell, 1e-9)
                analyzer.mock_imbalance = imbalance
                
                fg_val = fg_data.get(date_str, 50)
                smart_money_aggressive.FEAR_GREED_VALUE = fg_val
                
                result = await analyzer.analyze_symbol(symbol.split(':')[0])
                
                if result['signal'] and result['score'] >= config.MIN_INDICATORS_SCORE:
                    f_dict = result.get('features_dict', {})
                    direction = result['direction']
                    side_bin = 1 if direction == 'LONG' else 0
                    
                    ordered_vals = [
                        float(f_dict.get('rsi', 0.0)), float(f_dict.get('adx', 0.0)),
                        float(f_dict.get('ema200_dist_pct', 0.0)), float(imbalance),
                        float(fg_val), int(side_bin), float(f_dict.get('rsi_slope', 0.0)),
                        float(f_dict.get('adx_slope', 0.0)), float(f_dict.get('atr_pct', 0.0)),
                        float(f_dict.get('volume_ratio', 1.0)), float(f_dict.get('ema50_dist_pct', 0.0)),
                        float(f_dict.get('bullish_candles_ratio', 0.5)), float(f_dict.get('price_vs_equilibrium', 0.0)),
                        float(f_dict.get('macd_histogram', 0.0)),
                    ]
                    
                    features_df = pd.DataFrame([ordered_vals], columns=model.feature_names_in_)
                    prob = model.predict_proba(features_df)[0][1]
                    
                    if len(all_signals) + ml_rejected < 20:
                        logger.info(f"DEBUG prob={prob:.3f} | {symbol} {direction} | score={result.get('score', 0)}")
                    
                    if prob < 0.40:
                        ml_rejected += 1
                        continue
                        
                    entry_price = last_candle[4]
                    atr = result.get('atr', 0)
                    future_candles = klines_15m[i:i+100]
                        
                    roe_pct, duration_mins = simulate_trade(direction, entry_price, atr, future_candles)
                    
                    exit_timestamp = timestamp + (duration_mins * 60 * 1000)
                    
                    all_signals.append({
                        'timestamp': timestamp,
                        'exit_timestamp': exit_timestamp,
                        'symbol': symbol,
                        'direction': direction,
                        'prob': prob,
                        'score': result.get('score', config.MIN_INDICATORS_SCORE),
                        'roe_pct': roe_pct,
                    })
                    
                    skip_until_index = i + int(duration_mins / 15)
        except Exception as e:
            logger.error(f"Ошибка при обработке {symbol}: {e}")
            continue


    logger.info(f"\n{'='*50}\nХРОНОЛОГИЧЕСКАЯ СИМУЛЯЦИЯ (Всего сигналов: {len(all_signals)})\n{'='*50}")
    
    # Сортируем все сигналы со всех монет строго по времени
    all_signals.sort(key=lambda x: x['timestamp'])
    
    active_positions = []
    
    for signal in all_signals:
        current_time = signal['timestamp']
        
        # 1. Закрываем сделки, которые завершились ДО текущего момента
        closed_positions = [p for p in active_positions if p['exit_timestamp'] <= current_time]
        for p in closed_positions:
            pnl_usdt = p['margin'] * (p['roe_pct'] / 100.0)
            current_balance += pnl_usdt
            active_positions.remove(p)
            
            if current_balance > peak_balance:
                peak_balance = current_balance
            drawdown = (peak_balance - current_balance) / peak_balance * 100.0
            if drawdown > max_drawdown_pct:
                max_drawdown_pct = drawdown
                
            trade_time = datetime.fromtimestamp(p['exit_timestamp'] / 1000, tz=timezone.utc).strftime('%m-%d %H:%M')
            logger.info(f"[ЗАКРЫТО {trade_time}] {p['symbol']} {p['direction']} | PnL: ${pnl_usdt:+.2f} | Баланс: ${current_balance:.2f} | Маржа была: ${p['margin']:.2f}")

        # 2. Проверяем лимит параллельных сделок (как в боте: MAX_CONCURRENT_POSITIONS = 12)
        if len(active_positions) >= config.MAX_CONCURRENT_POSITIONS:
            logger.info(f"[ПРОПУСК] {signal['symbol']} - Достигнут лимит позиций ({len(active_positions)}/{config.MAX_CONCURRENT_POSITIONS})")
            continue
            
        # 3. Считаем свободную маржу
        locked_margin = sum(p['margin'] for p in active_positions)
        virtual_free = current_balance - locked_margin
        
        if virtual_free < config.MIN_SLOT_USDT:
            logger.info(f"[ПРОПУСК] {signal['symbol']} - Нет свободной маржи (Свободно: ${virtual_free:.2f})")
            continue
            
        # 4. Динамический расчет маржи с новым ИИ-масштабированием (до 80%)
        weight = min(max(signal['score'], config.MIN_INDICATORS_SCORE) / 5.0, 1.5)
        prob = signal['prob']
        
        base_amount = current_balance / config.MAX_CONCURRENT_POSITIONS
        
        max_risk = 0.80
        min_risk = config.MIN_SLOT_USDT / base_amount if base_amount > 0 else 0.1
        min_risk = min(min_risk, max_risk)
        
        risk_mult = min_risk + ((prob - 0.40) / 0.60) * (max_risk - min_risk)
        risk_mult = max(min_risk, min(risk_mult, max_risk))
        
        target_margin = base_amount * weight * risk_mult
        
        # Если расчетная маржа меньше минимума, подтягиваем до минимума
        if target_margin < config.MIN_SLOT_USDT:
            target_margin = config.MIN_SLOT_USDT
            
        margin = min(target_margin, virtual_free * 0.90)  # Разрешаем загружать до 90% свободной маржи
        
        if margin < config.MIN_SLOT_USDT:
            continue
            
        # Открываем сделку
        active_positions.append({
            'timestamp': signal['timestamp'],
            'exit_timestamp': signal['exit_timestamp'],
            'symbol': signal['symbol'],
            'direction': signal['direction'],
            'margin': margin,
            'roe_pct': signal['roe_pct']
        })
        
        total_trades += 1
        if signal['roe_pct'] > 0:
            winning_trades += 1
            
        trade_time = datetime.fromtimestamp(current_time / 1000, tz=timezone.utc).strftime('%m-%d %H:%M')
        logger.info(f"[ОТКРЫТО {trade_time}] {signal['symbol']} {signal['direction']} | Уверенность: {signal['prob']*100:.1f}% | Маржа: ${margin:.2f}")

    # Закрываем оставшиеся открытые сделки в конце периода
    for p in active_positions:
        pnl_usdt = p['margin'] * (p['roe_pct'] / 100.0)
        current_balance += pnl_usdt
        if current_balance > peak_balance:
            peak_balance = current_balance
        drawdown = (peak_balance - current_balance) / peak_balance * 100.0
        if drawdown > max_drawdown_pct:
            max_drawdown_pct = drawdown

    await exchange.close()
    
    logger.info(f"\n{'='*50}\nРЕЗУЛЬТАТЫ БЭКТЕСТА ЗА {days} ДНЕЙ\n{'='*50}")
    logger.info(f"Начальный баланс: ${initial_balance:.2f}")
    logger.info(f"Финальный баланс: ${current_balance:.2f}")
    
    if current_balance <= 5.0:
        logger.info("💀 ИТОГ: ЛИКВИДАЦИЯ ДЕПОЗИТА")
    else:
        net_profit = current_balance - initial_balance
        logger.info(f"💰 Чистая прибыль: ${net_profit:+.2f} ({(net_profit/initial_balance)*100:+.1f}%)")
        
    logger.info(f"📉 Максимальная просадка (Drawdown): {max_drawdown_pct:.1f}%")
    logger.info(f"📊 Всего сделок: {total_trades}")
    if total_trades > 0:
        logger.info(f"✅ Успешных сделок: {winning_trades} ({winning_trades/total_trades*100:.1f}%)")
    logger.info(f"🤖 Сделок отсеяно ИИ фильтром: {ml_rejected}")
    
    input("\nНажмите Enter для выхода...")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"Скрипт завершился с ошибкой: {e}")
        input("\nНажмите Enter для выхода...")
