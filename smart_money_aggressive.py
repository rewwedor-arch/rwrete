
# ===== MARKET FILTERS =====
BTC_TREND_FILTER = True
HIGH_TIMEFRAME_CONFIRMATION = True



# ===== EXTRA MON PROTECTION =====
def is_blacklisted_symbol(symbol):
    s = symbol.upper()
    return (
        s in [x.upper() for x in BLACKLIST_SYMBOLS]
        or "MON" in s
    )


# ===== BLACKLIST =====
BLACKLIST_SYMBOLS = [
    "SPACE/USDT:USDT",
    "MON/USDT:USDT",
]


# ===== RUNNER MODE =====
RUNNER_MODE = True
RUNNER_STEP_PERCENT = 100
RUNNER_CLOSE_PERCENT = 10


# ===== BINANCE DYNAMIC LEVERAGE =====
async def get_max_leverage(exchange, symbol):
    try:
        markets = await exchange.load_markets()
        market = markets.get(symbol)

        if market and 'limits' in market:
            leverage_info = market.get('limits', {}).get('leverage', {})

            max_lev = leverage_info.get('max')

            if max_lev:
                return int(max_lev)

        # fallback через tiers
        if hasattr(exchange, 'fetch_leverage_tiers'):
            tiers = await exchange.fetch_leverage_tiers([symbol])

            if tiers and symbol in tiers:
                first_tier = tiers[symbol][0]

                if 'maxLeverage' in first_tier:
                    return int(first_tier['maxLeverage'])

        return 125

    except Exception as e:
        print(f"Max leverage fetch error for {symbol}: {e}")
        return 125



# ===== SINGLE INSTANCE PROTECTION =====
import os

LOCK_FILE = "/tmp/smart_money_bot.lock"

try:
    if os.path.exists(LOCK_FILE):
        print("Another bot instance already running")
    else:
        with open(LOCK_FILE, "w") as f:
            f.write("running")
except Exception:
    pass



# ===== SAFE LEVERAGE FIX =====
def get_safe_leverage(symbol, requested_leverage=75):
    try:
        limits = {
            "BTC": 125,
            "ETH": 100,
            "BNB": 75,
            "SOL": 50,
            "XRP": 75,
            "ZEC": 50,
            "MON": 10,
        }

        for coin, max_lev in limits.items():
            if coin in symbol.upper():
                return min(requested_leverage, max_lev)

        return min(requested_leverage, 20)

    except Exception:
        return 20



# ===== REPORT COOLDOWN =====
LAST_HOURLY_REPORT = 0
HOURLY_REPORT_INTERVAL = 3600


# ===== TITAN INSTITUTIONAL FILTER =====
ENABLE_HTF_TREND = True
ENABLE_LIQUIDITY_SWEEP = True
ENABLE_FVG_RETEST = True
ENABLE_ORDER_BLOCK = True
ENABLE_VOLUME_SPIKE = True
ENABLE_SESSION_FILTER = True

MIN_ADX = 20
MIN_VOLUME_RATIO = 2.0
MIN_RR_RATIO = 2.5
MAX_OPEN_POSITIONS = 3

def institutional_filter(
    adx,
    volume_ratio,
    ema_trend,
    macd_ok,
    bos,
    fvg,
    liquidity_sweep,
    order_block,
    rsi
):
    score = 0

    if adx >= 25:
        score += 1

    if volume_ratio >= 1.8:
        score += 1

    if ema_trend:
        score += 1

    if macd_ok:
        score += 1

    if bos:
        score += 1

    if fvg:
        score += 1

    if liquidity_sweep:
        score += 1

    if order_block:
        score += 1

    if 55 <= rsi <= 70:
        score += 1

    return score >= 6






# ===== WEAK SIGNAL BLOCKER =====
def reject_weak_sm_signal(sm_score):
    if sm_score < 5:
        return True
    return False

# ===== LOSS COOLDOWN PROTECTION =====
LAST_LOSS_TIME = 0
LOSS_COOLDOWN_SECONDS = 1800

# ===== HIGH LEVERAGE PROTECTION =====
MAX_OPEN_POSITIONS = 3
USE_ISOLATED_MARGIN = True
ENABLE_DYNAMIC_SL = True
ENABLE_TRAILING_PROFIT_LOCK = True
MOVE_SL_TO_BREAKEVEN_AT = 8.5
MIN_RR_RATIO = 2.5


# ===== SAFE RISK MANAGEMENT =====
MAX_DAILY_LOSS_PERCENT = 8
MAX_CONSECUTIVE_LOSSES = 2
ENABLE_BREAKEVEN = True
BREAKEVEN_AT_PERCENT = 5.0
TRAILING_AFTER_PERCENT = 8.0
PARTIAL_TP_ENABLED = True


# ===== SMART MONEY SETTINGS =====
ENABLE_SMART_MONEY = True
ENABLE_BOS = True
ENABLE_FVG = True
ENABLE_EMA_FILTER = True
ENABLE_MACD_CONFIRMATION = True
ENABLE_RSI_FILTER = True
ENABLE_ADX_FILTER = True

MIN_SIGNAL_SCORE = 34
EMA_PERIOD = 200
RSI_LONG_MIN = 58
ADX_MIN = 22

MAX_VOLATILITY_PCT = 8.0
MAX_SINGLE_CANDLE_PCT = 5.0
SIDEWAYS_ADX_THRESHOLD = 12
SIDEWAYS_EMA_DISTANCE = 0.08

class SmartTrailingMixin:
    def calculate_dynamic_trailing(self, profit_pct: float) -> float:
        if profit_pct >= 25:
            return 0.7
        elif profit_pct >= 15:
            return 1.0
        elif profit_pct >= 8:
            return 2.0
        return 4.0

#!/usr/bin/env python3
"""
Smart Money Aggressive Trading Bot — ИСПРАВЛЕННАЯ ВЕРСИЯ
Патч-лист:
  1. SL реально выставляется на бирже (был pass)
  2. STOP_LOSS_PCT = 1.5% (был 0.25% — выбивало на шуме)
  3. Фильтр волатильности перед входом
  4. Часовой отчёт (run_hourly_report_loop) — период 3600 сек вместо 1800
  5. Telegram команды /report1h /report5h /report24h работают без update.message.reply_text crash
  6. Trailing stop loop исправлен: SHORT SL движется правильно
  7. Программный SL согласован с биржевым (не дублирует)
  8. Частичные TP пересчитаны на адекватные уровни
"""

import asyncio
import logging
import sqlite3
import os
import sys
import threading


# ===== UTF-8 SAFE OUTPUT =====
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
elif sys.stdout:
    import codecs
    try:
        if hasattr(sys.stdout, 'buffer'):
            sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer)
    except Exception:
        pass

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field
from pathlib import Path
import json

from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

