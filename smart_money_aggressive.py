#!/usr/bin/env python3
"""
Smart Money Aggressive Trading Bot
Точная копия стратегии SMART MONEY 1

КРИТИЧЕСКИ ВАЖНЫЕ ПАРАМЕТРЫ:
- DEPOSIT = 50 USDT (стартовый)
- Сумма входа = ДИНАМИЧЕСКАЯ (свободный капитал / доступные слоты)
- Число слотов = floor(equity / MIN_SLOT_USDT), минимум 1, без потолка
- STOP_LOSS = 3.5% (фиксированный от входа)
- Частичные TP / трейлинг / откат от пика — пороги в StrategyConfig (см. PARTIAL_*, TRAILING_*, PEAK_*)
- POSITION_TIMEOUT_HOURS — макс. время в позиции до принудительного закрытия
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
    DEPOSIT: float = 50.0  # Стартовый депозит USDT
    ENTRY_AMOUNT: float = 50.0  # Базовая сумма (используется если REINVEST=False)
    LEVERAGE: int = 75  # Максимальное плечо для агрессивного разгона

    # Риск-менеджмент — ШИРОКИЙ КОРИДОР для высоковолатильных альтов
    STOP_LOSS_PCT: float = 0.7  # Быстрый стоп для защиты депозита
    TAKE_PROFIT_PCT: float = 1.0  
    TAKE_PROFIT: float = TAKE_PROFIT_PCT  
    TP2_PCT: float = 2.0  
    TP3_PCT: float = 4.0  

    # Цели
    DAILY_TARGET_MIN: float = 10.0  # Минимальная цель в день %
    DAILY_TARGET_MAX: float = 15.0  # Максимальная цель в день %

    # Режим работы
    WORK_HOURS: str = "24/7"
    DIRECTION: str = "BOTH"  # LONG и SHORT

    # Параметры сигналов — КЛАССИЧЕСКИЕ 7 ИНДИКАТОРОВ
    MIN_INDICATORS_SCORE: int = 3  # Минимум 4 из 7
    TOTAL_INDICATORS: int = 8

    # Таймфреймы
    SCANNER_TIMEFRAME: str = '5m'
    TREND_TIMEFRAME: str = '15m'
    EMA_TIMEFRAME: str = '1h'

    # Алёрты по прибыли (в % ROE с учётом плеча)
    PROFIT_ALERT_10: float = 50.0    # +50% ROE
    PROFIT_ALERT_15: float = 150.0   # +150% ROE
    PROFIT_ALERT_40: float = 300.0   # +300% ROE
    DRAWDOWN_ALERT: float = 12.0

    # Momentum exit — закрытие слабых зависших сделок
    MOMENTUM_EXIT_MINUTES: int = 40
    MOMENTUM_MIN_PROFIT: float = 0.3
    MOMENTUM_MIN_ADX: float = 18.0

    # ===================================================================
    # ПОРТФЕЛЬНАЯ СТРАТЕГИЯ
    # ===================================================================
    REINVEST_PROFITS: bool = True   # Реинвестировать прибыль
    MIN_SLOT_USDT: float = 5.0     # Минимальный капитал на 1 сделку ($)

    # Выход по откату от пика (в % ROE)
    MIN_PEAK_PNL_TO_TRACK: float = 12.0
    PEAK_DRAWDOWN_CLOSE_PCT: float = 2.5

    # Трейлинг
    TRAILING_ACTIVATE_PCT: float = 18.0
    TRAILING_DRAWDOWN_CLOSE_PCT: float = 4.0
    TRAILING_DISTANCE_PCT: float = 6.0
    TRAILING_BREAKEVEN_PCT: float = 0.1
    # Экстренное закрытие плохой сделки
    MAX_POSITION_LOSS_PCT: float = -22.0


    # Частичные TP (в % ROE)
    PARTIAL_TP_ENABLED = True
    PARTIAL_TP1_PCT: float = 18.0   # Фиксируем 40%
    PARTIAL_TP2_PCT: float = 45.0   # Фиксируем 30%
    PARTIAL_TP3_PCT: float = 120.0  # Фиксируем остаток, оставляем раннер

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
    """Фоновая проверка Crypto Fear & Greed Index каждые 30 минут.
    При Extreme Fear (< 25) — приостановка открытия новых сделок.
    """
    global ALLOW_TRADING
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



class Database:
    """SQLite база данных для истории сделок и статистики"""

    def __init__(self, db_path: str = 'smart_money.db'):
        self.db_path = db_path
        self.init_db()

    def init_db(self):
        """Инициализация таблиц БД"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Таблица позиций
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

        # Таблица сигналов
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

        # Таблица статистики
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

        # Таблица алёртов
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
        # Исправление старых строк: total_pnl_pct раньше суммировали по сделкам — пересчитываем от DEPOSIT из .env
        try:
            ref = float(os.getenv('DEPOSIT', '140') or '140')
            if ref > 0:
                cursor.execute(
                    'UPDATE statistics SET total_pnl_pct = (total_pnl * 100.0 / ?) WHERE ABS(total_pnl) > 1e-9 OR total_trades > 0',
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
        """Добавление новой позиции"""
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
        """Обновление позиции при закрытии"""
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
        """Получение всех открытых позиций"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute('''
            SELECT * FROM positions WHERE status = 'OPEN'
        ''')

        positions = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return positions

    def add_signal(self, symbol: str, signal_type: str, entry_price: float,
                   smc_score: int, indicators: dict) -> int:
        """Добавление сигнала"""
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
        """Отметка выполненного сигнала"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE signals SET executed = 1 WHERE id = ?
        ''', (signal_id,))

        conn.commit()
        conn.close()

    def update_daily_statistics(
        self,
        pnl: float,
        pnl_pct: float,
        count_as_trade: bool = True,
        equity_reference: float = 140.0,
    ):
        """Обновление дневной статистики.

        total_pnl_pct — это результат дня в процентах от equity_reference (задайте DEPOSIT в .env),
        а не сумма «процентов по сделкам» (раньше так было — отчёт выглядел неправдоподобно).
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        ref = float(equity_reference) if equity_reference and equity_reference > 0 else 140.0

        cursor.execute('SELECT * FROM statistics WHERE date = ?', (today,))
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
                is_profitable = 1 if pnl > 0 else 0
                is_losing = 1 if pnl < 0 else 0
                cursor.execute(
                    '''
                    INSERT INTO statistics (date, total_trades, profitable_trades,
                                            losing_trades, total_pnl, total_pnl_pct,
                                            best_trade, worst_trade)
                    VALUES (?, 1, ?, ?, ?, 0, ?, ?)
                    ''',
                    (today, is_profitable, is_losing, pnl, pnl, pnl),
                )
            else:
                cursor.execute(
                    '''
                    INSERT INTO statistics (date, total_trades, profitable_trades,
                                            losing_trades, total_pnl, total_pnl_pct,
                                            best_trade, worst_trade)
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
        """Получение статистики за день"""
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
        """Добавление алёрта"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO alerts (position_id, alert_type, message)
            VALUES (?, ?, ?)
        ''', (position_id, alert_type, message))

        conn.commit()
        conn.close()

    def get_all_statistics(self) -> Dict:
        """Получение общей статистики"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Общая статистика
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

        # Статистика по дням
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
        """Получение статистики за последние N часов"""
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

    async def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> List[List]:
        """Получение свечных данных"""
        try:
            ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
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

        # SMA для первого значения
        sma = sum(prices[:period]) / period
        ema.append(sma)

        # EMA для остальных
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
            change = prices[i] - prices[i-1]
            gains.append(max(0, change))
            losses.append(max(0, -change))

        # Первый расчет
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        if avg_loss == 0:
            rsi.append(100)
        else:
            rs = avg_gain / avg_loss
            rsi.append(100 - (100 / (1 + rs)))

        # Последующие расчеты
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
        """Расчет ADX"""
        if len(close) < period + 1:
            return []

        plus_dm = []
        minus_dm = []
        tr = []

        for i in range(1, len(close)):
            # True Range
            tr_val = max(
                high[i] - low[i],
                abs(high[i] - close[i-1]),
                abs(low[i] - close[i-1])
            )
            tr.append(tr_val)

            # Directional Movement
            plus_dm_val = max(0, high[i] - high[i-1]) if high[i] - high[i-1] > low[i-1] - low[i] else 0
            minus_dm_val = max(0, low[i-1] - low[i]) if low[i-1] - low[i] > high[i] - high[i-1] else 0
            plus_dm.append(plus_dm_val)
            minus_dm.append(minus_dm_val)

        # Smoothed values
        atr = sum(tr[:period]) / period
        plus_di = [(sum(plus_dm[:period]) / atr) * 100]
        minus_di = [(sum(minus_dm[:period]) / atr) * 100]

        dx = []
        if plus_di[0] + minus_di[0] > 0:
            dx.append(abs(plus_di[0] - minus_di[0]) / (plus_di[0] + minus_di[0]) * 100)
        else:
            dx.append(0)

        # ADX
        adx = [sum(dx[:period]) / period] if len(dx) >= period else [0]

        for i in range(period, len(tr)):
            atr = (atr * (period - 1) + tr[i]) / period
            pdi = ((plus_di[-1] * (period - 1) + (plus_dm[i] / atr) * 100) / period) if atr > 0 else 0
            mdi = ((minus_di[-1] * (period - 1) + (minus_dm[i] / atr) * 100) / period) if atr > 0 else 0
            plus_di.append(pdi)
            minus_di.append(mdi)

            denominator = pdi + mdi

            if denominator <= 0:
                dx_val = 0
            else:
                dx_val = abs(pdi - mdi) / denominator * 100
            dx.append(dx_val)

            adx_val = (adx[-1] * (period - 1) + dx_val) / period
            adx.append(adx_val)

        return adx

    def calculate_macd(self, prices: List[float], fast: int = 12, 
                       slow: int = 26, signal: int = 9) -> Dict:
        """Расчет MACD"""
        if len(prices) < slow + signal:
            return {'macd': 0, 'signal': 0, 'histogram': 0}

        ema_fast = self.calculate_ema(prices, fast)
        ema_slow = self.calculate_ema(prices, slow)

        if len(ema_fast) < signal or len(ema_slow) < signal:
            return {'macd': 0, 'signal': 0, 'histogram': 0}

        # Выравнивание длин
        min_len = min(len(ema_fast), len(ema_slow))
        ema_fast = ema_fast[-min_len:]
        ema_slow = ema_slow[-min_len:]

        macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
        signal_line = self.calculate_ema(macd_line, signal)

        if not signal_line:
            return {'macd': 0, 'signal': 0, 'histogram': 0}

        histogram = macd_line[-1] - signal_line[-1] if len(signal_line) > 0 else 0

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
            sma.append(sum(prices[i-period:i]) / period)

        return sma

    def detect_bos_choch(self, ohlcv: List[List]) -> str:
        """Обнаружение BOS (Break of Structure) и CHoCH (Change of Character)
        Поддерживает и LONG и SHORT направления.
        """
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

        # BOS вверх — пробой максимума
        if current_price > highest:
            if closes[-15] > closes[-5]:
                return "CHoCH_BULLISH"
            return "BOS_UP"

        # BOS вниз — пробой минимума
        if current_price < lowest:
            if closes[-15] < closes[-5]:
                return "CHoCH_BEARISH"
            return "BOS_DOWN"

        return "NONE"

    def detect_fvg(self, ohlcv: List[List]) -> str:
        """Обнаружение Fair Value Gap с проверкой pullback к зоне."""
        if len(ohlcv) < 3:
            return ''

        current_price = ohlcv[-1][4]

        for i in range(len(ohlcv) - 3):  # не берём последнюю свечу
            c1, c2, c3 = ohlcv[i], ohlcv[i + 1], ohlcv[i + 2]
            high1, low1 = c1[2], c1[3]
            high3, low3 = c3[2], c3[3]

            # Бычий FVG: gap между high свечи 1 и low свечи 3
            if high1 < low3:
                # Цена должна вернуться в эту зону (pullback)
                if high1 * 0.998 <= current_price <= low3 * 1.003:
                    return 'BULLISH'

            # Медвежий FVG
            if low1 > high3:
                if high3 * 0.997 <= current_price <= low1 * 1.002:
                    return 'BEARISH'
        return ''

    def detect_order_block(self, ohlcv: List[List]) -> str:
        """Обнаружение Order Block с проверкой pullback."""
        if len(ohlcv) < 10:
            return ''

        current_price = ohlcv[-1][4]

        # Ищем импульсное движение (последние 10 свечей)
        for i in range(len(ohlcv) - 5, max(0, len(ohlcv) - 15), -1):
            candle = ohlcv[i]
            c_open, c_high, c_low, c_close = candle[1], candle[2], candle[3], candle[4]

            # Бычий OB: красная свеча перед импульсом вверх
            if c_open > c_close:  # красная
                # Проверяем что после неё был рост
                future_closes = [ohlcv[j][4] for j in range(i+1, min(i+4, len(ohlcv)))]
                if future_closes and max(future_closes) > c_high * 1.002:
                    # Pullback: цена вернулась к телу этой свечи
                    if c_low * 0.998 <= current_price <= c_high * 1.003:
                        return 'BULLISH'

            # Медвежий OB: зелёная свеча перед импульсом вниз
            if c_close > c_open:  # зелёная
                future_closes = [ohlcv[j][4] for j in range(i+1, min(i+4, len(ohlcv)))]
                if future_closes and min(future_closes) < c_low * 0.998:
                    if c_low * 0.997 <= current_price <= c_high * 1.002:
                        return 'BEARISH'
        return ''


    async def analyze_symbol(self, symbol: str) -> Dict[str, Any]:
        """
        АНАЛИЗ — 7 ИНДИКАТОРОВ, LONG + SHORT

        Бот анализирует оба направления и выбирает сильнейшее:
        1. BOS/CHoCH (Смена структуры) — вверх или вниз
        2. FVG (Имбаланс) — бычий или медвежий
        3. Тренд по EMA50 — выше или ниже
        4. RSI Momentum — бычий (50-80) или медвежий (20-50 и падает)
        5. ADX > 20 (Сила тренда) — нейтральный
        6. MACD — бычий или медвежий
        7. Объем (Всплеск > 1.3x) — нейтральный

        Минимум 4/7 для входа.
        """
        result = {
            'symbol': symbol,
            'score': 0,
            'direction': 'LONG',  # LONG или SHORT
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
            ohlcv_15m = await self.get_ohlcv(symbol, config.TREND_TIMEFRAME, limit=100)

            if not ohlcv_5m or not ohlcv_15m:
                return result

            closes_5m = [c[4] for c in ohlcv_5m]
            highs_5m = [h[2] for h in ohlcv_5m]
            lows_5m = [l[3] for l in ohlcv_5m]
            volumes_5m = [v[5] for v in ohlcv_5m]
            current_price = closes_5m[-1]

            # Считаем очки для LONG и SHORT отдельно
            long_score = 0
            short_score = 0
            long_ind = {}
            short_ind = {}

            # ═══ 1. BOS/CHoCH (Структура на 15m) ═══
            bos = self.detect_bos_choch(ohlcv_15m)
            result['bos'] = bos
            if bos in ['BOS_UP', 'CHoCH_BULLISH']:
                long_score += 1
                long_ind['bos'] = True
            if bos in ['BOS_DOWN', 'CHoCH_BEARISH']:
                short_score += 1
                short_ind['bos'] = True

            # ═══ 2. FVG (Имбаланс на 5m) ═══
            fvg = self.detect_fvg(ohlcv_5m[-20:])
            result['fvg'] = bool(fvg)
            if fvg == 'BULLISH':
                long_score += 1
                long_ind['fvg'] = True
            if fvg == 'BEARISH':
                short_score += 1
                short_ind['fvg'] = True

            # ═══ 2b. Order Block (на 5m) ═══
            ob = self.detect_order_block(ohlcv_5m[-20:])
            if ob == 'BULLISH':
                long_score += 1
                long_ind['ob'] = True
            if ob == 'BEARISH':
                short_score += 1
                short_ind['ob'] = True

            has_smc_structure = bool(fvg) or bool(ob)

            # ═══ 3. EMA 50 Тренд ═══
            ema50 = self.calculate_ema(closes_5m, 50)
            if ema50:
                result['ema200'] = ema50[-1]
                if current_price > ema50[-1]:
                    long_score += 1
                    long_ind['ema50_trend'] = True
                else:
                    short_score += 1
                    short_ind['ema50_trend'] = True

            # ═══ 4. RSI ═══
            rsi = self.calculate_rsi(closes_5m, 14)
            if rsi and len(rsi) >= 2:
                result['rsi'] = rsi[-1]
                # LONG: RSI 40-80 и растёт (расширенная зона)
                if 40 <= rsi[-1] <= 80 and rsi[-1] > rsi[-2]:
                    long_score += 1
                    long_ind['rsi_momentum'] = True
                # SHORT: RSI 20-60 и падает
                if 20 <= rsi[-1] <= 60 and rsi[-1] < rsi[-2]:
                    short_score += 1
                    short_ind['rsi_momentum'] = True

            # ═══ 5. ADX > 20 (Сила тренда — нейтральный) ═══
            adx = self.calculate_adx(highs_5m, lows_5m, closes_5m, 14)
            if adx:
                result['adx'] = adx[-1]
                if adx[-1] > 20:
                    long_score += 1
                    short_score += 1
                    long_ind['adx'] = True
                    short_ind['adx'] = True

            # ═══ 6. MACD ═══
            macd = self.calculate_macd(closes_5m)
            result['macd'] = macd
            if macd['histogram'] > 0 and macd['macd'] > macd['signal']:
                long_score += 1
                long_ind['macd'] = True
            if macd['histogram'] < 0 and macd['macd'] < macd['signal']:
                short_score += 1
                short_ind['macd'] = True

            # ═══ 7. Всплеск объема (нейтральный) ═══
            vol_sma = self.calculate_sma(volumes_5m, 20)
            if vol_sma and vol_sma[-1] > 0:
                vol_ratio = volumes_5m[-1] / vol_sma[-1]
                if vol_ratio > 1.3:  # Смягчено с 1.5 до 1.3
                    long_score += 1
                    short_score += 1
                    result['volume_ok'] = True
                    long_ind['volume_spike'] = True
                    short_ind['volume_spike'] = True

            # ═══ Выбираем лучшее направление ═══
            # СИГНАЛ ВАЛИДЕН ТОЛЬКО ЕСЛИ ЕСТЬ SMC СТРУКТУРА (FVG или OB)
            if long_score >= short_score and long_score >= config.MIN_INDICATORS_SCORE and has_smc_structure:
                result['score'] = long_score
                result['direction'] = 'LONG'
                result['indicators'] = long_ind
                result['signal'] = True
            elif short_score > long_score and short_score >= config.MIN_INDICATORS_SCORE and has_smc_structure:
                result['score'] = short_score
                result['direction'] = 'SHORT'
                result['indicators'] = short_ind
                result['signal'] = True
            else:
                # Нет сигнала, но показываем лучший score
                if long_score >= short_score:
                    result['score'] = long_score
                    result['direction'] = 'LONG'
                    result['indicators'] = long_ind
                else:
                    result['score'] = short_score
                    result['direction'] = 'SHORT'
                    result['indicators'] = short_ind

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
    amount_usdt: float
    leverage: int
    quantity: float
    timestamp: datetime
    remaining_quantity: float = 0.0   # Остаток после частичных закрытий
    peak_pnl: float = 0.0             # Пик движения цены %
    trailing_active: bool = False      # Трейлинг активирован
    trailing_peak: float = 0.0        # Пик при активном трейлинге
    partial_tp1_done: bool = False     # Частичная фиксация TP1 (+10%)
    partial_tp2_done: bool = False     # Частичная фиксация TP2 (+20%)
    partial_tp3_done: bool = False     # Полная фиксация TP3 (+30%)
    dynamic_sl_level: int = 0         # 0=нач, 1=безубыток, 2=+5%, 3=+10%
    realized_pnl_usd: float = 0.0     # Уже зафиксировано частичными выходами (в USDT)

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
            'options': {
                'defaultType': 'future',
                'recvWindow': 60000,
                'adjustForTimeDifference': True
            }
        }

        if testnet:
            logger.info("🔧 Используется Binance Demo Trading (demo-fapi.binance.com)")

        self.exchange = ccxt.binanceusdm(exchange_config)

        if testnet:
            # Обновляем URL на demo endpoints после инициализации (чтобы обойти ошибку ccxt)
            demo_urls = {
                'fapiPublic': 'https://demo-fapi.binance.com/fapi/v1',
                'fapiPrivate': 'https://demo-fapi.binance.com/fapi/v1',
                'fapiPublicV2': 'https://demo-fapi.binance.com/fapi/v2',
                'fapiPrivateV2': 'https://demo-fapi.binance.com/fapi/v2',
                'fapiPublicV3': 'https://demo-fapi.binance.com/fapi/v3',
                'fapiPrivateV3': 'https://demo-fapi.binance.com/fapi/v3',
            }
            self.exchange.urls['api'].update(demo_urls)

        self.exchange.has['fetchCurrencies'] = False

        # База данных
        self.db = Database()

        # SMC Анализатор
        self.smc_analyzer = SMCAnalyzer(self.exchange)

        # Активные позиции
        self.positions: Dict[int, Position] = {}

        # Список символов для сканирования
        self.symbols_to_scan = [
            'BTC/USDT', 'ETH/USDT', 'BNB/USDT', 'SOL/USDT', 'XRP/USDT',
            'ADA/USDT', 'AVAX/USDT', 'DOGE/USDT', 'DOT/USDT', 'LINK/USDT',
            'POL/USDT', 'UNI/USDT', 'ATOM/USDT', 'LTC/USDT', 'ETC/USDT',
            'NEAR/USDT', 'FIL/USDT', 'AAVE/USDT', 'ARB/USDT', 'OP/USDT',
            'VANA/USDT', 'APT/USDT', 'INJ/USDT', 'RNDR/USDT', 'SUI/USDT',
            'SEI/USDT', 'TIA/USDT', 'ORDI/USDT', 'WLD/USDT', 'GALA/USDT', 
            'FET/USDT', 'STX/USDT', 'LDO/USDT', 'GRT/USDT', 'SAND/USDT', 
            'MANA/USDT', 'FTM/USDT', 'WIF/USDT', 'JUP/USDT', 'PYTH/USDT', 
            'STRK/USDT', 'DYDX/USDT', 'GMX/USDT', 'CRV/USDT', 'CHZ/USDT', 
            'SNX/USDT', 'AXS/USDT', 'MKR/USDT', 'THETA/USDT', 'EGLD/USDT', 
            'RUNE/USDT', 'KAS/USDT', 'TON/USDT', 'IMX/USDT', 'MNT/USDT', 
            'QNT/USDT', 'FLOKI/USDT', 'BOME/USDT', 'MEME/USDT', 'ALT/USDT'
        ]

        # Статус бота
        self.is_running = False
        self.last_scan_time = None
        self.signals_today = 0
        self.max_signals_per_day = 9999  # Без лимита — агрессивный режим

        # Telegram bot application
        self.app = None
        self.active_chat_ids = set([str(self.telegram_chat_id)])
        if self.user_chat_id:
            self.active_chat_ids.add(str(self.user_chat_id))

        # Отдельный Bot для отправки сообщений (работает независимо от polling)
        self._bot = Bot(token=self.telegram_token)

    async def send_telegram_message(self, text: str):
        """Отправка сообщения во все активные чаты"""
        for chat_id in list(self.active_chat_ids):
            try:
                await self._bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
            except Exception as e:
                logger.warning(f"Не удалось отправить сообщение в чат {chat_id}: {e}")

    async def disconnect(self):
        """Закрытие соединения с биржей"""
        try:
            if self.exchange:
                await self.exchange.close()
        except Exception as e:
            logger.warning(f"Ошибка при закрытии биржи: {e}")

    async def connect(self):
        """Подключение к бирже"""
        try:
            await self.exchange.load_markets()
            logger.info("Markets loaded successfully - API ключ валиден")

            try:
                balance = await self.exchange.fetch_balance()
                logger.info(f"Подключено к Binance Futures. Баланс: {balance.get('total', {})}")
            except Exception as balance_error:
                err_str = str(balance_error)
                if '-2015' in err_str:
                    logger.error(
                        "❌ API ключ НЕ ИМЕЕТ ПРАВ на USDT-M Futures!\n"
                        "   Что проверить на Binance:\n"
                        "   1. API Management → ваш ключ → Включить «Enable Futures» / «USDT-M Futures»\n"
                        "   2. Убедитесь, что IP адрес в whitelist (если включён)\n"
                        "   3. Должны быть права «Чтение» и «Futures»"
                    )
                    raise balance_error
                raise balance_error
            return True
        except Exception as e:
            logger.error(f"Ошибка подключения к бирже: {e}")
            return False

    def compute_optimal_slots(self, virtual_equity: float) -> int:
        """
        Динамически вычисляет оптимальное число параллельных позиций
        на основе текущего виртуального баланса.

        Формула: slots = max(1, floor(equity / MIN_SLOT_USDT))

        Примеры (MIN_SLOT_USDT=5):
          $10  -> 2 слота
          $20  -> 4 слота
          $50  -> 10 слотов
          $100 -> 20 слотов
        """
        min_slot = config.MIN_SLOT_USDT
        raw = int(virtual_equity // min_slot)
        return max(1, raw)
    async def calculate_position_size(self, entry_price: float, score: int = 5) -> tuple:
        try:
            stats = self.db.get_all_statistics()
            total_pnl = float(stats.get('total_pnl') or 0.0)
            # Гарантируем минимум DEPOSIT даже если БД показывает убытки
            virtual_equity = max(config.DEPOSIT + total_pnl, config.DEPOSIT * 0.5)

            locked_margin = sum(p.amount_usdt for p in self.positions.values())
            free_equity = virtual_equity - locked_margin

            logger.info(
                f"POS_SIZE dbg: DEPOSIT={config.DEPOSIT} total_pnl={total_pnl} "
                f"virtual={virtual_equity} locked={locked_margin} free={free_equity} "
                f"score={score} entry={entry_price}"
            )

            if free_equity < config.MIN_SLOT_USDT:
                logger.warning(f"POS_SIZE: free_equity({free_equity}) < MIN_SLOT({config.MIN_SLOT_USDT})")
                return 0, 0, 0

            optimal_slots = self.compute_optimal_slots(free_equity)
            base_slot = virtual_equity / optimal_slots

            weight = max(score, config.MIN_INDICATORS_SCORE) / 5.0
            amount_usdt = base_slot * weight
            amount_usdt = min(amount_usdt, free_equity)
            if amount_usdt < config.MIN_SLOT_USDT:
                if free_equity >= config.MIN_SLOT_USDT:
                    amount_usdt = config.MIN_SLOT_USDT
                else:
                    logger.warning(f"POS_SIZE: amount_usdt({amount_usdt}) < MIN_SLOT but free_equity also < MIN_SLOT")
                    return 0, 0, 0

            quantity = amount_usdt * config.LEVERAGE / entry_price
            logger.info(f"POS_SIZE result: qty={amount_usdt}={amount_usdt} margin={amount_usdt} notional={amount_usdt * config.LEVERAGE}")
            return quantity, amount_usdt, amount_usdt * config.LEVERAGE
        except Exception as e:
            logger.error(f"Ошибка в calculate_position_size: {e}", exc_info=True)
            return 0, 0, 0



    async def open_position(self, symbol: str, entry_price: float,
                            smc_result: Dict) -> Optional[Position]:
        """Открытие позиции (LONG или SHORT)"""
        global ALLOW_TRADING
        if not ALLOW_TRADING:
            logger.info(f"Сигнал {symbol} пойман, но торговля приостановлена (новостной фон)")
            return None
        try:
            direction = smc_result.get('direction', 'LONG')

            market_info = self.exchange.market(symbol)
            min_notional = float(market_info.get('limits', {}).get('cost', {}).get('min', 5))

            # Расчет параметров (score определяет вес позиции)
            quantity, margin, actual_amount = await self.calculate_position_size(
                entry_price, score=smc_result['score']
            )
            if quantity == 0:
                logger.warning(f"Недостаточно средств для открытия позиции {symbol}")
                return None

            # Округление количества
            quantity = float(self.exchange.amount_to_precision(symbol, quantity))

            # Проверка минимального номинала (notional = quantity * price)
            notional = quantity * entry_price
            if notional < min_notional:
                logger.warning(f"Номинал ${notional:.2f} меньше минимума ${min_notional} для {symbol}. Увеличиваем qty.")
                quantity = float(self.exchange.amount_to_precision(symbol, min_notional / entry_price * 1.05))
                notional = quantity * entry_price
                if notional < min_notional:
                    logger.warning(f"Не удалось подобрать qty для {symbol}, пропускаем")
                    return None

            dir_emoji = '🟢 LONG' if direction == 'LONG' else '🔴 SHORT'
            logger.info(f"Открытие {dir_emoji} {symbol}: qty={quantity}, notional=${notional:.2f}")

            # 1. Кросс маржа и плечо
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

            # 2. Открытие позиции
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
                    new_lev = 50 if actual_leverage > 50 else 20
                    try:
                        await self.exchange.set_leverage(new_lev, symbol)
                        actual_leverage = new_lev
                        if direction == 'SHORT':
                            order = await self.exchange.create_market_sell_order(symbol, quantity)
                        else:
                            order = await self.exchange.create_market_buy_order(symbol, quantity)
                    except Exception as e2:
                        logger.error(f"Не удалось открыть {symbol}: {e2}")
                        raise e2
                else:
                    raise e

            actual_entry = float(order['average']) if order.get('average') else entry_price
            actual_qty = float(order['filled']) if order.get('filled') else quantity

            # SL/TP зависят от направления
            if direction == 'SHORT':
                actual_sl = float(self.exchange.price_to_precision(symbol, actual_entry * (1 + config.STOP_LOSS_PCT / 100)))
                actual_tp = float(self.exchange.price_to_precision(symbol, actual_entry * (1 - config.TP3_PCT / 100)))
                close_side = 'BUY'
            else:
                actual_sl = float(self.exchange.price_to_precision(symbol, actual_entry * (1 - config.STOP_LOSS_PCT / 100)))
                actual_tp = float(self.exchange.price_to_precision(symbol, actual_entry * (1 + config.TP3_PCT / 100)))
                close_side = 'SELL'

            # 3. SL
            try:
                await self.exchange.create_order(
                    symbol=symbol, type='STOP_MARKET', side=close_side,
                    amount=actual_qty,
                    params={'stopPrice': actual_sl, 'reduceOnly': True}
                )
            except Exception as e:
                logger.warning(f"⚠️ SL не выставлен для {symbol}: {e}")

            # 4. TP
            try:
                await self.exchange.create_order(
                    symbol=symbol, type='TAKE_PROFIT_MARKET', side=close_side,
                    amount=actual_qty,
                    params={'stopPrice': actual_tp, 'reduceOnly': True}
                )
            except Exception as e:
                logger.warning(f"⚠️ TP не выставлен для {symbol}: {e}")

            if direction == 'SHORT':
                tp1_price = float(self.exchange.price_to_precision(symbol, actual_entry * (1 - config.TAKE_PROFIT_PCT / 100)))
            else:
                tp1_price = float(self.exchange.price_to_precision(symbol, actual_entry * (1 + config.TAKE_PROFIT_PCT / 100)))

            position_id = self.db.add_position(
                symbol=symbol,
                side=direction,
                entry_price=actual_entry,
                stop_loss=actual_sl,
                take_profit=tp1_price,
                amount_usdt=margin,
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
                amount_usdt=margin,
                leverage=actual_leverage,
                quantity=actual_qty,
                remaining_quantity=actual_qty,
                timestamp=datetime.now(timezone.utc),
                realized_pnl_usd=0.0,
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
                f"📐 RR: 1:{rr:.1f} | Плечо: x{actual_leverage}\n\n"
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

    async def close_position(self, position_id: int, emergency: bool = False) -> bool:
        """Закрытие позиции"""
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

            # Закрытие: для LONG продаём, для SHORT покупаем
            try:
                if position.side == 'SHORT':
                    order = await self.exchange.create_market_buy_order(
                        symbol,
                        qty_close,
                        params={'reduceOnly': True}
                    )
                else:
                    order = await self.exchange.create_market_sell_order(
                        symbol,
                        qty_close,
                        params={'reduceOnly': True}
                    )

            except Exception as close_error:
                error_text = str(close_error)

                # Binance иногда отклоняет reduceOnly
                # особенно после частичных фиксаций или рассинхрона позиции
                if '-2022' in error_text or 'ReduceOnly Order is rejected' in error_text:
                    logger.warning(
                        f"⚠️ ReduceOnly отклонен для {symbol}, повтор без reduceOnly"
                    )

                    if position.side == 'SHORT':
                        order = await self.exchange.create_market_buy_order(
                            symbol,
                            qty_close
                        )
                    else:
                        order = await self.exchange.create_market_sell_order(
                            symbol,
                            qty_close
                        )
                else:
                    raise

            # Очистка оставшихся ордеров (SL/TP)
            try:
                await self.exchange.cancel_all_orders(symbol)
            except Exception as cancel_e:
                logger.warning(f"Не удалось отменить ордера для {symbol}: {cancel_e}")

            exit_price = order.get('average')
            if not exit_price:
                exit_price = order.get('price')
            if not exit_price:
                try:
                    ticker = await self.exchange.fetch_ticker(symbol)
                    exit_price = ticker['last']
                except:
                    exit_price = position.entry_price  # Fallback to avoid huge fake PnL

            exit_price = float(exit_price)
            # PnL зависит от направления
            if position.side == 'SHORT':
                leg_pnl = (position.entry_price - exit_price) * qty_close
            else:
                leg_pnl = (exit_price - position.entry_price) * qty_close
            total_pnl = position.realized_pnl_usd + leg_pnl
            # amount_usdt УЖЕ является маржой (не номиналом!), не делим повторно
            margin = position.amount_usdt
            pnl_pct = (total_pnl / margin) * 100 if margin > 0 else 0.0

            # Обновление БД
            self.db.update_position(position_id, exit_price, total_pnl, pnl_pct)
            self.db.update_daily_statistics(total_pnl, pnl_pct, count_as_trade=True, equity_reference=config.DEPOSIT)

            # Удаление из активных
            del self.positions[position_id]

            # Длительность позиции
            duration = datetime.now(timezone.utc) - position.timestamp

            # Сообщение в Telegram — подробный отчёт о закрытой сделке
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
                f"📈 Зафиксировано: {'+' if total_pnl >= 0 else ''}${total_pnl:.2f} ({'+' if total_pnl >= 0 else ''}{pnl_pct:.1f}%)\n"
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
        """Закрытие всех позиций"""
        position_ids = list(self.positions.keys())
        for pid in position_ids:
            await self.close_position(pid, emergency)

    async def close_partial_position(self, position: Position, qty_to_close: float, current_price: float):
        """Частичное закрытие позиции"""
        try:
            # Округляем количество до требований биржи
            qty_to_close = float(self.exchange.amount_to_precision(position.symbol, qty_to_close))
            if qty_to_close <= 0:
                return
            if position.side == 'SHORT':
                await self.exchange.create_market_buy_order(
                    position.symbol, qty_to_close,
                    params={'reduceOnly': True}
                )
                chunk_pnl = (position.entry_price - current_price) * qty_to_close
            else:
                await self.exchange.create_market_sell_order(
                    position.symbol, qty_to_close,
                    params={'reduceOnly': True}
                )
                chunk_pnl = (current_price - position.entry_price) * qty_to_close
            position.realized_pnl_usd += chunk_pnl
            position.remaining_quantity = max(0.0, position.remaining_quantity - qty_to_close)
            logger.info(f"Частичное закрытие {position.symbol}: {qty_to_close} @ {current_price}, остаток {position.remaining_quantity}")
        except Exception as e:
            logger.error(f"Ошибка частичного закрытия {position.symbol}: {e}")


    async def _update_exchange_sl(self, position: Position, new_sl_price: float):
        """Обновляет стоп-лосс на бирже: отменяет старые ордера и ставит новый."""
        try:
            new_sl_price = float(self.exchange.price_to_precision(position.symbol, new_sl_price))
            qty_rounded = float(self.exchange.amount_to_precision(position.symbol, position.remaining_quantity))
            if qty_rounded <= 0:
                return

            # Отменяем все существующие ордера по символу
            try:
                await self.exchange.cancel_all_orders(position.symbol)
            except Exception:
                pass

            # Ставим новый SL
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


    def calculate_position_roe(self, position, current_price):
        """Единый корректный расчет ROE для LONG/SHORT"""
        if position.side == 'SHORT':
            price_change_pct = (
                (position.entry_price - current_price)
                / position.entry_price
            ) * 100.0
        else:
            price_change_pct = (
                (current_price - position.entry_price)
                / position.entry_price
            ) * 100.0

        return price_change_pct * position.leverage

    
    def get_position_age_minutes(self, position):
        """Возраст позиции в минутах"""
        return (
            datetime.now(timezone.utc) - position.timestamp
        ).total_seconds() / 60


    async def monitor_positions(self):
        """Мониторинг позиций — Трейлинг, Частичные TP и Динамический SL"""
        for position_id, position in list(self.positions.items()):
            try:
                # 1. Получение текущей цены
                ticker = await self.exchange.fetch_ticker(position.symbol)
                current_price = ticker['last']

                # Автоматический перевод в безубыток
                if (
                    pnl_pct >= 0.8
                    and position.dynamic_sl_level == 0
                ):
                    position.stop_loss = position.entry_price
                    position.dynamic_sl_level = 1

                    logger.info(
                        f"BREAKEVEN ACTIVATED: {position.symbol}"
                    )

                # 2. Корректный расчет ROE
                pnl_pct = self.calculate_position_roe(
                    position,
                    current_price
                )

                # Momentum Exit — закрытие слабых зависших сделок
                position_age = self.get_position_age_minutes(position)

                try:
                    ohlcv = await self.exchange.fetch_ohlcv(
                        position.symbol,
                        timeframe='5m',
                        limit=40
                    )

                    highs = [x[2] for x in ohlcv]
                    lows = [x[3] for x in ohlcv]
                    closes = [x[4] for x in ohlcv]

                    adx_values = self.indicators.calculate_adx(
                        highs,
                        lows,
                        closes
                    )

                    current_adx = adx_values[-1] if adx_values else 0

                except Exception:
                    current_adx = 0

                if (
                    position_age > config.MOMENTUM_EXIT_MINUTES
                    and pnl_pct < config.MOMENTUM_MIN_PROFIT
                    and current_adx < config.MOMENTUM_MIN_ADX
                ):
                    logger.info(
                        f"MOMENTUM EXIT: {position.symbol} | "
                        f"Age={position_age:.1f}m | "
                        f"PNL={pnl_pct:.2f}% | "
                        f"ADX={current_adx:.1f}"
                    )

                    await self.close_position(
                        position.symbol,
                        "Momentum dead"
                    )

                    continue

                if position.side == 'SHORT':
                    price_change_pct = (
                        (position.entry_price - current_price)
                        / position.entry_price
                    ) * 100
                else:
                    price_change_pct = (
                        (current_price - position.entry_price)
                        / position.entry_price
                    ) * 100
                if position.side == 'SHORT':
                    pnl_usd = position.realized_pnl_usd + (position.entry_price - current_price) * position.remaining_quantity
                else:
                    pnl_usd = position.realized_pnl_usd + (current_price - position.entry_price) * position.remaining_quantity

                # 3. Обновление пика (peak_pnl)
                if isinstance(pnl_pct, (int, float)) and pnl_pct > position.peak_pnl:
                    position.peak_pnl = pnl_pct

                pair = position.symbol.replace('/USDT', '')

                # 3.5 Программный STOP LOSS (сравниваем price_change_pct с config.STOP_LOSS_PCT, т.к. стоп задан в % цены, а не ROE)
                if price_change_pct <= -config.STOP_LOSS_PCT:
                    message = (
                        f"❌ ПРОГРАММНЫЙ STOP LOSS | {pair}\n"
                        f"Достигнут предел убытка по цене: {price_change_pct:+.2f}%\n"
                        f"💰 Вложено: ${position.amount_usdt:.2f}\n"
                        f"Текущий PnL: {pnl_pct:+.1f}% ({'+' if pnl_usd >= 0 else ''}${pnl_usd:.2f})"
                    )
                    await self.send_telegram_message(message)
                    await self.close_position(position_id)
                    continue

                # 4. АНАЛИЗ ТРЕНДА ДЛЯ УБЫТОЧНЫХ ПОЗИЦИЙ
                # Если позиция в убытке более 1.5% и тренд против нас — закрываем раньше SL
                if price_change_pct <= -1.5 and position.peak_pnl < 5.0:
                    # Получаем свечи для анализа тренда
                    try:
                        ohlcv_5m = await self.smc_analyzer.get_ohlcv(position.symbol, '5m', limit=20)
                        if ohlcv_5m and len(ohlcv_5m) >= 10:
                            closes = [c[4] for c in ohlcv_5m]
                            # Простой анализ: последние 5 свечей вниз?
                            last_5 = closes[-5:]
                            downtrend = all(last_5[i] >= last_5[i+1] for i in range(len(last_5)-1))
                            # EMA анализ
                            ema20 = self.smc_analyzer.calculate_ema(closes, 20)
                            below_ema = ema20 and current_price < ema20[-1]

                            if downtrend and below_ema:
                                message = (
                                    f"🔴 АНАЛИЗ ТРЕНДА | {pair}\n"
                                    f"Позиция в убытке: {price_change_pct:+.2f}%\n"
                                    f"Тренд: НИСХОДЯЩИЙ (5 свечей вниз)\n"
                                    f"Цена ниже EMA20: {below_ema}\n"
                                    f"Решение: ЗАКРЫТЬ раньше SL\n"
                                    f"💰 Вложено: ${position.amount_usdt:.2f}\n"
                                    f"Текущий PnL: {'+' if pnl_usd >= 0 else ''}${pnl_usd:.2f}"
                                )
                                await self.send_telegram_message(message)
                                await self.close_position(position_id)
                                continue
                    except Exception as e:
                        logger.warning(f"Ошибка анализа тренда для {pair}: {e}")

                # 5. Откат от пика (пороги из config)
                # Если позиция достигла хорошего профита (например, >50% ROE), защищаем её от сильного отката в минус
                if position.peak_pnl >= config.MIN_PEAK_PNL_TO_TRACK:
                    drawdown = position.peak_pnl - pnl_pct
                    if drawdown > config.PEAK_DRAWDOWN_CLOSE_PCT:
                        # Если после отката мы все еще в хорошем плюсе, или откат слишком резкий
                        message = (
                            f"📉 ОТКАТ ОТ ПИКА | {pair}\n"
                            f"Было: +{position.peak_pnl:.1f}%\n"
                            f"Сейчас: {pnl_pct:+.1f}%\n"
                            f"Откат: {drawdown:.1f}%\n\n"
                            f"🚨 АВТОМАТИЧЕСКИЙ ВЫХОД!\n"
                            f"Фактический ROE: {pnl_pct:+.1f}%\n"
                            f"💰 Вложено: ${position.amount_usdt:.2f}\n"
                            f"Текущий PnL: {'+' if pnl_usd >= 0 else ''}${pnl_usd:.2f}"
                        )
                        await self.send_telegram_message(message)
                        await self.close_position(position_id)
                        continue

                # 6. TRAILING STOP
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
                            f"💰 Вложено: ${position.amount_usdt:.2f}\n"
                            f"Текущий PnL: {'+' if pnl_usd >= 0 else ''}${pnl_usd:.2f}"
                        )
                        await self.send_telegram_message(message)
                        await self.close_position(position_id)
                        continue
                # 7. ЧАСТИЧНАЯ ФИКСАЦИЯ
                if pnl_pct >= config.PARTIAL_TP1_PCT and not position.partial_tp1_done:
                    position.partial_tp1_done = True
                    await self.close_partial_position(position, position.quantity * 0.40, current_price)
                    new_sl = position.entry_price * (1 - 0.001) if position.side == 'SHORT' else position.entry_price * (1 + 0.001)
                    await self._update_exchange_sl(position, new_sl)
                    await self.send_telegram_message(
                        f"💰 ЧАСТИЧНАЯ ФИКСАЦИЯ TP1 | {pair}\n"
                        f"+{config.PARTIAL_TP1_PCT:.0f}% ROE — закрыто 40% | SL → безубыток"
                    )

                if pnl_pct >= config.PARTIAL_TP2_PCT and not position.partial_tp2_done:
                    position.partial_tp2_done = True
                    await self.close_partial_position(position, position.quantity * 0.30, current_price)
                    new_sl = position.entry_price * (1 - 0.009) if position.side == 'SHORT' else position.entry_price * (1 + 0.009)
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
                    new_sl = position.entry_price * (1 - 0.018) if position.side == 'SHORT' else position.entry_price * (1 + 0.018)
                    await self._update_exchange_sl(position, new_sl)
                    await self.send_telegram_message(
                        f"💎 TP3 +{config.PARTIAL_TP3_PCT:.0f}% ROE | {pair}\n"
                        f"Закрыто 90% позиции!\n"
                        f"🎯 Оставлен раннер 10% с трейлинг-стопом"
                    )
                    position.trailing_active = True
                    position.trailing_peak = pnl_pct
                    continue


                # 8. Проверка времени позиции
                await self.check_position_timeout(position)

            except Exception as e:
                logger.error(f"Ошибка мониторинга {position_id}: {e}")


    async def check_position_timeout(self, position: Position):
        """Проверка слабых зависших позиций"""

        try:
            now = datetime.now(timezone.utc)
            duration = now - position.timestamp
            duration_minutes = duration.total_seconds() / 60

            # Только для слабых позиций
            if duration_minutes < config.MOMENTUM_EXIT_MINUTES:
                return

            current_price = await self.get_current_price(position.symbol)
            pnl_pct = self.calculate_position_roe(position, current_price)

            # Не трогаем хорошие позиции
            if pnl_pct >= config.MOMENTUM_MIN_PROFIT:
                return

            logger.info(
                f"MOMENTUM EXIT: {position.symbol} | "
                f"{duration_minutes:.1f}m | PNL={pnl_pct:.2f}%"
            )

            await self.send_telegram_message(
                f"⚠️ Weak momentum exit: {position.symbol}\n"
                f"Возраст: {duration_minutes:.0f} мин\n"
                f"PNL: {pnl_pct:.2f}%"
            )

            await self.close_position(position.id)

        except Exception as e:
            logger.error(f"Ошибка timeout-проверки: {e}")

    async def scan_market(self):
        """Сканирование рынка на наличие сигналов"""
        # Проверка: бот включён?
        if not self.is_running:
            return

        if self.signals_today >= self.max_signals_per_day:
            logger.info(f"Лимит сигналов на сегодня исчерпан: {self.signals_today}")
            return

        logger.info(f"Начало сканирования рынка... ({len(self.symbols_to_scan)} символов)")

        for symbol in self.symbols_to_scan:
            # Проверка лимита
            if self.signals_today >= self.max_signals_per_day:
                break

            # Пропуск если уже есть позиция по этому символу
            if any(p.symbol == symbol for p in self.positions.values()):
                continue

            # Анализ
            try:
                smc_result = await self.smc_analyzer.analyze_symbol(symbol)
            except Exception as e:
                logger.debug(f"Пропуск {symbol}: {e}")
                continue

            # Проверяем, чтобы score был >= MIN_INDICATORS_SCORE (от 5/7)
            if smc_result['signal'] and smc_result['score'] >= config.MIN_INDICATORS_SCORE:
                logger.info(f"СИГНАЛ найден: {symbol} (score: {smc_result['score']}/{config.TOTAL_INDICATORS})")

                # Фильтр Funding Rate (защита от толпы)
                try:
                    funding_info = await self.exchange.fetch_funding_rate(symbol)
                    funding_rate = float(funding_info.get('fundingRate', 0))
                    direction = smc_result.get('direction', 'LONG')

                    if direction == 'LONG' and funding_rate > 0.0005:
                        logger.info(f"Пропуск LONG {symbol}: толпа в лонгах (funding={funding_rate:.4%})")
                        continue
                    elif direction == 'SHORT' and funding_rate < -0.0005:
                        logger.info(f"Пропуск SHORT {symbol}: толпа в шортах (funding={funding_rate:.4%})")
                        continue
                except Exception:
                    pass  # если API не поддерживает — пропускаем фильтр

                # Получение текущей цены для входа
                ticker = await self.exchange.fetch_ticker(symbol)
                entry_price = ticker['last']

                # Открытие позиции
                await self.open_position(symbol, entry_price, smc_result)

                # Пауза между сделками
                await asyncio.sleep(5)

        self.last_scan_time = datetime.now(timezone.utc)
        logger.info("Сканирование завершено")

    async def run_scanner_loop(self):
        """Цикл сканирования рынка"""
        while self.is_running:
            try:
                await self.scan_market()
                # Сканирование каждые 60 секунд (ультра-агрессивный режим)
                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"Ошибка в цикле сканирования: {e}")
                await asyncio.sleep(30)

    async def run_monitoring_loop(self):
        """Цикл мониторинга позиций"""
        while self.is_running:
            try:
                await self.monitor_positions()
                # Мониторинг каждые 2 секунды (уменьшено с 10 для точного трейлинга)
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Ошибка в цикле мониторинга: {e}")
                await asyncio.sleep(2)

    async def send_daily_report(self):
        """Отправка ежедневного отчета"""
        try:
            stats = self.db.get_daily_statistics()

            if not stats:
                return

            # Общая статистика
            all_stats = self.db.get_all_statistics()

            # Расчет среднего % в день
            avg_daily = all_stats.get('avg_daily_pct', 0) or 0

            # Начальный депозит и текущий баланс
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
        """Цикл отправки ежедневных отчетов (в 00:00 UTC)"""
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
        """Отправка часового отчёта в Telegram — результат за ДЕНЬ"""
        try:
            # Статистика закрытых сделок за сегодня из БД
            stats = self.db.get_daily_statistics()
            today_trades = stats.get('total_trades', 0) if stats else 0
            today_wins = stats.get('profitable_trades', 0) if stats else 0
            today_losses = stats.get('losing_trades', 0) if stats else 0
            today_closed_pnl = stats.get('total_pnl', 0) if stats else 0

            # Информация об открытых позициях + их текущий PnL
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
                except:
                    positions_info += f"  ⚪ {pos.symbol}: данные недоступны\n"

            if not positions_info:
                positions_info = "  Нет открытых позиций\n"

            # Итого за день = закрытые + незакрытые; % от DEPOSIT (.env), не захардкоженный 140
            total_day_pnl = today_closed_pnl + total_open_pnl
            dep = config.DEPOSIT if config.DEPOSIT > 0 else 140.0
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
            logger.info("Часовой отчёт отправлен в Telegram")

        except Exception as e:
            logger.error(f"Ошибка отправки часового отчёта: {e}")

    async def run_hourly_report_loop(self):
        """Цикл отправки часовых отчётов отключен (теперь по кнопке) — просто спим бесконечно"""
        while self.is_running:
            await asyncio.sleep(3600)

    # ========================================================================
    # TELEGRAM КОМАНДЫ И КНОПКИ
    # ========================================================================

    def get_main_keyboard(self):
        """Создает клавиатуру с кнопками для управления"""
        from telegram import ReplyKeyboardMarkup
        keyboard = [
            ['📊 Результаты', '🛑 Закрыть все'],
            ['🟢 Старт', '🔴 Стоп']
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик нажатий на кнопки (и любого текста)"""
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
            await update.message.reply_text("Выберите период отчёта:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
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
        """Отправка кастомного отчета за N часов"""
        stats = self.db.get_statistics_by_hours(hours)

        trades = stats.get('total_trades') or 0
        wins = stats.get('profitable_trades') or 0
        losses = stats.get('losing_trades') or 0
        pnl = stats.get('total_pnl') or 0.0

        dep = config.DEPOSIT if config.DEPOSIT > 0 else 140.0
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
            except:
                pass

        total_pnl = pnl + total_open_pnl
        total_pnl_pct = (total_pnl / dep) * 100

        pnl_emoji = "📈" if total_pnl >= 0 else "📉"

        message = (
            f"📊 ОТЧЁТ {label.upper()}\n"
            f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
            f"{pnl_emoji} ИТОГ: {'+' if total_pnl >= 0 else ''}${total_pnl:.2f} ({'+' if total_pnl_pct >= 0 else ''}{total_pnl_pct:.1f}%)\n\n"
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
        """Команда /start"""
        logger.info(f"Получена команда /start от {update.effective_chat.id}")
        if update.effective_chat:
            self.active_chat_ids.add(str(update.effective_chat.id))

        message = (
            f"🤖 Smart Money Aggressive Bot\n\n"
            f"Статус: {'🟢 РАБОТАЕТ' if self.is_running else '🔴 ОСТАНОВЛЕН'}\n"
            f"Позиций открыто: {len(self.positions)} (динамически)\n"
            f"Сигналов сегодня: {self.signals_today}/{self.max_signals_per_day}\n\n"
            f"Параметры:\n"
            f"- Депозит: ${config.DEPOSIT}\n"
            f"- Вход: ДИНАМИЧЕСКИЙ (капитал/слоты, макс 3)\n"
            f"- Плечо: x{config.LEVERAGE}\n"
            f"- Stop Loss: -{config.STOP_LOSS_PCT}%\n"
            f"- Режим: 🤖 ПОЛНЫЙ АВТОПИЛОТ\n\n"
            f"Используйте кнопки меню для управления!"
        )
        try:
            await update.message.reply_text(message, reply_markup=self.get_main_keyboard())
            logger.info(f"Ответ на /start отправлен с кнопками")
        except Exception as e:
            logger.error(f"Ошибка отправки кнопок: {e}")

    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /balance"""
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
        """Команда /positions"""
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
            except Exception as e:
                messages.append(f"Ошибка получения данных для {pos.symbol}")

        await update.message.reply_text("\n\n".join(messages))

    async def cmd_signals(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /signals"""
        # Упрощенная реализация - последние 10 сигналов
        message = "📡 ПОСЛЕДНИЕ СИГНАЛЫ\n\n"
        message += f"Сегодня: {self.signals_today}/{self.max_signals_per_day}\n"
        message += f"Последнее сканирование: {self.last_scan_time or 'Не было'}"
        await update.message.reply_text(message)

    async def cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /close {PAIR}"""
        if not context.args:
            await update.message.reply_text("Использование: /close {PAIR}\nПример: /close BTC")
            return

        pair = context.args[0].upper()
        symbol = f"{pair}/USDT"

        # Поиск позиции
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
        """Команда /close_all"""
        if not self.positions:
            await update.message.reply_text("Нет открытых позиций")
            return

        await update.message.reply_text(f"Закрываю {len(self.positions)} позиций...")
        await self.close_all_positions()

    async def cmd_emergency(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /emergency"""
        await update.message.reply_text("🚨 ЭКСТРЕННОЕ ЗАКРЫТИЕ ВСЕХ ПОЗИЦИЙ!")
        await self.close_all_positions(emergency=True)

    async def cmd_daily_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /daily_report"""
        await self.send_daily_report()

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /stats — полная статистика: баланс, PnL, winrate, открытые позиции"""
        try:
            # 1. Баланс с Binance
            balance = await self.exchange.fetch_balance()
            usdt_total = float(balance.get('USDT', {}).get('total', 0))
            usdt_free = float(balance.get('USDT', {}).get('free', 0))
            usdt_used = float(balance.get('USDT', {}).get('used', 0))
            total_pnl = usdt_total - config.DEPOSIT
            pnl_pct = (total_pnl / config.DEPOSIT * 100) if config.DEPOSIT > 0 else 0

            # 2. Статистика из БД (общая по всем дням)
            all_stats = self.db.get_all_statistics()
            total_trades = all_stats.get('total_trades', 0) or 0
            total_wins = all_stats.get('profitable', 0) or 0
            total_losses = all_stats.get('losing', 0) or 0
            winrate = (total_wins / total_trades * 100) if total_trades > 0 else 0

            # 3. Статистика за сегодня
            stats = self.db.get_daily_statistics()
            today_trades = stats.get('total_trades', 0) if stats else 0
            today_wins = stats.get('profitable_trades', 0) if stats else 0
            today_losses = stats.get('losing_trades', 0) if stats else 0
            today_pnl = stats.get('total_pnl', 0) if stats else 0
            today_winrate = (today_wins / today_trades * 100) if today_trades > 0 else 0

            # 4. Открытые позиции с текущим PnL
            positions_text = ""
            unrealized_pnl = 0.0
            if self.positions:
                for pid, pos in self.positions.items():
                    try:
                        ticker = await self.exchange.fetch_ticker(pos.symbol)
                        current_price = ticker['last']
                        if pos.side == 'SHORT':
                            price_change = ((pos.entry_price - current_price) / pos.entry_price) * 100
                        else:
                            price_change = ((current_price - pos.entry_price) / pos.entry_price) * 100
                        roe = price_change * pos.leverage
                        pos_pnl = pos.realized_pnl_usd + (current_price - pos.entry_price) * pos.remaining_quantity
                        if pos.side == 'SHORT':
                            pos_pnl = pos.realized_pnl_usd + (pos.entry_price - current_price) * pos.remaining_quantity
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

            # 5. Формируем сообщение
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
        """Команда /stop_trading — остановить торговлю и закрыть позиции с минимальными потерями"""
        if not self.is_running:
            await update.message.reply_text("🔴 Бот уже остановлен!")
            return

        self.is_running = False
        await update.message.reply_text(
            "🟡 РЕЖИМ ЗАВЕРШЕНИЯ ТОРГОВЛИ\n"
            "Новые сделки не открываются.\n"
            "Закрываю убыточные позиции...\n"
            "Прибыльные оставляю с трейлингом."
        )

        # Закрываем только убыточные позиции
        closed_count = 0
        kept_count = 0
        for pid, pos in list(self.positions.items()):
            try:
                ticker = await self.exchange.fetch_ticker(pos.symbol)
                current_price = ticker['last']
                if pos.side == 'SHORT':
                    price_change_pct = ((pos.entry_price - current_price) / pos.entry_price) * 100
                else:
                    price_change_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100
                pnl_usd = pos.realized_pnl_usd + (current_price - pos.entry_price) * pos.remaining_quantity
                if pos.side == 'SHORT':
                    pnl_usd = pos.realized_pnl_usd + (pos.entry_price - current_price) * pos.remaining_quantity

                # Закрываем если в убытке или на безубытке
                if pnl_usd < 0:
                    await self.close_position(pid)
                    closed_count += 1
                    logger.info(f"Закрыта убыточная позиция {pos.symbol}: PnL=${pnl_usd:.2f}")
                else:
                    # Прибыльную оставляем, но двигаем SL в безубыток
                    if pos.side == 'SHORT':
                        new_sl = pos.entry_price * 1.005  # SL чуть выше входа
                    else:
                        new_sl = pos.entry_price * 0.995  # SL чуть ниже входа
                    pos.dynamic_sl_level = 1
                    kept_count += 1
                    logger.info(f"Прибыльная позиция {pos.symbol} оставлена с PnL=${pnl_usd:.2f}")
            except Exception as e:
                logger.error(f"Ошибка обработки позиции {pos.symbol}: {e}")

        msg = (
            f"✅ РЕЖИМ ЗАВЕРШЕНИЯ АКТИВИРОВАН\n"
            f"Закрыто убыточных: {closed_count}\n"
            f"Оставлено прибыльных: {kept_count}\n"
            f"Оставшиеся позиции будут закрыты мониторингом."
        )
        await update.message.reply_text(msg)
        await self.send_telegram_message(msg)

    async def cmd_start_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start_bot — включить бота"""
        if self.is_running:
            await update.message.reply_text("🟢 Бот уже работает!")
            return
        self.is_running = True
        await update.message.reply_text(
            "🟢 БОТ ВКЛЮЧЁН!\n"
            "Сканирование и торговля возобновлены."
        )
        await self.send_telegram_message("🟢 Бот включён оператором. Торговля возобновлена.")

    async def cmd_stop_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /stop_bot — выключить бота (без закрытия позиций)"""
        if not self.is_running:
            await update.message.reply_text("🔴 Бот уже остановлен!")
            return
        self.is_running = False
        await update.message.reply_text(
            "🔴 БОТ ВЫКЛЮЧЕН!\n"
            "Новые сделки не открываются.\n"
            "Открытые позиции остаются на месте.\n"
            "Для закрытия всех позиций: /close_all"
        )
        await self.send_telegram_message("🔴 Бот выключен оператором. Новые сделки не открываются.")

    async def run_telegram_bot(self):
        """Запуск Telegram бота с авто-перезапуском"""

        app = None

        while self.is_running:
            try:
                logger.info("Инициализация Telegram бота...")
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
                logger.info("Telegram бот запускается асинхронно...")

                await app.initialize()
                await app.start()
                await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

                while self.is_running:
                    await asyncio.sleep(5)

            except telegram.error.Conflict as e:
                logger.warning(f"TG Conflict (другой экземпляр бота запущен): {e}. Повтор через 30 сек...")
                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"Ошибка Telegram бота: {e}")

            finally:
                if app is not None:
                    try:
                        if hasattr(app, 'updater') and app.updater.running:
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
                logger.info("Перезапуск Telegram бота через 15 сек...")
                await asyncio.sleep(15)

    async def start(self) -> bool:
        """Запуск бота. Возвращает False, если биржа недоступна (процесс можно завершить с кодом ≠ 0)."""
        logger.info("Запуск Smart Money Aggressive Bot...")

        # Подключение к бирже
        if not await self.connect():
            logger.error("Не удалось подключиться к бирже")
            try:
                await self.disconnect()
            except Exception:
                pass
            return False

        self.is_running = True

        # Уведомление о запуске (будет отправлено после инициализации Telegram)
        telegram_started = False

        # Запуск задач с логированием
        async def task_with_log(name, coro):
            try:
                await coro
            except Exception as e:
                logger.error(f"Task '{name}' finished with error: {e}")
            finally:
                logger.warning(f"Task '{name}' finished!")

        # Уведомление о запуске
        try:
            stats = self.db.get_all_statistics()
            total_pnl = float(stats.get('total_pnl') or 0.0)
            virtual_eq = max(config.DEPOSIT + total_pnl, config.DEPOSIT * 0.5)
            await self.send_telegram_message(
                f"🟢 БОТ ВКЛЮЧЁН\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 Депозит: ${config.DEPOSIT:.2f}\n"
                f"📊 Виртуальный баланс: ${virtual_eq:.2f}\n"
                f"⚙️ Плечо: x{config.LEVERAGE}\n"
                f"🛡 SL: {config.STOP_LOSS_PCT}% ({config.STOP_LOSS_PCT * config.LEVERAGE:.0f}% ROE)\n"
                f"🎯 TP: {config.PARTIAL_TP1_PCT:.0f}% / {config.PARTIAL_TP2_PCT:.0f}% / {config.PARTIAL_TP3_PCT:.0f}% ROE\n"
                f"📡 Монет: {len(self.symbols_to_scan)}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"SMART MONEY BOT v2.0"
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить стартовое сообщение: {e}")

        tasks = [
            asyncio.create_task(task_with_log("scanner", self.run_scanner_loop())),
            asyncio.create_task(task_with_log("monitoring", self.run_monitoring_loop())),
            asyncio.create_task(task_with_log("daily_report", self.run_daily_report_loop())),
            asyncio.create_task(task_with_log("hourly_report", self.run_hourly_report_loop())),
            asyncio.create_task(task_with_log("telegram", self.run_telegram_bot())),
            asyncio.create_task(task_with_log("fear_greed", check_fear_greed_index(self))),
        ]

        # Ждём завершения всех задач (gather завершится только если все упадут)
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Если дошли сюда — все задачи завершились, что-то пошло не так
        logger.error(f"All tasks finished! Results: {results}")

        # Ждём перед возможным перезапуском из main.py
        await asyncio.sleep(60)
        return True

    async def stop(self):
        """Остановка бота"""
        logger.info("Остановка бота...")
        self.is_running = False
        await self.disconnect()


# ============================================================================
# ЗАПУСК
# ============================================================================
def _env_secret(*names: str) -> str:
    """Читает первую непустую переменную окружения, убирает пробелы и BOM (частая причина -2015)."""
    for n in names:
        raw = os.getenv(n)
        if raw is None:
            continue
        s = raw.strip().strip('\ufeff').strip('"').strip("'")
        if s:
            return s
    return ''
async def main():
    """Точка входа"""
    from dotenv import load_dotenv

    _root = Path(__file__).resolve().parent
    load_dotenv(_root / '.env')

    API_KEY = _env_secret('BINANCE_API_KEY')
    API_SECRET = _env_secret('BINANCE_SECRET', 'BINANCE_API_SECRET')
    TELEGRAM_TOKEN = _env_secret('TELEGRAM_BOT_TOKEN')
    TELEGRAM_CHAT_ID = _env_secret('TELEGRAM_CHAT_ID')
    USER_CHAT_ID = _env_secret('USER_CHAT_ID')

    # Загрузка параметров стратегии из .env
    config.DEPOSIT = float(os.getenv('DEPOSIT', config.DEPOSIT))
    config.ENTRY_AMOUNT = float(os.getenv('ENTRY_AMOUNT', config.ENTRY_AMOUNT))
    config.LEVERAGE = int(os.getenv('LEVERAGE', config.LEVERAGE))
    config.STOP_LOSS_PCT = float(os.getenv('STOP_LOSS', config.STOP_LOSS_PCT))
    config.REINVEST_PROFITS = os.getenv('REINVEST_PROFITS', 'True').lower() == 'true'
    config.DRAWDOWN_ALERT = float(os.getenv('DRAWDOWN_ALERT', '12.0'))
    use_testnet = os.getenv('BINANCE_TESTNET', 'False').lower() == 'true'

    # Проверка наличия ключей
    if not all([API_KEY, API_SECRET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID]):
        logger.warning("⚠️ Не настроены переменные окружения! Бот будет работать только с aiohttp сервером.")
        logger.warning("   Установите: BINANCE_API_KEY, BINANCE_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID")

    # Создание и запуск бота
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
            logger.warning("⚠️ Бот не подключился к Binance. Повторная попытка через 60 сек...")
            # Не выходим, а ждём и пробуем снова
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