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

ИСПРАВЛЕНИЯ v2.1:
1. Корректное подтверждение исполнения ордера (filled/avg_price)
2. Обработка partial fills (< FILL_THRESHOLD → отмена позиции)
3. MAX_CONCURRENT_POSITIONS — жёсткое ограничение числа позиций
4. MAX_SESSION_LOSS_PCT — стоп торгов при достижении дневной просадки
5. Исправлены индексные ошибки в calculate_adx (выход за границы массива)
6. Исправлен SQL INSERT в update_daily_statistics (корректные параметры)
7. Блокировка дублирующих открытий через _opening_symbols (asyncio.Lock)
8. Комиссия учтена в расчёте ожидаемого PnL
9. Проверка спреда перед входом (MAX_SPREAD_PCT)
10. Логирование: expected_entry, real_entry, filled, fees
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


# ============================================================================
# КРИТИЧЕСКИ ВАЖНЫЕ ПАРАМЕТРЫ СТРАТЕГИИ
# ============================================================================

@dataclass
class StrategyConfig:
    """Конфигурация стратегии SMART MONEY — FAST COMPOUND MODE"""
    # Финансовые параметры
    DEPOSIT: float = 50.0
    ENTRY_AMOUNT: float = 50.0
    LEVERAGE: int = 50

    # Риск-менеджмент
    STOP_LOSS_PCT: float = 0.75
    TAKE_PROFIT_PCT: float = 2.5
    TAKE_PROFIT: float = 4.0
    TP2_PCT: float = 4.0
    TP3_PCT: float = 7.0

    # Цели
    DAILY_TARGET_MIN: float = 10.0
    DAILY_TARGET_MAX: float = 15.0
    MAX_DAILY_LOSS_PCT: float = 10.0

    # --- FIX #4: жёсткие лимиты безопасности ---
    # Максимальное число одновременных позиций
    MAX_CONCURRENT_POSITIONS: int = 8
    # Стоп торгов при достижении дневной просадки (% от депозита)
    MAX_SESSION_LOSS_PCT: float = 30.0
    # Минимальный процент заполнения ордера, иначе — отмена (partial fill)
    FILL_THRESHOLD: float = 0.90
    # Максимальный допустимый спред в % от цены
    MAX_SPREAD_PCT: float = 0.05
    # Комиссия тейкера на Binance Futures (в долях)
    TAKER_FEE: float = 0.0004  # 0.04%

    # Режим работы
    WORK_HOURS: str = "24/7"
    DIRECTION: str = "BOTH"

    # Параметры сигналов
    MIN_INDICATORS_SCORE: int = 5
    TOTAL_INDICATORS: int = 8

    # Таймфреймы
    SCANNER_TIMEFRAME: str = '5m'
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
    MOMENTUM_EXIT_MINUTES: int = 40
    MOMENTUM_MIN_PROFIT: float = 0.3
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
    PARTIAL_TP1_PCT: float = 30.0
    PARTIAL_TP2_PCT: float = 65.0
    PARTIAL_TP3_PCT: float = 120.0

    # Время позиции
    POSITION_TIMEOUT_HOURS: float = 1.8
    BAD_POSITION_TIMEOUT_MINUTES: int = 12
    BAD_TRADE_EXIT_MINUTES: int = 6
    SMART_EXIT_ANALYSIS: bool = True
    WEAK_MOMENTUM_EXIT: float = -8.0


config = StrategyConfig()


# ============================================================================
# ПРЕДОХРАНИТЕЛЬ: НОВОСТНОЙ ФОН / НАСТРОЕНИЕ РЫНКА
# ============================================================================
ALLOW_TRADING = True


