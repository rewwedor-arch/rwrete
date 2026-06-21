"""
Smart Money Aggressive Trading Bot
Точная копия стратегии SMART MONEY 1

КРИТИЧЕСКИ ВАЖНЫЕ ПАРАМЕТРЫ:
- DEPOSIT = 50 USDT (стартовый)
- Сумма входа = ДИНАМИЧЕСКАЯ (свободный капитал / доступные слоты)
- Число слотов = floor(equity / MIN_SLOT_USDT), минимум 1, без потолка
- STOP_LOSS = 3.5% (фиксированный от входа)
- Частичные TP / трейлинг / откат от пика — пороги в StrategyConfig
- POSITION_TIMEOUT_HOURS — макс. время в позиции до принудительного закрытия

ИСПРАВЛЕНИЯ v2.2:
1. [v2.1] Корректное подтверждение исполнения ордера (filled/avg_price)
2. [v2.1] Обработка partial fills (< FILL_THRESHOLD → отмена позиции)
3. [v2.1] MAX_CONCURRENT_POSITIONS — жёсткое ограничение числа позиций
4. [v2.1] MAX_SESSION_LOSS_PCT — стоп торгов при достижении дневной просадки
5. [v2.1] Исправлены индексные ошибки в calculate_adx
6. [v2.1] Исправлен SQL INSERT в update_daily_statistics
7. [v2.1] Блокировка дублирующих открытий через _opening_symbols
8. [v2.1] Комиссия учтена в расчёте ожидаемого PnL
9. [v2.1] Проверка спреда перед входом (MAX_SPREAD_PCT)
10. [v2.2] FIX ДВОЙНОЕ TP: asyncio.Lock на каждую позицию предотвращает двойное срабатывание
11. [v2.2] FIX БАЛАНС В ОТЧЁТЕ: daily_report берёт реальный баланс с биржи, не виртуальный
12. [v2.2] FIX РАЗМЕР ПОЗИЦИИ: virtual_equity ограничен реальным балансом биржи
13. [v2.2] FIX -4411: фильтрация TradFi символов (XAU, NVDA, GOOGL итд) при загрузке рынков
"""

import asyncio
import logging
import sqlite3
import os
import sys
import threading

# Fix Unicode encoding for Windows console (cp1251 -> utf-8)
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
elif sys.stdout:
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer)

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field
from pathlib import Path
import json