import ccxt.async_support as ccxt

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('smart_money_bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


async def task_with_log(task_name: str, coro):
    try:
        logger.info(f"🚀 Запуск задачи: {task_name}")
        result = await coro
        logger.info(f"✅ Задача завершена: {task_name}")
        return result
    except asyncio.CancelledError:
        logger.warning(f"⚠️ Задача отменена: {task_name}")
        raise
    except Exception as e:
        logger.exception(f"❌ Ошибка в задаче {task_name}: {e}")
        return None


# ============================================================================
# ПАРАМЕТРЫ СТРАТЕГИИ
# ============================================================================

MAX_CONSECUTIVE_LOSSES = 2
MIN_VOLUME_RATIO = 1.5        # Было 1.0 — слишком много мусорных сигналов
MIN_ADX = 20                  # Было 10 — входил в боковик без тренда
TRADE_COOLDOWN_MINUTES = 1
LOSS_COOLDOWN_MINUTES = 3

class StrategyConfig:
    """Конфигурация стратегии SMART MONEY"""
    DEPOSIT: float = 50.0
    ENTRY_AMOUNT: float = 10.0
    LEVERAGE: int = 75  # Максимальное плечо для агрессивного разгона

    # === SL/TP ДЛЯ АГРЕССИВНОГО СКАЛЬПИНГА x75 ===
    STOP_LOSS_PCT: float = 0.8       # -60% ROE: жёсткий SL
    TAKE_PROFIT_PCT: float = 1.5     # TP1 на бирже
    TAKE_PROFIT: float = 0.8
    TP2_PCT: float = 2.5             # TP2 на бирже
    TP3_PCT: float = 4.0             # TP3 на бирже (closePosition)

    DAILY_TARGET_MIN: float = 5.0
    DAILY_TARGET_MAX: float = 15.0

    WORK_HOURS: str = "24/7"
    DIRECTION: str = "BOTH"

    MIN_INDICATORS_SCORE: int = 2
    TOTAL_INDICATORS: int = 7

    SCANNER_TIMEFRAME: str = '5m'
    TREND_TIMEFRAME: str = '15m'
    EMA_TIMEFRAME: str = '1h'

    PROFIT_ALERT_10: float = 50.0
    PROFIT_ALERT_15: float = 150.0
    PROFIT_ALERT_40: float = 300.0
    DRAWDOWN_ALERT: float = 12.0

    REINVEST_PROFITS: bool = True
    MIN_SLOT_USDT: float = 5.0       # Мин $5 — позволяет 5+ мелких позиций

    # Откат от пика
    MIN_PEAK_PNL_TO_TRACK: float = 25.0
    PEAK_DRAWDOWN_CLOSE_PCT: float = 8.0
    # Трейлинг
    TRAILING_ACTIVATE_PCT: float = 15.0     # +15% ROE — включаем трейлинг
    TRAILING_DRAWDOWN_CLOSE_PCT: float = 5.0 # Откат 5% от пика — закрываем
    # Частичные TP (в % ROE)
    PARTIAL_TP1_PCT: float = 20.0    # +20% ROE → закрыть 40%, SL→безубыток
    PARTIAL_TP2_PCT: float = 45.0    # +45% ROE → закрыть 30%
    PARTIAL_TP3_PCT: float = 80.0    # +80% ROE → закрыть остаток

    POSITION_TIMEOUT_HOURS: int = 4  # Макс 4 часа на позицию

    # Трейлинг-стоп по цене
    TRAILING_DISTANCE_PCT: float = 0.2
    TRAILING_BREAKEVEN_PCT: float = 0.15

    # Фильтр волатильности
    MIN_VOLATILITY_PCT: float = 0.05
    MAX_VOLATILITY_PCT: float = 15.0

config = StrategyConfig()

ALLOW_TRADING = True


async def check_fear_greed_index(bot: 'SmartMoneyBot'):
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
                            if value < 10 and ALLOW_TRADING:
                                ALLOW_TRADING = False
                                await bot.send_telegram_message(
                                    f"⚠️ Паника на рынке!\nFear & Greed: {value} ({classification})\nНовые сделки приостановлены."
                                )
                            elif value >= 25 and not ALLOW_TRADING:
                                ALLOW_TRADING = True
                                await bot.send_telegram_message(
                                    f"✅ Рынок успокоился.\nFear & Greed: {value} ({classification})\nТорговля возобновлена."
                                )
        except Exception as e:
            logger.error(f"F&G index error: {e}")
        await asyncio.sleep(14400)


async def trailing_stop_loop(bot: 'SmartMoneyBot'):
    """Фоновый трейлинг-стоп каждые 10 секунд"""
    while bot.is_running:
        try:
            for pid, pos in list(bot.positions.items()):
                try:
                    ticker = await bot.exchange.fetch_ticker(pos.symbol)
                    current_price = ticker['last']

                    if pos.side == 'SHORT':
                        price_change_pct = ((pos.entry_price - current_price) / pos.entry_price) * 100
                    else:
                        price_change_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100

                    if pos.trailing_active:
                        # Получаем пиковое движение в процентах от цены (trailing_peak у нас в ROE)
                        peak_price_pct = pos.trailing_peak / pos.leverage

                        if pos.side == 'SHORT':
                            new_sl_price = pos.entry_price * (1 - (peak_price_pct - config.TRAILING_DISTANCE_PCT) / 100)
                            should_update = new_sl_price < pos.stop_loss
                            min_sl = pos.entry_price * (1 - config.TRAILING_BREAKEVEN_PCT / 100)
                            if new_sl_price > min_sl:
                                new_sl_price = min_sl
                        else:
                            new_sl_price = pos.entry_price * (1 + (peak_price_pct - config.TRAILING_DISTANCE_PCT) / 100)
                            should_update = new_sl_price > pos.stop_loss
                            min_sl = pos.entry_price * (1 + config.TRAILING_BREAKEVEN_PCT / 100)
                            if new_sl_price < min_sl:
                                new_sl_price = min_sl

                        if should_update:
                            new_sl_price = float(bot.exchange.price_to_precision(pos.symbol, new_sl_price))
                            old_sl = pos.stop_loss
                            pos.stop_loss = new_sl_price
                            logger.info(f"Трейлинг SL {pos.symbol}: {old_sl:.4f} → {new_sl_price:.4f}")

                            try:
                                qty_rounded = float(bot.exchange.amount_to_precision(pos.symbol, pos.remaining_quantity))
                                if qty_rounded <= 0:
                                    continue
                                await bot.exchange.cancel_all_orders(pos.symbol)
                                # Защита от Binance -2022 ReduceOnly rejected
                                positions_now = await bot.exchange.fetch_positions([pos.symbol])
                                active_pos = next(
                                    (p for p in positions_now if abs(float(p.get('contracts', 0) or 0)) > 0),
                                    None
                                )
                                if not active_pos:
                                    logger.warning(f"Позиция {pos.symbol} уже закрыта, SL/TP не обновляем")
                                    continue

                                if pos.side == 'SHORT':
                                    actual_tp = float(bot.exchange.price_to_precision(
                                        pos.symbol, pos.entry_price * (1 - config.TP3_PCT / 100)))
                                    await bot.exchange.create_order(
                                        pos.symbol, 'STOP_MARKET', 'BUY', qty_rounded,
                                        params={'stopPrice': new_sl_price, 'closePosition': True, 'workingType': 'MARK_PRICE'}
                                    )
                                    await bot.exchange.create_order(
                                        pos.symbol, 'TAKE_PROFIT_MARKET', 'BUY', qty_rounded,
                                        params={'stopPrice': actual_tp, 'closePosition': True, 'workingType': 'MARK_PRICE'}
                                    )
                                else:
                                    actual_tp = float(bot.exchange.price_to_precision(
                                        pos.symbol, pos.entry_price * (1 + config.TP3_PCT / 100)))
                                    await bot.exchange.create_order(
                                        pos.symbol, 'STOP_MARKET', 'SELL', qty_rounded,
                                        params={'stopPrice': new_sl_price, 'closePosition': True, 'workingType': 'MARK_PRICE'}
                                    )
                                    await bot.exchange.create_order(
                                        pos.symbol, 'TAKE_PROFIT_MARKET', 'SELL', qty_rounded,
                                        params={'stopPrice': actual_tp, 'closePosition': True, 'workingType': 'MARK_PRICE'}
                                    )
                            except Exception as e:
                                logger.warning(f"Не удалось обновить SL/TP {pos.symbol}: {e}")
                except Exception as e:
                    logger.error(f"Ошибка трейлинга {pos.symbol}: {e}")
        except Exception as e:
            logger.error(f"Ошибка в trailing_stop_loop: {e}")
        await asyncio.sleep(10)


# ============================================================================
# БАЗА ДАННЫХ
# ============================================================================

class Database:
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

        conn.commit()
        try:
            ref = float(os.getenv('DEPOSIT', '50') or '140')
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

    def add_position(self, symbol, side, entry_price, stop_loss, take_profit,
                     amount_usdt, leverage, quantity, smc_score, bos_info,
                     fvg_detected, rsi_value, adx_value) -> int:
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

    def update_position(self, position_id, close_price, pnl, pnl_pct, status='CLOSED'):
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

    def add_signal(self, symbol, signal_type, entry_price, smc_score, indicators) -> int:
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

    def mark_signal_executed(self, signal_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('UPDATE signals SET executed = 1 WHERE id = ?', (signal_id,))
        conn.commit()
        conn.close()

    def update_daily_statistics(self, pnl, pnl_pct, count_as_trade=True, equity_reference=config.DEPOSIT):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        ref = float(equity_reference) if equity_reference and equity_reference > 0 else 140.0

        cursor.execute('SELECT * FROM statistics WHERE date = ?', (today,))
        row = cursor.fetchone()

        if row:
            if count_as_trade:
                cursor.execute('''
                    UPDATE statistics
                    SET total_trades = total_trades + 1,
                        profitable_trades = profitable_trades + ?,
                        losing_trades = losing_trades + ?,
                        total_pnl = total_pnl + ?,
                        best_trade = MAX(COALESCE(best_trade, 0), ?),
                        worst_trade = MIN(COALESCE(worst_trade, 0), ?)
                    WHERE date = ?
                ''', (1 if pnl > 0 else 0, 1 if pnl < 0 else 0, pnl, pnl, pnl, today))
            else:
                cursor.execute('''
                    UPDATE statistics
                    SET total_pnl = total_pnl + ?,
                        best_trade = MAX(COALESCE(best_trade, 0), ?),
                        worst_trade = MIN(COALESCE(worst_trade, 0), ?)
                    WHERE date = ?
                ''', (pnl, pnl, pnl, today))
        else:
            if count_as_trade:
                cursor.execute('''
                    INSERT INTO statistics (date, total_trades, profitable_trades,
                                            losing_trades, total_pnl, total_pnl_pct,
                                            best_trade, worst_trade)
                    VALUES (?, 1, ?, ?, ?, 0, ?, ?)
                ''', (today, 1 if pnl > 0 else 0, 1 if pnl < 0 else 0, pnl, pnl, pnl))
            else:
                cursor.execute('''
                    INSERT INTO statistics (date, total_trades, profitable_trades,
                                            losing_trades, total_pnl, total_pnl_pct,
                                            best_trade, worst_trade)
                    VALUES (?, 0, 0, 0, ?, 0, ?, ?)
                ''', (today, pnl, pnl, pnl))

        cursor.execute('SELECT total_pnl FROM statistics WHERE date = ?', (today,))
        total_row = cursor.fetchone()
        total_day = float(total_row[0]) if total_row and total_row[0] is not None else 0.0
        day_pct = (total_day / ref) * 100.0 if ref > 0 else 0.0
        cursor.execute('UPDATE statistics SET total_pnl_pct = ? WHERE date = ?', (day_pct, today))
        conn.commit()
        conn.close()

    def get_daily_statistics(self, date=None) -> Optional[Dict]:
        if not date:
            date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM statistics WHERE date = ?', (date,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def add_alert(self, position_id, alert_type, message):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('INSERT INTO alerts (position_id, alert_type, message) VALUES (?, ?, ?)',
                       (position_id, alert_type, message))
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
# SMC АНАЛИЗАТОР
# ============================================================================

class SMCAnalyzer:
    def __init__(self, exchange: ccxt.binanceusdm):
        self.exchange = exchange

    async def get_ohlcv(self, symbol, timeframe, limit=100) -> List[List]:
        try:
            return await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        except Exception as e:
            if "-1122" not in str(e) and "Invalid symbol" not in str(e):
                logger.error(f"Ошибка OHLCV {symbol}: {e}")
            return []

    def calculate_ema(self, prices, period) -> List[float]:
        if len(prices) < period:
            return []
        ema = []
        multiplier = 2 / (period + 1)
        sma = sum(prices[:period]) / period
        ema.append(sma)
        for i in range(period, len(prices)):
            ema.append((prices[i] - ema[-1]) * multiplier + ema[-1])
        return ema

    def calculate_rsi(self, prices, period=14) -> List[float]:
        if len(prices) < period + 1:
            return []
        rsi = []
        gains, losses = [], []
        for i in range(1, len(prices)):
            change = prices[i] - prices[i-1]
            gains.append(max(0, change))
            losses.append(max(0, -change))
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        rsi.append(100 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss)))
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            rsi.append(100 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss)))
        return rsi

    def calculate_adx(self, high, low, close, period=14) -> List[float]:
        if len(close) < period + 1:
            return []
        plus_dm, minus_dm, tr = [], [], []
        for i in range(1, len(close)):
            tr.append(max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1])))
            pdm = max(0, high[i] - high[i-1]) if high[i] - high[i-1] > low[i-1] - low[i] else 0
            mdm = max(0, low[i-1] - low[i]) if low[i-1] - low[i] > high[i] - high[i-1] else 0
            plus_dm.append(pdm)
            minus_dm.append(mdm)
        atr = sum(tr[:period]) / period
        plus_di = [(sum(plus_dm[:period]) / atr) * 100]
        minus_di = [(sum(minus_dm[:period]) / atr) * 100]
        dx = []
        if plus_di[0] + minus_di[0] > 0:
            dx.append(abs(plus_di[0] - minus_di[0]) / (plus_di[0] + minus_di[0]) * 100)
        else:
            dx.append(0)
        adx = [sum(dx[:period]) / period] if len(dx) >= period else [0]
        for i in range(period, len(tr)):
            atr = (atr * (period - 1) + tr[i]) / period
            pdi = ((plus_di[-1] * (period - 1) + (plus_dm[i] / atr) * 100) / period) if atr > 0 else 0
            mdi = ((minus_di[-1] * (period - 1) + (minus_dm[i] / atr) * 100) / period) if atr > 0 else 0
            plus_di.append(pdi)
            minus_di.append(mdi)
            dx_val = abs(pdi - mdi) / (pdi + mdi) * 100 if pdi + mdi > 0 else 0
            dx.append(dx_val)
            adx.append((adx[-1] * (period - 1) + dx_val) / period)
        return adx

    def calculate_macd(self, prices, fast=12, slow=26, signal=9) -> Dict:
        if len(prices) < slow + signal:
            return {'macd': 0, 'signal': 0, 'histogram': 0}
        ema_fast = self.calculate_ema(prices, fast)
        ema_slow = self.calculate_ema(prices, slow)
        if len(ema_fast) < signal or len(ema_slow) < signal:
            return {'macd': 0, 'signal': 0, 'histogram': 0}
        min_len = min(len(ema_fast), len(ema_slow))
        macd_line = [f - s for f, s in zip(ema_fast[-min_len:], ema_slow[-min_len:])]
        signal_line = self.calculate_ema(macd_line, signal)
        if not signal_line:
            return {'macd': 0, 'signal': 0, 'histogram': 0}
        return {
            'macd': macd_line[-1],
            'signal': signal_line[-1],
            'histogram': macd_line[-1] - signal_line[-1]
        }

    def calculate_sma(self, prices, period) -> List[float]:
        if len(prices) < period:
            return []
        return [sum(prices[i-period:i]) / period for i in range(period, len(prices) + 1)]

    def detect_bos_choch(self, ohlcv) -> str:
        if len(ohlcv) < 20:
            return "NONE"
        closes = [c[4] for c in ohlcv]
        highs = [h[2] for h in ohlcv]
        lows = [l[3] for l in ohlcv]
        highest = max(highs[-11:-1])
        lowest = min(lows[-11:-1])
        current_price = closes[-1]
        if current_price > highest:
            return "CHoCH_BULLISH" if closes[-15] > closes[-5] else "BOS_UP"
        if current_price < lowest:
            return "CHoCH_BEARISH" if closes[-15] < closes[-5] else "BOS_DOWN"
        return "NONE"

    def detect_liquidity_sweep(self, ohlcv) -> str:
        if len(ohlcv) < 15:
            return 'NONE'
        c2 = ohlcv[-2]
        local_lows = min([x[3] for x in ohlcv[-15:-2]])
        local_highs = max([x[2] for x in ohlcv[-15:-2]])
        
        # Bullish Sweep: wick below local low, close above
        if c2[3] < local_lows and c2[4] > local_lows:
            return 'BULLISH'
        # Bearish Sweep: wick above local high, close below
        if c2[2] > local_highs and c2[4] < local_highs:
            return 'BEARISH'
        return 'NONE'

    def detect_fvg(self, ohlcv) -> str:
        if len(ohlcv) < 5:
            return ''
        current_price = ohlcv[-1][4]
        # Scan last 10 candles for FVG (wider window)
        start = max(0, len(ohlcv) - 12)
        for i in range(start, len(ohlcv) - 2):
            c1, c2, c3 = ohlcv[i], ohlcv[i+1], ohlcv[i+2]
            high1, low1 = c1[2], c1[3]
            high2, low2 = c2[2], c2[3]
            high3, low3 = c3[2], c3[3]
            # Bullish FVG: gap between candle 1 high and candle 3 low
            if low3 > high1:
                return 'BULLISH'
            # Bearish FVG: gap between candle 1 low and candle 3 high
            if high3 < low1:
                return 'BEARISH'
        return ''

    def detect_order_block(self, ohlcv) -> str:
        if len(ohlcv) < 10:
            return 'NONE'
        for i in range(len(ohlcv)-10, len(ohlcv)-2):
            c1, c2, c3 = ohlcv[i], ohlcv[i+1], ohlcv[i+2]
            # Bullish OB: down candle followed by up candles
            if c1[4] < c1[1] and c2[4] > c2[1] and c3[4] > c3[1]:
                move_pct = (c3[4] - c1[4]) / c1[4] * 100
                if move_pct > 0.15:
                    return 'BULLISH'
            # Bearish OB: up candle followed by down candles
            if c1[4] > c1[1] and c2[4] < c2[1] and c3[4] < c3[1]:
                move_pct = (c1[4] - c3[4]) / c1[4] * 100
                if move_pct > 0.15:
                    return 'BEARISH'
        return 'NONE'


    async def analyze_symbol(self, symbol: str) -> Dict[str, Any]:
        result = {
            'symbol': symbol, 'score': 0, 'direction': 'LONG',
            'indicators': {}, 'signal': False, 'bos': 'NONE',
            'fvg': False, 'rsi': 0, 'adx': 0, 'macd': {},
            'ema200': 0, 'volume_ok': False
        }
        try:
            ohlcv_5m = await self.get_ohlcv(symbol, config.SCANNER_TIMEFRAME, limit=100)
            ohlcv_15m = await self.get_ohlcv(symbol, config.TREND_TIMEFRAME, limit=100)
            ohlcv_1h = await self.get_ohlcv(symbol, config.EMA_TIMEFRAME, limit=100)
            if not ohlcv_5m or not ohlcv_15m or not ohlcv_1h:
                return result

            closes_5m = [c[4] for c in ohlcv_5m]
            highs_5m = [h[2] for h in ohlcv_5m]
            lows_5m = [l[3] for l in ohlcv_5m]
            volumes_5m = [v[5] for v in ohlcv_5m]
            current_price = closes_5m[-1]

            closes_1h = [c[4] for c in ohlcv_1h]
            ema200_1h = self.calculate_ema(closes_1h, 200)
            htf_trend = 'NONE'
            if ema200_1h:
                htf_trend = 'LONG' if closes_1h[-1] > ema200_1h[-1] else 'SHORT'

            long_score, short_score = 0, 0
            long_ind, short_ind = {}, {}
            vol_ratio = 0

            # 1. BOS/CHoCH
            bos = self.detect_bos_choch(ohlcv_15m)
            result['bos'] = bos
            if bos in ['BOS_UP', 'CHoCH_BULLISH']:
                long_score += 1; long_ind['bos'] = True
            if bos in ['BOS_DOWN', 'CHoCH_BEARISH']:
                short_score += 1; short_ind['bos'] = True

            # 2. FVG
            fvg = self.detect_fvg(ohlcv_5m[-20:])
            result['fvg'] = bool(fvg)
            if fvg == 'BULLISH':
                long_score += 1; long_ind['fvg'] = True
            if fvg == 'BEARISH':
                short_score += 1; short_ind['fvg'] = True

            # 3. EMA50 тренд
            ema50 = self.calculate_ema(closes_5m, 50)
            if not ema50:
                result['signal'] = False
                result['score'] = "NO_EMA"
                return result
            result['ema200'] = ema50[-1]
            if current_price > ema50[-1]:
                long_score += 1; long_ind['ema50_trend'] = True
            else:
                short_score += 1; short_ind['ema50_trend'] = True

            # 4. RSI
            rsi = self.calculate_rsi(closes_5m, 14)
            if rsi and len(rsi) >= 2:
                result['rsi'] = rsi[-1]
                if 40 <= rsi[-1] <= 70 and rsi[-1] > rsi[-2]:
                    long_score += 1; long_ind['rsi_momentum'] = True
                if 30 <= rsi[-1] <= 60 and rsi[-1] < rsi[-2]:
                    short_score += 1; short_ind['rsi_momentum'] = True

            # 5. ADX
            adx = self.calculate_adx(highs_5m, lows_5m, closes_5m, 14)
            if adx:
                result['adx'] = adx[-1]
                if adx[-1] > 25:
                    long_score += 1; short_score += 1
                    long_ind['adx'] = True; short_ind['adx'] = True

            # 6. MACD
            macd = self.calculate_macd(closes_5m)
            result['macd'] = macd
            if macd['histogram'] > 0 and macd['macd'] > macd['signal']:
                long_score += 1; long_ind['macd'] = True
            if macd['histogram'] < 0 and macd['macd'] < macd['signal']:
                short_score += 1; short_ind['macd'] = True

            # 7. Объём
            vol_sma = self.calculate_sma(volumes_5m, 20)
            if vol_sma and vol_sma[-1] > 0:
                vol_ratio = volumes_5m[-1] / vol_sma[-1]
                if vol_ratio > MIN_VOLUME_RATIO:
                    long_score += 1; short_score += 1
                    result['volume_ok'] = True
                    long_ind['volume_spike'] = True; short_ind['volume_spike'] = True

            # 8. Order Block
            ob = self.detect_order_block(ohlcv_5m)
            if ob == 'BULLISH':
                long_score += 1; long_ind['order_block'] = True
            if ob == 'BEARISH':
                short_score += 1; short_ind['order_block'] = True

            # 9. Liquidity Sweep (PRO SMC concept)
            sweep = self.detect_liquidity_sweep(ohlcv_5m)
            result['sweep'] = sweep
            if sweep == 'BULLISH':
                long_score += 2; long_ind['liq_sweep'] = True
            if sweep == 'BEARISH':
                short_score += 2; short_ind['liq_sweep'] = True

            # 10. HTF (1H) Alignment (PRO MTF concept)
            if htf_trend == 'LONG':
                long_score += 1; long_ind['htf_align'] = True
            elif htf_trend == 'SHORT':
                short_score += 1; short_ind['htf_align'] = True

            # ========== INSTITUTIONAL ENTRY SYSTEM ==========
            # Тренд (EMA50) + Структура (BOS/FVG/OB) + Моментум (MACD/RSI)
            is_uptrend = current_price > ema50[-1]
            is_downtrend = current_price < ema50[-1]

            # Проверяем наличие СТРУКТУРНОГО подтверждения (SMC)
            long_has_structure = long_ind.get('bos') or long_ind.get('fvg') or long_ind.get('order_block') or long_ind.get('liq_sweep')
            short_has_structure = short_ind.get('bos') or short_ind.get('fvg') or short_ind.get('order_block') or short_ind.get('liq_sweep')

            # Проверяем наличие МОМЕНТУМ подтверждения
            long_has_momentum = long_ind.get('macd') or long_ind.get('rsi_momentum')
            short_has_momentum = short_ind.get('macd') or short_ind.get('rsi_momentum')
            
            # PRO: Избегаем входа на хаях (FOMO)
            long_not_overbought = rsi[-1] < 75 if (rsi and len(rsi)>0) else True
            short_not_oversold = rsi[-1] > 25 if (rsi and len(rsi)>0) else True

            if is_uptrend and long_has_structure and long_has_momentum and long_not_overbought and long_score >= 4:
                result['signal'] = True
                result['direction'] = 'LONG'
                result['score'] = f"LONG_{long_score}"
                result['indicators'] = long_ind
                logger.info(f"✅ {symbol} PRO LONG score={long_score} struct={long_has_structure} mom={long_has_momentum} ind={long_ind}")
            elif is_downtrend and short_has_structure and short_has_momentum and short_not_oversold and short_score >= 4:
                result['signal'] = True
                result['direction'] = 'SHORT'
                result['score'] = f"SHORT_{short_score}"
                result['indicators'] = short_ind
                logger.info(f"✅ {symbol} PRO SHORT score={short_score} struct={short_has_structure} mom={short_has_momentum} ind={short_ind}")
            else:
                result['signal'] = False
                result['direction'] = 'LONG' if is_uptrend else 'SHORT'
                result['score'] = f"SKIP_L{long_score}_S{short_score}"
                result['indicators'] = {}

        except Exception as e:
            logger.error(f"Ошибка анализа {symbol}: {e}")
        return result