async def check_fear_greed_index(bot: 'SmartMoneyBot'):
    """Фоновая проверка Crypto Fear & Greed Index каждые 30 минут."""
    global ALLOW_TRADING
    return
    import aiohttp as _aiohttp

    while bot.is_running:
        try:
            async with _aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.alternative.me/fng/?limit=1",
                    timeout=15
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("data"):
                            entry = data["data"][0]
                            value = int(entry.get("value", 50))
                            classification = entry.get("value_classification", "Neutral")
                            logger.info(f"Fear & Greed Index: {value} ({classification})")

                            if value < 25 and ALLOW_TRADING:
                                ALLOW_TRADING = False
                                msg = (
                                    f"⚠️ На рынке паника!\n"
                                    f"Fear & Greed Index: {value} ({classification})\n"
                                    f"Открытие новых сделок приостановлено."
                                )
                                await bot.send_telegram_message(msg)
                                logger.warning(msg)
                            elif value >= 25 and not ALLOW_TRADING:
                                ALLOW_TRADING = True
                                msg = (
                                    f"✅ Рынок успокоился.\n"
                                    f"Fear & Greed Index: {value} ({classification})\n"
                                    f"Торговля возобновлена."
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

        # --- FIX #6: убраны пробелы в именах колонок, корректная схема ---
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

        conn.commit()

        # Пересчёт total_pnl_pct от DEPOSIT
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
        """Обновление дневной статистики.

        total_pnl_pct = (total_pnl / equity_reference) * 100
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        ref = float(equity_reference) if equity_reference and equity_reference > 0 else 50.0

        cursor.execute('SELECT id FROM statistics WHERE date = ?', (today,))
        row = cursor.fetchone()

        if row:
            if count_as_trade:
                # --- FIX #6: параметры точно совпадают с количеством ? ---
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
                # --- FIX #6: 9 колонок = 9 значений, без лишних параметров ---
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

        # Пересчитываем total_pnl_pct от реального депозита
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


# ============================================================================
# ИНДИКАТОРЫ И SMC АНАЛИЗ
# ============================================================================

class SMCAnalyzer:
    """Анализ Smart Money Concepts"""

    def __init__(self, exchange: ccxt.binanceusdm):
        self.exchange = exchange

    def calculate_atr(self, highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
        """Расчет ATR для динамических стопов и тейков"""
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
        """Получение свечных данных"""
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
        """Расчет EMA"""
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
        """Расчет RSI"""
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
        """Расчет ADX — FIX #3: исправлены индексные ошибки"""
        # Нужно минимум period*2 + 1 свечей для надёжного расчёта
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

        # Проверяем что хватает данных
        if len(tr) < period:
            return [0.0]

        # Первое сглаженное ATR
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

        # ADX = скользящее среднее DX за period баров
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
        """Расчет MACD"""
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
        """Расчет SMA"""
        if len(prices) < period:
            return []

        sma = []
        for i in range(period, len(prices) + 1):
            sma.append(sum(prices[i - period:i]) / period)

        return sma

    def detect_bos_choch(self, ohlcv: List[List]) -> str:
        """Обнаружение BOS / CHoCH"""
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
        """Обнаружение Fair Value Gap"""
        if len(ohlcv) < 3:
            return ''

        current_price = ohlcv[-1][4]

        for i in range(len(ohlcv) - 3):
            c1, c2, c3 = ohlcv[i], ohlcv[i + 1], ohlcv[i + 2]
            high1, low1 = c1[2], c1[3]
            high3, low3 = c3[2], c3[3]

            if high1 < low3:
                if high1 * 0.998 <= current_price <= low3 * 1.003:
                    return 'BULLISH'

            if low1 > high3:
                if high3 * 0.997 <= current_price <= low1 * 1.002:
                    return 'BEARISH'

        return ''

    def detect_order_block(self, ohlcv: List[List]) -> str:
        """Обнаружение Order Block"""
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
        """Анализ символа — 7 индикаторов, LONG + SHORT с фильтром Premium/Discount"""
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
            'volume_ok': False
        }

        try:
            ohlcv_5m = await self.get_ohlcv(symbol, config.SCANNER_TIMEFRAME, limit=100)
            ohlcv_1h = await self.get_ohlcv(symbol, config.TREND_TIMEFRAME, limit=300)

            if not ohlcv_5m or not ohlcv_1h:
                return result

            # Защита от слишком коротких данных
            if len(ohlcv_5m) < 50 or len(ohlcv_1h) < 200:
                return result

            closes_5m = [c[4] for c in ohlcv_5m]
            highs_5m = [h[2] for h in ohlcv_5m]
            lows_5m = [l[3] for l in ohlcv_5m]
            volumes_5m = [v[5] for v in ohlcv_5m]
            current_price = closes_5m[-1]
            
            # --- РАСЧЕТ МАТРИЦЫ ПРЕМИУМ / ДИСКОНТ (За последние 24 часа старшего таймфрейма) ---
            highs_1h = [h[2] for h in ohlcv_1h]
            lows_1h = [l[3] for l in ohlcv_1h]
            max_1h = max(highs_1h[-24:])
            min_1h = min(lows_1h[-24:])
            equilibrium = (max_1h + min_1h) / 2 # Справедливая цена середины диапазона

            atr_val = self.calculate_atr(highs_5m, lows_5m, closes_5m, 14)
            result['atr'] = atr_val
            long_score = 0
            short_score = 0
            long_ind = {}
            short_ind = {}

            # 1. BOS/CHoCH — Переносим на 5м для точного подтверждения точки входа!
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

            # ═══ 3. EMA 50 (Тренд и защита от перегретости) ═══
            ema50 = self.calculate_ema(closes_5m, 50)
            is_near_ema = False
            if ema50:
                result['ema200'] = ema50[-1]
                # Считаем, насколько далеко цена улетела от средней линии
                ema_dist_pct = abs(current_price - ema50[-1]) / ema50[-1] * 100
                
                # ВАЖНО: Не покупаем, если цена улетела от EMA больше чем на 0.8% (ждем откат!)
                if ema_dist_pct <= 0.8:
                    is_near_ema = True

                if current_price > ema50[-1] and is_near_ema:
                    long_score += 1
                    long_ind['ema50_trend'] = True
                elif current_price < ema50[-1] and is_near_ema:
                    short_score += 1
                    short_ind['ema50_trend'] = True

            # ═══ 4. RSI (Покупаем дно отката, а не хаи) ═══
            rsi = self.calculate_rsi(closes_5m, 14)
            if rsi and len(rsi) >= 2:
                result['rsi'] = rsi[-1]
                # LONG: RSI остыл (40-62) и начинает разворот вверх. Не покупаем перегретость >65!
                if 40 <= rsi[-1] <= 62 and rsi[-1] > rsi[-2]:
                    long_score += 1
                    long_ind['rsi_momentum'] = True
                # SHORT: RSI отскочил от перепроданности (38-60) и загибается вниз
                if 38 <= rsi[-1] <= 60 and rsi[-1] < rsi[-2]:
                    short_score += 1
                    short_ind['rsi_momentum'] = True

            # 5. ADX (Фильтр флэта)
            adx = self.calculate_adx(highs_5m, lows_5m, closes_5m, 14)
            if adx:
                result['adx'] = adx[-1]
                if adx[-1] < 20:
                    logger.info(f"Пропуск {symbol}: Рынок во флэте (ADX = {adx[-1]:.1f})")
                    return result
                elif adx[-1] >= 25:
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

            # 7. Объём
            vol_sma = self.calculate_sma(volumes_5m, 20)
            if vol_sma and vol_sma[-1] > 0:
                vol_ratio = volumes_5m[-1] / vol_sma[-1]
                if vol_ratio > 1.5:
                    result['volume_ok'] = True
                    if closes_5m[-1] > closes_5m[-2]:
                        long_score += 1
                        long_ind['volume_spike'] = True
                    elif closes_5m[-1] < closes_5m[-2]:
                        short_score += 1
                        short_ind['volume_spike'] = True

            # Фильтр глобального тренда по EMA200 (1h)
            ema200_1h = self.calculate_ema([c[4] for c in ohlcv_1h], 200)
            ema200_val = ema200_1h[-1] if ema200_1h else current_price

            # --- АГРЕССИВНАЯ ФИЛЬТРАЦИЯ (МНОГО СДЕЛОК) ---
            # Убрали проверку has_smc_structure, убрали HTF-тренд и Premium/Discount матрицы
            if long_score >= short_score and long_score >= config.MIN_INDICATORS_SCORE:
                result['score'] = long_score
                result['direction'] = 'LONG'
                result['indicators'] = long_ind
                result['signal'] = True
                    
            elif short_score > long_score and short_score >= config.MIN_INDICATORS_SCORE:
                result['score'] = short_score
                result['direction'] = 'SHORT'
                result['indicators'] = short_ind
                result['signal'] = True
            else:
                result['score'] = max(long_score, short_score)
                result['direction'] = 'LONG' if long_score >= short_score else 'SHORT'
                result['indicators'] = long_ind if long_score >= short_score else short_ind

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

    def __post_init__(self):
        if self.remaining_quantity == 0.0:
            self.remaining_quantity = self.quantity


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
            self.exchange.enable_demo_trading(True) # <--- НОВАЯ ОФИЦИАЛЬНАЯ КОМАНДА 

        self.exchange.has['fetchCurrencies'] = False


        self.exchange.has['fetchCurrencies'] = False

        self.db = Database()
        self.smc_analyzer = SMCAnalyzer(self.exchange)
        self.positions: Dict[int, Position] = {}

        # --- FIX #7: блокировка от параллельных открытий по одному символу ---
        self._opening_symbols: set = set()
        self._scan_lock = asyncio.Lock()

        # Список будет заполняться динамически с биржи при подключении
        self.symbols_to_scan = []


        self.is_running = False
        self.last_scan_time = None
        self.signals_today = 0
        self.max_signals_per_day = 9999

        self.app = None
        self.active_chat_ids = set([str(self.telegram_chat_id)])
        if self.user_chat_id:
            self.active_chat_ids.add(str(self.user_chat_id))

        self._bot = Bot(token=self.telegram_token)

    async def send_telegram_message(self, text: str):
        for chat_id in list(self.active_chat_ids):
            try:
                await self._bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
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
            # 1. Загружаем все рынки с биржи
            await self.exchange.load_markets()
            
            # 2. ДИНАМИЧЕСКИЙ СБОР АКТИВНЫХ МОНЕТ
            self.symbols_to_scan = []
            for symbol in self.exchange.markets.keys():
                # Биржа отдает фьючерсы в формате BTC/USDT:USDT. Находим их:
                if ':USDT' in symbol:
                    # Отрезаем хвостик :USDT, чтобы бот работал корректно
                    clean_symbol = symbol.split(':')[0] 
                    if clean_symbol not in self.symbols_to_scan:
                        self.symbols_to_scan.append(clean_symbol)
                    
            logger.info(f"Markets loaded successfully. Динамически загружено {len(self.symbols_to_scan)} активных пар!")

            try:
                balance = await self.exchange.fetch_balance()
                logger.info(f"Подключено к Binance Futures. Баланс: {balance.get('total', {})}")
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
        """Динамически вычисляет число параллельных позиций."""
        min_slot = config.MIN_SLOT_USDT
        raw = int(virtual_equity // min_slot)
        # FIX #2: не превышаем жёсткий лимит
        return max(1, min(raw, config.MAX_CONCURRENT_POSITIONS))

    def is_daily_loss_limit_reached(self) -> bool:
        """Проверка дневного лимита убытков."""
        try:
            stats = self.db.get_daily_statistics()
            if not stats:
                return False
            daily_pct = float(stats.get('total_pnl_pct') or 0.0)
            return daily_pct <= -abs(config.MAX_DAILY_LOSS_PCT)
        except Exception:
            return False

    def is_session_loss_limit_reached(self) -> bool:
        """FIX #4: Проверка максимального стоп-лосса сессии."""
        try:
            stats = self.db.get_daily_statistics()
            if not stats:
                return False
            daily_pct = float(stats.get('total_pnl_pct') or 0.0)
            return daily_pct <= -abs(config.MAX_SESSION_LOSS_PCT)
        except Exception:
            return False

    async def check_spread(self, symbol: str) -> bool:
        """FIX #9: Проверка спреда перед входом."""
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
            return True  # не блокируем если API недоступен

    async def calculate_position_size(self, entry_price: float, score: int = 5) -> tuple:
        """Рассчитывает размер позиции на основе виртуального депозита ($50) и реального баланса Binance.
        
        Логика:
        1. Считаем виртуальный капитал = Начальный Депозит (50) + PnL всех закрытых сделок
        2. Вычитаем маржу, которая УЖЕ в открытых позициях
        3. Делим виртуальный свободный баланс на количество свободных слотов
        4. Если реальный баланс на Binance вдруг меньше нужного - ограничиваем по реальному.
        """
        try:
            # 1. Считаем виртуальный капитал
            stats = self.db.get_all_statistics()
            total_pnl = float(stats.get('total_pnl') or 0.0)
            virtual_equity = max(config.DEPOSIT + total_pnl, config.DEPOSIT * 0.5)

            # 2. Вычитаем маржу в сделках
            bot_locked_margin = sum(p.amount_usdt for p in self.positions.values())
            virtual_free = max(virtual_equity - bot_locked_margin, 0.0)

            # 3. Сколько позиций ещё можно открыть
            current_positions = len(self.positions)
            max_slots = config.MAX_CONCURRENT_POSITIONS
            remaining_slots = max(1, max_slots - current_positions)

            logger.info(
                f"POS_SIZE: VIRTUAL_EQUITY=${virtual_equity:.2f} PNL=${total_pnl:.2f} "
                f"LOCKED=${bot_locked_margin:.2f} VIRTUAL_FREE=${virtual_free:.2f} "
                f"SLOTS={current_positions}/{max_slots} REMAINING={remaining_slots} "
                f"score={score} entry={entry_price}"
            )

            if virtual_free < config.MIN_SLOT_USDT:
                logger.warning(
                    f"POS_SIZE: virtual_free(${virtual_free:.2f}) < MIN_SLOT(${config.MIN_SLOT_USDT}) — ПРОПУСК"
                )
                return 0, 0, 0

            # === ПРОЦЕНТ НА ПОЗИЦИЮ ===
            amount_per_slot = virtual_free / remaining_slots
            weight = min(max(score, config.MIN_INDICATORS_SCORE) / 5.0, 1.5)
            amount_usdt = amount_per_slot * weight

            # Лимиты: не более 30% от виртуального капитала
            max_single_position = virtual_equity * 0.30
            amount_usdt = min(amount_usdt, virtual_free, max_single_position)

            if amount_usdt < config.MIN_SLOT_USDT:
                if virtual_free >= config.MIN_SLOT_USDT:
                    amount_usdt = config.MIN_SLOT_USDT
                else:
                    logger.warning(f"POS_SIZE: amount_usdt=${amount_usdt:.2f} < MIN_SLOT — ПРОПУСК")
                    return 0, 0, 0

            # 4. ФИНАЛЬНАЯ ПРОВЕРКА РЕАЛЬНОГО БАЛАНСА
            try:
                balance = await self.exchange.fetch_balance()
                real_free = float(balance.get('USDT', {}).get('free', 0) or 0)
                if amount_usdt > real_free:
                    logger.warning(f"POS_SIZE: Реальных средств (${real_free:.2f}) меньше нужных (${amount_usdt:.2f})! Ограничиваем.")
                    amount_usdt = real_free
            except Exception as bal_err:
                logger.warning(f"POS_SIZE: Не удалось проверить реальный баланс: {bal_err}")

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
                            smc_result: Dict) -> Optional[Position]:
        """
        Открытие позиции с корректным подтверждением исполнения.
        FIX #1: ждём filled, используем avg_filled_price для SL/TP.
        FIX #2: жёсткий лимит MAX_CONCURRENT_POSITIONS.
        FIX #7: блокировка от параллельных открытий по одному символу.
        FIX #8: комиссия учтена в логировании.
        FIX #9: проверка спреда.
        """
        global ALLOW_TRADING

        # Базовые блокировки
        if not ALLOW_TRADING:
            logger.info(f"Сигнал {symbol} — торговля приостановлена (Fear & Greed)")
            return None

        if self.is_session_loss_limit_reached():
            logger.warning(f"MAX_SESSION_LOSS достигнут — все сделки заблокированы")
            return None

        if self.is_daily_loss_limit_reached():
            logger.warning(f"Дневной лимит убытков достигнут — новые сделки заблокированы")
            return None

        # FIX #2: жёсткий лимит числа позиций
        if len(self.positions) >= config.MAX_CONCURRENT_POSITIONS:
            logger.info(f"Лимит позиций {config.MAX_CONCURRENT_POSITIONS} достигнут — пропуск {symbol}")
            return None

        # FIX #7: блокировка дублей по символу
        if symbol in self._opening_symbols:
            logger.info(f"Открытие {symbol} уже в процессе — пропуск")
            return None

        # FIX #9: проверка спреда
        if not await self.check_spread(symbol):
            return None

        self._opening_symbols.add(symbol)

        try:
            direction = smc_result.get('direction', 'LONG')
            market_info = self.exchange.market(symbol)
            min_notional = float(market_info.get('limits', {}).get('cost', {}).get('min', 5))

            quantity, margin, actual_amount = await self.calculate_position_size(
                entry_price, score=smc_result['score']
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

            # Установка плеча и маржи
            try:
                await self.exchange.set_margin_mode('cross', symbol)
            except Exception:
                pass

            actual_leverage = config.LEVERAGE
            try:
                await self.exchange.set_leverage(actual_leverage, symbol)
            except Exception as lev_err:
                err_str = str(lev_err)
                if '-2015' in err_str or 'Invalid API-key' in err_str or 'permissions' in err_str:
                    raise
                for fallback_lev in [50, 20]:
                    try:
                        await self.exchange.set_leverage(fallback_lev, symbol)
                        actual_leverage = fallback_lev
                        break
                    except Exception:
                        continue

            # Открытие рыночного ордера
            try:
                if direction == 'SHORT':
                    order = await self.exchange.create_market_sell_order(symbol, quantity)
                else:
                    order = await self.exchange.create_market_buy_order(symbol, quantity)
            except Exception as e:
                err_str = str(e)
                if '-2015' in err_str or 'Invalid API-key' in err_str:
                    raise
                if '-2027' in err_str or 'Exceeded' in err_str:
                    for fallback_lev in [50, 20]:
                        try:
                            await self.exchange.set_leverage(fallback_lev, symbol)
                            actual_leverage = fallback_lev
                            if direction == 'SHORT':
                                order = await self.exchange.create_market_sell_order(symbol, quantity)
                            else:
                                order = await self.exchange.create_market_buy_order(symbol, quantity)
                            break
                        except Exception:
                            continue
                    else:
                        raise e
                else:
                    raise e

            # --- FIX #1: используем реальный filled price ---
            actual_entry = float(order.get('average') or order.get('price') or entry_price)
            actual_qty = float(order.get('filled') or quantity)

            # FIX #1: обработка partial fill — если заполнено < FILL_THRESHOLD → отмена
            if actual_qty < quantity * config.FILL_THRESHOLD:
                logger.warning(
                    f"Partial fill {symbol}: заполнено {actual_qty:.4f} из {quantity:.4f} "
                    f"({actual_qty / quantity * 100:.1f}%) — позиция не открывается"
                )
                # Пробуем закрыть то что заполнилось
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

            # FIX #8: логирование с учётом комиссии
            estimated_fee = actual_qty * actual_entry * config.TAKER_FEE * 2  # вход + выход
            actual_margin = (actual_qty * actual_entry) / actual_leverage
            
            logger.info(
                f"ORDER FILLED: {symbol} {direction} | "
                f"expected_entry={entry_price:.6f} real_entry={actual_entry:.6f} | "
                f"filled={actual_qty:.4f} | margin=${actual_margin:.2f} | "
                f"est_fee=${estimated_fee:.4f}"
            )

            # --- ПОЛНОСТЬЮ ДИНАМИЧЕСКИЕ ЦЕЛИ (НА ОСНОВЕ ВОЛАТИЛЬНОСТИ МОНЕТЫ) ---
            atr = smc_result.get('atr', 0)
            
            # Единственное ограничение - защита от принудительной ликвидации биржей.
            # При 50-м плече биржа сжигает сделку при движении цены на ~1.8%.
            # Ставим хард-лимит на 1.5%, чтобы бот закрывал сделку сам, а не биржа.
            max_safe_dist = actual_entry * 0.015 

            if atr > 0:
                # Бот САМ решает: ставит стоп за пределами рыночного шума (2.5 ATR)
                sl_dist = atr * 2.5
                
                # Если монета совсем бешеная, ограничиваем стоп защитой от ликвидации
                if sl_dist > max_safe_dist:
                    sl_dist = max_safe_dist
                    
                # Тейк-профиты тоже подстраиваются под размах конкретной монеты
                tp1_dist = sl_dist * 1.5  # Забираем первую прибыль
                tp3_dist = sl_dist * 3.5  # Оставляем раннер на сильное движение
            else:
                sl_dist = actual_entry * (config.STOP_LOSS_PCT / 100)
                tp1_dist = actual_entry * (config.TAKE_PROFIT_PCT / 100)
                tp3_dist = actual_entry * (config.TP3_PCT / 100)


            if direction == 'SHORT':
                actual_sl = float(self.exchange.price_to_precision(symbol, actual_entry + sl_dist))
                actual_tp = float(self.exchange.price_to_precision(symbol, actual_entry - tp3_dist))
                close_side = 'BUY'
                tp1_price = float(self.exchange.price_to_precision(symbol, actual_entry - tp1_dist))
            else:
                actual_sl = float(self.exchange.price_to_precision(symbol, actual_entry - sl_dist))
                actual_tp = float(self.exchange.price_to_precision(symbol, actual_entry + tp3_dist))
                close_side = 'SELL'
                tp1_price = float(self.exchange.price_to_precision(symbol, actual_entry + tp1_dist))
                

            # Выставляем SL
            try:
                await self.exchange.create_order(
                    symbol=symbol, type='STOP_MARKET', side=close_side,
                    amount=actual_qty,
                    params={'stopPrice': actual_sl, 'reduceOnly': True}
                )
            except Exception as e:
                logger.warning(f"⚠️ SL не выставлен для {symbol}: {e}")

            # Выставляем TP
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
                take_profit=tp1_price,  # <--- ДОБАВИТЬ ЭТУ СТРОКУ (ОБЯЗАТЕЛЬНО С ЗАПЯТОЙ!)
                amount_usdt=actual_margin,
                leverage=actual_leverage,
                quantity=actual_qty,
                remaining_quantity=actual_qty,
                timestamp=datetime.now(timezone.utc),
                realized_pnl_usd=-(actual_qty * actual_entry * config.TAKER_FEE),
            )

            self.positions[position_id] = position

            score = smc_result['score']
            quality = "★★★ СИЛЬНЫЙ" if score >= 6 else ("★★☆ ХОРОШИЙ" if score >= 5 else "★☆☆ СРЕДНИЙ")
            rr = config.TP3_PCT / config.STOP_LOSS_PCT if config.STOP_LOSS_PCT > 0 else 0

            message = (
                f"✅ ПОЗИЦИЯ ОТКРЫТА\n"
                f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
                f"{dir_emoji} | #{symbol.replace('/', '')}\n"
                f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n\n"
                f"🛒 Вход:   {actual_entry:.5f}\n"
                f"💰 Вложено: ${margin:.2f}\n"
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
            # FIX #7: снимаем блокировку символа в любом случае
            self._opening_symbols.discard(symbol)

    async def _handle_already_closed_position(self, position_id: int, position: Position, margin: float):
        symbol = position.symbol
        logger.info(f"Позиция {symbol} закрыта на Binance (по SL/TP или вручную)")
        realized_pnl = 0.0
        exit_price = position.entry_price
        try:
            trades = await self.exchange.fetch_my_trades(symbol, limit=20)
            if trades:
                close_pnl = 0.0
                found_close = False
                for t in reversed(trades):
                    info = t.get('info', {})
                    pnl_str = info.get('realizedPnl', '0')
                    pnl_val = float(pnl_str)
                    
                    if abs(pnl_val) > 0:
                        close_pnl += pnl_val
                        found_close = True
                        exit_price = float(t.get('price', exit_price))
                    elif found_close:
                        break 
                
                if found_close:
                    realized_pnl = close_pnl - position.realized_pnl_usd
                else:
                    qty = position.remaining_quantity
                    if position.side == 'SHORT':
                        realized_pnl = (position.entry_price - exit_price) * qty
                    else:
                        realized_pnl = (exit_price - position.entry_price) * qty
                    fee = qty * exit_price * config.TAKER_FEE
                    realized_pnl -= fee

                total_pnl = position.realized_pnl_usd + realized_pnl
                pnl_pct = (total_pnl / margin) * 100 if margin > 0 else 0.0

                self.db.update_position(position_id, exit_price, total_pnl, pnl_pct)
                self.db.update_daily_statistics(
                    total_pnl, pnl_pct,
                    count_as_trade=True,
                    equity_reference=config.DEPOSIT
                )

        except Exception as e:
            logger.warning(f'Ошибка получения истории сделок: {e}')
            total_pnl = position.realized_pnl_usd + realized_pnl
            pnl_pct = 0.0

        emoji = "✅" if realized_pnl >= 0 else "❌"
        await self.send_telegram_message(
            f"{emoji} БИРЖА ЗАКРЫЛА ПОЗИЦИЮ (SL/TP) | #{symbol.replace('/USDT', '')}\n"
            f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
            f"🛒 Вход:  {position.entry_price:.5f}\n"
            f"🏁 Выход: {exit_price:.5f}\n"
            f"💰 Вложено: ${margin:.2f}\n"
            f"📈 REAL PnL: {'+' if realized_pnl >= 0 else ''}${realized_pnl:.2f}\n"
            f"📊 REAL ROE: {'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}%\n"
            f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
            f"SMART MONEY 1 BOT"
        )

        if position_id in self.positions:
            del self.positions[position_id]
        return True

    async def close_position(self, position_id: int, emergency: bool = False) -> bool:
        """Закрытие позиции с синхронизацией с биржей."""
        try:
            if position_id not in self.positions:
                logger.warning(f"Позиция {position_id} не найдена")
                return False

            position = self.positions[position_id]
            symbol = position.symbol
            qty_close = position.remaining_quantity if position.remaining_quantity > 0 else position.quantity

            if qty_close <= 0:
                logger.warning(f"Нечего закрывать по позиции {position_id}")
                return False

            # Синхронизация с Binance
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
                    return await self._handle_already_closed_position(position_id, position, margin)

                qty_close = min(qty_close, real_contracts)

            except Exception as sync_error:
                logger.warning(f"Ошибка синхронизации позиции: {sync_error}")

            # Закрытие
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
                    return await self._handle_already_closed_position(position_id, position, margin)
                raise

            # Отмена оставшихся ордеров (SL/TP)
            try:
                await self.exchange.cancel_all_orders(symbol)
            except Exception as cancel_e:
                logger.warning(f"Не удалось отменить ордера для {symbol}: {cancel_e}")

            # --- FIX #1: используем реальный exit price из ордера ---
            exit_price = order.get('average') or order.get('price')
            if not exit_price:
                try:
                    ticker = await self.exchange.fetch_ticker(symbol)
                    exit_price = ticker['last']
                except Exception:
                    exit_price = position.entry_price
            exit_price = float(exit_price)

            if position.side == 'SHORT':
                leg_pnl = (position.entry_price - exit_price) * qty_close
            else:
                leg_pnl = (exit_price - position.entry_price) * qty_close

            # Вычитаем комиссию из PnL (работает для LONG и SHORT)
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

            del self.positions[position_id]

            duration = datetime.now(timezone.utc) - position.timestamp
            emoji = "✅" if total_pnl >= 0 else "❌"
            result_text = "ПРИБЫЛЬ" if total_pnl >= 0 else "УБЫТОК"
            hours = int(duration.total_seconds() // 3600)
            minutes = int((duration.total_seconds() % 3600) // 60)

            message = (
                f"{emoji} ПОЗИЦИЯ ЗАКРЫТА | {result_text}\n"
                f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
                f"📍 Монета: #{position.symbol.replace('/', '')}\n"
                f"🛒 Вход:  {position.entry_price:.5f}\n"
                f"🏁 Выход: {exit_price:.5f}\n"
                f"💰 Вложено: ${margin:.2f}\n"
                f"📈 REAL PnL: {'+' if total_pnl >= 0 else ''}${total_pnl:.2f} | "
                f"REAL ROE: {'+' if total_pnl >= 0 else ''}{pnl_pct:.1f}%\n"
                f"💸 Комиссия выхода: ~${fee:.4f}\n"
                f"💼 Итог с баланса: ${margin + total_pnl:.2f}\n"
                f"⏱ Время в сделке: {hours}ч {minutes}мин\n"
                f"🔧 Плечо: x{position.leverage}\n"
                f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
                f"SMART MONEY 1 BOT"
            )

            if emergency:
                message = f"🚨 ЭКСТРЕННОЕ ЗАКРЫТИЕ\n{message}"

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

    async def close_partial_position(self, position: Position, qty_to_close: float, current_price: float):
        """Частичное закрытие — только накапливает realized_pnl_usd в памяти.
        В БД НЕ пишем — это сделает финальный close_position."""
        try:
            qty_to_close = float(self.exchange.amount_to_precision(position.symbol, qty_to_close))
            if qty_to_close <= 0:
                return

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

            # Только накапливаем в памяти — НЕ пишем в БД
            position.realized_pnl_usd += chunk_pnl
            position.remaining_quantity = max(0.0, position.remaining_quantity - qty_to_close)

            logger.info(
                f"Частичное закрытие {position.symbol}: "
                f"{qty_to_close} @ {executed_price:.5f}, "
                f"chunk_pnl=${chunk_pnl:.4f}, fee=${fee:.4f}, "
                f"накоплено realized=${position.realized_pnl_usd:.4f}, "
                f"остаток {position.remaining_quantity:.4f}"
            )

        except Exception as e:
            logger.error(f"Ошибка частичного закрытия {position.symbol}: {e}")



    async def _update_exchange_sl(self, position: Position, new_sl_price: float):
        """Обновляет стоп-лосс на бирже."""
        try:
            new_sl_price = float(self.exchange.price_to_precision(position.symbol, new_sl_price))
            qty_rounded = float(self.exchange.amount_to_precision(position.symbol, position.remaining_quantity))
            if qty_rounded <= 0:
                return

            try:
                await self.exchange.cancel_all_orders(position.symbol)
            except Exception:
                pass

            close_side = 'BUY' if position.side == 'SHORT' else 'SELL'
            await self.exchange.create_order(
                symbol=position.symbol,
                type='STOP_MARKET',
                side=close_side,
                amount=qty_rounded,
                params={'stopPrice': new_sl_price, 'reduceOnly': True}
            )
            position.stop_loss = new_sl_price
            logger.info(f"SL обновлён {position.symbol}: → {new_sl_price}")
        except Exception as e:
            logger.error(f"Ошибка переноса SL {position.symbol}: {e}")

    def calculate_position_roe(self, position: Position, current_price: float) -> float:
        """Корректный расчёт ROE."""
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

    def get_position_age_minutes(self, position: Position) -> float:
        return (datetime.now(timezone.utc) - position.timestamp).total_seconds() / 60

    async def monitor_positions(self):
        """Мониторинг позиций — Трейлинг, Частичные TP, Динамический SL."""
        for position_id, position in list(self.positions.items()):
            try:
                ticker = await self.exchange.fetch_ticker(position.symbol)
                current_price = ticker['last']

                pnl_pct = self.calculate_position_roe(position, current_price)


 

                if position.side == 'SHORT':
                    price_change_pct = (
                        (position.entry_price - current_price)
                        / max(position.entry_price, 1e-9)
                    ) * 100
                    pnl_usd = position.realized_pnl_usd + (position.entry_price - current_price) * position.remaining_quantity
                else:
                    price_change_pct = (
                        (current_price - position.entry_price)
                        / max(position.entry_price, 1e-9)
                    ) * 100
                    pnl_usd = position.realized_pnl_usd + (current_price - position.entry_price) * position.remaining_quantity

                # Обновление пика
                if isinstance(pnl_pct, (int, float)) and pnl_pct > position.peak_pnl:
                    position.peak_pnl = pnl_pct

                pair = position.symbol.replace('/USDT', '')

                # Программный STOP LOSS (Синхронизирован с умным ATR)
                is_sl_hit = False
                if position.side == 'LONG' and current_price <= position.stop_loss:
                    is_sl_hit = True
                elif position.side == 'SHORT' and current_price >= position.stop_loss:
                    is_sl_hit = True

                if is_sl_hit:
                    message = (
                        f"❌ STOP LOSS | {pair}\n"
                        f"Убыток по ROE: {pnl_pct:+.1f}%\n"
                        f"💰 Вложено: ${position.amount_usdt:.2f}\n"
                        f"Текущий PnL: {'+' if pnl_usd >= 0 else ''}${pnl_usd:.2f}"
                    )
                    await self.send_telegram_message(message)
                    await self.close_position(position_id)
                    continue

 

                # Trailing Stop
                if pnl_pct >= config.TRAILING_ACTIVATE_PCT and not position.trailing_active:
                    position.trailing_active = True
                    position.trailing_peak = pnl_pct
                    logger.info(f"Трейлинг активирован для {position.symbol} на {pnl_pct:.1f}%")

                if position.trailing_active:
                    if pnl_pct > position.trailing_peak:
                        position.trailing_peak = pnl_pct

                    trailing_drawdown = position.trailing_peak - pnl_pct
                    if trailing_drawdown >= config.TRAILING_DRAWDOWN_CLOSE_PCT:
                        message = (
                            f"🛡 TRAILING STOP | {pair}\n"
                            f"Пик: +{position.trailing_peak:.1f}%\n"
                            f"Откат: {trailing_drawdown:.1f}%\n"
                            f"Фактический ROE: {pnl_pct:+.1f}%\n"
                            f"PnL: {'+' if pnl_usd >= 0 else ''}${pnl_usd:.2f}"
                        )
                        await self.send_telegram_message(message)
                        await self.close_position(position_id)
                        continue

                # УМНАЯ ЧАСТИЧНАЯ ФИКСАЦИЯ TP1 (По графику ATR)
                is_tp1_hit = False
                if position.side == 'LONG' and current_price >= position.take_profit:
                    is_tp1_hit = True
                elif position.side == 'SHORT' and current_price <= position.take_profit:
                    is_tp1_hit = True

                if is_tp1_hit and not position.partial_tp1_done:
                    position.partial_tp1_done = True
                    await self.close_partial_position(position, position.quantity * 0.40, current_price)
                    new_sl = (position.entry_price * (1 - 0.001)
                              if position.side == 'SHORT'
                              else position.entry_price * (1 + 0.001))
                    await self._update_exchange_sl(position, new_sl)
                    await self.send_telegram_message(
                        f"💰 ЧАСТИЧНАЯ ФИКСАЦИЯ TP1 (ATR) | {pair}\n"
                        f"Достигнута цель по волатильности! Закрыто 40% | SL → безубыток"
                    )

                if pnl_pct >= config.PARTIAL_TP2_PCT and not position.partial_tp2_done:
                    position.partial_tp2_done = True
                    await self.close_partial_position(position, position.remaining_quantity * 0.50, current_price)
                    new_sl = (position.entry_price * (1 - 0.009)
                              if position.side == 'SHORT'
                              else position.entry_price * (1 + 0.009))
                    await self._update_exchange_sl(position, new_sl)
                    await self.send_telegram_message(
                        f"🚀 TP2 | {pair}\n+{config.PARTIAL_TP2_PCT:.0f}% ROE — закрыто ещё 30% | SL → +40% ROE"
                    )

                if pnl_pct >= config.PARTIAL_TP3_PCT and not position.partial_tp3_done:
                    position.partial_tp3_done = True
                    runner_qty = position.remaining_quantity * 0.10
                    close_qty = position.remaining_quantity - runner_qty
                    if close_qty > 0:
                        await self.close_partial_position(position, close_qty, current_price)
                    new_sl = (position.entry_price * (1 - 0.018)
                              if position.side == 'SHORT'
                              else position.entry_price * (1 + 0.018))
                    await self._update_exchange_sl(position, new_sl)
                    await self.send_telegram_message(
                        f"💎 TP3 +{config.PARTIAL_TP3_PCT:.0f}% ROE | {pair}\n"
                        f"Закрыто 90% позиции!\n"
                        f"🎯 Оставлен раннер 10% с трейлинг-стопом"
                    )
                    position.trailing_active = True
                    position.trailing_peak = pnl_pct
                    continue



            except Exception as e:
                logger.error(f"Ошибка мониторинга {position_id}: {e}")

    async def check_position_timeout(self, position: Position):
        """Проверка слабых зависших позиций."""
        try:
            now = datetime.now(timezone.utc)
            duration_minutes = (now - position.timestamp).total_seconds() / 60

            if duration_minutes < config.MOMENTUM_EXIT_MINUTES:
                return

            ticker = await self.exchange.fetch_ticker(position.symbol)
            current_price = ticker['last']
            pnl_pct = self.calculate_position_roe(position, current_price)

            if pnl_pct >= config.MOMENTUM_MIN_PROFIT:
                return

            logger.info(
                f"MOMENTUM EXIT: {position.symbol} | "
                f"{duration_minutes:.1f}m | PNL={pnl_pct:.2f}%"
            )
            await self.send_telegram_message(
                f"⚠️ Weak momentum exit: {position.symbol}\n"
                f"Возраст: {duration_minutes:.0f} мин\nPNL: {pnl_pct:.2f}%"
            )
            await self.close_position(position.id)

        except Exception as e:
            logger.error(f"Ошибка timeout-проверки: {e}")

    async def scan_market(self):
        """
        Сканирование рынка.
        FIX #7: используем _scan_lock чтобы один скан не запускался поверх другого.
        """
        if not self.is_running:
            return

        # FIX #7: не запускаем параллельно
        if self._scan_lock.locked():
            logger.debug("Предыдущее сканирование ещё не завершено — пропуск")
            return

        # === ЖЕСТКОЕ РАСПИСАНИЕ ТОРГОВ (08:00 - 22:00 по твоему времени) ===
        # Сервер Render работает по UTC. 
        # 05:00 UTC = 08:00 утра по твоему времени (Старт)
        # 19:00 UTC = 22:00 вечера по твоему времени (Стоп)
        current_hour_utc = datetime.now(timezone.utc).hour
        
        if current_hour_utc < 5 or current_hour_utc >= 19:
            return
        # ===================================================================

        async with self._scan_lock:
            if self.signals_today >= self.max_signals_per_day:
                logger.info(f"Лимит сигналов исчерпан: {self.signals_today}")
                return




            # FIX #4: проверка session loss перед сканом
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

                # FIX #2: проверяем лимит позиций
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

                    # Фильтр Funding Rate
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

                # Бот отдыхает треть секунды перед следующей монетой
                await asyncio.sleep(0.3)

            self.last_scan_time = datetime.now(timezone.utc)
            logger.info("Сканирование завершено")

    async def run_scanner_loop(self):
        while self.is_running:
            try:
                await self.scan_market()
                await asyncio.sleep(60)
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
            if not stats:
                return

            all_stats = self.db.get_all_statistics()
            avg_daily = all_stats.get('avg_daily_pct', 0) or 0
            deposit = config.DEPOSIT
            total_pnl = all_stats.get('total_pnl', 0) or 0
            current_balance = deposit + total_pnl
            pnl_sign = '+' if total_pnl >= 0 else ''
            pnl_pct_total = (total_pnl / deposit * 100) if deposit > 0 else 0

            message = (
                f"📊 ДНЕВНОЙ ОТЧЕТ\n"
                f"Дата: {datetime.now(timezone.utc).strftime('%d.%m.%Y')}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"💰 БАЛАНС:\n"
                f"  Начальный депозит: ${deposit:.2f}\n"
                f"  Текущий баланс: ${current_balance:.2f}\n"
                f"  PnL: {pnl_sign}${total_pnl:.2f} ({pnl_sign}{pnl_pct_total:.1f}%)\n\n"
                f"📋 СТАТИСТИКА ЗА ДЕНЬ:\n"
                f"  Сделок: {stats.get('total_trades', 0)}\n"
                f"  Прибыльных: {stats.get('profitable_trades', 0)}\n"
                f"  Убыточных: {stats.get('losing_trades', 0)}\n"
                f"  PnL: {pnl_sign}${stats.get('total_pnl', 0):.2f} "
                f"({'+' if stats.get('total_pnl_pct', 0) >= 0 else ''}{stats.get('total_pnl_pct', 0):.1f}%)\n\n"
                f"📈 ОБЩАЯ СТАТИСТИКА:\n"
                f"  Всего сделок: {all_stats.get('total_trades', 0)}\n"
                f"  Win Rate: {(all_stats.get('profitable', 0) / max(all_stats.get('total_trades', 1), 1) * 100):.1f}%\n"
                f"  Лучшая сделка: ${all_stats.get('best_trade', 0):.2f}\n"
                f"  Худшая сделка: ${all_stats.get('worst_trade', 0):.2f}\n"
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
                        pnl = pos.realized_pnl_usd + (pos.entry_price - current_price) * rem
                        pnl_pct = ((pos.entry_price - current_price) / pos.entry_price) * 100 * pos.leverage
                    else:
                        pnl = pos.realized_pnl_usd + (current_price - pos.entry_price) * rem
                        pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100 * pos.leverage
                    total_open_pnl += pnl

                    emoji = "🟢" if pnl >= 0 else "🔴"
                    positions_info += (
                        f"  {emoji} {pos.symbol.replace('/USDT', '')}: "
                        f"{'+' if pnl >= 0 else ''}${pnl:.2f} "
                        f"({'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}%)\n"
                    )
                except Exception:
                    positions_info += f"  ⚪ {pos.symbol}: данные недоступны\n"

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
            f"🤖 Smart Money Aggressive Bot v2.1\n\n"
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
                if pos.side == 'SHORT':
                    pnl = pos.realized_pnl_usd + (pos.entry_price - current_price) * rem
                    pnl_pct = ((pos.entry_price - current_price) / pos.entry_price) * 100 * pos.leverage
                else:
                    pnl = pos.realized_pnl_usd + (current_price - pos.entry_price) * rem
                    pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100 * pos.leverage

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
                        if pos.side == 'SHORT':
                            roe = ((pos.entry_price - current_price) / pos.entry_price) * 100 * pos.leverage
                            pos_pnl = pos.realized_pnl_usd + (pos.entry_price - current_price) * pos.remaining_quantity
                        else:
                            roe = ((current_price - pos.entry_price) / pos.entry_price) * 100 * pos.leverage
                            pos_pnl = pos.realized_pnl_usd + (current_price - pos.entry_price) * pos.remaining_quantity
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
                f"💰 БАЛАНС\n"
                f"Всего: ${usdt_total:.2f}\n"
                f"Свободно: ${usdt_free:.2f}\n"
                f"В позициях: ${usdt_used:.2f}\n\n"
                f"📈 PnL\n"
                f"Общий: {'+' if total_pnl >= 0 else ''}${total_pnl:.2f} ({pnl_pct:+.1f}%)\n"
                f"Сегодня: {'+' if today_pnl >= 0 else ''}${today_pnl:.2f}\n"
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
        """Запуск Telegram бота с авто-перезапуском и защитой от Conflict."""
        app = None
        MAX_CONFLICT_RETRIES = 5
        conflict_count = 0

        while self.is_running:
            try:
                logger.info("Инициализация Telegram бота...")

                # === ПРИНУДИТЕЛЬНАЯ ОЧИСТКА ВЕБХУКА И СТАРЫХ СЕССИЙ ===
                # Это решает проблему Conflict, когда другой экземпляр бота
                # (или предыдущий незавершённый) держит polling
                try:
                    temp_bot = Bot(token=self.telegram_token)
                    await temp_bot.delete_webhook(drop_pending_updates=True)
                    # Ждём чтобы Telegram серверы отпустили polling
                    await asyncio.sleep(2)
                    logger.info("Webhook и pending updates очищены")
                except Exception as cleanup_err:
                    logger.warning(f"Ошибка очистки webhook: {cleanup_err}")

                app = (
                    Application.builder()
                    .token(self.telegram_token)
                    .connect_timeout(30)
                    .read_timeout(30)
                    .pool_timeout(30)
                    .build()
                )

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
                await app.initialize()
                await app.start()
                await app.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True
                )

                # Если дошли сюда — polling работает, сбрасываем счётчик
                conflict_count = 0
                logger.info("✅ Telegram polling запущен успешно!")

                while self.is_running:
                    await asyncio.sleep(5)

            except telegram.error.Conflict as e:
                conflict_count += 1
                wait_time = min(30 * conflict_count, 120)  # экспоненциальный бэкофф до 2 мин
                logger.warning(
                    f"TG Conflict ({conflict_count}/{MAX_CONFLICT_RETRIES}): {e}. "
                    f"Повтор через {wait_time} сек..."
                )
                if conflict_count >= MAX_CONFLICT_RETRIES:
                    logger.error(
                        "Слишком много Conflict ошибок! Вероятно запущен другой экземпляр бота. "
                        "Telegram команды будут недоступны до перезапуска."
                    )
                    # Не убиваем бот, просто ждём дольше
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
        logger.info("Запуск Smart Money Aggressive Bot v2.1...")

        if not await self.connect():
            logger.error("Не удалось подключиться к бирже")
            try:
                await self.disconnect()
            except Exception:
                pass
            return False

        self.is_running = True
        self.is_running = True

        # === ВОССТАНОВЛЕНИЕ ПАМЯТИ НАПРЯМУЮ С БИРЖИ BINANCE ===
        try:
            logger.info("Синхронизация открытых позиций с Binance...")
            exchange_positions = await self.exchange.fetch_positions()
            
            restored_count = 0
            for ep in exchange_positions:
                contracts = float(ep.get('contracts', 0) or 0)
                if abs(contracts) > 0:  # Если позиция реально открыта
                    symbol = ep['symbol']
                    side = 'LONG' if ep.get('side') == 'long' else 'SHORT'
                    entry_price = float(ep.get('entryPrice', 0))
                    leverage = int(ep.get('leverage', config.LEVERAGE))
                    
                    # Восстанавливаем вложенную маржу
                    amount_usdt = (abs(contracts) * entry_price) / leverage
                    
                    # Ищем на бирже стоп-лоссы и тейк-профиты для этой монеты
                    open_orders = await self.exchange.fetch_open_orders(symbol)
                    sl_price = entry_price * (0.5 if side == 'LONG' else 1.5) # Дефолт (далеко)
                    tp_price = entry_price * (1.5 if side == 'LONG' else 0.5)
                    
                    for ord in open_orders:
                        o_type = ord.get('type', '').lower()
                        if 'stop' in o_type:
                            sl_price = float(ord.get('stopPrice') or ord.get('price') or sl_price)
                        elif 'take_profit' in o_type:
                            tp_price = float(ord.get('stopPrice') or ord.get('price') or tp_price)

                    # Собираем позицию заново в память бота
                    pos = Position(
                        id=int(datetime.now().timestamp() * 1000) + restored_count, # Уникальный ID
                        symbol=symbol,
                        side=side,
                        entry_price=entry_price,
                        stop_loss=sl_price,
                        take_profit=tp_price,
                        amount_usdt=amount_usdt,
                        leverage=leverage,
                        quantity=abs(contracts),
                        remaining_quantity=abs(contracts),
                        timestamp=datetime.now(timezone.utc),
                        realized_pnl_usd=float(ep.get('realizedPnl', 0))
                    )
                    self.positions[pos.id] = pos
                    restored_count += 1
                    
            logger.info(f"🔄 Синхронизировано с биржей: {restored_count} позиций. Лимит: {config.MAX_CONCURRENT_POSITIONS}")
        except Exception as e:
            logger.error(f"Ошибка синхронизации с биржей: {e}")
        # ==========================================================


        async def task_with_log(name, coro):
            try:
                await coro
            except Exception as e:
                logger.error(f"Task '{name}' finished with error: {e}")
            finally:
                logger.warning(f"Task '{name}' finished!")

        try:
            stats = self.db.get_all_statistics()
            total_pnl = float(stats.get('total_pnl') or 0.0)
            virtual_eq = max(config.DEPOSIT + total_pnl, config.DEPOSIT * 0.5)
            await self.send_telegram_message(
                f"🟢 БОТ ВКЛЮЧЁН v2.1\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 Депозит: ${config.DEPOSIT:.2f}\n"
                f"📊 Виртуальный баланс: ${virtual_eq:.2f}\n"
                f"⚙️ Плечо: x{config.LEVERAGE}\n"
                f"🛡 SL: {config.STOP_LOSS_PCT}% ({config.STOP_LOSS_PCT * config.LEVERAGE:.0f}% ROE)\n"
                f"🎯 TP: {config.PARTIAL_TP1_PCT:.0f}% / {config.PARTIAL_TP2_PCT:.0f}% / {config.PARTIAL_TP3_PCT:.0f}% ROE\n"
                f"📡 Монет: {len(self.symbols_to_scan)}\n"
                f"🔒 Макс. позиций: {config.MAX_CONCURRENT_POSITIONS}\n"
                f"🚨 Стоп сессии: -{config.MAX_SESSION_LOSS_PCT}%\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"SMART MONEY BOT v2.1"
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить стартовое сообщение: {e}")

        # === ВОТ ЭТОТ БЛОК НУЖНО ВЕРНУТЬ ===
        tasks = [
            asyncio.create_task(task_with_log("scanner", self.run_scanner_loop())),
            asyncio.create_task(task_with_log("monitoring", self.run_monitoring_loop())),
            asyncio.create_task(task_with_log("daily_report", self.run_daily_report_loop())),
            asyncio.create_task(task_with_log("hourly_report", self.run_hourly_report_loop())),
            asyncio.create_task(task_with_log("telegram", self.run_telegram_bot()))
        ]
        # ===================================

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
    """Фейковый веб-сервер для того, чтобы Render Web Service не убивал бота (обход Timed Out)"""
    port = int(os.getenv("PORT", 10000))
    class SimpleHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is alive and trading!")
        # Отключаем спам в логи от постоянных проверок Render
        def log_message(self, format, *args):
            pass
            
    try:
        server = HTTPServer(('0.0.0.0', port), SimpleHandler)
        server.serve_forever()
    except Exception as e:
        pass

async def main():
    # Запуск сервера-заглушки для Render в отдельном потоке
    threading.Thread(target=run_dummy_server, daemon=True).start()

    # === ЖЕСТКИЕ НАСТРОЙКИ (Без переменных окружения Render) ===
    
    # 1. Твои ключи от ДЕМО аккаунта Binance (Demo Trading / Testnet)
    API_KEY = 'WILLvD57TbxmrqThprlaVe3ZjzxXt3pkR6zsVqTiJOAg1Iy2hKMa7Jbiu6Y0nCFm'
    API_SECRET = 'MqpeOJl0QxSzBV0OKvLYLZU34FtrkmNEtgQfZHeuCP8etYJZBxxOur3w2OUIGKSC'
    
    # 2. Твои данные Телеграм
    TELEGRAM_TOKEN = '7752692912:AAEcK1B0vtzEqAGbO-L9EQrN_0U4hzS8dwQ'
    TELEGRAM_CHAT_ID = '-1003325030622'
    USER_CHAT_ID = '259909392'

    # Загрузка параметров
    config.DEPOSIT = 50.0
    config.ENTRY_AMOUNT = 50.0
    config.LEVERAGE = 75
    config.STOP_LOSS_PCT = 1.0
    config.REINVEST_PROFITS = True
    config.DRAWDOWN_ALERT = 12.0
    config.MAX_CONCURRENT_POSITIONS = 4
    config.MAX_SESSION_LOSS_PCT = 30.0
    config.FILL_THRESHOLD = 0.90
    
    # 3. СТРОГО True, так как эти ключи работают только в тестовой сети!
    use_testnet = True  

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