# Telegram Bot
import telegram
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# CCXT для Binance Futures
import ccxt.async_support as ccxt

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('smart_money_bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# FIX #13: Список TradFi символов которые требуют отдельного соглашения на Binance
# Эти символы вызывают ошибку -4411 "Please sign TradFi-Perps agreement"
TRADFI_SYMBOLS_BLACKLIST = {
    'XAU', 'XAG',           # Металлы
    'AAPL', 'GOOGL', 'GOOG', 'MSFT', 'AMZN', 'META', 'NVDA', 'TSLA',
    'NFLX', 'BABA', 'AMD', 'INTC', 'PYPL', 'SQ', 'SHOP', 'COIN',
    'GME', 'AMC', 'SPY', 'QQQ', 'DJI', 'NDX',  # Акции и индексы
}


# ============================================================================
# КРИТИЧЕСКИ ВАЖНЫЕ ПАРАМЕТРЫ СТРАТЕГИИ
# ============================================================================

@dataclass
class StrategyConfig:
    """Конфигурация стратегии SMART MONEY — FAST COMPOUND MODE"""
    # Финансовые параметры
    DEPOSIT: float = 50.0
    ENTRY_AMOUNT: float = 50.0
    LEVERAGE: int = 10

    # Риск-менеджмент
    STOP_LOSS_PCT: float = 1.5
    TAKE_PROFIT_PCT: float = 2.5
    TAKE_PROFIT: float = 4.0
    TP2_PCT: float = 4.0
    TP3_PCT: float = 7.0

    # Цели
    DAILY_TARGET_MIN: float = 10.0
    DAILY_TARGET_MAX: float = 15.0
    MAX_DAILY_LOSS_PCT: float = 10.0

    MAX_CONCURRENT_POSITIONS: int = 4
    MAX_SESSION_LOSS_PCT: float = 30.0
    FILL_THRESHOLD: float = 0.90
    MAX_SPREAD_PCT: float = 0.05
    TAKER_FEE: float = 0.0004  # 0.04%

    # Режим работы
    DIRECTION: str = "BOTH"
    MIN_VOLUME_USDT: float = 5000000.0   # Снижаем до 5 млн, чтобы бот видел волатильные альткоины

 # Минимальный суточный объем (15 млн $)
    
    # Фильтр по времени (защита от ночного флэта и ложных пробоев)
    RESTRICT_HOURS: bool = True
    # Время по Гринвичу (UTC). 5:00 UTC = 8:00 МСК, 18:00 UTC = 21:00 МСК
    TRADE_START_HOUR_UTC: int = 5
    TRADE_END_HOUR_UTC: int = 18


    # Параметры сигналов
    MIN_INDICATORS_SCORE: int = 5  # Возвращаем жесткий фильтр качества
    TOTAL_INDICATORS: int = 8

    # Таймфреймы
    SCANNER_TIMEFRAME: str = '15m'
    TREND_TIMEFRAME: str = '1h'
    EMA_TIMEFRAME: str = '1h'
    USE_HTF_TREND_FILTER: bool = True
    HTF_EMA_PERIOD: int = 200

    # Алёрты по прибыли (в % ROE с учётом плеча)
    PROFIT_ALERT_10: float = 50.0
    PROFIT_ALERT_15: float = 150.0
    PROFIT_ALERT_40: float = 300.0
    DRAWDOWN_ALERT: float = 12.0

    # Momentum exit
    MOMENTUM_EXIT_MINUTES: int = 45
    MOMENTUM_MIN_PROFIT: float = 1.0
    MOMENTUM_MIN_ADX: float = 23.0

    # Портфельная стратегия
    REINVEST_PROFITS: bool = True
    MIN_SLOT_USDT: float = 5.0

    # Выход по откату от пика (в % ROE)
    MIN_PEAK_PNL_TO_TRACK: float = 12.0
    PEAK_DRAWDOWN_CLOSE_PCT: float = 2.5

    # Трейлинг
    TRAILING_ACTIVATE_PCT: float = 35.0
    TRAILING_DRAWDOWN_CLOSE_PCT: float = 15.0
    TRAILING_DISTANCE_PCT: float = 6.0
    TRAILING_BREAKEVEN_PCT: float = 0.1
    MAX_POSITION_LOSS_PCT: float = -22.0

    # Частичные TP (в % ROE)
    PARTIAL_TP_ENABLED: bool = True
    PARTIAL_TP1_PCT: float = 100.0  # Ждем минимум 100% ROE (10% движения цены)
    PARTIAL_TP2_PCT: float = 200.0  # Удвоение
    PARTIAL_TP3_PCT: float = 400.0  # Оставляем на туземун

    # Время позиции
    POSITION_TIMEOUT_HOURS: float = 8.0
    BAD_POSITION_TIMEOUT_MINUTES: int = 12
    BAD_TRADE_EXIT_MINUTES: int = 6
    SMART_EXIT_ANALYSIS: bool = True
    WEAK_MOMENTUM_EXIT: float = -8.0


config = StrategyConfig()


# ============================================================================
# ПРЕДОХРАНИТЕЛЬ: НОВОСТНОЙ ФОН / НАСТРОЕНИЕ РЫНКА
# ============================================================================
ALLOW_TRADING = True
ALLOW_LONG_ALTS = True   # Фича 2: блокировка ЛОНГОВ альткоинов при Extreme Fear
FEAR_GREED_VALUE = 50    # Фича 3: глобальное значение для ML features


async def check_fear_greed_index(bot: 'SmartMoneyBot'):
    """Фоновая проверка Crypto Fear & Greed Index каждые 30 минут.
    При Extreme Fear (<25):
      - ЛОНГИ по альткоинам ЗАПРЕЩЕНЫ
      - ШОРТЫ по-прежнему РАЗРЕШЕНЫ
    """
    global ALLOW_TRADING, ALLOW_LONG_ALTS, FEAR_GREED_VALUE
    import aiohttp as _aiohttp

    while bot.is_running:
        try:
            async with _aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.alternative.me/fng/?limit=1",
                    timeout=_aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        if data.get("data"):
                            entry = data["data"][0]
                            value = int(entry.get("value", 50))
                            classification = entry.get("value_classification", "Neutral")
                            FEAR_GREED_VALUE = value
                            logger.info(f"Fear & Greed Index: {value} ({classification})")

                            if value < 25 and ALLOW_LONG_ALTS:
                                ALLOW_LONG_ALTS = False
                                msg = (
                                    f"⚠️ EXTREME FEAR!\n"
                                    f"Fear & Greed Index: {value} ({classification})\n"
                                    f"🚫 ЛОНГИ по альткоинам ЗАПРЕЩЕНЫ\n"
                                    f"✅ ШОРТЫ по-прежнему разрешены"
                                )
                                await bot.send_telegram_message(msg)
                                logger.warning(msg)
                            elif value >= 25 and not ALLOW_LONG_ALTS:
                                ALLOW_LONG_ALTS = True
                                msg = (
                                    f"✅ Рынок успокоился.\n"
                                    f"Fear & Greed Index: {value} ({classification})\n"
                                    f"ЛОНГИ по альткоинам снова разрешены."
                                )
                                await bot.send_telegram_message(msg)
                                logger.info(msg)
        except Exception as e:
            logger.error(f"Ошибка проверки Fear & Greed Index: {e}")

        await asyncio.sleep(1800)


# ============================================================================
# БАЗА ДАННЫХ
# ============================================================================

class Database:
    """SQLite база данных для истории сделок и статистики"""

    def __init__(self, db_path: str = 'smart_money.db'):
        self.db_path = db_path
        self.init_db()

    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                stop_loss REAL NOT NULL,
                take_profit REAL,
                amount_usdt REAL NOT NULL,
                leverage INTEGER NOT NULL,
                quantity REAL NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                close_price REAL,
                close_timestamp DATETIME,
                pnl REAL,
                pnl_pct REAL,
                status TEXT DEFAULT 'OPEN',
                smc_score INTEGER,
                bos_info TEXT,
                fvg_detected INTEGER,
                rsi_value REAL,
                adx_value REAL
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                entry_price REAL NOT NULL,
                smc_score INTEGER,
                indicators TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                executed INTEGER DEFAULT 0
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS statistics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE NOT NULL,
                total_trades INTEGER DEFAULT 0,
                profitable_trades INTEGER DEFAULT 0,
                losing_trades INTEGER DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                total_pnl_pct REAL DEFAULT 0,
                best_trade REAL DEFAULT 0,
                worst_trade REAL DEFAULT 0,
                daily_report_sent INTEGER DEFAULT 0
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER,
                alert_type TEXT NOT NULL,
                message TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                sent INTEGER DEFAULT 0
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ml_training_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                rsi REAL,
                adx REAL,
                ema200_dist_pct REAL,
                order_book_imbalance REAL,
                fear_greed_index INTEGER,
                rsi_slope REAL DEFAULT 0,
                adx_slope REAL DEFAULT 0,
                atr_pct REAL DEFAULT 0,
                volume_ratio REAL DEFAULT 0,
                ema50_dist_pct REAL DEFAULT 0,
                bullish_candles_ratio REAL DEFAULT 0,
                price_vs_equilibrium REAL DEFAULT 0,
                macd_histogram REAL DEFAULT 0,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                result_pnl_pct REAL
            )
        ''')

        conn.commit()

        try:
            ref = float(os.getenv('DEPOSIT', '50') or '50')
            if ref > 0:
                cursor.execute(
                    'UPDATE statistics SET total_pnl_pct = (total_pnl * 100.0 / ?) '
                    'WHERE ABS(total_pnl) > 1e-9 OR total_trades > 0',
                    (ref,)
                )
                conn.commit()
        except Exception:
            pass

        conn.close()
        logger.info("База данных инициализирована")

    def add_position(self, symbol: str, side: str, entry_price: float,
                     stop_loss: float, take_profit: float, amount_usdt: float, leverage: int,
                     quantity: float, smc_score: int, bos_info: str,
                     fvg_detected: bool, rsi_value: float, adx_value: float) -> int:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO positions (symbol, side, entry_price, stop_loss, take_profit,
                                   amount_usdt, leverage, quantity, smc_score, bos_info,
                                   fvg_detected, rsi_value, adx_value)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (symbol, side, entry_price, stop_loss, take_profit,
              amount_usdt, leverage, quantity, smc_score, bos_info,
              1 if fvg_detected else 0, rsi_value, adx_value))
        position_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return position_id

    def update_position(self, position_id: int, close_price: float,
                        pnl: float, pnl_pct: float, status: str = 'CLOSED'):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE positions
            SET close_price = ?, close_timestamp = CURRENT_TIMESTAMP,
                pnl = ?, pnl_pct = ?, status = ?
            WHERE id = ?
        ''', (close_price, pnl, pnl_pct, status, position_id))
        conn.commit()
        conn.close()

    def get_open_positions(self) -> List[Dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM positions WHERE status = 'OPEN'")
        positions = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return positions

    def add_signal(self, symbol: str, signal_type: str, entry_price: float,
                   smc_score: int, indicators: dict) -> int:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO signals (symbol, signal_type, entry_price, smc_score, indicators)
            VALUES (?, ?, ?, ?, ?)
        ''', (symbol, signal_type, entry_price, smc_score, json.dumps(indicators)))
        signal_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return signal_id

    def mark_signal_executed(self, signal_id: int):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('UPDATE signals SET executed = 1 WHERE id = ?', (signal_id,))
        conn.commit()
        conn.close()

    def update_daily_statistics(
        self,
        pnl: float,
        pnl_pct: float,
        count_as_trade: bool = True,
        equity_reference: float = 50.0,
    ):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        ref = float(equity_reference) if equity_reference and equity_reference > 0 else 50.0

        cursor.execute('SELECT id FROM statistics WHERE date = ?', (today,))
        row = cursor.fetchone()

        if row:
            if count_as_trade:
                cursor.execute(
                    '''
                    UPDATE statistics
                    SET total_trades = total_trades + 1,
                        profitable_trades = profitable_trades + ?,
                        losing_trades = losing_trades + ?,
                        total_pnl = total_pnl + ?,
                        best_trade = MAX(COALESCE(best_trade, 0), ?),
                        worst_trade = MIN(COALESCE(worst_trade, 0), ?)
                    WHERE date = ?
                    ''',
                    (1 if pnl > 0 else 0, 1 if pnl < 0 else 0, pnl, pnl, pnl, today),
                )
            else:
                cursor.execute(
                    '''
                    UPDATE statistics
                    SET total_pnl = total_pnl + ?,
                        best_trade = MAX(COALESCE(best_trade, 0), ?),
                        worst_trade = MIN(COALESCE(worst_trade, 0), ?)
                    WHERE date = ?
                    ''',
                    (pnl, pnl, pnl, today),
                )
        else:
            if count_as_trade:
                cursor.execute(
                    '''
                    INSERT INTO statistics
                        (date, total_trades, profitable_trades, losing_trades,
                         total_pnl, total_pnl_pct, best_trade, worst_trade)
                    VALUES (?, 1, ?, ?, ?, 0, ?, ?)
                    ''',
                    (today,
                     1 if pnl > 0 else 0,
                     1 if pnl < 0 else 0,
                     pnl,
                     pnl,
                     pnl),
                )
            else:
                cursor.execute(
                    '''
                    INSERT INTO statistics
                        (date, total_trades, profitable_trades, losing_trades,
                         total_pnl, total_pnl_pct, best_trade, worst_trade)
                    VALUES (?, 0, 0, 0, ?, 0, ?, ?)
                    ''',
                    (today, pnl, pnl, pnl),
                )

        cursor.execute('SELECT total_pnl FROM statistics WHERE date = ?', (today,))
        total_row = cursor.fetchone()
        total_day = float(total_row[0]) if total_row and total_row[0] is not None else 0.0
        day_pct = (total_day / ref) * 100.0 if ref > 0 else 0.0
        cursor.execute('UPDATE statistics SET total_pnl_pct = ? WHERE date = ?', (day_pct, today))

        conn.commit()
        conn.close()

    def get_daily_statistics(self, date: str = None) -> Optional[Dict]:
        if not date:
            date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM statistics WHERE date = ?', (date,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def add_alert(self, position_id: int, alert_type: str, message: str):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO alerts (position_id, alert_type, message)
            VALUES (?, ?, ?)
        ''', (position_id, alert_type, message))
        conn.commit()
        conn.close()

    def get_all_statistics(self) -> Dict:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute('''
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as profitable,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing,
                SUM(pnl) as total_pnl,
                AVG(pnl_pct) as avg_pnl_pct,
                MAX(pnl) as best_trade,
                MIN(pnl) as worst_trade
            FROM positions WHERE status = 'CLOSED'
        ''')
        row = cursor.fetchone()

        cursor.execute('''
            SELECT COUNT(*) as total_days,
                   SUM(CASE WHEN total_pnl > 0 THEN 1 ELSE 0 END) as profitable_days,
                   SUM(CASE WHEN total_pnl < 0 THEN 1 ELSE 0 END) as losing_days,
                   AVG(total_pnl_pct) as avg_daily_pct
            FROM statistics
        ''')
        days_row = cursor.fetchone()
        conn.close()

        stats = dict(row) if row else {}
        days_stats = dict(days_row) if days_row else {}
        stats.update(days_stats)
        return stats

    def get_statistics_by_hours(self, hours: int) -> Dict:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as profitable_trades,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
                SUM(pnl) as total_pnl
            FROM positions
            WHERE status = 'CLOSED' AND close_timestamp >= datetime('now', ?)
        ''', (f'-{hours} hours',))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else {}

    def save_ml_features(self, features: Dict) -> int:
        """Save ML training features when a position is opened."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Ensure new columns exist (migration for old DBs)
        for col, col_type in [
            ('rsi_slope', 'REAL DEFAULT 0'), ('adx_slope', 'REAL DEFAULT 0'),
            ('atr_pct', 'REAL DEFAULT 0'), ('volume_ratio', 'REAL DEFAULT 0'),
            ('ema50_dist_pct', 'REAL DEFAULT 0'), ('bullish_candles_ratio', 'REAL DEFAULT 0'),
            ('price_vs_equilibrium', 'REAL DEFAULT 0'), ('macd_histogram', 'REAL DEFAULT 0'),
        ]:
            try:
                cursor.execute(f'ALTER TABLE ml_training_data ADD COLUMN {col} {col_type}')
            except sqlite3.OperationalError:
                pass  # Column already exists
        
        cursor.execute('''
            INSERT INTO ml_training_data
                (symbol, side, rsi, adx, ema200_dist_pct,
                 order_book_imbalance, fear_greed_index,
                 rsi_slope, adx_slope, atr_pct, volume_ratio,
                 ema50_dist_pct, bullish_candles_ratio, price_vs_equilibrium, macd_histogram)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            features.get('symbol', ''),
            features.get('side', ''),
            features.get('rsi', 0.0),
            features.get('adx', 0.0),
            features.get('ema200_dist_pct', 0.0),
            features.get('order_book_imbalance', 1.0),
            features.get('fear_greed_index', 50),
            features.get('rsi_slope', 0.0),
            features.get('adx_slope', 0.0),
            features.get('atr_pct', 0.0),
            features.get('volume_ratio', 0.0),
            features.get('ema50_dist_pct', 0.0),
            features.get('bullish_candles_ratio', 0.0),
            features.get('price_vs_equilibrium', 0.0),
            features.get('macd_histogram', 0.0),
        ))
        ml_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return ml_id

    def update_ml_result(self, ml_id: int, result_pnl_pct: float):
        """Update ML training row with final PnL after position is closed."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE ml_training_data SET result_pnl_pct = ? WHERE id = ?',
            (result_pnl_pct, ml_id)
        )
        conn.commit()
        conn.close()


# ============================================================================
# ИНДИКАТОРЫ И SMC АНАЛИЗ
# ============================================================================

class SMCAnalyzer:
    """Анализ Smart Money Concepts"""

    def __init__(self, exchange: ccxt.binanceusdm):
        self.exchange = exchange

    async def analyze_order_book(self, symbol: str, limit: int = 20) -> float:
        """Analyze L2 Order Book imbalance.
        Returns imbalance_ratio = total_bid_volume / total_ask_volume.
        >1 means buy pressure dominates, <1 means sell pressure dominates.
        Returns 1.0 (neutral) on error.
        """
        try:
            ob = await self.exchange.fetch_order_book(symbol, limit=limit)
            bids = ob.get('bids', [])
            asks = ob.get('asks', [])

            if not bids or not asks:
                return 1.0

            total_bid_vol = sum(entry[1] for entry in bids)
            total_ask_vol = sum(entry[1] for entry in asks)

            if total_ask_vol <= 0:
                return 2.0  # No asks = extreme buy pressure

            imbalance_ratio = total_bid_vol / total_ask_vol
            logger.debug(f"Order Book {symbol}: bid_vol={total_bid_vol:.2f} ask_vol={total_ask_vol:.2f} imbalance={imbalance_ratio:.3f}")
            return imbalance_ratio
        except Exception as e:
            logger.warning(f"Ошибка анализа стакана {symbol}: {e}")
            return 1.0

    def calculate_atr(self, highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
        if len(closes) < period + 1:
            return 0.0
        tr = []
        for i in range(1, len(closes)):
            tr1 = highs[i] - lows[i]
            tr2 = abs(highs[i] - closes[i - 1])
            tr3 = abs(lows[i] - closes[i - 1])
            tr.append(max(tr1, tr2, tr3))
        return sum(tr[-period:]) / period

    async def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> List[List]:
        try:
            if not hasattr(self.exchange, 'markets') or not self.exchange.markets:
                await self.exchange.load_markets()

            if symbol not in self.exchange.markets:
                futures_symbol = f"{symbol}:USDT"
                if futures_symbol in self.exchange.markets:
                    symbol = futures_symbol
                else:
                    logger.warning(f"Символ недоступен на Binance Futures: {symbol}")
                    return []

            ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

            if not ohlcv or len(ohlcv) < 20:
                return []

            return ohlcv

        except Exception as e:
            logger.error(f"Ошибка получения OHLCV для {symbol}: {e}")
            return []

    def calculate_ema(self, prices: List[float], period: int) -> List[float]:
        if len(prices) < period:
            return []

        ema = []
        multiplier = 2 / (period + 1)
        sma = sum(prices[:period]) / period
        ema.append(sma)

        for i in range(period, len(prices)):
            ema_val = (prices[i] - ema[-1]) * multiplier + ema[-1]
            ema.append(ema_val)

        return ema

    def calculate_rsi(self, prices: List[float], period: int = 14) -> List[float]:
        if len(prices) < period + 1:
            return []

        rsi = []
        gains = []
        losses = []

        for i in range(1, len(prices)):
            change = prices[i] - prices[i - 1]
            gains.append(max(0, change))
            losses.append(max(0, -change))

        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        if avg_loss == 0:
            rsi.append(100)
        else:
            rs = avg_gain / avg_loss
            rsi.append(100 - (100 / (1 + rs)))

        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

            if avg_loss == 0:
                rsi.append(100)
            else:
                rs = avg_gain / avg_loss
                rsi.append(100 - (100 / (1 + rs)))

        return rsi

    def calculate_adx(self, high: List[float], low: List[float],
                      close: List[float], period: int = 14) -> List[float]:
        if len(close) < period * 2 + 1:
            return [0.0]

        plus_dm = []
        minus_dm = []
        tr = []

        for i in range(1, len(close)):
            tr_val = max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1])
            )
            tr.append(tr_val)

            up_move = high[i] - high[i - 1]
            down_move = low[i - 1] - low[i]

            plus_dm_val = up_move if (up_move > down_move and up_move > 0) else 0
            minus_dm_val = down_move if (down_move > up_move and down_move > 0) else 0
            plus_dm.append(plus_dm_val)
            minus_dm.append(minus_dm_val)

        if len(tr) < period:
            return [0.0]

        atr = sum(tr[:period])
        plus_dm_smooth = sum(plus_dm[:period])
        minus_dm_smooth = sum(minus_dm[:period])

        if atr <= 0:
            return [0.0]

        plus_di_val = (plus_dm_smooth / atr) * 100
        minus_di_val = (minus_dm_smooth / atr) * 100

        denom = plus_di_val + minus_di_val
        dx_val = (abs(plus_di_val - minus_di_val) / denom * 100) if denom > 0 else 0.0

        dx_list = [dx_val]
        adx_list = []

        for i in range(period, len(tr)):
            atr = atr - (atr / period) + tr[i]
            plus_dm_smooth = plus_dm_smooth - (plus_dm_smooth / period) + plus_dm[i]
            minus_dm_smooth = minus_dm_smooth - (minus_dm_smooth / period) + minus_dm[i]

            if atr <= 0:
                dx_list.append(0.0)
                continue

            pdi = (plus_dm_smooth / atr) * 100
            mdi = (minus_dm_smooth / atr) * 100
            denom2 = pdi + mdi
            dx_v = (abs(pdi - mdi) / denom2 * 100) if denom2 > 0 else 0.0
            dx_list.append(dx_v)

        if len(dx_list) < period:
            return [0.0]

        adx_val = sum(dx_list[:period]) / period
        adx_list.append(adx_val)

        for i in range(period, len(dx_list)):
            adx_val = (adx_val * (period - 1) + dx_list[i]) / period
            adx_list.append(adx_val)

        return adx_list if adx_list else [0.0]

    def calculate_macd(self, prices: List[float], fast: int = 12,
                       slow: int = 26, signal: int = 9) -> Dict:
        if len(prices) < slow + signal:
            return {'macd': 0, 'signal': 0, 'histogram': 0}

        ema_fast = self.calculate_ema(prices, fast)
        ema_slow = self.calculate_ema(prices, slow)

        if len(ema_fast) < signal or len(ema_slow) < signal:
            return {'macd': 0, 'signal': 0, 'histogram': 0}

        min_len = min(len(ema_fast), len(ema_slow))
        ema_fast = ema_fast[-min_len:]
        ema_slow = ema_slow[-min_len:]

        macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
        signal_line = self.calculate_ema(macd_line, signal)

        if not signal_line:
            return {'macd': 0, 'signal': 0, 'histogram': 0}

        histogram = macd_line[-1] - signal_line[-1] if signal_line else 0

        return {
            'macd': macd_line[-1] if macd_line else 0,
            'signal': signal_line[-1] if signal_line else 0,
            'histogram': histogram
        }

    def calculate_sma(self, prices: List[float], period: int) -> List[float]:
        if len(prices) < period:
            return []

        sma = []
        for i in range(period, len(prices) + 1):
            sma.append(sum(prices[i - period:i]) / period)

        return sma

    def detect_bos_choch(self, ohlcv: List[List]) -> str:
        if len(ohlcv) < 20:
            return "NONE"

        closes = [c[4] for c in ohlcv]
        highs = [h[2] for h in ohlcv]
        lows = [l[3] for l in ohlcv]

        previous_highs = highs[-11:-1]
        previous_lows = lows[-11:-1]
        highest = max(previous_highs)
        lowest = min(previous_lows)
        current_price = closes[-1]

        if current_price > highest:
            if closes[-15] > closes[-5]:
                return "CHoCH_BULLISH"
            return "BOS_UP"

        if current_price < lowest:
            if closes[-15] < closes[-5]:
                return "CHoCH_BEARISH"
            return "BOS_DOWN"

        return "NONE"

    def detect_fvg(self, ohlcv: List[List]) -> str:
        """Detect Fair Value Gap — real gap between candle 1 and candle 3.
        
        Bullish FVG: candle1.high < candle3.low (gap up) — price near or inside the gap.
        Bearish FVG: candle1.low > candle3.high (gap down) — price near or inside the gap.
        
        Checks real gap existence. Allows partially filled gaps (mitigation zone).
        """
        if len(ohlcv) < 4:
            return ''

        current_price = ohlcv[-1][4]

        # Scan last 12 candle triplets for FVG
        for i in range(len(ohlcv) - 3, max(0, len(ohlcv) - 15) - 1, -1):
            if i + 2 >= len(ohlcv):
                continue
            c1, c2, c3 = ohlcv[i], ohlcv[i + 1], ohlcv[i + 2]
            high1, low1 = c1[2], c1[3]
            high3, low3 = c3[2], c3[3]

            # Bullish FVG: real gap between candle1 high and candle3 low
            if high1 < low3:
                gap_top = low3
                gap_bottom = high1
                gap_size_pct = (gap_top - gap_bottom) / gap_bottom * 100
                
                # Gap must be at least 0.03% to be meaningful
                if gap_size_pct < 0.03:
                    continue
                
                # Allow price within gap or slightly above (mitigation zone +0.1%)
                if gap_bottom * 0.999 <= current_price <= gap_top * 1.001:
                    return 'BULLISH'

            # Bearish FVG: real gap between candle1 low and candle3 high  
            if low1 > high3:
                gap_top = low1
                gap_bottom = high3
                gap_size_pct = (gap_top - gap_bottom) / gap_bottom * 100
                
                if gap_size_pct < 0.03:
                    continue
                
                if gap_bottom * 0.999 <= current_price <= gap_top * 1.001:
                    return 'BEARISH'

        return ''

    def detect_order_block(self, ohlcv: List[List]) -> str:
        if len(ohlcv) < 10:
            return ''

        current_price = ohlcv[-1][4]

        for i in range(len(ohlcv) - 5, max(0, len(ohlcv) - 15), -1):
            candle = ohlcv[i]
            c_open, c_high, c_low, c_close = candle[1], candle[2], candle[3], candle[4]

            if c_open > c_close:
                future_closes = [ohlcv[j][4] for j in range(i + 1, min(i + 4, len(ohlcv)))]
                if future_closes and max(future_closes) > c_high * 1.002:
                    if c_low * 0.998 <= current_price <= c_high * 1.003:
                        return 'BULLISH'

            if c_close > c_open:
                future_closes = [ohlcv[j][4] for j in range(i + 1, min(i + 4, len(ohlcv)))]
                if future_closes and min(future_closes) < c_low * 0.998:
                    if c_low * 0.997 <= current_price <= c_high * 1.002:
                        return 'BEARISH'

        return ''

    async def analyze_symbol(self, symbol: str) -> Dict[str, Any]:
        result = {
            'symbol': symbol,
            'score': 0,
            'direction': 'LONG',
            'indicators': {},
            'signal': False,
            'bos': 'NONE',
            'fvg': False,
            'rsi': 0,
            'adx': 0,
            'macd': {},
            'ema200': 0,
            'volume_ok': False,
            'order_book_imbalance': 1.0,
            'features_dict': {}
        }

        try:
            ohlcv_5m = await self.get_ohlcv(symbol, config.SCANNER_TIMEFRAME, limit=100)
            ohlcv_1h = await self.get_ohlcv(symbol, config.TREND_TIMEFRAME, limit=300)

            if not ohlcv_5m or not ohlcv_1h:
                return result

            if len(ohlcv_5m) < 50 or len(ohlcv_1h) < 200:
                return result

            closes_5m = [c[4] for c in ohlcv_5m]
            highs_5m = [h[2] for h in ohlcv_5m]
            lows_5m = [l[3] for l in ohlcv_5m]
            volumes_5m = [v[5] for v in ohlcv_5m]
            current_price = closes_5m[-1]

            highs_1h = [h[2] for h in ohlcv_1h]
            lows_1h = [l[3] for l in ohlcv_1h]
            max_1h = max(highs_1h[-24:])
            min_1h = min(lows_1h[-24:])
            equilibrium = (max_1h + min_1h) / 2

            atr_val = self.calculate_atr(highs_5m, lows_5m, closes_5m, 14)
            result['atr'] = atr_val
            
            atr_pct = (atr_val / current_price * 100) if atr_val and current_price > 0 else 0.0
            if atr_pct < 1.0:
                logger.info(f"Пропуск {symbol}: слишком низкая волатильность (ATR = {atr_pct:.2f}%)")
                return result

            long_score = 0
            short_score = 0
            long_ind = {}
            short_ind = {}

            # 1. BOS/CHoCH
            bos = self.detect_bos_choch(ohlcv_5m)
            result['bos'] = bos
            if bos in ['BOS_UP', 'CHoCH_BULLISH']:
                long_score += 1
                long_ind['bos'] = True
            if bos in ['BOS_DOWN', 'CHoCH_BEARISH']:
                short_score += 1
                short_ind['bos'] = True

            # 2. FVG
            fvg = self.detect_fvg(ohlcv_5m[-20:])
            result['fvg'] = bool(fvg)
            if fvg == 'BULLISH':
                long_score += 1
                long_ind['fvg'] = True
            if fvg == 'BEARISH':
                short_score += 1
                short_ind['fvg'] = True

            # 2b. Order Block
            ob = self.detect_order_block(ohlcv_5m[-20:])
            if ob == 'BULLISH':
                long_score += 1
                long_ind['ob'] = True
            if ob == 'BEARISH':
                short_score += 1
                short_ind['ob'] = True

            has_smc_structure = bool(fvg) or bool(ob)

            # 3. EMA 50
            ema50 = self.calculate_ema(closes_5m, 50)
            is_near_ema = False
            if ema50:
                result['ema50'] = ema50[-1]
                ema_dist_pct = abs(current_price - ema50[-1]) / ema50[-1] * 100

                if ema_dist_pct <= 2.0:
                    is_near_ema = True

                if current_price > ema50[-1] and is_near_ema:
                    long_score += 1
                    long_ind['ema50_trend'] = True
                elif current_price < ema50[-1] and is_near_ema:
                    short_score += 1
                    short_ind['ema50_trend'] = True

            # 4. RSI (зона 40-60 + импульс в направлении)
            # 4. RSI (зона трендового импульса)
            rsi = self.calculate_rsi(closes_5m, 14)
            if rsi and len(rsi) >= 2:
                result['rsi'] = rsi[-1]
                # Ищем импульс в зоне тренда, а не во флэте
                if 45 <= rsi[-1] <= 75 and rsi[-1] > rsi[-2]:
                    long_score += 1
                    long_ind['rsi_momentum'] = True
                if 25 <= rsi[-1] <= 55 and rsi[-1] < rsi[-2]:
                    short_score += 1
                    short_ind['rsi_momentum'] = True


            # 5. ADX (минимум 25 — начало тренда)
            adx = self.calculate_adx(highs_5m, lows_5m, closes_5m, 14)
            if adx:
                result['adx'] = adx[-1]
                if adx[-1] < 18:
                    logger.info(f"Пропуск {symbol}: Глубокий флэт (ADX = {adx[-1]:.1f} < 18)")
                    return result

                # ADX >= 22 = начало тренда, даём балл
                if adx[-1] >= 22:
                    if ema50 and current_price > ema50[-1]:
                        long_score += 1
                        long_ind['adx'] = True
                    elif ema50 and current_price < ema50[-1]:
                        short_score += 1
                        short_ind['adx'] = True

            # 6. MACD
            macd = self.calculate_macd(closes_5m)
            result['macd'] = macd
            if macd['histogram'] > 0 and macd['macd'] > macd['signal']:
                long_score += 1
                long_ind['macd'] = True
            if macd['histogram'] < 0 and macd['macd'] < macd['signal']:
                short_score += 1
                short_ind['macd'] = True

            # 7. Объём (сильный спайк на 1 свече ИЛИ 2 свечи с умеренным объёмом)
            vol_sma = self.calculate_sma(volumes_5m, 20)
            if vol_sma and vol_sma[-1] > 0 and len(volumes_5m) >= 3 and len(closes_5m) >= 3:
                vol_ratio_last = volumes_5m[-1] / vol_sma[-1]
                
                # Вариант A: Сильный спайк на последней свече (> 1.8x среднего)
                strong_spike = vol_ratio_last > 1.8
                
                # Вариант B: 2 из 3 свечей с объёмом > 1.3x среднего
                above_avg_count = 0
                bullish_vol_count = 0
                bearish_vol_count = 0
                for k in range(-3, 0):
                    if volumes_5m[k] > vol_sma[-1] * 1.3:
                        above_avg_count += 1
                        if closes_5m[k] > closes_5m[k - 1]:
                            bullish_vol_count += 1
                        elif closes_5m[k] < closes_5m[k - 1]:
                            bearish_vol_count += 1
                
                if strong_spike or above_avg_count >= 2:
                    result['volume_ok'] = True
                    # Определяем направление
                    if strong_spike and closes_5m[-1] > closes_5m[-2]:
                        long_score += 1
                        long_ind['volume_spike'] = True
                    elif strong_spike and closes_5m[-1] < closes_5m[-2]:
                        short_score += 1
                        short_ind['volume_spike'] = True
                    elif bullish_vol_count >= 2:
                        long_score += 1
                        long_ind['volume_spike'] = True
                    elif bearish_vol_count >= 2:
                        short_score += 1
                        short_ind['volume_spike'] = True



            # === HTF TREND FILTER (EMA 200 on 1H) — ЖЁСТКИЙ БЛОК ===
            if config.USE_HTF_TREND_FILTER:
                htf_closes = [c[4] for c in ohlcv_1h]
                htf_ema200 = self.calculate_ema(htf_closes, config.HTF_EMA_PERIOD)
                if htf_ema200:
                    result['ema200'] = htf_ema200[-1]
                    # Цена ниже EMA200 — лонги ПОЛНОСТЬЮ запрещены
                    if current_price < htf_ema200[-1]:
                        long_score = 0
                        long_ind.clear()
                        logger.info(f"{symbol}: Лонг запрещён — цена {current_price:.4f} < EMA200 {htf_ema200[-1]:.4f}")
                    # Цена выше EMA200 — шорты ПОЛНОСТЬЮ запрещены
                    if current_price > htf_ema200[-1]:
                        short_score = 0
                        short_ind.clear()
                        logger.info(f"{symbol}: Шорт запрещён — цена {current_price:.4f} > EMA200 {htf_ema200[-1]:.4f}")

            # === СТРУКТУРНОЕ ПОДТВЕРЖДЕНИЕ: BOS ИЛИ SMC-зона (FVG/OB) ===
            # Нужен хотя бы один SMC элемент: BOS/CHoCH ИЛИ (FVG/OB)
            if long_score >= short_score and long_score >= config.MIN_INDICATORS_SCORE:
                # Для ЛОНГА
                has_smc = (
                    long_ind.get('bos') or
                    long_ind.get('fvg') or long_ind.get('ob')
                )

                if not has_smc:
                    logger.info(f"{symbol}: Лонг {long_score} баллов, но нет SMC подтверждения (BOS/FVG/OB)")
                    result['score'] = long_score
                    result['direction'] = 'LONG'
                    result['indicators'] = long_ind
                    result['signal'] = False
                else:
                    result['score'] = long_score
                    result['direction'] = 'LONG'
                    result['indicators'] = long_ind
                    result['signal'] = True

            elif short_score > long_score and short_score >= config.MIN_INDICATORS_SCORE:
                # Для ШОРТА
                has_smc = (
                    short_ind.get('bos') or
                    short_ind.get('fvg') or short_ind.get('ob')
                )

                if not has_smc:
                    logger.info(f"{symbol}: Шорт {short_score} баллов, но нет SMC подтверждения (BOS/FVG/OB)")
                    result['score'] = short_score
                    result['direction'] = 'SHORT'
                    result['indicators'] = short_ind
                    result['signal'] = False
                else:
                    result['score'] = short_score
                    result['direction'] = 'SHORT'
                    result['indicators'] = short_ind
                    result['signal'] = True
            else:
                result['score'] = max(long_score, short_score)
                result['direction'] = 'LONG' if long_score >= short_score else 'SHORT'
                result['indicators'] = long_ind if long_score >= short_score else short_ind

            # === ФИЧА 1: ORDER BOOK IMBALANCE FILTER ===
            if result['signal']:
                imbalance = await self.analyze_order_book(symbol)
                result['order_book_imbalance'] = imbalance

                # Раньше тут стоял ручной фильтр по стакану. 
                # Теперь мы передаем эти данные в ML-модель, и она сама решает,
                # блокировать сделку или нет, учитывая все остальные факторы!
            # === ФИЧА 3: FEATURES DICT ДЛЯ ML (РАСШИРЕННЫЙ) ===
            ema200_dist_pct = 0.0
            if result.get('ema200') and result['ema200'] > 0:
                ema200_dist_pct = ((current_price - result['ema200']) / result['ema200']) * 100.0

            # Скорость изменения RSI (моментум: растёт/падает)
            rsi_slope = 0.0
            rsi_vals = self.calculate_rsi(closes_5m, 14)
            if rsi_vals and len(rsi_vals) >= 5:
                rsi_slope = rsi_vals[-1] - rsi_vals[-5]  # разница RSI за 5 свечей

            # Скорость изменения ADX
            adx_slope = 0.0
            adx_vals = self.calculate_adx(highs_5m, lows_5m, closes_5m, 14)
            if adx_vals and len(adx_vals) >= 5:
                adx_slope = adx_vals[-1] - adx_vals[-5]

            # Волатильность (ATR в % от цены)
            atr_pct = (atr_val / current_price * 100) if atr_val and current_price > 0 else 0.0

            # Объёмный профиль (текущий объём vs средний)
            vol_sma_vals = self.calculate_sma(volumes_5m, 20)
            volume_ratio = (volumes_5m[-1] / vol_sma_vals[-1]) if vol_sma_vals and vol_sma_vals[-1] > 0 else 1.0

            # Расстояние до EMA50
            ema50_dist_pct = 0.0
            if result.get('ema50') and result['ema50'] > 0:
                ema50_dist_pct = ((current_price - result['ema50']) / result['ema50']) * 100.0

            # Паттерн последних свечей (какой % из последних 10 свечей зелёные)
            recent = closes_5m[-10:] if len(closes_5m) >= 10 else closes_5m
            bullish_count = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i-1])
            bullish_candles_ratio = bullish_count / max(len(recent) - 1, 1)

            # Позиция цены относительно равновесия (equilibrium): >0 = premium, <0 = discount
            price_vs_eq = ((current_price - equilibrium) / equilibrium * 100) if equilibrium > 0 else 0.0

            # MACD histogram
            macd_hist = result.get('macd', {}).get('histogram', 0.0)

            result['features_dict'] = {
                'symbol': symbol,
                'side': result['direction'],
                'rsi': result.get('rsi', 0.0),
                'adx': result.get('adx', 0.0),
                'ema200_dist_pct': ema200_dist_pct,
                'order_book_imbalance': result.get('order_book_imbalance', 1.0),
                'fear_greed_index': FEAR_GREED_VALUE,
                # Новые фичи для предсказания импульса:
                'rsi_slope': rsi_slope,
                'adx_slope': adx_slope,
                'atr_pct': atr_pct,
                'volume_ratio': volume_ratio,
                'ema50_dist_pct': ema50_dist_pct,
                'bullish_candles_ratio': bullish_candles_ratio,
                'price_vs_equilibrium': price_vs_eq,
                'macd_histogram': macd_hist,
            }

        except Exception as e:
            logger.error(f"Ошибка анализа {symbol}: {e}")

        return result


# ============================================================================
# ТОРГОВЫЙ БОТ
# ============================================================================

@dataclass
class Position:
    """Данные о позиции"""
    id: int
    symbol: str
    side: str
    entry_price: float
    stop_loss: float
    take_profit: float
    amount_usdt: float
    leverage: int
    quantity: float
    timestamp: datetime
    remaining_quantity: float = 0.0
    peak_pnl: float = 0.0
    trailing_active: bool = False
    trailing_peak: float = 0.0
    partial_tp1_done: bool = False
    partial_tp2_done: bool = False
    partial_tp3_done: bool = False
    dynamic_sl_level: int = 0
    realized_pnl_usd: float = 0.0
    
    # ✅ ДОБАВЛЯЕМ ПЕРЕМЕННЫЕ ДЛЯ ХРАНЕНИЯ ЦЕН ЦЕЛЕЙ
    tp2_price: float = 0.0
    tp3_price: float = 0.0

    # Фича 3: ID записи в ml_training_data для обновления PnL при закрытии
    ml_data_id: int = 0
    
    # FIX #10: локальный лок для предотвращения двойного срабатывания TP
    _monitor_lock: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self.remaining_quantity == 0.0:
            self.remaining_quantity = self.quantity
        # FIX #10: создаём asyncio.Lock для каждой позиции
        if self._monitor_lock is None:
            self._monitor_lock = asyncio.Lock()



class SmartMoneyBot:
    """Основной класс торгового бота"""

    def __init__(self, api_key: str, api_secret: str, telegram_token: str,
                 telegram_chat_id: str, user_chat_id: str = None, testnet: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self.user_chat_id = user_chat_id
        self.testnet = testnet

        exchange_config = {
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'timeout': 30000,
            'options': {
                'defaultType': 'future',
                'recvWindow': 60000,
                'adjustForTimeDifference': True,
                'keepAlive': True
            }
        }

        self.exchange = ccxt.binanceusdm(exchange_config)

        if testnet:
            logger.info("🔧 Используется Binance Demo Trading (Testnet)")
            self.exchange.enable_demo_trading(True)

        self.exchange.has['fetchCurrencies'] = False

        self.db = Database()
        self.smc_analyzer = SMCAnalyzer(self.exchange)
        self.positions: Dict[int, Position] = {}
        
        # Загрузка ML модели
        self.ml_model = None
        try:
            import joblib
            import os
            
            # Получаем точный, железобетонный путь к папке самого скрипта
            current_dir = os.path.dirname(os.path.abspath(__file__))
            model_path = os.path.join(current_dir, 'trade_model.pkl')
            
            if os.path.exists(model_path):
                self.ml_model = joblib.load(model_path)
                logger.info(f"🧠 ML Модель успешно загружена из: {model_path}")
            else:
                logger.warning(f"Файл модели не найден по пути: {model_path}")
        except Exception as e:
            logger.warning(f"Ошибка при загрузке ML-фильтра: {e}")
        except Exception as e:
            logger.warning(f"Ошибка загрузки ML модели: {e}")

        self._opening_symbols: set = set()
        self._scan_lock = asyncio.Lock()

        self.symbols_to_scan = []

        self.is_running = False
        self.last_scan_time = None
        self.signals_today = 0
        self.max_signals_per_day = 9999

        self.app = None
        self.active_chat_ids = set([str(self.telegram_chat_id)])
        if self.user_chat_id:
            self.active_chat_ids.add(str(self.user_chat_id))

        self.app = None


        # FIX #12: кэш реального баланса биржи (обновляется при каждом расчёте позиции)
        self._cached_real_balance: float = 0.0
        self._balance_cache_time: float = 0.0

    async def send_telegram_message(self, text: str):
        if not getattr(self, 'app', None) or not self.app.bot:
            return
            
        for chat_id in list(self.active_chat_ids):
            try:
                await self.app.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
            except Exception as e:
                logger.warning(f"Не удалось отправить сообщение в чат {chat_id}: {e}")



    async def disconnect(self):
        try:
            if self.exchange:
                await self.exchange.close()
        except Exception as e:
            logger.warning(f"Ошибка при закрытии биржи: {e}")

    async def connect(self):
        try:
            await self.exchange.load_markets()

            # Получаем данные по всем парам, чтобы узнать их суточный объем
            logger.info("Запрашиваем объемы торгов с Binance...")
            tickers = await self.exchange.fetch_tickers()

            # FIX #13 + Фильтр ликвидности
            self.symbols_to_scan = []
            skipped_tradfi = 0
            skipped_low_vol = 0  # Счетчик отсеянных шиткоинов

            for symbol in self.exchange.markets.keys():
                if ':USDT' in symbol:
                    market = self.exchange.markets[symbol]
                    # Читаем скрытый тег биржи. Оставляем ТОЛЬКО чистую крипту
                    underlying_type = market.get('info', {}).get('underlyingType', 'COIN')
                    
                    if underlying_type != 'COIN':
                        skipped_tradfi += 1
                        continue

                    clean_symbol = symbol.split(':')[0]
                    base_asset = clean_symbol.split('/')[0]
                    
                    if base_asset in TRADFI_SYMBOLS_BLACKLIST:
                        skipped_tradfi += 1
                        continue
                        
                    # Фильтр шиткоинов по объему
                    ticker = tickers.get(symbol, {})
                    vol_usdt = float(ticker.get('quoteVolume', 0) or 0)
                    
                    if vol_usdt < config.MIN_VOLUME_USDT:
                        skipped_low_vol += 1
                        continue

                    if clean_symbol not in self.symbols_to_scan:
                        self.symbols_to_scan.append(clean_symbol)

            # Сканируем весь ликвидный рынок без приоритетов
            logger.info(
                f"Markets loaded. Загружено: {len(self.symbols_to_scan)} пар.\n"
                f"Пропущено TradFi: {skipped_tradfi}\n"
                f"Пропущено шиткоинов (<${config.MIN_VOLUME_USDT/1000000:.0f}M): {skipped_low_vol}"
            )


            try:
                balance = await self.exchange.fetch_balance()
                real_free = float(balance.get('USDT', {}).get('free', 0) or 0)
                self._cached_real_balance = real_free
                self._balance_cache_time = asyncio.get_event_loop().time()
                logger.info(f"Подключено к Binance Futures. Баланс USDT: {balance.get('total', {})}")
            except Exception as balance_error:
                err_str = str(balance_error)
                if '-2015' in err_str:
                    logger.error(
                        "❌ API ключ НЕ ИМЕЕТ ПРАВ на USDT-M Futures!\n"
                        "   Включите «Enable Futures» в настройках API Binance."
                    )
                raise balance_error
            return True
        except Exception as e:
            logger.error(f"Ошибка подключения к бирже: {e}")
            return False

    def compute_optimal_slots(self, virtual_equity: float) -> int:
        min_slot = config.MIN_SLOT_USDT
        raw = int(virtual_equity // min_slot)
        return max(1, min(raw, config.MAX_CONCURRENT_POSITIONS))

    def is_daily_loss_limit_reached(self) -> bool:
        try:
            stats = self.db.get_daily_statistics()
            if not stats:
                return False
            daily_pct = float(stats.get('total_pnl_pct') or 0.0)
            return daily_pct <= -abs(config.MAX_DAILY_LOSS_PCT)
        except Exception:
            return False

    def is_session_loss_limit_reached(self) -> bool:
        try:
            stats = self.db.get_daily_statistics()
            if not stats:
                return False
            daily_pct = float(stats.get('total_pnl_pct') or 0.0)
            return daily_pct <= -abs(config.MAX_SESSION_LOSS_PCT)
        except Exception:
            return False

    async def check_spread(self, symbol: str) -> bool:
        try:
            ob = await self.exchange.fetch_order_book(symbol, limit=5)
            best_bid = ob['bids'][0][0] if ob.get('bids') else 0
            best_ask = ob['asks'][0][0] if ob.get('asks') else 0
            if best_bid <= 0 or best_ask <= 0:
                return False
            spread_pct = (best_ask - best_bid) / best_bid * 100
            if spread_pct > config.MAX_SPREAD_PCT:
                logger.info(f"Пропуск {symbol}: спред {spread_pct:.4f}% > {config.MAX_SPREAD_PCT}%")
                return False
            return True
        except Exception as e:
            logger.warning(f"Не удалось проверить спред {symbol}: {e}")
            return True

    async def _get_real_balance(self) -> float:
        """
        FIX #12: получаем реальный баланс биржи с кэшем (не чаще раза в 30 сек).
        Это предотвращает открытие позиций на больше чем есть на счёте.
        """
        import time
        now = asyncio.get_event_loop().time()
        # Обновляем кэш не чаще раза в 30 секунд
        if now - self._balance_cache_time > 30:
            try:
                balance = await self.exchange.fetch_balance()
                self._cached_real_balance = float(balance.get('USDT', {}).get('free', 0) or 0)
                self._balance_cache_time = now
            except Exception as e:
                logger.warning(f"Не удалось обновить кэш баланса: {e}")
        return self._cached_real_balance
    async def calculate_position_size(self, entry_price: float, score: int = 5, risk_multiplier: float = 1.0) -> tuple:
        """
        FIX #12: virtual_equity теперь ограничивается реальным балансом биржи.
        Это предотвращает ситуацию когда бот "думает" что у него $50 виртуально,
        но реальный баланс уже $26 из-за убытков закрытых биржей.
        """
        try:
            # 1. Считаем виртуальный капитал
            stats = self.db.get_all_statistics()
            total_pnl = float(stats.get('total_pnl') or 0.0)

            # 🔥 ИСПРАВЛЕНИЕ: Вытягиваем зафиксированную прибыль по еще открытым позициям (TP1, TP2)
            floating_realized_pnl = sum(max(0, p.realized_pnl_usd) for p in self.positions.values())
            total_pnl += floating_realized_pnl

            # Если реинвест включен, прибавляем профит к депо
            if config.REINVEST_PROFITS and total_pnl > 0:

                virtual_equity = config.DEPOSIT + total_pnl
            else:
                # Если выключен или убыток - отталкиваемся от депо/убытка (но не ниже 50% депо)
                virtual_equity = max(config.DEPOSIT + min(0, total_pnl), config.DEPOSIT * 0.5)

            # FIX #12: РЕАЛЬНЫЙ баланс биржи — жёсткий потолок для виртуального
            real_free = await self._get_real_balance()
            # Виртуальный не может превышать реальный (с учётом маржи в позициях)
            real_total_approx = real_free + sum(p.amount_usdt for p in self.positions.values())
            if virtual_equity > real_total_approx and real_total_approx > 0:
                logger.info(
                    f"POS_SIZE: virtual_equity ${virtual_equity:.2f} > real_total ${real_total_approx:.2f} "
                    f"— ограничиваем виртуальный капитал реальным"
                )
                virtual_equity = real_total_approx

            # 2. Вычитаем маржу в сделках
            bot_locked_margin = sum(p.amount_usdt for p in self.positions.values())
            virtual_free = max(virtual_equity - bot_locked_margin, 0.0)

            # 3. Сколько позиций ещё можно открыть
            current_positions = len(self.positions)
            max_slots = config.MAX_CONCURRENT_POSITIONS
            remaining_slots = max(1, max_slots - current_positions)

            logger.info(
                f"POS_SIZE: VIRTUAL_EQUITY=${virtual_equity:.2f} REAL_FREE=${real_free:.2f} "
                f"PNL=${total_pnl:.2f} LOCKED=${bot_locked_margin:.2f} "
                f"VIRTUAL_FREE=${virtual_free:.2f} SLOTS={current_positions}/{max_slots} "
                f"REMAINING={remaining_slots} score={score} entry={entry_price}"
            )

            if virtual_free < config.MIN_SLOT_USDT:
                logger.warning(
                    f"POS_SIZE: virtual_free(${virtual_free:.2f}) < MIN_SLOT(${config.MIN_SLOT_USDT}) — ПРОПУСК"
                )
                return 0, 0, 0

            # === ПРОЦЕНТ НА ПОЗИЦИЮ ===
            weight = min(max(score, config.MIN_INDICATORS_SCORE) / 5.0, 1.5)
            
            # Строгое равное деление на слоты (рекомендация для надежного риск-менеджмента)
            base_amount = virtual_equity / config.MAX_CONCURRENT_POSITIONS
            
            amount_usdt = base_amount * weight * risk_multiplier
            amount_usdt = min(amount_usdt, virtual_free)

            if amount_usdt < config.MIN_SLOT_USDT:
                if virtual_free >= config.MIN_SLOT_USDT:
                    amount_usdt = config.MIN_SLOT_USDT
                else:
                    logger.warning(f"POS_SIZE: amount_usdt=${amount_usdt:.2f} < MIN_SLOT — ПРОПУСК")
                    return 0, 0, 0

            # FIX #12: финальная проверка — amount не может превышать реальный свободный баланс
            if real_free > 0 and amount_usdt > real_free:
                logger.warning(
                    f"POS_SIZE: amount_usdt=${amount_usdt:.2f} > real_free=${real_free:.2f} "
                    f"— ограничиваем реальным балансом"
                )
                amount_usdt = real_free

            if amount_usdt < config.MIN_SLOT_USDT:
                return 0, 0, 0

            pct_of_balance = (amount_usdt / virtual_equity * 100) if virtual_equity > 0 else 0
            logger.info(
                f"POS_SIZE RESULT: amount_usdt=${amount_usdt:.2f} "
                f"({pct_of_balance:.1f}% от вирт. баланса) "
                f"notional=${amount_usdt * config.LEVERAGE:.2f}"
            )

            quantity = amount_usdt * config.LEVERAGE / entry_price
            return quantity, amount_usdt, amount_usdt * config.LEVERAGE

        except Exception as e:
            logger.error(f"Ошибка в calculate_position_size: {e}", exc_info=True)
            return 0, 0, 0

    async def open_position(self, symbol: str, entry_price: float,
                            smc_result: Dict) -> Optional['Position']:
        global ALLOW_TRADING

        if not ALLOW_TRADING:
            logger.info(f"Сигнал {symbol} — торговля приостановлена (Fear & Greed)")
            return None

        # Фича 2: При Extreme Fear блокируем ЛОНГИ по альткоинам, ШОРТЫ разрешены
        direction = smc_result.get('direction', 'LONG')
        is_btc = symbol.upper().startswith('BTC')
        if not ALLOW_LONG_ALTS and direction == 'LONG' and not is_btc:
            logger.info(
                f"Сигнал ЛОНГ {symbol} заблокирован — Extreme Fear "
                f"(F&G={FEAR_GREED_VALUE}), только ШОРТЫ разрешены для альткоинов"
            )
            return None

        if self.is_session_loss_limit_reached():
            logger.warning(f"MAX_SESSION_LOSS достигнут — все сделки заблокированы")
            return None

        if self.is_daily_loss_limit_reached():
            logger.warning(f"Дневной лимит убытков достигнут — новые сделки заблокированы")
            return None

        if len(self.positions) >= config.MAX_CONCURRENT_POSITIONS:
            logger.info(f"Лимит позиций {config.MAX_CONCURRENT_POSITIONS} достигнут — пропуск {symbol}")
            return None

        if symbol in self._opening_symbols:
            logger.info(f"Открытие {symbol} уже в процессе — пропуск")
            return None

        if not await self.check_spread(symbol):
            return None

        self._opening_symbols.add(symbol)

        try:
            direction = smc_result.get('direction', 'LONG')
            
            # --- ФИЧА: Адаптивный Риск-менеджмент на основе ИИ ---
            ai_prob_str = ""
            risk_mult = 1.0
            if self.ml_model is not None:
                try:
                    import numpy as np
                    
                    f_dict = smc_result.get('features_dict', {})
                    side_bin = 1 if direction == 'LONG' else 0
                    
                    ordered_vals = [
                        float(f_dict.get('rsi', 0.0)),
                        float(f_dict.get('adx', 0.0)),
                        float(f_dict.get('ema200_dist_pct', 0.0)),
                        float(f_dict.get('order_book_imbalance', 1.0)),
                        float(f_dict.get('fear_greed_index', 50.0)),
                        int(side_bin),
                        float(f_dict.get('rsi_slope', 0.0)),
                        float(f_dict.get('adx_slope', 0.0)),
                        float(f_dict.get('atr_pct', 0.0)),
                        float(f_dict.get('volume_ratio', 1.0)),
                        float(f_dict.get('ema50_dist_pct', 0.0)),
                        float(f_dict.get('bullish_candles_ratio', 0.5)),
                        float(f_dict.get('price_vs_equilibrium', 0.0)),
                        float(f_dict.get('macd_histogram', 0.0)),
                    ]
                    
                    # Скармливаем ИИ голый двумерный массив
                    prob = self.ml_model.predict_proba(np.array([ordered_vals]))[0][1]
                    
                    if prob < 0.55:
                        msg = f"🧠 AI Фильтр: Сигнал #{symbol} отменен (Вероятность {prob*100:.1f}% < 50%)"
                        logger.info(msg)
                        # Можно раскомментировать, чтобы бот писал об отмене в ТГ:
                        # await self.send_telegram_message(msg)
                        return None
                    
                    ai_prob_str = f"🧠 AI Уверенность: {prob*100:.1f}%\n"
                    
                    if prob >= 0.80:
                        logger.info(f"{symbol}: ИИ уверен ({prob*100:.1f}%) -> повышаем риск x1.5")
                        risk_mult = 1.5
                        
                except Exception as e:
                    logger.warning(f"Ошибка предсказания ML: {e}")
                    # ВАЖНО: Отправляем ошибку в ТГ, чтобы больше не гадать, почему ИИ молчит!
                    await self.send_telegram_message(f"⚠️ Ошибка ML: {e}")
            # ------------------------



            market_info = self.exchange.market(symbol)
            min_notional = float(market_info.get('limits', {}).get('cost', {}).get('min', 5))

            quantity, margin, actual_amount = await self.calculate_position_size(
                entry_price, score=smc_result['score'], risk_multiplier=risk_mult
            )

            if quantity == 0:
                logger.warning(f"Недостаточно средств для {symbol}")
                return None

            quantity = float(self.exchange.amount_to_precision(symbol, quantity))

            notional = quantity * entry_price
            if notional < min_notional:
                logger.warning(f"Номинал ${notional:.2f} < минимума ${min_notional} для {symbol}. Увеличиваем qty.")
                quantity = float(self.exchange.amount_to_precision(symbol, min_notional / entry_price * 1.05))
                notional = quantity * entry_price
                if notional < min_notional:
                    logger.warning(f"Не удалось подобрать qty для {symbol} — пропуск")
                    return None

            dir_emoji = '🟢 LONG' if direction == 'LONG' else '🔴 SHORT'
            logger.info(f"Открытие {dir_emoji} {symbol}: qty={quantity}, notional=${notional:.2f}, expected_entry={entry_price}")

            try:
                await self.exchange.set_margin_mode('isolated', symbol)
            except Exception as e:
                logger.warning(f"Не удалось установить isolated маржу для {symbol}: {e}")


            # --- ИСПРАВЛЕННЫЙ БЛОК УСТАНОВКИ ПЛЕЧА И ОТКРЫТИЯ ОРДЕРА ---
            actual_leverage = config.LEVERAGE
            order = None
            
            async def try_open_with_leverage(target_lev):
                nonlocal quantity, actual_leverage
                try:
                    await self.exchange.set_leverage(target_lev, symbol)
                    actual_leverage = target_lev
                    
                    # Пересчитываем количество монет под новое плечо, чтобы сохранить маржу
                    quantity = margin * actual_leverage / entry_price
                    quantity = float(self.exchange.amount_to_precision(symbol, quantity))
                    
                    if direction == 'SHORT':
                        return await self.exchange.create_market_sell_order(symbol, quantity)
                    else:
                        return await self.exchange.create_market_buy_order(symbol, quantity)
                except Exception as e:
                    return e

            res = await try_open_with_leverage(config.LEVERAGE)
            
            if isinstance(res, Exception):
                err_str = str(res)
                if '-2015' in err_str or 'Invalid API-key' in err_str or 'permissions' in err_str:
                    raise res 
                    
                if 'leverage' in err_str.lower() or '-2027' in err_str or 'Exceeded' in err_str:
                    for fallback_lev in [50, 25, 20, 10, 5]:
                        logger.info(f"Снижаем плечо до {fallback_lev}x для {symbol} из-за лимитов биржи...")
                        fallback_res = await try_open_with_leverage(fallback_lev)
                        if not isinstance(fallback_res, Exception):
                            order = fallback_res
                            break
                    else:
                        raise res 
                else:
                    raise res
            else:
                order = res
            # --- КОНЕЦ БЛОКА ---


            actual_entry = float(order.get('average') or order.get('price') or entry_price)
            actual_qty = float(order.get('filled') or quantity)

            if actual_qty < quantity * config.FILL_THRESHOLD:
                logger.warning(
                    f"Partial fill {symbol}: заполнено {actual_qty:.4f} из {quantity:.4f} "
                    f"({actual_qty / quantity * 100:.1f}%) — позиция не открывается"
                )
                if actual_qty > 0:
                    try:
                        close_side = 'BUY' if direction == 'SHORT' else 'SELL'
                        await self.exchange.create_market_order(
                            symbol, close_side, actual_qty,
                            params={'reduceOnly': True}
                        )
                    except Exception as ex:
                        logger.error(f"Не удалось отменить partial fill {symbol}: {ex}")
                return None

            estimated_fee = actual_qty * actual_entry * config.TAKER_FEE * 2
            actual_margin = (actual_qty * actual_entry) / actual_leverage

            logger.info(
                f"ORDER FILLED: {symbol} {direction} | "
                f"expected_entry={entry_price:.6f} real_entry={actual_entry:.6f} | "
                f"filled={actual_qty:.4f} | margin=${actual_margin:.2f} | "
                f"est_fee=${estimated_fee:.4f}"
            )

            # FIX #12: сбрасываем кэш баланса после открытия позиции
            self._balance_cache_time = 0

            # 🧠 ДИНАМИЧЕСКИЙ СТОП НА ОСНОВЕ ATR (защита от сквизов мемкоинов)
            atr_val = smc_result.get('atr', 0)
            if atr_val > 0 and actual_entry > 0:
                # Базовый стоп = 1.5 ATR (золотой стандарт для импульсных альтов)
                sl_dist_atr = atr_val * 1.5
                sl_dist_pct = (sl_dist_atr / actual_entry) * 100
                
                # 🛡 Жёсткие границы (Floor & Ceiling):
                # Минимум 1.5% (чтобы на BTC/ETH не ставить стоп 0.1%)
                # Максимум 4.5% (чтобы на шиткоинах стоп не улетал на 15% и не ждал ликвидации)
                sl_dist_pct = max(1.5, min(sl_dist_pct, 4.5))
                
                sl_dist = actual_entry * (sl_dist_pct / 100)
                logger.info(f"{symbol}: 🧠 Dynamic SL = {sl_dist_pct:.2f}% (ATR={atr_val:.6f})")
            else:
                # Фоллбек: если ATR не рассчитался, используем конфиг
                sl_dist = actual_entry * (config.STOP_LOSS_PCT / 100)
                logger.warning(f"{symbol}: ATR недоступен, используем фиксированный SL {config.STOP_LOSS_PCT}%")

            # Твои TP (TP1_PCT и т.д.) в конфиге заданы в ROE. 
            # Чтобы перевести ROE в реальное движение цены, делим на плечо.
            tp1_dist = actual_entry * (config.PARTIAL_TP1_PCT / actual_leverage / 100)
            tp2_dist = actual_entry * (config.PARTIAL_TP2_PCT / actual_leverage / 100)
            tp3_dist = actual_entry * (config.PARTIAL_TP3_PCT / actual_leverage / 100)

            if direction == 'SHORT':
                actual_sl = float(self.exchange.price_to_precision(symbol, actual_entry + sl_dist))
                tp1_price = float(self.exchange.price_to_precision(symbol, actual_entry - tp1_dist))
                tp2_price = float(self.exchange.price_to_precision(symbol, actual_entry - tp2_dist))
                tp3_price = float(self.exchange.price_to_precision(symbol, actual_entry - tp3_dist))
                close_side = 'BUY'
            else:
                actual_sl = float(self.exchange.price_to_precision(symbol, actual_entry - sl_dist))
                tp1_price = float(self.exchange.price_to_precision(symbol, actual_entry + tp1_dist))
                tp2_price = float(self.exchange.price_to_precision(symbol, actual_entry + tp2_dist))
                tp3_price = float(self.exchange.price_to_precision(symbol, actual_entry + tp3_dist))
                close_side = 'SELL'

            # На биржу в качестве финального Тейка выставляем самую дальнюю точку — TP3
            actual_tp = tp3_price


            try:
                await self.exchange.create_order(
                    symbol=symbol, type='STOP_MARKET', side=close_side,
                    amount=actual_qty,
                    params={'stopPrice': actual_sl, 'reduceOnly': True}
                )
            except Exception as e:
                logger.warning(f"⚠️ SL не выставлен для {symbol}: {e}")

            try:
                await self.exchange.create_order(
                    symbol=symbol, type='TAKE_PROFIT_MARKET', side=close_side,
                    amount=actual_qty,
                    params={'stopPrice': actual_tp, 'reduceOnly': True}
                )
            except Exception as e:
                logger.warning(f"⚠️ TP не выставлен для {symbol}: {e}")

            position_id = self.db.add_position(
                symbol=symbol,
                side=direction,
                entry_price=actual_entry,
                stop_loss=actual_sl,
                take_profit=tp1_price,
                amount_usdt=actual_margin,
                leverage=actual_leverage,
                quantity=actual_qty,
                smc_score=smc_result['score'],
                bos_info=smc_result['bos'],
                fvg_detected=smc_result['fvg'],
                rsi_value=smc_result['rsi'],
                adx_value=smc_result['adx']
            )

            position = Position(
                id=position_id,
                symbol=symbol,
                side=direction,
                entry_price=actual_entry,
                stop_loss=actual_sl,
                take_profit=tp1_price,
                tp2_price=tp2_price,  # ✅ Передаем TP2
                tp3_price=tp3_price,  # ✅ Передаем TP3
                amount_usdt=actual_margin,
                leverage=actual_leverage,
                quantity=actual_qty,
                remaining_quantity=actual_qty,
                timestamp=datetime.now(timezone.utc),
                realized_pnl_usd=-(actual_qty * actual_entry * config.TAKER_FEE),
            )


            self.positions[position_id] = position

            # Фича 3: Сохраняем ML features для будущего обучения нейросети
            features_dict = smc_result.get('features_dict', {})
            if features_dict:
                features_dict['side'] = direction  # Обновляем направление на реальное
                try:
                    ml_id = self.db.save_ml_features(features_dict)
                    position.ml_data_id = ml_id
                    logger.info(f"ML features saved: id={ml_id} for {symbol} {direction}")
                except Exception as ml_err:
                    logger.warning(f"Не удалось сохранить ML features для {symbol}: {ml_err}")

            score = smc_result['score']
            quality = "★★★ СИЛЬНЫЙ" if score >= 6 else ("★★☆ ХОРОШИЙ" if score >= 5 else "★☆☆ СРЕДНИЙ")
            rr = (tp3_dist / sl_dist) if sl_dist > 0 else 0

            message = (
                f"✅ ПОЗИЦИЯ ОТКРЫТА\n"
                f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
                f"{dir_emoji} | #{symbol.replace('/', '')}\n"
                f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n\n"
                f"{ai_prob_str}"
                f"🛒 Вход:   {actual_entry:.5f}\n"
                f"💰 Вложено: ${actual_margin:.2f}\n"
                f"🎯 TP1:    {tp1_price:.5f}\n"
                f"🔴 Стоп:   {actual_sl:.5f}\n"
                f"📐 RR: 1:{rr:.1f} | Плечо: x{actual_leverage}\n"
                f"💸 Ожид. комиссия: ~${estimated_fee:.3f}\n\n"
                f"📊 {quality} ({score}/{config.TOTAL_INDICATORS})\n"
                f"  Индикаторы: {', '.join(smc_result['indicators'].keys())}\n"
                f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
                f"SMART MONEY 1 BOT"
            )

            await self.send_telegram_message(message)

            signal_id = self.db.add_signal(
                symbol=symbol,
                signal_type=direction,
                entry_price=actual_entry,
                smc_score=smc_result['score'],
                indicators=smc_result['indicators']
            )
            self.db.mark_signal_executed(signal_id)
            self.signals_today += 1

            logger.info(f"Позиция открыта: {direction} {symbol} @ {actual_entry}")
            return position

        except Exception as e:
            logger.error(f"Ошибка открытия позиции {symbol}: {e}")
            await self.send_telegram_message(f"❌ Ошибка открытия {symbol}: {e}")
            return None
        finally:
            self._opening_symbols.discard(symbol)

    async def _handle_already_closed_position(self, position_id: int, position: 'Position', margin: float, reason: str = None):
        symbol = position.symbol
        logger.info(f"Позиция {symbol} закрыта на Binance (по SL/TP или вручную)")
        
        exit_price = 0.0
        try:
            close_side = 'sell' if position.side == 'LONG' else 'buy'
            
            # Ищем конкретно исполненный ордер (SL/TP), который закрыл позицию
            closed_orders = await self.exchange.fetch_closed_orders(symbol, limit=10)
            
            valid_orders = [
                o for o in closed_orders 
                if o.get('side', '').lower() == close_side 
                and o.get('timestamp', 0) >= position.timestamp.timestamp() * 1000
                and float(o.get('filled', 0)) > 0
            ]
            
            if valid_orders:
                # Надежная сортировка по времени: самый свежий ордер точно будет последним [-1]
                valid_orders.sort(key=lambda x: x.get('timestamp', 0))
                last_order = valid_orders[-1]
                exit_price = float(last_order.get('average') or last_order.get('price') or 0.0)
            else:
                # Резервный запрос через трейды
                since_ms = int(position.timestamp.timestamp() * 1000) + 1000
                trades = await self.exchange.fetch_my_trades(symbol, since=since_ms, limit=10)
                if trades:
                    closing_trades = [t for t in trades if t.get('side', '').lower() == close_side]
                    if closing_trades:
                        closing_trades.sort(key=lambda x: x.get('timestamp', 0))
                        exit_price = float(closing_trades[-1].get('price', 0.0))
                    else:
                        trades.sort(key=lambda x: x.get('timestamp', 0))
                        exit_price = float(trades[-1].get('price', 0.0))
        except Exception as e:
            logger.warning(f'Ошибка получения истории исполнения: {e}')

        # Финальная защита от нулевой цены
        if exit_price <= 0:
            logger.error(f"КРИТИЧЕСКАЯ ОШИБКА: Не удалось найти реальную цену выхода для {symbol}.")
            exit_price = position.entry_price


        # 1. Считаем PnL ТОЛЬКО для оставшегося хвостика по точной цене выхода
        qty = position.remaining_quantity
        if position.side == 'SHORT':
            final_leg_pnl = (position.entry_price - exit_price) * qty
        else:
            final_leg_pnl = (exit_price - position.entry_price) * qty
            
        fee = qty * exit_price * config.TAKER_FEE
        final_leg_pnl -= fee

        # 2. Складываем накопленный профит (от TP1, TP2) и профит финального хвостика
        total_pnl = position.realized_pnl_usd + final_leg_pnl
        pnl_pct = (total_pnl / margin) * 100 if margin > 0 else 0.0

        # Обновляем базу данных
        self.db.update_position(position_id, exit_price, total_pnl, pnl_pct)
        self.db.update_daily_statistics(
            total_pnl, pnl_pct,
            count_as_trade=True,
            equity_reference=config.DEPOSIT
        )
        # Фича 3: Обновляем ML запись итоговым PnL
        if position.ml_data_id:
            self.db.update_ml_result(position.ml_data_id, pnl_pct)
        self._balance_cache_time = 0

        # ФОРМАТИРОВАНИЕ ЕДИНОГО СООБЩЕНИЯ (без дублей)
        emoji = "✅" if total_pnl >= 0 else "❌"
        duration = datetime.now(timezone.utc) - position.timestamp
        hours = int(duration.total_seconds() // 3600)
        minutes = int((duration.total_seconds() % 3600) // 60)
        
        if reason:
            parts = reason.split('\n', 1)
            title = parts[0]
            details = parts[1] + "\n" if len(parts) > 1 else ""
            header = f"{title} | #{symbol.replace('/USDT', '')}\n{details}"
        else:
            header = f"{emoji} БИРЖА ЗАКРЫЛА ПОЗИЦИЮ (SL/TP) | #{symbol.replace('/USDT', '')}\n"

        await self.send_telegram_message(
            f"{header}"
            f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
            f"🛒 Вход:  {position.entry_price:.5f}\n"
            f"🏁 Выход: {exit_price:.5f}\n"
            f"💰 Вложено: ${margin:.2f}\n"
            f"📈 REAL PnL: {'+' if total_pnl >= 0 else ''}${total_pnl:.2f}\n"
            f"📊 REAL ROE: {'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}%\n"
            f"⏱ Время: {hours}ч {minutes}мин\n"
            f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
            f"SMART MONEY 1 BOT"
        )

        if position_id in self.positions:
            del self.positions[position_id]
        return True


    async def close_position(self, position_id: int, emergency: bool = False, reason: str = None) -> bool:
        try:
            if position_id not in self.positions:
                logger.warning(f"Позиция {position_id} не найдена")
                return False

            position = self.positions[position_id]
            symbol = position.symbol
            margin = position.amount_usdt
            qty_close = position.remaining_quantity if position.remaining_quantity > 0 else position.quantity

            if qty_close <= 0:
                logger.warning(f"Нечего закрывать по позиции {position_id}")
                return False

            try:
                exchange_positions = await self.exchange.fetch_positions([symbol])
                real_position_exists = False
                real_contracts = 0.0

                for ep in exchange_positions:
                    contracts = float(ep.get('contracts', 0) or 0)
                    if abs(contracts) > 0:
                        real_position_exists = True
                        real_contracts = abs(contracts)
                        break

                if not real_position_exists:
                    # ДОБАВЛЕН reason
                    return await self._handle_already_closed_position(position_id, position, margin, reason)

                qty_close = min(qty_close, real_contracts)

            except Exception as sync_error:
                logger.warning(f"Ошибка синхронизации позиции: {sync_error}")

            try:
                order_params = {'reduceOnly': True, 'positionSide': 'BOTH'}

                if position.side == 'SHORT':
                    order = await self.exchange.create_market_buy_order(symbol, qty_close, params=order_params)
                else:
                    order = await self.exchange.create_market_sell_order(symbol, qty_close, params=order_params)

            except Exception as close_error:
                error_text = str(close_error)
                if '-2022' in error_text or 'ReduceOnly Order is rejected' in error_text:
                    logger.warning(f"Позиция {symbol} уже закрыта (ошибка: {error_text})")
                    # ДОБАВЛЕН reason
                    return await self._handle_already_closed_position(position_id, position, margin, reason)
                raise

            try:
                await self.exchange.cancel_all_orders(symbol)
            except Exception as cancel_e:
                logger.warning(f"Не удалось отменить ордера для {symbol}: {cancel_e}")

            # Истинное исправление: дожидаемся реальной цены исполнения
            exit_price = float(order.get('average') or 0.0)
            
            if exit_price <= 0 and 'id' in order:
                try:
                    await asyncio.sleep(0.5)
                    fetched_order = await self.exchange.fetch_order(order['id'], symbol)
                    exit_price = float(fetched_order.get('average') or fetched_order.get('price') or 0.0)
                except Exception as e:
                    logger.warning(f"Не удалось получить детали ордера {order['id']}: {e}")

            if exit_price <= 0:
                try:
                    closed = await self.exchange.fetch_closed_orders(symbol, limit=5)
                    if closed:
                        exit_price = float(closed[-1].get('average') or closed[-1].get('price') or 0.0)
                except Exception:
                    pass
            
            # --- ДОБАВЬ ВОТ ЭТИ ТРИ СТРОЧКИ СЮДА ---
            if exit_price <= 0:
                logger.error(f"exit_price=0 для {symbol}. Ставим цену входа, чтобы избежать бага.")
                exit_price = position.entry_price
            # --------------------------------------
            
            exit_price = float(exit_price)

            
            exit_price = float(exit_price)


            if position.side == 'SHORT':
                leg_pnl = (position.entry_price - exit_price) * qty_close
            else:
                leg_pnl = (exit_price - position.entry_price) * qty_close

            fee = qty_close * exit_price * config.TAKER_FEE
            leg_pnl -= fee

            total_pnl = position.realized_pnl_usd + leg_pnl
            margin = position.amount_usdt
            pnl_pct = (total_pnl / margin) * 100 if margin > 0 else 0.0

            self.db.update_position(position_id, exit_price, total_pnl, pnl_pct)
            self.db.update_daily_statistics(
                total_pnl, pnl_pct,
                count_as_trade=True,
                equity_reference=config.DEPOSIT
            )

            # Фича 3: Обновляем ML запись итоговым PnL
            if position.ml_data_id:
                self.db.update_ml_result(position.ml_data_id, pnl_pct)

            self._balance_cache_time = 0

            del self.positions[position_id]

            duration = datetime.now(timezone.utc) - position.timestamp
            emoji = "✅" if total_pnl >= 0 else "❌"
            result_text = "ПРИБЫЛЬ" if total_pnl >= 0 else "УБЫТОК"
            hours = int(duration.total_seconds() // 3600)
            minutes = int((duration.total_seconds() % 3600) // 60)

            # ОБНОВЛЕННАЯ СБОРКА СООБЩЕНИЯ (чтобы не было двойных текстов)
            if emergency:
                header = f"🚨 ЭКСТРЕННОЕ ЗАКРЫТИЕ | #{position.symbol.replace('/', '')}\n"
            elif reason:
                parts = reason.split('\n', 1)
                title = parts[0]
                details = parts[1] + "\n" if len(parts) > 1 else ""
                header = f"{title} | #{position.symbol.replace('/', '')}\n{details}"
            else:
                header = f"{emoji} ПОЗИЦИЯ ЗАКРЫТА ({result_text}) | #{position.symbol.replace('/', '')}\n"

            message = (
                f"{header}"
                f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
                f"🛒 Вход:  {position.entry_price:.5f}\n"
                f"🏁 Выход: {exit_price:.5f}\n"
                f"💰 Вложено: ${margin:.2f}\n"
                f"📈 REAL PnL: {'+' if total_pnl >= 0 else ''}${total_pnl:.2f} | "
                f"REAL ROE: {'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}%\n"
                f"💸 Комиссия выхода: ~${fee:.4f}\n"
                f"💼 Итог с баланса: ${margin + total_pnl:.2f}\n"
                f"⏱ Время в сделке: {hours}ч {minutes}мин\n"
                f"🔧 Плечо: x{position.leverage}\n"
                f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
                f"SMART MONEY 1 BOT"
            )

            await self.send_telegram_message(message)
            logger.info(f"Позиция закрыта: {position.symbol}, PnL: ${total_pnl:.2f}")
            return True

        except Exception as e:
            logger.error(f"Ошибка закрытия позиции {position_id}: {e}")
            await self.send_telegram_message(f"❌ Ошибка закрытия: {e}")
            return False


    async def close_all_positions(self, emergency: bool = False):
        position_ids = list(self.positions.keys())
        for pid in position_ids:
            await self.close_position(pid, emergency)

    async def close_partial_position(self, position: 'Position', qty_to_close: float, current_price: float) -> bool:
        """Частичное закрытие — только накапливает realized_pnl_usd в памяти."""
        try:
            qty_to_close = float(self.exchange.amount_to_precision(position.symbol, qty_to_close))
            if qty_to_close <= 0:
                return False

            if position.side == 'SHORT':
                order = await self.exchange.create_market_buy_order(
                    position.symbol, qty_to_close, params={'reduceOnly': True}
                )
            else:
                order = await self.exchange.create_market_sell_order(
                    position.symbol, qty_to_close, params={'reduceOnly': True}
                )

            executed_price = float(order.get('average') or order.get('price') or current_price)

            if position.side == 'SHORT':
                chunk_pnl = (position.entry_price - executed_price) * qty_to_close
            else:
                chunk_pnl = (executed_price - position.entry_price) * qty_to_close

            fee = qty_to_close * executed_price * config.TAKER_FEE
            chunk_pnl -= fee

            position.realized_pnl_usd += chunk_pnl
            position.remaining_quantity = max(0.0, position.remaining_quantity - qty_to_close)

            
            # Корректируем заблокированную маржу пропорционально закрытому объему


            logger.info(

                f"Частичное закрытие {position.symbol}: "
                f"{qty_to_close} @ {executed_price:.5f}, "
                f"chunk_pnl=${chunk_pnl:.4f}, fee=${fee:.4f}, "
                f"накоплено realized=${position.realized_pnl_usd:.4f}, "
                f"остаток {position.remaining_quantity:.4f}"
            )
            return True

        except Exception as e:
            error_text = str(e)
            logger.error(f"Ошибка частичного закрытия {position.symbol}: {error_text}")
            
            # ЗАЩИТА ОТ "ПОЗИЦИЙ-ПРИЗРАКОВ"
            if '-2022' in error_text or 'ReduceOnly' in error_text or 'insufficient margin' in error_text.lower():
                logger.warning(f"👻 {position.symbol} была закрыта биржей за нашей спиной! Финализируем...")
                # Ждем финального закрытия (без create_task), чтобы корректно рассчитать PnL
                await self.close_position(position.id, reason="👻 ПОЗИЦИЯ ЗАКРЫТА БИРЖЕЙ\nБот выявил закрытие по SL/TP.")

            
            return False


    async def _update_exchange_sl(self, position: 'Position', new_sl_price: float):
        try:
            new_sl_price = float(self.exchange.price_to_precision(position.symbol, new_sl_price))
            qty_rounded = float(self.exchange.amount_to_precision(position.symbol, position.remaining_quantity))
            if qty_rounded <= 0:
                return

            close_side = 'BUY' if position.side == 'SHORT' else 'SELL'
            
            # Делаем 3 попытки, если биржа глючит (Testnet этим славится)
            for attempt in range(3):
                try:
                    open_orders = await self.exchange.fetch_open_orders(position.symbol)
                    for ord in open_orders:
                        if 'stop' in ord.get('type', '').lower():
                            await self.exchange.cancel_order(ord['id'], position.symbol)
                            
                    await self.exchange.create_order(
                        symbol=position.symbol,
                        type='STOP_MARKET',
                        side=close_side,
                        amount=qty_rounded,
                        params={'stopPrice': new_sl_price, 'reduceOnly': True}
                    )
                    position.stop_loss = new_sl_price
                    logger.info(f"SL обновлён {position.symbol}: → {new_sl_price}")
                    return  # Успех, выходим из функции
                except Exception as e:
                    logger.warning(f"Попытка {attempt+1}/3: Ошибка переноса SL для {position.symbol}: {e}")
                    await asyncio.sleep(2)  # Ждем 2 секунды перед повтором
            
            # Если дошли сюда, значит все 3 попытки провалились
            error_msg = (
                f"⚠️ КРИТИЧЕСКАЯ ОШИБКА БИРЖИ\n"
                f"Не удалось перенести SL в безубыток для #{position.symbol.replace('/USDT', '')} "
                f"после 3 попыток!\nПозиция под угрозой старого стопа."
            )
            logger.error(error_msg)
            await self.send_telegram_message(error_msg)

        except Exception as e:
            logger.error(f"Глобальная ошибка в _update_exchange_sl для {position.symbol}: {e}")


    async def _update_exchange_tp(self, position: 'Position', new_tp_price: float):
        """
        Обновляет TAKE_PROFIT_MARKET ордер на бирже с новым объёмом.
        Необходимо после частичных закрытий (TP1, TP2, TP3),
        чтобы биржа не отклонила ордер из-за флага reduceOnly.
        """
        try:
            new_tp_price = float(self.exchange.price_to_precision(position.symbol, new_tp_price))
            qty_rounded = float(self.exchange.amount_to_precision(position.symbol, position.remaining_quantity))
            
            if qty_rounded <= 0:
                return

            # Отменяем старые TP ордера
            try:
                open_orders = await self.exchange.fetch_open_orders(position.symbol)
                for ord in open_orders:
                    order_type = ord.get('type', '').lower()
                    # Отменяем только TP, не трогая SL
                    if 'take_profit' in order_type or order_type == 'take_profit_market':
                        await self.exchange.cancel_order(ord['id'], position.symbol)
            except Exception as e:
                logger.warning(f"Ошибка отмены старого TP: {e}")

            # Выставляем новый TP на оставшийся объём
            close_side = 'BUY' if position.side == 'SHORT' else 'SELL'
            await self.exchange.create_order(
                symbol=position.symbol,
                type='TAKE_PROFIT_MARKET',
                side=close_side,
                amount=qty_rounded,
                params={'stopPrice': new_tp_price, 'reduceOnly': True}
            )
            position.take_profit = new_tp_price
            logger.info(f"TP обновлён {position.symbol}: → {new_tp_price} (qty: {qty_rounded})")
        except Exception as e:
            logger.error(f"Ошибка обновления TP {position.symbol}: {e}")


    def calculate_position_roe(self, position: 'Position', current_price: float) -> float:
        if position.side == 'SHORT':
            price_change_pct = (
                (position.entry_price - current_price)
                / max(position.entry_price, 1e-9)
            ) * 100.0
        else:
            price_change_pct = (
                (current_price - position.entry_price)
                / max(position.entry_price, 1e-9)
            ) * 100.0
        return price_change_pct * position.leverage

    def get_position_age_minutes(self, position: 'Position') -> float:
        return (datetime.now(timezone.utc) - position.timestamp).total_seconds() / 60

    async def monitor_positions(self):
        """
        FIX #10: каждая позиция обрабатывается под своим asyncio.Lock.
        Это предотвращает двойное срабатывание TP1/TP2/TP3 когда monitor_positions
        вызывается каждые 2 секунды и успевает запуститься дважды до завершения первого.
        """
        for position_id, position in list(self.positions.items()):
            # FIX #10: пропускаем позицию если предыдущий мониторинг ещё не завершился
            if position._monitor_lock is None:
                position._monitor_lock = asyncio.Lock()
            if position._monitor_lock.locked():
                logger.debug(f"Пропуск мониторинга {position.symbol} — предыдущий цикл ещё активен")
                continue

            async with position._monitor_lock:
                # ✅ ЗАЩИТА: Если позиция уже полностью закрыта, пропускаем итерацию
                if position.remaining_quantity <= 0:
                    logger.info(f"Позиция {position.symbol} уже имеет 0 остатка, удаляем из памяти.")
                    if position_id in self.positions:
                        del self.positions[position_id]
                    continue
                
                try:
                    ticker = await self.exchange.fetch_ticker(position.symbol)

                    current_price = ticker['last']

                    # Единый расчет для мониторинга: с учетом уже зафиксированного PnL и остатка
                    if position.side == 'SHORT':
                        unrealized_pnl = (position.entry_price - current_price) * position.remaining_quantity
                    else:
                        unrealized_pnl = (current_price - position.entry_price) * position.remaining_quantity

                    # Вычитаем примерную комиссию на закрытие остатка
                    fee_estimate = position.remaining_quantity * current_price * config.TAKER_FEE
                    unrealized_pnl -= fee_estimate

                    pnl_usd = position.realized_pnl_usd + unrealized_pnl
                    
                    # ROE считается от ИЗНАЧАЛЬНОЙ маржи, чтобы цифры сходились с отчетами о закрытии
                    pnl_pct = (pnl_usd / position.amount_usdt) * 100 if position.amount_usdt > 0 else 0.0

                    if isinstance(pnl_pct, (int, float)) and pnl_pct > position.peak_pnl:
                        position.peak_pnl = pnl_pct


                    pair = position.symbol.replace('/USDT', '')

                    # Программный STOP LOSS — передаём reason в close_position
                    # НЕ отправляем отдельное сообщение здесь (close_position отправит единое)
                    is_sl_hit = False
                    if position.side == 'LONG' and current_price <= position.stop_loss:
                        is_sl_hit = True
                    elif position.side == 'SHORT' and current_price >= position.stop_loss:
                        is_sl_hit = True

                    if is_sl_hit:
                        sl_reason = (
                            f"❌ STOP LOSS\n"
                            f"Убыток по ROE: {pnl_pct:+.1f}%\n"
                            f"Текущий PnL: {'+' if pnl_usd >= 0 else ''}${pnl_usd:.2f}"
                        )
                        await self.close_position(position_id, reason=sl_reason)
                        continue

                    # Trailing Stop
                    #if pnl_pct >= config.TRAILING_ACTIVATE_PCT and not position.trailing_active:
                        #position.trailing_active = True
                        #position.trailing_peak = pnl_pct
                        #logger.info(f"Трейлинг активирован для {position.symbol} на {pnl_pct:.1f}%")

                    if position.trailing_active:
                        if pnl_pct > position.trailing_peak:
                            position.trailing_peak = pnl_pct

                        trailing_drawdown = position.trailing_peak - pnl_pct
                        if trailing_drawdown >= config.TRAILING_DRAWDOWN_CLOSE_PCT:
                            trail_reason = (
                                f"🛡 TRAILING STOP\n"
                                f"Пик: +{position.trailing_peak:.1f}%\n"
                                f"Откат: {trailing_drawdown:.1f}%\n"
                                f"Фактический ROE: {pnl_pct:+.1f}%\n"
                                f"PnL: {'+' if pnl_usd >= 0 else ''}${pnl_usd:.2f}"
                            )
                            await self.close_position(position_id, reason=trail_reason)
                            continue

                    # FIX #10: проверяем флаги ДО вызова close_partial — флаг ставим сразу
                    # TP1 (ATR)
                    is_tp1_hit = False
                    if position.side == 'LONG' and current_price >= position.take_profit:
                        is_tp1_hit = True
                    elif position.side == 'SHORT' and current_price <= position.take_profit:
                        is_tp1_hit = True

                    # TP1 (ATR)
                    # TP1 (ATR)
                    if is_tp1_hit and not position.partial_tp1_done:
                        position.partial_tp1_done = True
                        is_success = await self.close_partial_position(position, position.quantity * 0.40, current_price)
                        if is_success:
                            # Перенос в +0.1% ROE для покрытия комиссии
                            be_pct = 0.1 / position.leverage / 100 
                            new_sl = (position.entry_price * (1 - be_pct)
                                      if position.side == 'SHORT'
                                      else position.entry_price * (1 + be_pct))
                            await self._update_exchange_sl(position, new_sl)
                            await self._update_exchange_tp(position, position.tp2_price)
                            
                            await self.send_telegram_message(
                                f"💰 ЧАСТИЧНАЯ ФИКСАЦИЯ TP1 (ATR) | {pair}\n"
                                f"Достигнута цель по волатильности! Закрыто 40% | SL → безубыток"
                            )




                    # TP2
                    # TP2
                    # TP2
                    is_tp2_hit = False
                    if position.side == 'LONG' and current_price >= position.tp2_price:
                        is_tp2_hit = True
                    elif position.side == 'SHORT' and current_price <= position.tp2_price:
                        is_tp2_hit = True

                    if is_tp2_hit and not position.partial_tp2_done:
                        position.partial_tp2_done = True
                        is_success = await self.close_partial_position(position, position.remaining_quantity * 0.50, current_price)
                        if is_success:
                            # Перенос стопа в +10% ROE
                            sl_pct_tp2 = 10.0 / position.leverage / 100
                            new_sl = (position.entry_price * (1 - sl_pct_tp2)
                                      if position.side == 'SHORT'
                                      else position.entry_price * (1 + sl_pct_tp2))
                            await self._update_exchange_sl(position, new_sl)
                            await self._update_exchange_tp(position, position.tp3_price)
                            
                            await self.send_telegram_message(
                                f"🚀 TP2 | {pair}\nДостигнута вторая цель! Закрыто ещё 30% | SL → +40% ROE"
                            )





                    # TP3
                    # TP3
                    # TP3
                    is_tp3_hit = False
                    if position.side == 'LONG' and current_price >= position.tp3_price:
                        is_tp3_hit = True
                    elif position.side == 'SHORT' and current_price <= position.tp3_price:
                        is_tp3_hit = True

                    if is_tp3_hit and not position.partial_tp3_done:
                        position.partial_tp3_done = True 

                        runner_qty = position.quantity * 0.10
                        runner_qty = min(runner_qty, position.remaining_quantity)
                        close_qty = position.remaining_quantity - runner_qty
                        
                        if close_qty > 0:
                            is_success = await self.close_partial_position(position, close_qty, current_price)
                            if is_success:
                                # Перенос стопа в +30% ROE (защита раннера)
                                sl_pct_tp3 = 30.0 / position.leverage / 100
                                new_sl = (position.entry_price * (1 - sl_pct_tp3)
                                          if position.side == 'SHORT'
                                          else position.entry_price * (1 + sl_pct_tp3))
                                await self._update_exchange_sl(position, new_sl)
                                await self._update_exchange_tp(position, position.tp3_price)
                                
                                await self.send_telegram_message(
                                    f"💎 TP3 +{config.PARTIAL_TP3_PCT:.0f}% ROE | {pair}\n"
                                    f"🎯 Оставлен раннер 10% с трейлинг-стопом"
                                )
                                position.trailing_active = True
                                position.trailing_peak = pnl_pct

                    # === ТАЙМАУТ И МЕДЛЕННЫЙ МИНУС ===
                    await self.check_position_timeout(position)

                except Exception as e:
                    logger.error(f"Ошибка мониторинга {position_id}: {e}")

    async def check_position_timeout(self, position: 'Position'):
        try:
            now = datetime.now(timezone.utc)
            duration_minutes = (now - position.timestamp).total_seconds() / 60

            # Получаем PnL один раз
            ticker = await self.exchange.fetch_ticker(position.symbol)
            current_price = ticker['last']
            pnl_pct = self.calculate_position_roe(position, current_price)

            # 1. Если сделка висит слишком долго (всю ночь) и не в огромном плюсе — рубим
            if duration_minutes > config.POSITION_TIMEOUT_HOURS * 60:
                if pnl_pct < 10.0:  # Не закрываем, только если мы в хорошем профите
                    reason = (
                        f"⏳ ТАЙМАУТ ПОЗИЦИИ\n"
                        f"Висит больше {config.POSITION_TIMEOUT_HOURS} часов\nPNL: {pnl_pct:.2f}%"
                    )
                    await self.close_position(position.id, reason=reason)
                    return

            # 2. Momentum exit (раннее закрытие медленных сделок, если они не дали прибыль)
            if duration_minutes >= config.MOMENTUM_EXIT_MINUTES:
                if pnl_pct < config.MOMENTUM_MIN_PROFIT:
                    logger.info(
                        f"MOMENTUM EXIT: {position.symbol} | "
                        f"{duration_minutes:.1f}m | PNL={pnl_pct:.2f}%"
                    )
                    momentum_reason = (
                        f"⚠️ MOMENTUM EXIT (Нет импульса)\n"
                        f"Возраст: {duration_minutes:.0f} мин\nPNL: {pnl_pct:.2f}%"
                    )
                    await self.close_position(position.id, reason=momentum_reason)

        except Exception as e:
            logger.error(f"Ошибка timeout-проверки: {e}")

    async def scan_market(self):
        if not self.is_running:
            return

        if self._scan_lock.locked():
            logger.debug("Предыдущее сканирование ещё не завершено — пропуск")
            return


        async with self._scan_lock:
            if self.signals_today >= self.max_signals_per_day:
                logger.info(f"Лимит сигналов исчерпан: {self.signals_today}")
                return
                
            # --- ФИЛЬТР ПО ВРЕМЕНИ (ЗАЩИТА ОТ НОЧНОГО РЫНКА) ---
            if config.RESTRICT_HOURS:
                now_utc = datetime.now(timezone.utc)
                current_hour = now_utc.hour
                # Проверяем, находится ли текущий час внутри разрешенного диапазона
                if config.TRADE_START_HOUR_UTC <= config.TRADE_END_HOUR_UTC:
                    is_allowed = config.TRADE_START_HOUR_UTC <= current_hour < config.TRADE_END_HOUR_UTC
                else: # Переход через полночь
                    is_allowed = current_hour >= config.TRADE_START_HOUR_UTC or current_hour < config.TRADE_END_HOUR_UTC
                    
                if not is_allowed:
                    # Раз в час или при старте пишем, что мы спим
                    if now_utc.minute == 0 and now_utc.second < 30:
                        logger.info(f"💤 Ночной режим. Торги приостановлены до {config.TRADE_START_HOUR_UTC}:00 UTC (Текущий час: {current_hour}:00 UTC)")
                    return

            if self.is_session_loss_limit_reached():
                logger.warning(
                    f"MAX_SESSION_LOSS {config.MAX_SESSION_LOSS_PCT}% достигнут — сканирование остановлено"
                )
                return

            logger.info(f"Начало сканирования... ({len(self.symbols_to_scan)} символов)")

            for symbol in self.symbols_to_scan:
                if not self.is_running:
                    break

                if self.signals_today >= self.max_signals_per_day:
                    break

                if len(self.positions) >= config.MAX_CONCURRENT_POSITIONS:
                    logger.info(f"Лимит позиций {config.MAX_CONCURRENT_POSITIONS} — сканирование приостановлено")
                    break

                if any(p.symbol == symbol for p in self.positions.values()):
                    continue

                try:
                    smc_result = await self.smc_analyzer.analyze_symbol(symbol)
                except Exception as e:
                    logger.debug(f"Пропуск {symbol}: {e}")
                    continue

                if smc_result['signal'] and smc_result['score'] >= config.MIN_INDICATORS_SCORE:
                    logger.info(f"СИГНАЛ: {symbol} (score: {smc_result['score']}/{config.TOTAL_INDICATORS})")

                    try:
                        funding_info = await self.exchange.fetch_funding_rate(symbol)
                        funding_rate = float(funding_info.get('fundingRate', 0))
                        direction = smc_result.get('direction', 'LONG')

                        if direction == 'LONG' and funding_rate > 0.0005:
                            logger.info(f"Пропуск LONG {symbol}: funding={funding_rate:.4%}")
                            continue
                        elif direction == 'SHORT' and funding_rate < -0.0005:
                            logger.info(f"Пропуск SHORT {symbol}: funding={funding_rate:.4%}")
                            continue
                    except Exception:
                        pass

                    ticker = await self.exchange.fetch_ticker(symbol)
                    entry_price = ticker['last']

                    await self.open_position(symbol, entry_price, smc_result)
                    await asyncio.sleep(5)

                await asyncio.sleep(0.3)

            self.last_scan_time = datetime.now(timezone.utc)
            logger.info("Сканирование завершено")

    async def run_scanner_loop(self):
        while self.is_running:
            try:
                await self.scan_market()
                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"Ошибка в цикле сканирования: {e}")
                await asyncio.sleep(30)

    async def run_monitoring_loop(self):
        while self.is_running:
            try:
                await self.monitor_positions()
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Ошибка в цикле мониторинга: {e}")
                await asyncio.sleep(2)

    async def send_daily_report(self):
        try:
            stats = self.db.get_daily_statistics()
            db_today_pnl = float(stats.get('total_pnl') or 0.0) if stats else 0.0
            today_trades = stats.get('total_trades', 0) if stats else 0
            today_wins = stats.get('profitable_trades', 0) if stats else 0
            today_losses = stats.get('losing_trades', 0) if stats else 0

            all_stats = self.db.get_all_statistics()
            avg_daily = all_stats.get('avg_daily_pct', 0) or 0
            deposit = config.DEPOSIT

            # ИСПРАВЛЕНИЕ: Плюсуем уже зафиксированный профит по открытым сделкам (TP1, TP2)
            floating_realized_pnl = sum(max(0, p.realized_pnl_usd) for p in self.positions.values())
            today_pnl = db_today_pnl + floating_realized_pnl
            today_pnl_pct = (today_pnl / deposit * 100) if deposit > 0 else 0.0

            real_balance = deposit
            real_pnl = 0.0
            try:
                balance = await self.exchange.fetch_balance()
                real_balance = float(balance.get('USDT', {}).get('total', deposit) or deposit)
                real_pnl = real_balance - deposit
            except Exception as bal_err:
                logger.warning(f"Не удалось получить реальный баланс для отчёта: {bal_err}")
                total_pnl_all = float(all_stats.get('total_pnl') or 0.0)
                real_balance = deposit + total_pnl_all + floating_realized_pnl
                real_pnl = total_pnl_all + floating_realized_pnl

            pnl_sign = '+' if real_pnl >= 0 else ''
            pnl_pct_total = (real_pnl / deposit * 100) if deposit > 0 else 0

            message = (
                f"📊 ДНЕВНОЙ ОТЧЕТ\n"
                f"Дата: {datetime.now(timezone.utc).strftime('%d.%m.%Y')}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"💰 БАЛАНС (РЕАЛЬНЫЙ):\n"
                f"  Начальный депозит: ${deposit:.2f}\n"
                f"  Реальный баланс: ${real_balance:.2f}\n"
                f"  PnL всего: {pnl_sign}${real_pnl:.2f} ({pnl_sign}{pnl_pct_total:.1f}%)\n\n"
                f"📋 СТАТИСТИКА ЗА ДЕНЬ:\n"
                f"  Сделок закрыто: {today_trades}\n"
                f"  Прибыльных: {today_wins} | Убыточных: {today_losses}\n"
                f"  PnL дня: {'+' if today_pnl >= 0 else ''}${today_pnl:.2f} "
                f"({'+' if today_pnl_pct >= 0 else ''}{today_pnl_pct:.1f}%)\n\n"
                f"📈 ОБЩАЯ СТАТИСТИКА:\n"
                f"  Всего сделок: {all_stats.get('total_trades', 0)}\n"
                f"  Win Rate: {(all_stats.get('profitable', 0) / max(all_stats.get('total_trades', 1), 1) * 100):.1f}%\n"
                f"  Средний % в день: {'+' if avg_daily >= 0 else ''}{avg_daily:.1f}%"
            )

            await self.send_telegram_message(message)
        except Exception as e:
            logger.error(f"Ошибка отправки отчета: {e}")


    async def run_daily_report_loop(self):
        while self.is_running:
            try:
                now = datetime.now(timezone.utc)
                next_midnight = (now + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                sleep_seconds = (next_midnight - now).total_seconds()
                await asyncio.sleep(sleep_seconds)
                if self.is_running:
                    await self.send_daily_report()
            except Exception as e:
                logger.error(f"Ошибка в цикле отчетов: {e}")
                await asyncio.sleep(3600)

    async def send_hourly_report(self):
        try:
            stats = self.db.get_daily_statistics()
            today_trades = stats.get('total_trades', 0) if stats else 0
            today_wins = stats.get('profitable_trades', 0) if stats else 0
            today_losses = stats.get('losing_trades', 0) if stats else 0
            today_closed_pnl = stats.get('total_pnl', 0) if stats else 0

            positions_info = ""
            total_open_pnl = 0

            for pid, pos in self.positions.items():
                try:
                    ticker = await self.exchange.fetch_ticker(pos.symbol)
                    current_price = ticker['last']
                    rem = max(pos.remaining_quantity, 0.0)
                    
                    if pos.side == 'SHORT':
                        unrealized = (pos.entry_price - current_price) * rem
                    else:
                        unrealized = (current_price - pos.entry_price) * rem
                        
                    fee = rem * current_price * config.TAKER_FEE
                    unrealized -= fee
                    
                    pnl = pos.realized_pnl_usd + unrealized
                    margin = pos.amount_usdt
                    pnl_pct = (pnl / margin) * 100 if margin > 0 else 0.0

                    emoji = "🟢 " if pnl >= 0 else "🔴 "
                    positions_info += (
                        f"  {emoji} {pos.symbol.replace('/USDT', '')}:  "
                        f"{'+' if pnl >= 0 else ''}${pnl:.2f}  "
                        f"({'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}%)\n "
                    )
                except Exception:
                    positions_info += f"  ⚪ {pos.symbol}: данные недоступны\n "

            if not positions_info:
                positions_info = "  Нет открытых позиций\n"

            total_day_pnl = today_closed_pnl + total_open_pnl
            dep = config.DEPOSIT if config.DEPOSIT > 0 else 50.0
            total_day_pnl_pct = (total_day_pnl / dep) * 100

            now_moscow = datetime.now(timezone.utc) + timedelta(hours=3)
            pnl_emoji = "📈" if total_day_pnl >= 0 else "📉"
            winrate = (today_wins / today_trades * 100) if today_trades > 0 else 0

            message = (
                f"⏰ ЧАСОВОЙ ОТЧЁТ | {now_moscow.strftime('%H:%M МСК')}\n"
                f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n\n"
                f"{pnl_emoji} РЕЗУЛЬТАТ ЗА СЕГОДНЯ:\n"
                f"  Закрытые сделки: {'+' if today_closed_pnl >= 0 else ''}${today_closed_pnl:.2f}\n"
                f"  Открытые сделки: {'+' if total_open_pnl >= 0 else ''}${total_open_pnl:.2f}\n"
                f"  ━━━━━━━━━━━━━━\n"
                f"  ИТОГО: {'+' if total_day_pnl >= 0 else ''}${total_day_pnl:.2f} "
                f"({'+' if total_day_pnl_pct >= 0 else ''}{total_day_pnl_pct:.1f}%)\n\n"
                f"📊 ОТКРЫТЫЕ ПОЗИЦИИ ({len(self.positions)}):\n"
                f"{positions_info}\n"
                f"📋 СТАТИСТИКА ДНЯ:\n"
                f"  Сделок закрыто: {today_trades}\n"
                f"  Прибыльных: ✅ {today_wins} | Убыточных: ❌ {today_losses}\n"
                f"  Win Rate: {winrate:.0f}%\n\n"
                f"🤖 Статус: {'🟢 РАБОТАЕТ' if self.is_running else '🔴 ОСТАНОВЛЕН'}\n"
                f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
                f"SMART MONEY 1 BOT"
            )

            await self.send_telegram_message(message)
            logger.info("Часовой отчёт отправлен")
        except Exception as e:
            logger.error(f"Ошибка отправки часового отчёта: {e}")


    async def run_hourly_report_loop(self):
        while self.is_running:
            await asyncio.sleep(3600)

    # ========================================================================
    # TELEGRAM КОМАНДЫ
    # ========================================================================

    def get_main_keyboard(self):
        from telegram import ReplyKeyboardMarkup
        keyboard = [
            ['📊 Результаты', '🛑 Закрыть все'],
            ['🟢 Старт', '🔴 Стоп']
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat:
            self.active_chat_ids.add(str(update.effective_chat.id))

        text = update.message.text
        if text == '📊 Результаты':
            from telegram import ReplyKeyboardMarkup
            keyboard = [
                ['⏳ За 1 час', '⏳ За 5 часов', '⏳ За 10 часов'],
                ['📅 За 24 часа', '📅 За 7 дней'],
                ['🔙 Назад']
            ]
            await update.message.reply_text(
                "Выберите период отчёта:",
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            )
        elif text == '🔙 Назад':
            await update.message.reply_text("Главное меню:", reply_markup=self.get_main_keyboard())
        elif text in ['⏳ За 1 час', '⏳ За 5 часов', '⏳ За 10 часов', '📅 За 24 часа', '📅 За 7 дней']:
            mapping = {
                '⏳ За 1 час': (1, 'за последний час'),
                '⏳ За 5 часов': (5, 'за последние 5 часов'),
                '⏳ За 10 часов': (10, 'за последние 10 часов'),
                '📅 За 24 часа': (24, 'за последние 24 часа'),
                '📅 За 7 дней': (168, 'за последние 7 дней')
            }
            hours, label = mapping[text]
            await self.send_custom_report(update, hours, label)
        elif text == '🛑 Закрыть все':
            await self.cmd_close_all(update, context)
        elif text == '🟢 Старт':
            await self.cmd_start_bot(update, context)
        elif text == '🔴 Стоп':
            await self.cmd_stop_bot(update, context)

    async def send_custom_report(self, update: Update, hours: int, label: str):
        stats = self.db.get_statistics_by_hours(hours)

        trades = stats.get('total_trades') or 0
        wins = stats.get('profitable_trades') or 0
        losses = stats.get('losing_trades') or 0
        pnl = stats.get('total_pnl') or 0.0

        dep = config.DEPOSIT if config.DEPOSIT > 0 else 50.0
        pnl_pct = (pnl / dep) * 100
        winrate = (wins / trades * 100) if trades > 0 else 0

        total_open_pnl = 0
        open_count = len(self.positions)
        for pos in self.positions.values():
            try:
                ticker = await self.exchange.fetch_ticker(pos.symbol)
                current_price = ticker['last']
                rem = max(pos.remaining_quantity, 0.0)
                if pos.side == 'SHORT':
                    pos_pnl = pos.realized_pnl_usd + (pos.entry_price - current_price) * rem
                else:
                    pos_pnl = pos.realized_pnl_usd + (current_price - pos.entry_price) * rem
                total_open_pnl += pos_pnl
            except Exception:
                pass

        total_pnl = pnl + total_open_pnl
        total_pnl_pct = (total_pnl / dep) * 100
        pnl_emoji = "📈" if total_pnl >= 0 else "📉"

        message = (
            f"📊 ОТЧЁТ {label.upper()}\n"
            f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
            f"{pnl_emoji} ИТОГ: {'+' if total_pnl >= 0 else ''}${total_pnl:.2f} "
            f"({'+' if total_pnl_pct >= 0 else ''}{total_pnl_pct:.1f}%)\n\n"
            f"ЗАКРЫТЫЕ СДЕЛКИ:\n"
            f"  Количество: {trades}\n"
            f"  Прибыльных: ✅ {wins} | Убыточных: ❌ {losses}\n"
            f"  Win Rate: {winrate:.0f}%\n"
            f"  PnL: {'+' if pnl >= 0 else ''}${pnl:.2f} ({'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}%)\n\n"
            f"ОТКРЫТЫЕ СДЕЛКИ ({open_count}):\n"
            f"  Текущий PnL: {'+' if total_open_pnl >= 0 else ''}${total_open_pnl:.2f}\n"
        )
        await update.message.reply_text(message)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info(f"Команда /start от {update.effective_chat.id}")
        if update.effective_chat:
            self.active_chat_ids.add(str(update.effective_chat.id))

        message = (
            f"🤖 Smart Money Aggressive Bot v2.2\n\n"
            f"Статус: {'🟢 РАБОТАЕТ' if self.is_running else '🔴 ОСТАНОВЛЕН'}\n"
            f"Позиций открыто: {len(self.positions)} / {config.MAX_CONCURRENT_POSITIONS}\n"
            f"Сигналов сегодня: {self.signals_today}\n\n"
            f"Параметры:\n"
            f"- Депозит: ${config.DEPOSIT}\n"
            f"- Макс. позиций: {config.MAX_CONCURRENT_POSITIONS}\n"
            f"- Плечо: x{config.LEVERAGE}\n"
            f"- Stop Loss: -{config.STOP_LOSS_PCT}%\n"
            f"- Макс. сессионный убыток: -{config.MAX_SESSION_LOSS_PCT}%\n"
            f"- Режим: 🤖 ПОЛНЫЙ АВТОПИЛОТ\n\n"
            f"Используйте кнопки меню для управления!"
        )
        await update.message.reply_text(message, reply_markup=self.get_main_keyboard())

    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            balance = await self.exchange.fetch_balance()
            usdt_balance = balance.get('USDT', {}).get('total', 0)
            free = balance.get('USDT', {}).get('free', 0)
            used = balance.get('USDT', {}).get('used', 0)

            message = (
                f"💰 БАЛАНС\n\n"
                f"USDT Total: ${usdt_balance:.2f}\n"
                f"USDT Free: ${free:.2f}\n"
                f"USDT Used: ${used:.2f}\n\n"
                f"Стартовый депозит: ${config.DEPOSIT}\n"
                f"PnL: {'+' if usdt_balance - config.DEPOSIT >= 0 else ''}${usdt_balance - config.DEPOSIT:.2f}"
            )
            await update.message.reply_text(message)
        except Exception as e:
            await update.message.reply_text(f"Ошибка получения баланса: {e}")

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.positions:
            await update.message.reply_text("Нет открытых позиций")
            return

        messages = []
        for pid, pos in self.positions.items():
            try:
                ticker = await self.exchange.fetch_ticker(pos.symbol)
                current_price = ticker['last']
                rem = max(pos.remaining_quantity, 0.0)
                
                # Считаем "грязный" PnL
                if pos.side == 'SHORT':
                    unrealized = (pos.entry_price - current_price) * rem
                else:
                    unrealized = (current_price - pos.entry_price) * rem
                    
                # Вычитаем комиссию будущего закрытия
                fee = rem * current_price * config.TAKER_FEE
                unrealized -= fee
                
                # Итоговые чистые значения
                pnl = pos.realized_pnl_usd + unrealized
                margin = pos.amount_usdt
                pnl_pct = (pnl / margin) * 100 if margin > 0 else 0.0

                msg = (
                    f"📍 {'🔴' if pos.side == 'SHORT' else '🟢'} #{pos.symbol.replace('/USDT', '')}\n"
                    f"Вход: {pos.entry_price}\n"
                    f"Текущая: {current_price}\n"
                    f"Stop Loss: {pos.stop_loss}\n"
                    f"PnL: {'+' if pnl >= 0 else ''}${pnl:.2f} ({'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}%)\n"
                    f"Время: {datetime.now(timezone.utc) - pos.timestamp}"
                )
                messages.append(msg)
            except Exception:
                messages.append(f"Ошибка получения данных для {pos.symbol}")

        await update.message.reply_text("\n\n".join(messages))


    async def cmd_signals(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = "📡 ПОСЛЕДНИЕ СИГНАЛЫ\n\n"
        message += f"Сегодня: {self.signals_today}\n"
        message += f"Последнее сканирование: {self.last_scan_time or 'Не было'}"
        await update.message.reply_text(message)

    async def cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Использование: /close {PAIR}\nПример: /close BTC")
            return

        pair = context.args[0].upper()
        found = None
        for pid, pos in self.positions.items():
            if pos.symbol.startswith(f"{pair}/"):
                found = pid
                break

        if not found:
            await update.message.reply_text(f"Позиция по {pair} не найдена")
            return

        await self.close_position(found)

    async def cmd_close_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.positions:
            await update.message.reply_text("Нет открытых позиций")
            return

        await update.message.reply_text(f"Закрываю {len(self.positions)} позиций...")
        await self.close_all_positions()

    async def cmd_emergency(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🚨 ЭКСТРЕННОЕ ЗАКРЫТИЕ ВСЕХ ПОЗИЦИЙ!")
        await self.close_all_positions(emergency=True)

    async def cmd_daily_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.send_daily_report()

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            balance = await self.exchange.fetch_balance()
            usdt_total = float(balance.get('USDT', {}).get('total', 0))
            usdt_free = float(balance.get('USDT', {}).get('free', 0))
            usdt_used = float(balance.get('USDT', {}).get('used', 0))
            total_pnl = usdt_total - config.DEPOSIT
            pnl_pct = (total_pnl / config.DEPOSIT * 100) if config.DEPOSIT > 0 else 0

            all_stats = self.db.get_all_statistics()
            total_trades = all_stats.get('total_trades', 0) or 0
            total_wins = all_stats.get('profitable', 0) or 0
            total_losses = all_stats.get('losing', 0) or 0
            winrate = (total_wins / total_trades * 100) if total_trades > 0 else 0

            stats = self.db.get_daily_statistics()
            today_trades = stats.get('total_trades', 0) if stats else 0
            today_wins = stats.get('profitable_trades', 0) if stats else 0
            today_losses = stats.get('losing_trades', 0) if stats else 0
            today_pnl = stats.get('total_pnl', 0) if stats else 0
            today_winrate = (today_wins / today_trades * 100) if today_trades > 0 else 0

            positions_text = ""
            unrealized_pnl = 0.0

            if self.positions:
                for pid, pos in self.positions.items():
                    try:
                        ticker = await self.exchange.fetch_ticker(pos.symbol)
                        current_price = ticker['last']
                        rem = max(pos.remaining_quantity, 0.0)
                        if pos.side == 'SHORT':
                            unrealized = (pos.entry_price - current_price) * rem
                        else:
                            unrealized = (current_price - pos.entry_price) * rem
                            
                        fee = rem * current_price * config.TAKER_FEE
                        unrealized -= fee
                        
                        pos_pnl = pos.realized_pnl_usd + unrealized
                        margin = pos.amount_usdt
                        roe = (pos_pnl / margin) * 100 if margin > 0 else 0.0

                        unrealized_pnl += pos_pnl
                        emoji = "🟢" if roe >= 0 else "🔴"
                        positions_text += (
                            f"{emoji} {pos.symbol.replace('/USDT', '')} {pos.side}\n"
                            f"   Вход: ${pos.entry_price:.4f} | Текущая: ${current_price:.4f}\n"
                            f"   ROE: {roe:+.1f}% | PnL: {'+' if pos_pnl >= 0 else ''}${pos_pnl:.2f}\n\n"
                        )
                    except Exception:
                        positions_text += f"⚠️ {pos.symbol} — ошибка получения цены\n\n"
            else:
                positions_text = "Нет открытых позиций\n"

            msg = (
                f"📊 СТАТИСТИКА\n"
                f"━━━━━━━━━━━━━━━\n\n"
                f"💰 БАЛАНС (РЕАЛЬНЫЙ)\n"
                f"Всего: ${usdt_total:.2f}\n"
                f"Свободно: ${usdt_free:.2f}\n"
                f"В позициях: ${usdt_used:.2f}\n\n"
                f"📈 PnL\n"
                f"Биржа vs депозит: {'+' if total_pnl >= 0 else ''}${total_pnl:.2f} ({pnl_pct:+.1f}%)\n"
                f"Сегодня (DB): {'+' if today_pnl >= 0 else ''}${today_pnl:.2f}\n"
                f"Нереализованный: {'+' if unrealized_pnl >= 0 else ''}${unrealized_pnl:.2f}\n\n"
                f"🎯 СДЕЛКИ ЗА ВСЁ ВРЕМЯ\n"
                f"Всего: {total_trades} (W:{total_wins} L:{total_losses})\n"
                f"Winrate: {winrate:.1f}%\n\n"
                f"📅 СЕГОДНЯ\n"
                f"Сделок: {today_trades} (W:{today_wins} L:{today_losses})\n"
                f"Winrate: {today_winrate:.1f}%\n\n"
                f"📋 ОТКРЫТЫЕ ПОЗИЦИИ\n"
                f"━━━━━━━━━━━━━━━\n"
                f"{positions_text}"
            )
            await update.message.reply_text(msg)
        except Exception as e:
            await update.message.reply_text(f"Ошибка получения статистики: {e}")

    async def cmd_stop_trading(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_running:
            await update.message.reply_text("🔴 Бот уже остановлен!")
            return

        self.is_running = False
        await update.message.reply_text(
            "🟡 РЕЖИМ ЗАВЕРШЕНИЯ ТОРГОВЛИ\n"
            "Новые сделки не открываются.\n"
            "Закрываю убыточные позиции..."
        )

        closed_count = 0
        kept_count = 0
        for pid, pos in list(self.positions.items()):
            try:
                ticker = await self.exchange.fetch_ticker(pos.symbol)
                current_price = ticker['last']
                if pos.side == 'SHORT':
                    pnl_usd = pos.realized_pnl_usd + (pos.entry_price - current_price) * pos.remaining_quantity
                else:
                    pnl_usd = pos.realized_pnl_usd + (current_price - pos.entry_price) * pos.remaining_quantity

                if pnl_usd < 0:
                    await self.close_position(pid)
                    closed_count += 1
                else:
                    pos.dynamic_sl_level = 1
                    kept_count += 1
            except Exception as e:
                logger.error(f"Ошибка обработки {pos.symbol}: {e}")

        msg = (
            f"✅ РЕЖИМ ЗАВЕРШЕНИЯ АКТИВИРОВАН\n"
            f"Закрыто убыточных: {closed_count}\n"
            f"Оставлено прибыльных: {kept_count}"
        )
        await update.message.reply_text(msg)
        await self.send_telegram_message(msg)

    async def cmd_start_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.is_running:
            await update.message.reply_text("🟢 Бот уже работает!")
            return
        self.is_running = True
        await update.message.reply_text("🟢 БОТ ВКЛЮЧЁН!\nСканирование и торговля возобновлены.")
        await self.send_telegram_message("🟢 Бот включён оператором. Торговля возобновлена.")

    async def cmd_stop_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_running:
            await update.message.reply_text("🔴 Бот уже остановлен!")
            return
        self.is_running = False
        await update.message.reply_text(
            "🔴 БОТ ВЫКЛЮЧЕН!\n"
            "Новые сделки не открываются.\n"
            "Открытые позиции остаются.\n"
            "Для закрытия всех: /close_all"
        )
        await self.send_telegram_message("🔴 Бот выключен оператором.")

    async def run_telegram_bot(self):
        app = None
        MAX_CONFLICT_RETRIES = 5
        conflict_count = 0
    
        while self.is_running:
            try:
                logger.info("Инициализация Telegram бота...")
    
                # 1. Строим Application
                app = (
                    Application.builder()
                    .token(self.telegram_token)
                    .connect_timeout(30)
                    .read_timeout(30)
                    .pool_timeout(30)
                    .build()
                )
                self.app = app
    
                # 2. Обязательно инициализируем перед вызовами API
                await app.initialize()
    
                # 3. Теперь безопасно удаляем вебхук
                try:
                    await app.bot.delete_webhook(drop_pending_updates=True)
                    await asyncio.sleep(1)
                    logger.info("Webhook и pending updates очищены")
                except Exception as cleanup_err:
                    logger.warning(f"Ошибка очистки webhook: {cleanup_err}")
    
    
    
                app.add_handler(CommandHandler("start", self.cmd_start))
                app.add_handler(CommandHandler("balance", self.cmd_balance))
                app.add_handler(CommandHandler("positions", self.cmd_positions))
                app.add_handler(CommandHandler("signals", self.cmd_signals))
                app.add_handler(CommandHandler("close", self.cmd_close))
                app.add_handler(CommandHandler("close_all", self.cmd_close_all))
                app.add_handler(CommandHandler("start_bot", self.cmd_start_bot))
                app.add_handler(CommandHandler("stop_bot", self.cmd_stop_bot))
                app.add_handler(CommandHandler("emergency", self.cmd_emergency))
                app.add_handler(CommandHandler("daily_report", self.cmd_daily_report))
                app.add_handler(CommandHandler("stop_trading", self.cmd_stop_trading))
                app.add_handler(CommandHandler("stats", self.cmd_stats))
                app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))
    
                try:
                    from telegram import BotCommand
                    await app.bot.set_my_commands([
                        BotCommand("start", "Показать кнопки управления"),
                        BotCommand("balance", "Текущий баланс"),
                        BotCommand("positions", "Открытые сделки"),
                        BotCommand("signals", "Последние сигналы"),
                        BotCommand("close", "Закрыть сделку"),
                        BotCommand("close_all", "Закрыть все сделки"),
                        BotCommand("start_bot", "Включить бота"),
                        BotCommand("stop_bot", "Выключить бота"),
                        BotCommand("emergency", "Экстренная остановка"),
                        BotCommand("daily_report", "Статистика за день"),
                        BotCommand("stats", "Полная статистика"),
                        BotCommand("stop_trading", "Остановить торговлю с закрытием"),
                    ])
                except Exception as e:
                    logger.warning(f"Не удалось установить меню: {e}")
    
                self.app = app
                # await app.initialize()  <-- Убрали дублирование
                await app.start()

                await app.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True
                )
    
                conflict_count = 0
                logger.info("✅ Telegram polling запущен успешно!")
    
                while self.is_running:
                    await asyncio.sleep(5)
                    if hasattr(app, 'updater') and app.updater and not app.updater.running:
                        logger.warning("Telegram updater остановлен (возможно Conflict). Перезапуск...")
                        break
    
            except telegram.error.Conflict as e:
                conflict_count += 1
                wait_time = min(30 * conflict_count, 120)
                logger.warning(
                    f"TG Conflict ({conflict_count}/{MAX_CONFLICT_RETRIES}): {e}. "
                    f"Повтор через {wait_time} сек..."
                )
                if conflict_count >= MAX_CONFLICT_RETRIES:
                    logger.error(
                        "Слишком много Conflict ошибок! Вероятно запущен другой экземпляр бота."
                    )
                    await asyncio.sleep(300)
                    conflict_count = 0
                else:
                    await asyncio.sleep(wait_time)
            except Exception as e:
                logger.error(f"Ошибка Telegram бота: {e}")
    
            finally:
                if app is not None:
                    try:
                        if hasattr(app, 'updater') and app.updater and app.updater.running:
                            await app.updater.stop()
                    except Exception:
                        pass
                    try:
                        if app.running:
                            await app.stop()
                    except Exception:
                        pass
                    try:
                        await app.shutdown()
                    except Exception:
                        pass
                    app = None
    
                if self.is_running:
                    logger.info("Перезапуск Telegram через 10 сек...")
                    await asyncio.sleep(10)
    
    async def start(self) -> bool:
        logger.info("Запуск Smart Money Aggressive Bot v2.2...")

        if not await self.connect():
            logger.error("Не удалось подключиться к бирже")
            try:
                await self.disconnect()
            except Exception:
                pass
            return False

        self.is_running = True

        # Восстановление позиций с биржи
        try:
            logger.info("Синхронизация открытых позиций с Binance...")
            exchange_positions = await self.exchange.fetch_positions()
            
            # Подтягиваем локальную базу данных, чтобы не терять оригинальную маржу
            db_positions_list = self.db.get_open_positions()
            db_positions_map = {p['symbol']: p for p in db_positions_list}

            restored_count = 0
            for ep in exchange_positions:
                contracts = float(ep.get('contracts', 0) or 0)
                if abs(contracts) > 0:
                    symbol = ep['symbol']
                    side = 'LONG' if ep.get('side') == 'long' else 'SHORT'
                    entry_price = float(ep.get('entryPrice', 0))
                    
                    # Сначала ищем сделку в БД, чтобы восстановить оригинальные параметры
                    saved_pos = db_positions_map.get(symbol, {})
                    
                    leverage = int(saved_pos.get('leverage', ep.get('leverage', config.LEVERAGE)))

                    if 'amount_usdt' in saved_pos and saved_pos['amount_usdt']:
                        amount_usdt = float(saved_pos['amount_usdt'])
                    else:
                        amount_usdt = (abs(contracts) * entry_price) / leverage


                    open_orders = await self.exchange.fetch_open_orders(symbol)
                    sl_price = entry_price * (0.5 if side == 'LONG' else 1.5)
                    tp_price = entry_price * (1.5 if side == 'LONG' else 0.5)

                    for ord in open_orders:
                        o_type = ord.get('type', '').lower()
                        if 'stop' in o_type:
                            sl_price = float(ord.get('stopPrice') or ord.get('price') or sl_price)
                        elif 'take_profit' in o_type:
                            tp_price = float(ord.get('stopPrice') or ord.get('price') or tp_price)

                    # Подтягиваем оригинальный ID и объем из БД
                    db_id = int(saved_pos.get('id', int(datetime.now().timestamp() * 1000) + restored_count))
                    original_qty = float(saved_pos.get('quantity', abs(contracts)))
                    current_qty = abs(contracts)
                    
                    # Восстанавливаем флаги частичных закрытий
                    # Восстанавливаем флаги частичных закрытий
                    tp1_done = current_qty <= original_qty * 0.65
                    tp2_done = current_qty <= original_qty * 0.35
                    tp3_done = current_qty <= original_qty * 0.15

                    # Высчитываем цены для TP2 и TP3, чтобы бот не закрыл позицию мгновенно при рестарте
                    calc_tp2_dist = entry_price * (config.PARTIAL_TP2_PCT / config.LEVERAGE / 100)
                    calc_tp3_dist = entry_price * (config.PARTIAL_TP3_PCT / config.LEVERAGE / 100)
                    fallback_tp2 = entry_price - calc_tp2_dist if side == 'SHORT' else entry_price + calc_tp2_dist
                    fallback_tp3 = entry_price - calc_tp3_dist if side == 'SHORT' else entry_price + calc_tp3_dist

                    pos = Position(
                        id=db_id,
                        symbol=symbol,
                        side=side,
                        entry_price=entry_price,
                        stop_loss=sl_price,
                        take_profit=tp_price,
                        tp2_price=fallback_tp2,
                        tp3_price=fallback_tp3,
                        amount_usdt=amount_usdt,
                        leverage=leverage,
                        quantity=original_qty,
                        remaining_quantity=current_qty,
                        timestamp=datetime.now(timezone.utc),
                        realized_pnl_usd=float(ep.get('realizedPnl', 0)),
                        partial_tp1_done=tp1_done,
                        partial_tp2_done=tp2_done,
                        partial_tp3_done=tp3_done
                    )


                    self.positions[pos.id] = pos
                    restored_count += 1

            logger.info(f"🔄 Синхронизировано с биржей: {restored_count} позиций. Лимит: {config.MAX_CONCURRENT_POSITIONS}")
        except Exception as e:
            logger.error(f"Ошибка синхронизации с биржей: {e}")

        async def task_with_log(name, coro):
            try:
                await coro
            except Exception as e:
                logger.error(f"Task '{name}' finished with error: {e}")
            finally:
                logger.warning(f"Task '{name}' finished!")

        tasks = [
            asyncio.create_task(task_with_log("scanner", self.run_scanner_loop())),
            asyncio.create_task(task_with_log("monitoring", self.run_monitoring_loop())),
            asyncio.create_task(task_with_log("daily_report", self.run_daily_report_loop())),
            asyncio.create_task(task_with_log("hourly_report", self.run_hourly_report_loop())),
            asyncio.create_task(task_with_log("telegram", self.run_telegram_bot()))
        ]

        # Функция отправит сообщение через 3 секунды, когда Telegram-клиент полностью подключится
        async def send_startup_msg():
            await asyncio.sleep(3)
            try:
                stats = self.db.get_all_statistics()
                total_pnl = float(stats.get('total_pnl') or 0.0)
                real_free = await self._get_real_balance()
                virtual_eq = max(config.DEPOSIT + total_pnl, config.DEPOSIT * 0.5)
                await self.send_telegram_message(
                    f"🟢 БОТ ВКЛЮЧЁН v2.2\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"💰 Депозит: ${config.DEPOSIT:.2f}\n"
                    f"🏦 Реальный баланс: ${real_free:.2f}\n"
                    f"📊 Виртуальный (DB): ${virtual_eq:.2f}\n"
                    f"⚙️ Плечо: x{config.LEVERAGE}\n"
                    f"🛡 SL: {config.STOP_LOSS_PCT}% ({config.STOP_LOSS_PCT * config.LEVERAGE:.0f}% ROE)\n"
                    f"🎯 TP: {config.PARTIAL_TP1_PCT:.0f}% / {config.PARTIAL_TP2_PCT:.0f}% / {config.PARTIAL_TP3_PCT:.0f}% ROE\n"
                    f"📡 Монет: {len(self.symbols_to_scan)}\n"
                    f"🔒 Макс. позиций: {config.MAX_CONCURRENT_POSITIONS}\n"
                    f"🚨 Стоп сессии: -{config.MAX_SESSION_LOSS_PCT}%\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"SMART MONEY BOT v2.2"
                )
            except Exception as e:
                logger.warning(f"Не удалось отправить стартовое сообщение: {e}")

        asyncio.create_task(send_startup_msg())

        results = await asyncio.gather(*tasks, return_exceptions=True)
        logger.error(f"All tasks finished! Results: {results}")

        await asyncio.sleep(60)
        return True


    async def stop(self):
        logger.info("Остановка бота...")
        self.is_running = False
        await self.disconnect()


# ============================================================================
# ЗАПУСК
# ============================================================================

def _env_secret(*names: str) -> str:
    for n in names:
        raw = os.getenv(n)
        if raw is None:
            continue
        s = raw.strip().strip('\ufeff').strip('"').strip("'")
        if s:
            return s
    return ''


from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import os

def run_dummy_server():
    port = int(os.getenv("PORT", 10000))
    class SimpleHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is alive and trading!")
        def log_message(self, format, *args):
            pass

    try:
        server = HTTPServer(('0.0.0.0', port), SimpleHandler)
        server.serve_forever()
    except Exception as e:
        pass

async def main():
    threading.Thread(target=run_dummy_server, daemon=True).start()

    API_KEY = os.getenv('BINANCE_API_KEY')
    API_SECRET = os.getenv('BINANCE_SECRET')

    TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
    USER_CHAT_ID = os.getenv('USER_CHAT_ID')


    config.DEPOSIT = 100.0                 # Твой реальный депозит
    config.ENTRY_AMOUNT = 100.0
    config.LEVERAGE = 10                   # ⚠️ КРИТИЧНО: 10x! 50x тебя ликвидирует за минуту.
    config.STOP_LOSS_PCT = 2.5             # Фоллбек, если ATR не сработает
    config.REINVEST_PROFITS = True         # Включаем сложный процент (компаундинг)
    config.DRAWDOWN_ALERT = 12.0
    config.MAX_CONCURRENT_POSITIONS = 4    # ⚠️ Максимум 4 сделки. На $100 больше нельзя.
    config.MAX_SESSION_LOSS_PCT = 15.0     # 🛑 Стоп торгов, если слил $15 за день (спасает от тильта)
    config.FILL_THRESHOLD = 0.90

    use_testnet = True                     # Оставь True для теста. Поставь False, когда закинешь $100.


    if not all([API_KEY, API_SECRET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID]):
        logger.warning(
            "⚠️ В коде не прописаны ключи! Проверь строки API_KEY и TELEGRAM_TOKEN"
        )

    bot = SmartMoneyBot(
        api_key=API_KEY,
        api_secret=API_SECRET,
        telegram_token=TELEGRAM_TOKEN,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        user_chat_id=USER_CHAT_ID,
        testnet=use_testnet
    )

    try:
        started = await bot.start()
        if started is False:
            logger.warning("⚠️ Бот не подключился к Binance. Повтор через 60 сек...")
            while True:
                await asyncio.sleep(60)
                started = await bot.start()
                if started:
                    break
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C")
        await bot.stop()
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        await bot.stop()

if __name__ == '__main__':
    asyncio.run(main())