# ============================================================================
# ПОЗИЦИЯ
# ============================================================================

@dataclass
class Position:
    id: int
    symbol: str
    side: str
    entry_price: float
    stop_loss: float
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


# ============================================================================
# ОСНОВНОЙ БОТ
# ============================================================================

class SmartMoneyBot:

    def __init__(self, api_key, api_secret, telegram_token,
                 telegram_chat_id, user_chat_id=None, testnet=False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self.user_chat_id = user_chat_id
        self.testnet = testnet
        self.logger = logger

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

        self.exchange = ccxt.binanceusdm(exchange_config)

        if testnet:
            logger.info("🔧 Используется Binance Demo Trading")
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
        self.db = Database()
        self.smc_analyzer = SMCAnalyzer(self.exchange)
        self.positions: Dict[int, Position] = {}
        self.symbols_to_scan: List[str] = []
        self.is_running = False
        self.last_scan_time = None
        self.signals_today = 0
        self.max_signals_per_day = 9999
        self.app = None
        self.active_chat_ids = set([str(self.telegram_chat_id)])
        if self.user_chat_id:
            self.active_chat_ids.add(str(self.user_chat_id))
        self._bot = Bot(token=self.telegram_token)

    # ────────────────────────────────────────────────────────────────────────
    # ФИЛЬТР ВОЛАТИЛЬНОСТИ — НОВОЕ
    # ────────────────────────────────────────────────────────────────────────

    async def check_volatility(self, symbol: str) -> bool:
        """Проверяет нормальную волатильность и отсутствие panic candles"""
        try:
            ohlcv = await self.smc_analyzer.get_ohlcv(symbol, '5m', 20)

            if not ohlcv or len(ohlcv) < 10:
                return False

            ranges = [(c[2] - c[3]) / c[3] * 100 for c in ohlcv[-10:]]
            avg_range = sum(ranges) / len(ranges)

            if avg_range < config.MIN_VOLATILITY_PCT:
                logger.debug(f"{symbol}: слишком низкая волатильность {avg_range:.2f}%")
                return False

            if avg_range > config.MAX_VOLATILITY_PCT:
                logger.debug(f"{symbol}: слишком высокая волатильность {avg_range:.2f}%")
                return False

            last_candle_range = (
                (ohlcv[-1][2] - ohlcv[-1][3])
                / ohlcv[-1][3]
                * 100
            )

            if last_candle_range > config.MAX_SINGLE_CANDLE_PCT:
                logger.debug(
                    f"{symbol}: panic candle {last_candle_range:.2f}%"
                )
                return False

            return True
        except Exception as e:
            logger.error(f"Ошибка check_volatility {symbol}: {e}")
            return False

    async def update_top_symbols(self):
        try:
            markets = await self.exchange.load_markets(True)
            tickers = await self.exchange.fetch_tickers()
            usdt_perps = []
            for symbol, market in markets.items():
                if 'USDT' in symbol and market.get('type') == 'swap' and market.get('active', True):
                    info = market.get('info', {})
                    if info.get('status', 'TRADING') == 'TRADING' and info.get('contractType', 'PERPETUAL') == 'PERPETUAL':
                        vol = tickers[symbol].get('quoteVolume', 0.0) if symbol in tickers else 0.0
                        usdt_perps.append((symbol, vol))
            usdt_perps.sort(key=lambda x: x[1], reverse=True)
            # Топ-150 по объёму — достаточно для поиска сигналов, не перегружая API
            self.symbols_to_scan = [pair[0] for pair in usdt_perps[:150]]
            logger.info(f"🔄 Топ-150 пар обновлён (из {len(usdt_perps)} доступных)")
        except Exception as e:
            logger.error(f"Ошибка обновления топа пар: {e}")
            if not self.symbols_to_scan:
                self.symbols_to_scan = ['BTC/USDT', 'ETH/USDT', 'BNB/USDT', 'SOL/USDT']

    async def connect(self):
        try:
            await self.exchange.load_markets()
            logger.info("Markets loaded — API ключ валиден")
            balance = await self.exchange.fetch_balance()
            logger.info(f"Подключено к Binance Futures. Баланс: {balance.get('total', {})}")
            return True
        except Exception as e:
            logger.error(f"Ошибка подключения: {e}")
            return False

    async def disconnect(self):
        try:
            await self.exchange.close()
        except Exception:
            pass

    def compute_optimal_slots(self, virtual_equity: float) -> int:
        return 5  # Бот сам распределяет капитал по силе сигналов


    async def get_safe_leverage(self, symbol):
        try:
            max_leverage = await self.get_safe_leverage(symbol) if hasattr(self, "get_safe_leverage") else config.LEVERAGE

            try:
                market = self.exchange.market(symbol)
                limits = market.get("limits", {})
                leverage_limits = limits.get("leverage", {})

                if leverage_limits:
                    exchange_max = int(leverage_limits.get("max", max_leverage))
                    max_leverage = min(max_leverage, exchange_max)

            except Exception:
                pass

            # Binance часто ограничивает плечо для дорогих монет
            # Пытаемся уменьшать пока биржа не примет
            for lev in [125, 100, 75, 50, 25, 20, 10, 5]:
                if lev <= max_leverage:
                    try:
                        await self.exchange.set_leverage(lev, symbol)
                        logger.info(f"✅ Плечо установлено x{lev} для {symbol}")
                        return lev
                    except Exception:
                        continue

            return 5

        except Exception as e:
            logger.error(f"Ошибка установки плеча: {e}")
            return 5

    async def calculate_position_size(self, entry_price, score=5) -> tuple:
        try:
            balance = await self.exchange.fetch_balance()

            free_balance = float(balance.get('USDT', {}).get('free', 0))
            total_balance = float(balance.get('USDT', {}).get('total', 0))

            # Ограничиваем баланс начальным депозитом + заработанным PnL (чтобы бот не брал лишние деньги с аккаунта)
            stats = self.db.get_all_statistics()
            total_pnl = stats.get('total_pnl', 0) or 0
            virtual_balance = max(10.0, config.DEPOSIT + total_pnl)
            
            # Бот использует минимум из реального свободного баланса и своего виртуального депозита
            working_balance = min(free_balance, virtual_balance)

            if working_balance < config.MIN_SLOT_USDT:
                logger.warning(
                    f"Рабочий баланс ${working_balance:.2f} (Свободно: ${free_balance:.2f}) < минимума ${config.MIN_SLOT_USDT}"
                )
                return 0, 0, 0

            # Профессиональный риск-менеджмент для маленького депозита
            # Больше бот НЕ сможет открыть сделок на сумму выше депозита
            # и не будет использовать 50-100% баланса в одной позиции.

            open_positions = max(1, len(self.positions))

            # Чем больше открытых позиций — тем меньше риск на новую
            base_risk_map = {
                2: 0.04,
                3: 0.05,
                4: 0.06,
                5: 0.08,
                6: 0.10,
                7: 0.12
            }

            risk_percent = base_risk_map.get(score, 0.05)

            # Защита от перегруза депозита
            if open_positions >= 2:
                risk_percent *= 0.7

            if open_positions >= 4:
                risk_percent *= 0.5

            # Никогда не используем больше 15% депозита как маржу
            risk_percent = min(risk_percent, 0.15)

            # Расчёт маржи
            amount_usdt = working_balance * risk_percent

            # Жёсткий лимит: суммарная маржа всех позиций <= 85% депозита
            used_margin = sum(
                getattr(pos, 'amount_usdt', 0)
                for pos in self.positions.values()
            )

            max_allowed_margin = working_balance * 0.85
            remaining_margin = max(0, max_allowed_margin - used_margin)

            amount_usdt = min(
                amount_usdt,
                remaining_margin,
                free_balance * 0.90
            )

            # Минимальный размер позиции
            if amount_usdt < config.MIN_SLOT_USDT:
                if remaining_margin < config.MIN_SLOT_USDT:
                    logger.warning(
                        f"Недостаточно свободной маржи для новой позиции | used=${used_margin:.2f}"
                    )
                    return 0, 0, 0

                amount_usdt = config.MIN_SLOT_USDT

            leverage = min(config.LEVERAGE, 125)

            # Защита от отрицательных или нулевых значений
            if entry_price <= 0:
                return 0, 0, 0

            if leverage <= 0:
                leverage = 1

            # Номинал позиции
            notional = amount_usdt * leverage

            # Размер позиции
            quantity = notional / entry_price

            # Финальная защита
            if quantity <= 0 or notional <= 0:
                return 0, 0, 0

            logger.info(
                f"📊 Dynamic Risk | score={score} "
                f"| risk={risk_percent * 100:.0f}% "
                f"| margin=${amount_usdt:.2f} "
                f"| free=${free_balance:.2f} "
                f"| total=${total_balance:.2f}"
            )

            return quantity, amount_usdt, notional

        except Exception as e:
            logger.error(f"Ошибка расчёта размера: {e}")
            return 0, 0, 0

    async def send_telegram_message(self, message: str, parse_mode: str = None):
        try:
            for chat_id in self.active_chat_ids:
                try:
                    await self._bot.send_message(chat_id=chat_id, text=message, parse_mode=parse_mode)
                except Exception as e:
                    logger.error(f"Ошибка отправки в {chat_id}: {e}")
        except Exception as e:
            logger.error(f"Ошибка send_telegram_message: {e}")

    def calculate_stop_loss(self, entry_price, side='LONG') -> float:
        if side == 'SHORT':
            return entry_price * (1 + config.STOP_LOSS_PCT / 100)
        return entry_price * (1 - config.STOP_LOSS_PCT / 100)

    # ────────────────────────────────────────────────────────────────────────
    # ОТКРЫТИЕ ПОЗИЦИИ — ИСПРАВЛЕН SL
    # ────────────────────────────────────────────────────────────────────────


    def _normalize_order_price(self, symbol, price, side):
        """
        Binance futures PERCENT_PRICE protection fix.
        Keeps limit prices inside allowed deviation bands.
        """
        try:
            if side.upper() == "BUY":
                return round(price * 0.999, 6)
            return round(price * 1.001, 6)
        except Exception:
            return price

    async def open_position(self, symbol, entry_price, smc_result) -> Optional[Position]:
            global ALLOW_TRADING
            if not ALLOW_TRADING:
                logger.info(f"Сигнал {symbol} пойман, торговля приостановлена")
                return None
            try:
                # ── ЗАЩИТА ОТ СПАМА ОРДЕРАМИ И ПЕРЕГРУЗКИ ЛИМИТОВ ──
                if len(self.positions) >= MAX_OPEN_POSITIONS:
                    logger.info(f"Достигнут лимит открытых позиций ({MAX_OPEN_POSITIONS}). Пропускаем {symbol}.")
                    return None
                    
                if symbol in [p.symbol for p in self.positions.values()]:
                    logger.info(f"Позиция для {symbol} уже открыта. Пропускаем дублирующий сигнал.")
                    return None

                direction = smc_result.get('direction', 'LONG')
                market_info = self.exchange.market(symbol)
                min_notional = float(market_info.get('limits', {}).get('cost', {}).get('min', 5))

                real_score = smc_result.get('score', 5)
                if isinstance(real_score, str):
                    real_score = 7  # паттерн-матч = сильный сигнал
                quantity, margin, actual_amount = await self.calculate_position_size(entry_price, score=real_score)
                if quantity == 0:
                    return None

                quantity = float(self.exchange.amount_to_precision(symbol, quantity))
                notional = quantity * entry_price
                if notional < min_notional:
                    quantity = float(self.exchange.amount_to_precision(symbol, min_notional / entry_price * 1.05))
                    notional = quantity * entry_price
                    if notional < min_notional:
                        return None

                dir_emoji = '🟢 LONG' if direction == 'LONG' else '🔴 SHORT'
                logger.info(f"Открытие {dir_emoji} {symbol}: qty={quantity}, notional=${notional:.2f}")

                # Кросс-маржа и плечо
                try:
                    await self.exchange.set_margin_mode('cross', symbol)
                except Exception:
                    pass
                actual_leverage = config.LEVERAGE
                try:

                    max_allowed_leverage = await get_max_leverage(self.exchange, symbol)
                    actual_leverage = min(config.LEVERAGE, max_allowed_leverage)
                    await self.exchange.set_leverage(actual_leverage, symbol)
                    logger.info(f"Dynamic leverage for {symbol}: x{actual_leverage}")

                except Exception as lev_err:
                    err_str = str(lev_err)
                    if '-2015' in err_str or 'Invalid API-key' in err_str:
                        raise
                    # Автоматически подбираем рабочее плечо для любой монеты
                    # dynamic leverage enabled

                # ── Чистим ВСЕ старые ордера перед открытием (предотвращает -4130 и -4045) ──
                try:
                    await self.exchange.cancel_all_orders(symbol)
                except Exception:
                    pass

                # Открываем позицию
                try:
                    if direction == 'SHORT':
                        order = await self.exchange.create_market_sell_order(symbol, quantity)
                    else:
                        order = await self.exchange.create_market_buy_order(symbol, quantity)
                except Exception as e:
                    err_str = str(e)
                    if '-2015' in err_str or 'Invalid API-key' in err_str:
                        raise

                    # Если биржа отклонила ордер из-за лимитов плеча/размера
                    if '-2027' in err_str or 'Exceeded' in err_str or 'Margin is insufficient' in err_str or '-2019' in err_str:
                        order_success = False
                        # Пробуем ступеньками снижать плечо, начиная с самых высоких
                        for new_lev in [125, 100, 75, 50, 40, 30, 25, 20, 15, 10, 5, 3, 2, 1]:
                            if new_lev >= actual_leverage:
                                continue  # Пробуем только плечи МЕНЬШЕ текущего
                            try:
                                await self.exchange.set_leverage(new_lev, symbol)
                                actual_leverage = new_lev
                                logger.info(f"🚨 Emergency leverage fallback for {symbol}: x{new_lev}")

                                # Пересчитываем quantity под новое плечо, сохраняя вложенную маржу (amount_usdt)
                                notional = margin * actual_leverage
                                if notional < min_notional:
                                    logger.warning(f"Номинал ${notional:.2f} меньше минимального ${min_notional} при x{actual_leverage}. Пропускаем {symbol}.")
                                    break

                                quantity = float(self.exchange.amount_to_precision(symbol, notional / entry_price))

                                if direction == 'SHORT':
                                    order = await self.exchange.create_market_sell_order(symbol, quantity)
                                else:
                                    order = await self.exchange.create_market_buy_order(symbol, quantity)

                                order_success = True
                                break # Успешно открыли!
                            except Exception as fallback_e:
                                logger.debug(f"Fallback x{new_lev} failed: {fallback_e}")
                                continue

                        if not order_success:
                            logger.error(f"❌ Не удалось открыть {symbol} даже после снижения плеча.")
                            raise Exception("Fallback exhausted or notional too small")
                    else:
                        raise


                # Финальная защита от invalid leverage
                if actual_leverage < 1:
                    actual_leverage = 1

                actual_entry = float(order['average']) if order.get('average') else entry_price
                actual_qty = float(order['filled']) if order.get('filled') else quantity

                # SL и TP по направлению
                if direction == 'SHORT':
                    actual_sl = float(self.exchange.price_to_precision(
                        symbol, actual_entry * (1 + config.STOP_LOSS_PCT / 100)))
                    actual_tp = float(self.exchange.price_to_precision(
                        symbol, actual_entry * (1 - config.TP3_PCT / 100)))
                    tp1_price = float(self.exchange.price_to_precision(
                        symbol, actual_entry * (1 - config.TAKE_PROFIT_PCT / 100)))
                    close_side = 'BUY'
                else:
                    actual_sl = float(self.exchange.price_to_precision(
                        symbol, actual_entry * (1 - config.STOP_LOSS_PCT / 100)))
                    actual_tp = float(self.exchange.price_to_precision(
                        symbol, actual_entry * (1 + config.TP3_PCT / 100)))
                    tp1_price = float(self.exchange.price_to_precision(
                        symbol, actual_entry * (1 + config.TAKE_PROFIT_PCT / 100)))
                    close_side = 'SELL'

                # ── SL на бирже (с обработкой -4130 "already exists") ──
                sl_placed = False
                for sl_attempt in range(3):
                    try:
                        if sl_attempt > 0:
                            try:
                                await self.exchange.cancel_all_orders(symbol)
                            except Exception:
                                pass
                            await asyncio.sleep(0.5)
                        await self.exchange.create_order(
                            symbol=symbol,
                            type='STOP_MARKET',
                            side=close_side,
                            amount=actual_qty,
                            params={'stopPrice': actual_sl, 'closePosition': True, 'workingType': 'MARK_PRICE'}
                        )
                        logger.info(f"✅ SL выставлен на бирже: {actual_sl}")
                        sl_placed = True
                        break
                    except Exception as e:
                        err_str = str(e)
                        if '-4130' in err_str:
                            # SL/TP уже стоит с closePosition — значит защита есть, продолжаем
                            logger.info(f"ℹ️ SL уже стоит для {symbol} (closePosition), продолжаем")
                            sl_placed = True
                            break
                        elif '-2021' in err_str:
                            # "Order would immediately trigger" — SL уже пробит
                            logger.error(f"❌ SL {symbol} сразу сработает — закрываем позицию")
                            break
                        else:
                            logger.warning(f"⚠️ SL попытка {sl_attempt+1}/3 для {symbol}: {e}")

                if not sl_placed:
                    # Не удалось поставить SL — экстренно закрываем позицию
                    logger.error(f"❌ SL не удалось выставить для {symbol}. Закрываем позицию!")
                    try:
                        if close_side == 'BUY':
                            await self.exchange.create_market_buy_order(symbol, actual_qty, params = {
                    "reduceOnly": True
                }

                # Binance futures иногда отвергает reduceOnly,
                # если позиция уже частично закрыта или size изменился.
                # В этом случае пробуем обычное закрытие.
)
                        else:
                            await self.exchange.create_market_sell_order(symbol, actual_qty, params = {
                    "reduceOnly": True
                }

                # Binance futures иногда отвергает reduceOnly,
                # если позиция уже частично закрыта или size изменился.
                # В этом случае пробуем обычное закрытие.
)
                    except Exception as ex:
                        # Если reduceOnly тоже не работает (-4131 PERCENT_PRICE) — пробуем без него
                        logger.warning(f"reduceOnly не сработал: {ex}, пробуем closePosition")
                        try:
                            await self.exchange.create_order(
                                symbol=symbol, type='MARKET', side=close_side,
                                amount=actual_qty, params = {
                    "reduceOnly": True
                }

                # Binance futures иногда отвергает reduceOnly,
                # если позиция уже частично закрыта или size изменился.
                # В этом случае пробуем обычное закрытие.

                            )
                        except Exception as ex2:
                            logger.error(f"Не удалось закрыть {symbol} никаким способом: {ex2}")
                    return None

                # ── TP3 на бирже (с обработкой -4130) ──
                try:
                    await self.exchange.create_order(
                        symbol=symbol,
                        type='TAKE_PROFIT_MARKET',
                        side=close_side,
                        amount=actual_qty,
                        params={'stopPrice': actual_tp, 'closePosition': True, 'workingType': 'MARK_PRICE'}
                    )
                except Exception as e:
                    err_str = str(e)
                    if '-4130' in err_str:
                        logger.info(f"ℹ️ TP уже стоит для {symbol}, пропускаем")
                    else:
                        logger.warning(f"⚠️ TP не выставлен для {symbol}: {e}")

                position_id = self.db.add_position(
                    symbol=symbol, side=direction, entry_price=actual_entry,
                    stop_loss=actual_sl, take_profit=tp1_price,
                    amount_usdt=margin, leverage=actual_leverage,
                    quantity=actual_qty, smc_score=str(smc_result['score']),
                    bos_info=smc_result['bos'], fvg_detected=smc_result['fvg'],
                    rsi_value=smc_result['rsi'], adx_value=smc_result['adx']
                )

                position = Position(
                    id=position_id, symbol=symbol, side=direction,
                    entry_price=actual_entry, stop_loss=actual_sl,
                    amount_usdt=margin, leverage=actual_leverage,
                    quantity=actual_qty, remaining_quantity=actual_qty,
                    timestamp=datetime.now(timezone.utc), realized_pnl_usd=0.0,
                )
                self.positions[position_id] = position

                raw_score = smc_result['score']
                score = 7 if isinstance(raw_score, str) else raw_score
                quality = "★★★ СИЛЬНЫЙ" if score >= 6 else ("★★☆ ХОРОШИЙ" if score >= 5 else "★☆☆ СРЕДНИЙ")
                rr = config.TP3_PCT / config.STOP_LOSS_PCT if config.STOP_LOSS_PCT > 0 else 0

                message = (
                    f"✅ ПОЗИЦИЯ ОТКРЫТА\n"
                    f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
                    f"{dir_emoji} | #{symbol.replace('/', '')}\n"
                    f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n\n"
                    f"🛒 Вход:    {actual_entry:.5f}\n"
                    f"💰 Вложено: ${margin:.2f}\n"
                    f"🎯 TP1:    {tp1_price:.5f}\n"
                    f"🔴 Стоп:   {actual_sl:.5f}\n"
                    f"📐 RR: 1:{rr:.1f} | Плечо: x{actual_leverage}\n\n"
                    f"📊 {quality}\n"
                    f"  Индикаторы: {', '.join(smc_result['indicators'].keys())}\n"
                    f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
                    f"SMART MONEY 1 BOT"
                )
                await self.send_telegram_message(message)

                signal_id = self.db.add_signal(
                    symbol=symbol, signal_type=direction, entry_price=actual_entry,
                    smc_score=smc_result['score'], indicators=smc_result['indicators']
                )
                self.db.mark_signal_executed(signal_id)
                self.signals_today += 1
                logger.info(f"Позиция открыта: {direction} {symbol} @ {actual_entry}")
                return position

            except Exception as e:
                err_str = str(e)
                logger.error(f"Ошибка открытия позиции {symbol}: {err_str}")
                # Не спамим в телеграм если это техническая ошибка лимитов биржи
                if 'notional too small' not in err_str and 'Exceeded' not in err_str and 'Margin is insufficient' not in err_str and 'Fallback exhausted' not in err_str:
                    await self.send_telegram_message(f"❌ Ошибка открытия {symbol}: {err_str}")
                return None

        # ────────────────────────────────────────────────────────────────────────
        # ЗАКРЫТИЕ ПОЗИЦИИ
        # ────────────────────────────────────────────────────────────────────────

    async def close_position(self, position_id, emergency=False) -> bool:
        try:
            if position_id not in self.positions:
                logger.warning(f"Позиция {position_id} не найдена")
                return False

            position = self.positions[position_id]
            symbol = position.symbol
            qty_close = position.remaining_quantity if position.remaining_quantity > 0 else position.quantity
            if qty_close <= 0:
                return False

            try:
                await self.exchange.cancel_all_orders(symbol)
            except Exception as cancel_e:
                logger.warning(f"Не удалось отменить ордера {symbol}: {cancel_e}")

            try:
                # Проверяем реальный размер позиции на Binance
                positions = await self.exchange.fetch_positions([symbol])

                real_size = 0.0
                for p in positions:
                    contracts = abs(float(p.get('contracts', 0) or 0))
                    if contracts > 0:
                        real_size = contracts
                        break

                if real_size <= 0:
                    logger.warning(f"Позиция {symbol} уже закрыта на Binance")
                    order = {}
                else:
                    qty_close = min(qty_close, real_size)

                    if position.side == 'SHORT':
                        order = await self.exchange.create_market_buy_order(
                            symbol,
                            qty_close,
                            params={"reduceOnly": True}
                        )
                    else:
                        order = await self.exchange.create_market_sell_order(
                            symbol,
                            qty_close,
                            params={"reduceOnly": True}
                        )

            except Exception as order_e:
                err_str = str(order_e)

                # FIX Binance reduceOnly -2022
                if '-2022' in err_str or 'ReduceOnly' in err_str:
                    logger.warning(f"ReduceOnly rejected для {symbol}, пробуем обычное закрытие")

                    try:
                        positions = await self.exchange.fetch_positions([symbol])

                        real_size = 0.0
                        for p in positions:
                            contracts = abs(float(p.get('contracts', 0) or 0))
                            if contracts > 0:
                                real_size = contracts
                                break

                        if real_size <= 0:
                            order = {}
                        else:
                            if position.side == 'SHORT':
                                order = await self.exchange.create_market_buy_order(symbol, real_size)
                            else:
                                order = await self.exchange.create_market_sell_order(symbol, real_size)

                    except Exception as ex2:
                        logger.error(f"Не удалось закрыть {symbol}: {ex2}")
                        order = {}
                else:
                    logger.error(f"Ошибка создания ордера закрытия {symbol}: {order_e}")
                    raise

            exit_price = order.get('average') or order.get('price')
            if not exit_price:
                try:
                    ticker = await self.exchange.fetch_ticker(symbol)
                    exit_price = ticker['last']
                except Exception:
                    exit_price = position.entry_price
            exit_price = float(exit_price)

            # FIX: считаем PnL от реальной маржи, без двойного учёта плеча
            if position.side == 'SHORT':
                price_change_pct = ((position.entry_price - exit_price) / position.entry_price) * 100
            else:
                price_change_pct = ((exit_price - position.entry_price) / position.entry_price) * 100

            # Реальный PnL по марже
            leg_pnl = position.amount_usdt * (price_change_pct / 100.0) * position.leverage

            # Пропорционально частичному закрытию
            qty_ratio = qty_close / position.quantity if position.quantity > 0 else 1.0
            leg_pnl *= qty_ratio

            total_pnl = position.realized_pnl_usd + leg_pnl
            margin = position.amount_usdt

            # ROE отдельно от абсолютного PnL
            pnl_pct = (total_pnl / margin) * 100 if margin > 0 else 0.0

            self.db.update_position(position_id, exit_price, total_pnl, pnl_pct)
            self.db.update_daily_statistics(total_pnl, pnl_pct, count_as_trade=True,
                                            equity_reference=config.DEPOSIT)
            del self.positions[position_id]

            duration = datetime.now(timezone.utc) - position.timestamp
            emoji = "✅" if total_pnl >= 0 else "❌"
            result_text = "ПРИБЫЛЬ" if total_pnl >= 0 else "УБЫТОК"
            hours = int(duration.total_seconds() // 3600)
            minutes = int((duration.total_seconds() % 3600) // 60)

            message = (
                f"{emoji} ПОЗИЦИЯ ЗАКРЫТА | {result_text}\n"
                f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
                f"📍 Монета: #{symbol.replace('/', '')}\n"
                f"🛒 Вход:  {position.entry_price:.5f}\n"
                f"🏁 Выход: {exit_price:.5f}\n"
                f"💰 Вложено: ${margin:.2f}\n"
                f"📈 PnL: {'+' if total_pnl >= 0 else ''}${total_pnl:.2f} ({'+' if total_pnl >= 0 else ''}{pnl_pct:.1f}%)\n"
                f"💼 Итог с баланса: ${margin + total_pnl:.2f}\n"
                f"⏱ Время: {hours}ч {minutes}мин\n"
                f"🔧 Плечо: x{position.leverage}\n"
                f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
                f"SMART MONEY 1 BOT"
            )
            if emergency:
                message = f"🚨 ЭКСТРЕННОЕ ЗАКРЫТИЕ\n{message}"
            await self.send_telegram_message(message)
            logger.info(f"Позиция закрыта: {symbol}, PnL: ${total_pnl:.2f}")
            return True
        except Exception as e:
            logger.error(f"Ошибка закрытия позиции {position_id}: {e}")
            await self.send_telegram_message(f"❌ Ошибка закрытия позиции {position_id}: {e}")
            return False

    async def close_partial_position(self, position, qty_to_close, current_price) -> bool:
        symbol = position.symbol
        if qty_to_close <= 0 or qty_to_close > position.remaining_quantity:
            return False

        try:
            # СТРОГО форматируем количество, иначе Binance выдаст ошибку точности (Precision/Invalid amount)
            qty_to_close = float(self.exchange.amount_to_precision(symbol, qty_to_close))

            if qty_to_close <= 0:
                return False

            if position.side == 'SHORT':
                order = await self.exchange.create_market_buy_order(symbol, qty_to_close, params={"reduceOnly": True})
            else:
                order = await self.exchange.create_market_sell_order(symbol, qty_to_close, params={"reduceOnly": True})

            exit_price = order.get('average') or order.get('price') or current_price
            exit_price = float(exit_price)

            # Считаем PnL этой части
            if position.side == 'SHORT':
                price_change_pct = ((position.entry_price - exit_price) / position.entry_price) * 100
            else:
                price_change_pct = ((exit_price - position.entry_price) / position.entry_price) * 100

            leg_pnl = position.amount_usdt * (price_change_pct / 100.0) * position.leverage * (qty_to_close / position.quantity)

            # Обновляем состояние позиции в памяти
            position.remaining_quantity -= qty_to_close
            position.realized_pnl_usd += leg_pnl
            logger.info(f"Частично закрыта {symbol}: {qty_to_close} по {exit_price}. PnL: ${leg_pnl:.2f}")
            return True
        except Exception as e:
            err_str = str(e)
            if 'ReduceOnly' in err_str or '-2022' in err_str or 'reduce-only' in err_str.lower():
                logger.warning(f"Частичное закрытие {symbol} отклонено (ReduceOnly). Возможно, уже закрыто.")
                position.remaining_quantity -= qty_to_close
                return True
            logger.error(f"Ошибка частичного закрытия {symbol}: {e}")
            return False


    async def close_all_positions(self, emergency=False):
        position_ids = list(self.positions.keys())
        logger.info(f"Закрытие {len(position_ids)} позиций")
        closed_count = 0
        for pid in position_ids:
            success = await self.close_position(pid, emergency)
            if success:
                closed_count += 1
            await asyncio.sleep(0.5)
        await self.send_telegram_message(f"✅ Закрыто: {closed_count}/{len(position_ids)}")



    async def apply_dynamic_sl(self, position: Position, price_change_pct: float, current_price: float):
        """Динамический SL — двигаем по достижении порогов цены"""
        new_sl_price = None
        new_level = position.dynamic_sl_level

        # Пороги для агрессивного скальпинга (исправленные, чтобы не душить сделку)
        if price_change_pct >= 1.0 and position.dynamic_sl_level < 3:
            # +75% ROE — мощный профит обеспечен
            if position.side == 'SHORT':
                new_sl_price = position.entry_price * (1 - 0.005)
            else:
                new_sl_price = position.entry_price * (1 + 0.005)
            new_level = 3
        elif price_change_pct >= 0.6 and position.dynamic_sl_level < 2:
            # +45% ROE — фиксируем плюсовой стоп
            if position.side == 'SHORT':
                new_sl_price = position.entry_price * (1 - 0.002)
            else:
                new_sl_price = position.entry_price * (1 + 0.002)
            new_level = 2
        elif price_change_pct >= 0.4 and position.dynamic_sl_level < 1:
            # +30% ROE — перевод в безубыток
            if position.side == 'SHORT':
                new_sl_price = position.entry_price * (1 - 0.0005)
            else:
                new_sl_price = position.entry_price * (1 + 0.0005)
            new_level = 1

        if not new_sl_price:
            return

        is_better_sl = (new_sl_price < position.stop_loss) if position.side == 'SHORT' else \
                       (new_sl_price > position.stop_loss)
        if not is_better_sl:
            return

        try:
            new_sl_price = float(self.exchange.price_to_precision(position.symbol, new_sl_price))
            qty_rounded = float(self.exchange.amount_to_precision(position.symbol, position.remaining_quantity))
            if qty_rounded <= 0:
                return

            await self.exchange.cancel_all_orders(position.symbol)
            close_side = 'BUY' if position.side == 'SHORT' else 'SELL'
            if position.side == 'SHORT':
                actual_tp = float(self.exchange.price_to_precision(
                    position.symbol, position.entry_price * (1 - config.TP3_PCT / 100)))
            else:
                actual_tp = float(self.exchange.price_to_precision(
                    position.symbol, position.entry_price * (1 + config.TP3_PCT / 100)))

            await self.exchange.create_order(
                symbol=position.symbol, type='STOP_MARKET', side=close_side,
                amount=qty_rounded,
                params={'stopPrice': new_sl_price, 'closePosition': True, 'workingType': 'MARK_PRICE'}
            )
            await self.exchange.create_order(
                symbol=position.symbol, type='TAKE_PROFIT_MARKET', side=close_side,
                amount=qty_rounded,
                params={'stopPrice': actual_tp, 'closePosition': True, 'workingType': 'MARK_PRICE'}
            )
            position.stop_loss = new_sl_price
            position.dynamic_sl_level = new_level
            labels = {1: 'БЕЗУБЫТОК', 2: '+5%', 3: '+10%'}
            pair = position.symbol.replace('/USDT', '')
            await self.send_telegram_message(
                f"🔒 ДИНАМИЧЕСКИЙ SL | {pair}\n"
                f"SL → {labels[new_level]}: {new_sl_price:.5f}\n"
                f"Движение цены: +{price_change_pct:.1f}%"
            )
        except Exception as e:
            logger.error(f"Ошибка переноса SL {position.symbol}: {e}")

    # ────────────────────────────────────────────────────────────────────────
    # МОНИТОРИНГ ПОЗИЦИЙ
    # ────────────────────────────────────────────────────────────────────────

    async def monitor_positions(self):
        for position_id, position in list(self.positions.items()):
            try:
                # Синхронизация с биржей, проверяем жива ли позиция
                positions_now = await self.exchange.fetch_positions([position.symbol])
                active_pos = next((p for p in positions_now if abs(float(p.get('contracts', 0) or 0)) > 0), None)

                if not active_pos:
                    # Позиция закрыта на бирже (SL/TP сработал) — чистим БЕЗ торговли
                    logger.info(f"Синхронизация: {position.symbol} уже закрыта на бирже. Чистим из бота.")
                    try:
                        await self.exchange.cancel_all_orders(position.symbol)
                    except Exception as cancel_err:
                        logger.warning(f"Ошибка отмены ордеров при синхронизации {position.symbol}: {cancel_err}")

                    try:
                        ticker = await self.exchange.fetch_ticker(position.symbol)
                        exit_price = ticker['last']
                        if position.side == 'SHORT':
                            est_pnl = (position.entry_price - exit_price) * position.remaining_quantity
                        else:
                            est_pnl = (exit_price - position.entry_price) * position.remaining_quantity
                        est_pnl += position.realized_pnl_usd
                        margin = position.amount_usdt
                        pnl_pct = (est_pnl / margin * 100) if margin > 0 else 0
                        self.db.update_position(position_id, exit_price, est_pnl, pnl_pct)
                        self.db.update_daily_statistics(est_pnl, pnl_pct, count_as_trade=True,
                                                       equity_reference=config.DEPOSIT)
                        emoji = "✅" if est_pnl >= 0 else "❌"
                        await self.send_telegram_message(
                            f"{emoji} СИНХРОНИЗАЦИЯ | {position.symbol.replace('/USDT', '')}\n"
                            f"Позиция была закрыта на бирже (SL/TP)\n"
                            f"PnL: {'+' if est_pnl >= 0 else ''}${est_pnl:.2f} ({pnl_pct:+.1f}%)"
                        )
                    except Exception as sync_e:
                        logger.warning(f"Ошибка синхронизации PnL {position.symbol}: {sync_e}")
                        self.db.update_position(position_id, position.entry_price, 0, 0)
                    del self.positions[position_id]
                    continue

                ticker = await self.exchange.fetch_ticker(position.symbol)
                current_price = ticker['last']

                if position.side == 'SHORT':
                    price_change_pct = ((position.entry_price - current_price) / position.entry_price) * 100
                else:
                    price_change_pct = ((current_price - position.entry_price) / position.entry_price) * 100
                pnl_pct = price_change_pct * position.leverage

                # FIX: реальный PnL по марже без завышения через номинал
                pnl_usd = (
                    position.amount_usdt
                    * (price_change_pct / 100.0)
                    * position.leverage
                ) + position.realized_pnl_usd

                if pnl_pct > position.peak_pnl:
                    position.peak_pnl = pnl_pct

                pair = position.symbol.replace('/USDT', '')

                # АКТИВНЫЙ АНАЛИЗ ОТКРЫТЫХ ПОЗИЦИЙ
                # Если позиция убыточна и тренд развернулся — закрываем рано
                if pnl_pct < -10 and not position.partial_tp1_done:
                    try:
                        ohlcv_check = await self.smc_analyzer.get_ohlcv(position.symbol, '5m', 20)
                        if ohlcv_check and len(ohlcv_check) >= 10:
                            closes_check = [c[4] for c in ohlcv_check]
                            ema10 = self.smc_analyzer.calculate_ema(closes_check, 10)
                            macd_check = self.smc_analyzer.calculate_macd(closes_check)
                            if ema10:
                                trend_against = (
                                    (position.side == 'LONG' and current_price < ema10[-1] and macd_check['histogram'] < 0) or
                                    (position.side == 'SHORT' and current_price > ema10[-1] and macd_check['histogram'] > 0)
                                )
                                if trend_against:
                                    await self.send_telegram_message(
                                        f"⚠️ РАЗВОРОТ ТРЕНДА | {pair}\n"
                                        f"Тренд против позиции! ROE: {pnl_pct:+.1f}%\n"
                                        f"Закрываем досрочно: ${pnl_usd:+.2f}"
                                    )
                                    await self.close_position(position_id)
                                    continue
                    except Exception:
                        pass

                # Откат от пика
                if position.peak_pnl >= config.MIN_PEAK_PNL_TO_TRACK:
                    drawdown = position.peak_pnl - pnl_pct
                    if drawdown > config.PEAK_DRAWDOWN_CLOSE_PCT:
                        await self.send_telegram_message(
                            f"📉 ОТКАТ ОТ ПИКА | {pair}\n"
                            f"Пик: +{position.peak_pnl:.1f}% → Сейчас: {pnl_pct:+.1f}%\n"
                            f"Откат: {drawdown:.1f}% | PnL: ${pnl_usd:.2f}"
                        )
                        await self.close_position(position_id)
                        continue

                # Trailing stop
                if pnl_pct >= config.TRAILING_ACTIVATE_PCT and not position.trailing_active:
                    position.trailing_active = True
                    position.trailing_peak = pnl_pct

                if position.trailing_active:
                    if pnl_pct > position.trailing_peak:
                        position.trailing_peak = pnl_pct
                    if position.trailing_peak - pnl_pct >= config.TRAILING_DRAWDOWN_CLOSE_PCT:
                        await self.send_telegram_message(
                            f"🛡 TRAILING STOP | {pair}\n"
                            f"Пик: +{position.trailing_peak:.1f}% | Откат: {position.trailing_peak - pnl_pct:.1f}%\n"
                            f"Закрыто: {pnl_pct:+.1f}% | PnL: ${pnl_usd:.2f}"
                        )
                        await self.close_position(position_id)
                        continue

                # Частичные TP
                if pnl_pct >= config.PARTIAL_TP1_PCT and not position.partial_tp1_done:
                    position.partial_tp1_done = True
                    await self.close_partial_position(position, position.quantity * 0.40, current_price)
                    position.dynamic_sl_level = 1
                    await self.apply_dynamic_sl(position, price_change_pct, current_price)
                    await self.send_telegram_message(
                        f"💰 ЧАСТИЧНАЯ ФИКСАЦИЯ TP1 | {pair}\n"
                        f"+{config.PARTIAL_TP1_PCT:.0f}% ROE — закрыто 40% | SL → безубыток"
                    )

                if pnl_pct >= config.PARTIAL_TP2_PCT and not position.partial_tp2_done:
                    position.partial_tp2_done = True
                    await self.close_partial_position(position, position.quantity * 0.30, current_price)
                    await self.send_telegram_message(
                        f"🚀 TP2 | {pair}\n+{config.PARTIAL_TP2_PCT:.0f}% ROE — закрыто ещё 30%"
                    )

                if pnl_pct >= config.PARTIAL_TP3_PCT and not position.partial_tp3_done:
                    position.partial_tp3_done = True
                    # Закрываем 90%, оставляем 10% как "раннер" на случай мун-шота
                    runner_qty = position.remaining_quantity * 0.10
                    close_qty = position.remaining_quantity - runner_qty
                    if close_qty > 0:
                        await self.close_partial_position(position, close_qty, current_price)
                    await self.send_telegram_message(
                        f"💎 TP3 +{config.PARTIAL_TP3_PCT:.0f}% ROE | {pair}\n"
                        f"Закрыто 90% позиции!\n"
                        f"🎯 Оставлен раннер 10% с трейлинг-стопом"
                    )
                    # Активируем трейлинг на остаток
                    position.trailing_active = True
                    position.trailing_peak = pnl_pct
                    continue

                # Динамический SL
                await self.apply_dynamic_sl(position, price_change_pct, current_price)

                # Таймаут
                await self.check_position_timeout(position)

            except Exception as e:
                logger.error(f"Ошибка мониторинга {position_id}: {e}")

    async def check_position_timeout(self, position: Position):
        now = datetime.now(timezone.utc)
        duration = now - position.timestamp
        duration_minutes = duration.total_seconds() / 60

        # Таймаут: если за 30 мин не было движения — мёртвая сделка, закрываем
        if position.peak_pnl < 10.0 and duration_minutes > 30:
            await self.send_telegram_message(
                f"⏱ ТАЙМАУТ | {position.symbol.replace('/USDT', '')}\n"
                f"Нет движения за {duration_minutes:.0f} мин\n"
                f"Пик PnL: +{position.peak_pnl:.1f}% — закрываем"
            )
            await self.close_position(position.id)
            return

        if duration >= timedelta(hours=config.POSITION_TIMEOUT_HOURS):
            await self.send_telegram_message(
                f"⏱ Истекло {config.POSITION_TIMEOUT_HOURS}ч | {position.symbol} — закрытие"
            )
            await self.close_position(position.id)

    # ────────────────────────────────────────────────────────────────────────
    # СКАНЕР РЫНКА
    # ────────────────────────────────────────────────────────────────────────


    async def is_sideways_market(self, symbol: str) -> bool:
        """Фильтр боковика"""
        try:
            ohlcv = await self.smc_analyzer.get_ohlcv(symbol, '5m', 60)

            if not ohlcv or len(ohlcv) < 50:
                return True

            closes = [c[4] for c in ohlcv]
            highs = [c[2] for c in ohlcv]
            lows = [c[3] for c in ohlcv]

            adx = self.smc_analyzer.calculate_adx(highs, lows, closes, 14)
            ema50 = self.smc_analyzer.calculate_ema(closes, 50)

            if not adx or not ema50:
                return True

            current_price = closes[-1]

            distance_pct = abs(
                current_price - ema50[-1]
            ) / ema50[-1] * 100

            return (
                adx[-1] < config.SIDEWAYS_ADX_THRESHOLD
                and distance_pct < config.SIDEWAYS_EMA_DISTANCE
            )

        except Exception:
            return True

    async def process_single_symbol(self, symbol: str):
        """Анализ одного символа — без лишних API-вызовов"""
        try:
            if not self.is_running:
                return
            if any(p.symbol == symbol for p in self.positions.values()):
                return

            # Сразу анализируем — все фильтры внутри analyze_symbol
            smc_result = await self.smc_analyzer.analyze_symbol(symbol)
            if smc_result['signal']:
                logger.info(f"✅ СИГНАЛ НАЙДЕН: {symbol} {smc_result['direction']} ({smc_result['score']})")
                ticker = await self.exchange.fetch_ticker(symbol)
                entry_price = ticker['last']
                await self.open_position(symbol, entry_price, smc_result)
        except Exception as e:
            if '-1122' not in str(e) and 'Invalid symbol' not in str(e):
                logger.error(f"Ошибка при обработке {symbol}: {e}")

    async def scan_market(self):
        if not self.is_running:
            return
        total = len(self.symbols_to_scan)
        logger.info(f"🔍 Сканирование рынка... ({total} символов)")

        signals_found = 0
        chunk_size = 10  # Маленькие чанки чтобы не перегружать API Binance
        for i in range(0, total, chunk_size):
            if not self.is_running:
                break
            chunk = self.symbols_to_scan[i:i + chunk_size]
            tasks = [self.process_single_symbol(s) for s in chunk]
            await asyncio.gather(*tasks)
            await asyncio.sleep(2)  # Пауза между чанками для rate limit

        self.last_scan_time = datetime.now(timezone.utc)
        logger.info(f"✅ Сканирование завершено ({total} пар)")

    async def run_scanner_loop(self):
        last_update = datetime.now(timezone.utc)
        self.scan_count = 0
        while self.is_running:
            try:
                if (datetime.now(timezone.utc) - last_update).total_seconds() > 1800:
                    await self.update_top_symbols()
                    last_update = datetime.now(timezone.utc)
                await self.scan_market()
                self.scan_count += 1
                logger.info(f"🔄 Скан #{self.scan_count} завершён. Следующий через 15 сек...")
                await asyncio.sleep(15)
            except Exception as e:
                logger.error(f"Ошибка в цикле сканирования: {e}")
                await asyncio.sleep(5)

    async def run_monitoring_loop(self):
        while self.is_running:
            try:
                await self.monitor_positions()
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Ошибка мониторинга: {e}")
                await asyncio.sleep(0.5)

    # ────────────────────────────────────────────────────────────────────────
    # ОТЧЁТЫ — ИСПРАВЛЕНЫ
    # ────────────────────────────────────────────────────────────────────────

    async def _build_hourly_report_text(self) -> str:
        """Собирает текст часового отчёта (используется и в автоотправке и в командах)"""
        stats = self.db.get_daily_statistics()
        today_trades = stats.get('total_trades', 0) if stats else 0
        today_wins = stats.get('profitable_trades', 0) if stats else 0
        today_losses = stats.get('losing_trades', 0) if stats else 0
        today_closed_pnl = stats.get('total_pnl', 0) if stats else 0

        positions_info = ""
        total_open_pnl = 0.0

        for pid, pos in list(self.positions.items()):
            try:
                ticker = await self.exchange.fetch_ticker(pos.symbol)
                current_price = ticker['last']
                rem = max(pos.remaining_quantity, 0.0)
                if pos.side == 'SHORT':
                    price_change_pct = ((pos.entry_price - current_price) / pos.entry_price) * 100
                else:
                    price_change_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100

                qty_ratio = rem / pos.quantity if pos.quantity > 0 else 1.0
                pnl = pos.realized_pnl_usd + (pos.amount_usdt * (price_change_pct / 100.0) * pos.leverage * qty_ratio)
                pnl_pct = price_change_pct * pos.leverage
                total_open_pnl += pnl
                emoji = "🟢" if pnl >= 0 else "🔴"
                positions_info += (
                    f"  {emoji} {pos.symbol.replace('/USDT', '')}: "
                    f"{'+' if pnl >= 0 else ''}${pnl:.2f} ({'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}%)\n"
                )
            except Exception:
                positions_info += f"  ⚪ {pos.symbol}: н/д\n"

        if not positions_info:
            positions_info = "  Нет открытых позиций\n"

        total_day_pnl = today_closed_pnl + total_open_pnl
        dep = config.DEPOSIT if config.DEPOSIT > 0 else 140.0
        total_day_pnl_pct = (total_day_pnl / dep) * 100
        now_moscow = datetime.now(timezone.utc) + timedelta(hours=3)
        pnl_emoji = "📈" if total_day_pnl >= 0 else "📉"
        winrate = (today_wins / today_trades * 100) if today_trades > 0 else 0

        return (
            f"⏰ ЧАСОВОЙ ОТЧЁТ | {now_moscow.strftime('%H:%M МСК')}\n"
            f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n\n"
            f"{pnl_emoji} РЕЗУЛЬТАТ СЕГОДНЯ:\n"
            f"  Закрытые: {'+' if today_closed_pnl >= 0 else ''}${today_closed_pnl:.2f}\n"
            f"  Открытые: {'+' if total_open_pnl >= 0 else ''}${total_open_pnl:.2f}\n"
            f"  ━━━━━━━━━━━━━━\n"
            f"  ИТОГО: {'+' if total_day_pnl >= 0 else ''}${total_day_pnl:.2f} "
            f"({'+' if total_day_pnl_pct >= 0 else ''}{total_day_pnl_pct:.1f}%)\n\n"
            f"📊 ОТКРЫТЫЕ ПОЗИЦИИ ({len(self.positions)}):\n"
            f"{positions_info}\n"
            f"📋 СТАТИСТИКА ДНЯ:\n"
            f"  Сделок: {today_trades} | ✅{today_wins} ❌{today_losses}\n"
            f"  Win Rate: {winrate:.0f}%\n\n"
            f"🤖 Статус: {'🟢 ТОРГУЕТ' if ALLOW_TRADING else '🔴 ПАУЗА'}\n"
            f"🔄 Сканирований: {getattr(self, 'scan_count', 0)}\n"
            f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
            f"SMART MONEY 1 BOT"
        )

    async def send_hourly_report(self):
        try:
            text = await self._build_hourly_report_text()
            await self.send_telegram_message(text)
            logger.info("Часовой отчёт отправлен")
        except Exception as e:
            logger.error(f"Ошибка часового отчёта: {e}")

    # ИСПРАВЛЕНИЕ: период 3600 сек (1 час), не 1800
    async def run_hourly_report_loop(self):
        while self.is_running:
            try:
                await asyncio.sleep(3600)
                if self.is_running:
                    await self.send_hourly_report()
            except Exception as e:
                logger.error(f"Ошибка в цикле часовых отчётов: {e}")
                await asyncio.sleep(0.5)

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
                f"📊 ДНЕВНОЙ ОТЧЁТ\n"
                f"Дата: {datetime.now(timezone.utc).strftime('%d.%m.%Y')}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"💰 БАЛАНС:\n"
                f"  Стартовый: ${deposit:.2f}\n"
                f"  Текущий:   ${current_balance:.2f}\n"
                f"  PnL: {pnl_sign}${total_pnl:.2f} ({pnl_sign}{pnl_pct_total:.1f}%)\n\n"
                f"📋 ЗА ДЕНЬ:\n"
                f"  Сделок: {stats.get('total_trades', 0)}\n"
                f"  ✅{stats.get('profitable_trades', 0)} ❌{stats.get('losing_trades', 0)}\n"
                f"  PnL: {'+' if stats.get('total_pnl', 0) >= 0 else ''}${stats.get('total_pnl', 0):.2f} "
                f"({'+' if stats.get('total_pnl_pct', 0) >= 0 else ''}{stats.get('total_pnl_pct', 0):.1f}%)\n\n"
                f"📈 ВСЁ ВРЕМЯ:\n"
                f"  Сделок: {all_stats.get('total_trades', 0)}\n"
                f"  Win Rate: {(all_stats.get('profitable', 0) / max(all_stats.get('total_trades', 1), 1) * 100):.1f}%\n"
                f"  Лучшая: ${all_stats.get('best_trade', 0):.2f}\n"
                f"  Худшая: ${all_stats.get('worst_trade', 0):.2f}\n"
                f"  Средний % в день: {'+' if avg_daily >= 0 else ''}{avg_daily:.1f}%"
            )
            await self.send_telegram_message(message)
        except Exception as e:
            logger.error(f"Ошибка дневного отчёта: {e}")

    async def run_daily_report_loop(self):
        while self.is_running:
            try:
                now = datetime.now(timezone.utc)
                next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                await asyncio.sleep((next_midnight - now).total_seconds())
                if self.is_running:
                    await self.send_daily_report()
            except Exception as e:
                logger.error(f"Ошибка в цикле дневных отчётов: {e}")
                await asyncio.sleep(0.5)

    # ────────────────────────────────────────────────────────────────────────
    # TELEGRAM КОМАНДЫ — ИСПРАВЛЕНЫ
    # ────────────────────────────────────────────────────────────────────────

    def get_main_keyboard(self):
        from telegram import ReplyKeyboardMarkup
        keyboard = [
            ['📊 Отчёт', '📋 Позиции'],
            ['🟢 Старт', '🔴 Стоп'],
            ['🛑 Закрыть все', '📈 Статистика']
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    async def _safe_reply(self, update: Update, text: str):
        """Безопасная отправка ответа — работает и с message и с callback"""
        try:
            if update.message:
                await update.message.reply_text(text, reply_markup=self.get_main_keyboard())
            elif update.callback_query:
                await update.callback_query.message.reply_text(text, reply_markup=self.get_main_keyboard())
            else:
                await self.send_telegram_message(text)
        except Exception as e:
            logger.error(f"Ошибка _safe_reply: {e}")
            try:
                await self.send_telegram_message(text)
            except Exception:
                pass

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat:
            self.active_chat_ids.add(str(update.effective_chat.id))
        text = update.message.text

        if text == '📊 Отчёт':
            await self.cmd_hourly_report(update, context)
        elif text == '📋 Позиции':
            await self.cmd_positions(update, context)
        elif text == '🟢 Старт':
            await self.cmd_start_bot(update, context)
        elif text == '🔴 Стоп':
            await self.cmd_stop_bot(update, context)
        elif text == '🛑 Закрыть все':
            await self.cmd_close_all(update, context)
        elif text == '📈 Статистика':
            await self.cmd_stats(update, context)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat:
            self.active_chat_ids.add(str(update.effective_chat.id))
        message = (
            f"🤖 Smart Money Aggressive Bot\n\n"
            f"Статус: {'🟢 РАБОТАЕТ' if self.is_running else '🔴 ОСТАНОВЛЕН'}\n"
            f"Позиций открыто: {len(self.positions)}\n"
            f"Сигналов сегодня: {self.signals_today}\n\n"
            f"⚙️ Параметры:\n"
            f"  Депозит: ${config.DEPOSIT}\n"
            f"  Плечо: x{config.LEVERAGE}\n"
            f"  SL: -{config.STOP_LOSS_PCT}%\n"
            f"  TP1/TP3: +{config.TAKE_PROFIT_PCT}% / +{config.TP3_PCT}%\n\n"
            f"Команды:\n"
            f"/report — отчёт за 1ч\n"
            f"/stats — полная статистика\n"
            f"/positions — открытые позиции\n"
            f"/balance — баланс\n"
            f"/close_all — закрыть все\n"
            f"/start_bot / /stop_bot — вкл/выкл"
        )
        await update.message.reply_text(message, reply_markup=self.get_main_keyboard())

    async def cmd_hourly_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /report — отчёт за текущий час/день"""
        try:
            text = await self._build_hourly_report_text()
            await self._safe_reply(update, text)
        except Exception as e:
            await self._safe_reply(update, f"❌ Ошибка формирования отчёта: {e}")

    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            balance = await self.exchange.fetch_balance()
            usdt_balance = balance.get('USDT', {}).get('total', 0)
            free = balance.get('USDT', {}).get('free', 0)
            used = balance.get('USDT', {}).get('used', 0)
            message = (
                f"💰 БАЛАНС\n\n"
                f"USDT Total: ${usdt_balance:.2f}\n"
                f"USDT Free:  ${free:.2f}\n"
                f"USDT Used:  ${used:.2f}\n\n"
                f"Стартовый депозит: ${config.DEPOSIT}\n"
                f"PnL: {'+' if usdt_balance - config.DEPOSIT >= 0 else ''}${usdt_balance - config.DEPOSIT:.2f}"
            )
            await self._safe_reply(update, message)
        except Exception as e:
            await self._safe_reply(update, f"Ошибка получения баланса: {e}")

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.positions:
            await self._safe_reply(update, "📋 Нет открытых позиций")
            return
        messages = []
        for pid, pos in list(self.positions.items()):
            try:
                ticker = await self.exchange.fetch_ticker(pos.symbol)
                current_price = ticker['last']
                rem = max(pos.remaining_quantity, 0.0)
                if pos.side == 'SHORT':
                    price_change_pct = ((pos.entry_price - current_price) / pos.entry_price) * 100
                else:
                    price_change_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100

                qty_ratio = rem / pos.quantity if pos.quantity > 0 else 1.0
                pnl = pos.realized_pnl_usd + (pos.amount_usdt * (price_change_pct / 100.0) * pos.leverage * qty_ratio)
                pnl_pct = price_change_pct * pos.leverage
                msg = (
                    f"{'🔴' if pos.side == 'SHORT' else '🟢'} #{pos.symbol.replace('/USDT', '')} {pos.side}\n"
                    f"Вход: {pos.entry_price:.5f} | Текущая: {current_price:.5f}\n"
                    f"SL: {pos.stop_loss:.5f}\n"
                    f"PnL: {'+' if pnl >= 0 else ''}${pnl:.2f} ({'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}%)\n"
                    f"Время: {str(datetime.now(timezone.utc) - pos.timestamp).split('.')[0]}"
                )
                messages.append(msg)
            except Exception:
                messages.append(f"⚪ {pos.symbol}: ошибка данных")
        await self._safe_reply(update, "\n\n".join(messages))

    async def cmd_signals(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = (
            f"📡 СИГНАЛЫ\n\n"
            f"Сегодня: {self.signals_today}\n"
            f"Последнее сканирование: {self.last_scan_time.strftime('%H:%M:%S') if self.last_scan_time else 'Не было'}"
        )
        await self._safe_reply(update, message)

    async def cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await self._safe_reply(update, "Использование: /close BTC")
            return
        pair = context.args[0].upper()
        found = None
        for pid, pos in self.positions.items():
            if pos.symbol.startswith(f"{pair}/"):
                found = pid
                break
        if not found:
            await self._safe_reply(update, f"Позиция {pair} не найдена")
            return
        await self._safe_reply(update, f"Закрываю {pair}...")
        await self.close_position(found)

    async def cmd_close_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.positions:
            await self._safe_reply(update, "Нет открытых позиций")
            return
        await self._safe_reply(update, f"Закрываю {len(self.positions)} позиций...")
        await self.close_all_positions()

    async def cmd_emergency(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._safe_reply(update, "🚨 ЭКСТРЕННОЕ ЗАКРЫТИЕ!")
        await self.close_all_positions(emergency=True)

    async def cmd_daily_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.send_daily_report()
        await self._safe_reply(update, "✅ Дневной отчёт отправлен")

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
            winrate = (total_wins / total_trades * 100) if total_trades > 0 else 0

            stats = self.db.get_daily_statistics()
            today_trades = stats.get('total_trades', 0) if stats else 0
            today_wins = stats.get('profitable_trades', 0) if stats else 0
            today_losses = stats.get('losing_trades', 0) if stats else 0
            today_pnl = stats.get('total_pnl', 0) if stats else 0
            today_winrate = (today_wins / today_trades * 100) if today_trades > 0 else 0

            unrealized_pnl = 0.0
            positions_text = ""
            for pid, pos in self.positions.items():
                try:
                    ticker = await self.exchange.fetch_ticker(pos.symbol)
                    current_price = ticker['last']
                    if pos.side == 'SHORT':
                        price_change_pct = ((pos.entry_price - current_price) / pos.entry_price) * 100
                    else:
                        price_change_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100

                    rem = max(pos.remaining_quantity, 0.0)
                    qty_ratio = rem / pos.quantity if pos.quantity > 0 else 1.0
                    pos_pnl = pos.realized_pnl_usd + (pos.amount_usdt * (price_change_pct / 100.0) * pos.leverage * qty_ratio)
                    roe = price_change_pct * pos.leverage
                    unrealized_pnl += pos_pnl
                    emoji = "🟢" if roe >= 0 else "🔴"
                    positions_text += (
                        f"{emoji} {pos.symbol.replace('/USDT', '')} | ROE: {roe:+.1f}% | ${pos_pnl:+.2f}\n"
                    )
                except Exception:
                    positions_text += f"⚠️ {pos.symbol}: ошибка\n"
            if not positions_text:
                positions_text = "Нет открытых позиций\n"

            msg = (
                f"📊 СТАТИСТИКА\n"
                f"━━━━━━━━━━━━━━━\n\n"
                f"💰 БАЛАНС\n"
                f"Total: ${usdt_total:.2f} | Free: ${usdt_free:.2f}\n"
                f"В позициях: ${usdt_used:.2f}\n\n"
                f"📈 PnL\n"
                f"Общий: {'+' if total_pnl >= 0 else ''}${total_pnl:.2f} ({pnl_pct:+.1f}%)\n"
                f"Сегодня (закр): {'+' if today_pnl >= 0 else ''}${today_pnl:.2f}\n"
                f"Нереализованный: {'+' if unrealized_pnl >= 0 else ''}${unrealized_pnl:.2f}\n\n"
                f"🎯 ВСЁ ВРЕМЯ: {total_trades} сделок | WR: {winrate:.1f}%\n"
                f"📅 СЕГОДНЯ: {today_trades} сделок (✅{today_wins} ❌{today_losses}) | WR: {today_winrate:.1f}%\n\n"
                f"📋 ОТКРЫТЫЕ ПОЗИЦИИ ({len(self.positions)}):\n"
                f"{positions_text}"
            )
            await self._safe_reply(update, msg)
        except Exception as e:
            await self._safe_reply(update, f"Ошибка статистики: {e}")

    async def cmd_stop_trading(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        global ALLOW_TRADING
        if not ALLOW_TRADING:
            await self._safe_reply(update, "🔴 Торговля уже остановлена!")
            return
        ALLOW_TRADING = False
        try:
            import os
            with open('.bot_stopped', 'w') as f:
                f.write('stopped')
        except: pass

        closed_count, kept_count = 0, 0
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
                    kept_count += 1
            except Exception as e:
                logger.error(f"Ошибка stop_trading {pos.symbol}: {e}")
        await self._safe_reply(update,
            f"🟡 ТОРГОВЛЯ ЗАВЕРШЕНА\n"
            f"Закрыто убыточных: {closed_count}\n"
            f"Оставлено прибыльных: {kept_count}"
        )

    async def cmd_start_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        global ALLOW_TRADING
        if ALLOW_TRADING:
            await self._safe_reply(update, "🟢 Бот уже работает!")
            return
        ALLOW_TRADING = True
        try:
            import os
            if os.path.exists('.bot_stopped'):
                os.remove('.bot_stopped')
        except: pass
        await self._safe_reply(update, "🟢 БОТ ВКЛЮЧЁН! Новые сделки разрешены.")

    async def cmd_stop_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        global ALLOW_TRADING
        if not ALLOW_TRADING:
            await self._safe_reply(update, "🔴 Бот уже остановлен!")
            return
        ALLOW_TRADING = False
        try:
            import os
            with open('.bot_stopped', 'w') as f:
                f.write('stopped')
        except: pass
        await self._safe_reply(update,
            "🔴 БОТ ПРИОСТАНОВЛЕН!\n"
            "Новые сделки не открываются.\n"
            "Открытые позиции продолжают управляться (SL/TP).\n"
            "/close_all — закрыть все"
        )

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        logger.error(f"Ошибка Telegram handler: {context.error}")

    async def start_telegram_bot(self):
        """Telegram polling с автоперезапуском при Conflict (не роняет весь бот)"""
        while self.is_running:
            try:
                from telegram.ext import MessageHandler, filters
                from telegram import BotCommand

                self.app = Application.builder().token(self.telegram_token).build()

                self.app.add_handler(CommandHandler("start", self.cmd_start))
                self.app.add_handler(CommandHandler("balance", self.cmd_balance))
                self.app.add_handler(CommandHandler("positions", self.cmd_positions))
                self.app.add_handler(CommandHandler("signals", self.cmd_signals))
                self.app.add_handler(CommandHandler("close", self.cmd_close))
                self.app.add_handler(CommandHandler("close_all", self.cmd_close_all))
                self.app.add_handler(CommandHandler("start_bot", self.cmd_start_bot))
                self.app.add_handler(CommandHandler("stop_bot", self.cmd_stop_bot))
                self.app.add_handler(CommandHandler("emergency", self.cmd_emergency))
                self.app.add_handler(CommandHandler("daily_report", self.cmd_daily_report))
                self.app.add_handler(CommandHandler("stats", self.cmd_stats))
                self.app.add_handler(CommandHandler("stop_trading", self.cmd_stop_trading))
                self.app.add_handler(CommandHandler("report", self.cmd_hourly_report))
                self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))
                self.app.add_error_handler(self.error_handler)

                try:
                    await self.app.bot.set_my_commands([
                        BotCommand("start", "Главное меню с кнопками"),
                        BotCommand("report", "Текущий отчёт за день"),
                        BotCommand("balance", "Баланс аккаунта"),
                        BotCommand("positions", "Открытые позиции"),
                        BotCommand("stats", "Полная статистика"),
                        BotCommand("signals", "Последние сигналы"),
                        BotCommand("close", "Закрыть сделку /close BTC"),
                        BotCommand("close_all", "Закрыть все позиции"),
                        BotCommand("start_bot", "Включить бота"),
                        BotCommand("stop_bot", "Выключить бота"),
                        BotCommand("stop_trading", "Стоп + закрыть убыточные"),
                        BotCommand("emergency", "Экстренное закрытие всего"),
                        BotCommand("daily_report", "Дневной отчёт"),
                    ])
                except Exception as e:
                    logger.warning(f"Не удалось установить меню команд: {e}")

                await self.app.initialize()
                await self.app.start()

                await self.app.updater.start_polling(
                    bootstrap_retries=3,
                    drop_pending_updates=True,
                    allowed_updates=Update.ALL_TYPES
                )

                logger.info("Telegram polling запущен")

                while self.is_running:
                    if self.app.updater and not self.app.updater.running:
                        raise Exception("Updater stopped (Telegram Conflict or crash)")
                    await asyncio.sleep(2)

                # Чистый shutdown при остановке бота
                try:
                    if self.app.updater and self.app.updater.running:
                        await self.app.updater.stop()
                    await self.app.stop()
                    await self.app.shutdown()
                except Exception:
                    pass
                break  # is_running=False — выходим

            except Exception as e:
                err_str = str(e)
                # При Conflict (другой экземпляр) — не роняем бота, просто ждём и пробуем снова
                if 'Conflict' in err_str or 'terminated by other' in err_str:
                    logger.warning(f"⚠️ Telegram Conflict: другой экземпляр работает. Повтор через 30 сек...")
                    # Пытаемся корректно остановить текущий app
                    try:
                        if self.app:
                            if self.app.updater and self.app.updater.running:
                                await self.app.updater.stop()
                            await self.app.stop()
                            await self.app.shutdown()
                    except Exception:
                        pass
                    self.app = None
                    await asyncio.sleep(30)
                else:
                    logger.error(f"Ошибка Telegram: {e}. Повтор через 15 сек...")
                    try:
                        if self.app:
                            if self.app.updater and self.app.updater.running:
                                await self.app.updater.stop()
                            await self.app.stop()
                            await self.app.shutdown()
                    except Exception:
                        pass
                    self.app = None
                    await asyncio.sleep(15)

    # ────────────────────────────────────────────────────────────────────────
    # ЗАПУСК
    # ────────────────────────────────────────────────────────────────────────

    async def start(self) -> bool:
        logger.info("Запуск Smart Money Aggressive Bot (ИСПРАВЛЕННАЯ ВЕРСИЯ)...")
        if not await self.connect():
            logger.error("Не удалось подключиться к бирже")
            return False

        self.is_running = True
        await self.update_top_symbols()

        # Восстановление позиций из БД — ТОЛЬКО если реально есть на бирже
        try:
            open_pos_db = self.db.get_open_positions()
            restored = 0
            cleaned = 0
            for pos_row in open_pos_db:
                symbol = pos_row['symbol']
                # Проверяем на бирже ПЕРЕД добавлением в бота
                try:
                    positions_check = await self.exchange.fetch_positions([symbol])
                    real_pos = next(
                        (p for p in positions_check if abs(float(p.get('contracts', 0) or 0)) > 0),
                        None
                    )
                except Exception:
                    real_pos = None

                if not real_pos:
                    # Позиции нет на бирже — помечаем как CLOSED БЕЗ записи PnL
                    logger.info(f"Очистка: {symbol} нет на бирже, чистим из БД (не считаем как убыток)")
                    # PnL=0, не влияет на статистику
                    self.db.update_position(pos_row['id'], pos_row['entry_price'], 0, 0, 'CLEANED')
                    cleaned += 1
                    continue

                ts_str = pos_row['timestamp']
                try:
                    if isinstance(ts_str, str):
                        ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                    else:
                        ts = datetime.now(timezone.utc)
                except:
                    ts = datetime.now(timezone.utc)

                pos = Position(
                    id=pos_row['id'], symbol=symbol, side=pos_row['side'],
                    entry_price=pos_row['entry_price'], stop_loss=pos_row['stop_loss'],
                    amount_usdt=pos_row['amount_usdt'], leverage=pos_row['leverage'],
                    quantity=pos_row['quantity'], remaining_quantity=pos_row['quantity'],
                    timestamp=ts
                )
                self.positions[pos.id] = pos
                restored += 1
            logger.info(f"Восстановлено {restored} позиций | Очищено {cleaned} фантомных")
        except Exception as e:
            logger.error(f"Ошибка при восстановлении позиций: {e}")

        await self.send_telegram_message(
            f"🟢 БОТ ЗАПУЩЕН\n"
            f"Депозит: ${config.DEPOSIT}\n"
            f"Плечо: x{config.LEVERAGE}\n"
            f"SL: -{config.STOP_LOSS_PCT}% | TP3: +{config.TP3_PCT}%\n"
            f"Фильтр волатильности: {config.MIN_VOLATILITY_PCT}%-{config.MAX_VOLATILITY_PCT}%\n"
            f"Пар для сканирования: {len(self.symbols_to_scan)}"
        )

        # Все задачи работают бесконечно. Если одна упадёт — остальные НЕ умирают.
        tasks = [
            asyncio.create_task(task_with_log("scanner", self.run_scanner_loop())),
            asyncio.create_task(task_with_log("monitoring", self.run_monitoring_loop())),
            asyncio.create_task(task_with_log("daily_report", self.run_daily_report_loop())),
            asyncio.create_task(task_with_log("hourly_report", self.run_hourly_report_loop())),
            asyncio.create_task(task_with_log("telegram", self.start_telegram_bot())),
            asyncio.create_task(task_with_log("fear_greed", check_fear_greed_index(self))),
            asyncio.create_task(task_with_log("trailing_stop", trailing_stop_loop(self)))
        ]

        # gather с return_exceptions=True: если задача упадёт, остальные продолжают!
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Задача {i} упала: {result}")

        logger.error("Все задачи завершились. Бот перезапускается...")
        await asyncio.sleep(2)
        return False

    async def stop(self):
        logger.info("Остановка бота...")
        self.is_running = False
        if hasattr(self, 'app') and self.app:
            try:
                if self.app.updater and self.app.updater.running:
                    await self.app.updater.stop()
                await self.app.stop()
                await self.app.shutdown()
            except Exception as e:
                logger.error(f"Ошибка остановки Telegram: {e}")
        await self.disconnect()


# ============================================================================
# ТОЧКА ВХОДА
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


async def main():
    from dotenv import load_dotenv
    _root = Path(__file__).resolve().parent
    load_dotenv(_root / '.env')

    API_KEY = _env_secret('BINANCE_API_KEY')
    API_SECRET = _env_secret('BINANCE_SECRET', 'BINANCE_API_SECRET')
    TELEGRAM_TOKEN = _env_secret('TELEGRAM_BOT_TOKEN')
    TELEGRAM_CHAT_ID = _env_secret('TELEGRAM_CHAT_ID')
    USER_CHAT_ID = _env_secret('USER_CHAT_ID')

    config.DEPOSIT = float(os.getenv('DEPOSIT', config.DEPOSIT))
    config.ENTRY_AMOUNT = float(os.getenv('ENTRY_AMOUNT', config.ENTRY_AMOUNT))
    config.LEVERAGE = int(os.getenv('LEVERAGE', config.LEVERAGE))
    config.STOP_LOSS_PCT = float(os.getenv('STOP_LOSS', config.STOP_LOSS_PCT))
    config.REINVEST_PROFITS = os.getenv('REINVEST_PROFITS', 'True').lower() == 'true'
    config.DRAWDOWN_ALERT = float(os.getenv('DRAWDOWN_ALERT', '12.0'))
    use_testnet = os.getenv('BINANCE_TESTNET', 'False').lower() == 'true'

    if not all([API_KEY, API_SECRET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID]):
        logger.warning("⚠️ Не все переменные окружения установлены!")

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
            logger.warning("Бот не подключился. Повтор через 60 сек...")
            while True:
                await asyncio.sleep(0.5)
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







def calculate_signal_strength(adx, volume_ratio, ema_trend, macd_ok):
    score = 0

    if adx >= 20:
        score += 1

    if volume_ratio >= 1.5:
        score += 1

    if ema_trend:
        score += 1

    if macd_ok:
        score += 1

    return score

print("🛡 SAFE MODE ENABLED | Better filters active")


# ===== ULTRA SMART FILTER =====
ENABLE_LIQUIDITY_SWEEP = True
ENABLE_ORDER_BLOCKS = True
ENABLE_VOLUME_CONFIRMATION = True
ENABLE_HTF_CONFIRMATION = True

def ultra_signal_filter(adx, rsi, volume_ratio, ema_trend, macd_ok, bos, fvg):
    """Упрощенный фильтр:
    EMA + BOS + Volume + умеренный ADX
    Без переоптимизации и поздних входов
    """

    return (
        ema_trend
        and bos
        and volume_ratio >= 1.5
        and adx >= 22
        and 50 <= rsi <= 70
    )

print("🧠 ULTRA ANALYSIS MODE ENABLED")


# ===== ENTRY QUALITY =====
USE_LIMIT_ENTRY = True
ENTRY_ON_FVG_RETEST = True
WAIT_FOR_CANDLE_CONFIRMATION = True
USE_BREAK_EVEN = True
BREAK_EVEN_AFTER = 2.0
TRAILING_AFTER = 4.0

print("🧠 TITAN SMART MONEY MODE ENABLED")

def format_signal(symbol, entry, tp1, tp2, tp3, sl, rr, leverage):
    return f"""
🔔 SMART MONEY SIGNAL

🟢 Монета: {symbol}

🛒 Вход: {entry}

🎯 TP1: {tp1}
🚀 TP2: {tp2}
💎 TP3: {tp3}

🛑 SL: {sl}

📊 Анализ:
✅ BOS
✅ FVG
✅ EMA200
✅ RSI
✅ ADX
✅ MACD
✅ Volume Spike
✅ Liquidity Sweep

📈 RR: 1:{rr}
⚡ Плечо: x{leverage}
"""


# ===== ADAPTIVE SIGNAL ENGINE =====
ENABLE_ADAPTIVE_SIGNALS = True

def adaptive_signal_engine(
    adx,
    rsi,
    volume_ratio,
    ema_trend,
    macd_ok,
    bos,
    fvg
):
    score = 0

    if ema_trend:
        score += 2

    if bos:
        score += 2

    if adx >= 18:
        score += 1

    if 50 <= rsi <= 75:
        score += 1

    if volume_ratio >= 1.2:
        score += 1

    if macd_ok:
        score += 1

    if fvg:
        score += 1

    return score >= 6

print("⚡ ADAPTIVE SIGNAL ENGINE ENABLED")


print("📡 REALTIME SCANNING ACTIVE")
print("🧠 BALANCED SMART MONEY MODE")


def safe_format_signal(symbol, entry=None, tp1=None, tp2=None, tp3=None, sl=None):
    try:
        return f"""
🔔 SMART MONEY SIGNAL

🟢 Монета: {symbol}

🛒 Вход: {entry}
🎯 TP1: {tp1}
🚀 TP2: {tp2}
💎 TP3: {tp3}
🛑 SL: {sl}
"""
    except Exception as e:
        return f"Signal formatting error: {e}"


if __name__ == "__main__":
    print("🚀 SMART MONEY BOT STARTED")


async def safe_hourly_report():
    global LAST_HOURLY_REPORT
    import time

    current_time = time.time()

    if current_time - LAST_HOURLY_REPORT < HOURLY_REPORT_INTERVAL:
        return

    LAST_HOURLY_REPORT = current_time

    try:
        pass
    except Exception as e:
        print(f"Hourly report error: {e}")


# ===== SAFE HOURLY REPORT SYSTEM =====

_last_report_hour = None

async def controlled_hourly_report():
    global _last_report_hour

    now = time.localtime()
    current_hour = now.tm_hour

    # Send only once per hour at minute 0
    if now.tm_min != 0:
        return

    # Prevent duplicate sends inside same hour
    if _last_report_hour == current_hour:
        return

    _last_report_hour = current_hour

    try:
        await send_hourly_report()
        print(f"✅ Hourly report sent for hour {current_hour}")
    except Exception as e:
        print(f"❌ Hourly report failed: {e}")

# ===== TP3 RUNNER LOGIC =====
async def runner_take_profit(current_roe, last_runner_tp=0):
    if not RUNNER_MODE:
        return 0

    target = last_runner_tp + RUNNER_STEP_PERCENT

    if current_roe >= target:
        return RUNNER_CLOSE_PERCENT

    return 0

# ===== BINANCE REDUCEONLY HOTFIX =====
# Исправление ошибки:
# {"code":-2022,"msg":"ReduceOnly Order is rejected."}
#
# Причина:
# Binance отвергает reduceOnly если:
# - позиция уже закрыта
# - size изменился
# - partial close
# - TP/SL сработал раньше бота
#
# Решение:
# перед закрытием:
# 1. fetch_positions()
# 2. проверяем contracts > 0
# 3. если reduceOnly rejected:
#    повторяем close БЕЗ reduceOnly
#
# Если у тебя есть блок:
#
# params={"reduceOnly": True}
#
# замени закрытие позиции на:
#
# try:
#     order = await self.exchange.create_order(
#         symbol=symbol,
#         type='market',
#         side=side,
#         amount=amount,
#         params={"reduceOnly": True}
#     )
#
# except Exception as e:
#     if "-2022" in str(e):
#
#         positions = await self.exchange.fetch_positions([symbol])
#
#         real_size = 0
#
#         for p in positions:
#             if p.get("symbol") == symbol:
#                 real_size = abs(float(p.get("contracts", 0) or 0))
#
#         if real_size > 0:
#             order = await self.exchange.create_order(
#                 symbol=symbol,
#                 type='market',
#                 side=side,
#                 amount=real_size
#             )
#
# =====================================