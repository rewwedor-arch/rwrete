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
from telegram.ext import Application, CommandHandler, ContextTypes

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
    """Конфигурация стратегии SMART MONEY — ТУРБО РЕЖИМ (Пампы)"""
    # Финансовые параметры
    DEPOSIT: float = 50.0  # Стартовый депозит USDT
    ENTRY_AMOUNT: float = 50.0  # Базовая сумма (используется если REINVEST=False)
    LEVERAGE: int = 75  # Максимальное плечо (x75)

    # Риск-менеджмент — ШИРОКИЙ КОРИДОР для высоковолатильных альтов
    STOP_LOSS_PCT: float = 0.8  # SL -3.5% от цены (чтобы не выбивало шумом)
    TAKE_PROFIT_PCT: float = 3.0  # TP1 +3.0% (быстрый фикс)
    TAKE_PROFIT: float = TAKE_PROFIT_PCT  # Backward compatibility for code that uses TAKE_PROFIT
    TP2_PCT: float = 6.0  # TP2 +6.0% (основной профит)
    TP3_PCT: float = 12.0  # TP3 +12.0% (луншот)

    # Цели
    DAILY_TARGET_MIN: float = 10.0  # Минимальная цель в день %
    DAILY_TARGET_MAX: float = 15.0  # Максимальная цель в день %

    # Режим работы
    WORK_HOURS: str = "24/7"
    DIRECTION: str = "BOTH"  # LONG и SHORT

    # Параметры сигналов — КЛАССИЧЕСКИЕ 7 ИНДИКАТОРОВ
    MIN_INDICATORS_SCORE: int = 4  # Минимум 4 из 7
    TOTAL_INDICATORS: int = 7

    # Таймфреймы
    SCANNER_TIMEFRAME: str = '5m'
    TREND_TIMEFRAME: str = '15m'
    EMA_TIMEFRAME: str = '1h'

    # Алёрты по прибыли (в % ROE с учётом плеча)
    PROFIT_ALERT_10: float = 50.0    # +50% ROE
    PROFIT_ALERT_15: float = 150.0   # +150% ROE
    PROFIT_ALERT_40: float = 300.0   # +300% ROE
    DRAWDOWN_ALERT: float = 12.0

    # ===================================================================
    # ПОРТФЕЛЬНАЯ СТРАТЕГИЯ: 3 слота x 33% капитала
    # ===================================================================
    # С $50 и 75x плечом:
    #   Слот 1: $16.67 мар x 75 = $1250 номинал (монета A)
    #   Слот 2: $16.67 мар x 75 = $1250 номинал (монета B)
    #   Слот 3: $16.67 мар x 75 = $1250 номинал (монета C)
    # Слот 1 закрылся +$8 -> след. вход: ($50+$8)/3 = $19.33
    # 3 шанса поймать движение, реинвест после КАЖДОЙ сделки
    # ===================================================================
    REINVEST_PROFITS: bool = True   # Реинвестировать прибыль

    # ===================================================================
    # ДИНАМИЧЕСКОЕ УПРАВЛЕНИЕ ПОЗИЦИЯМИ (авто-масштаб)
    # ===================================================================
    # Бот сам считает оптимальное число слотов и размер входа:
    #
    #   slots = floor(equity / MIN_SLOT_USDT)
    #   entry  = (free_equity / slots) * (SMC_Score / 5.0)
    #
    # Чем больше денег, тем больше позиций бот может открыть одновременно.
    # Сильные сигналы (7/7) получают больше денег, слабые (4/7) - меньше.
    # ===================================================================
    MIN_SLOT_USDT: float = 5.0     # Минимальный капитал на 1 сделку ($)

    # Выход по откату от пика (в % ROE)
    MIN_PEAK_PNL_TO_TRACK: float = 20.0    # Следим за пиком с +20% ROE (было 30)
    PEAK_DRAWDOWN_CLOSE_PCT: float = 8.0    # Закрыть при откате 8% ROE от пика (было 15)
    TRAILING_ACTIVATE_PCT: float = 35.0     # Трейлинг после +35% ROE (было 45)
    TRAILING_DRAWDOWN_CLOSE_PCT: float = 5.0 # Жёсткий трейлинг: закрыть при откате 5% (было 10)
    PARTIAL_TP1_PCT: float = 15.0   # Фиксируем 30% при +22% ROE
    PARTIAL_TP2_PCT: float = 30.0   # Фиксируем ещё 30% при +40% ROE
    PARTIAL_TP3_PCT: float = 50.0   # Полная фиксация при +60% ROE

    # Время позиции
    POSITION_TIMEOUT_HOURS: int = 36

    # Трейлинг-стоп (в % цены, без плеча)
    TRAILING_ACTIVATE_PCT: float = 1.0      # Активировать трейлинг при +1% цены
    TRAILING_DISTANCE_PCT: float = 0.5      # Дистанция SL от пика (0.5% цены)
    TRAILING_BREAKEVEN_PCT: float = 0.2     # Безубыток: SL на 0.2% от входа

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


async def trailing_stop_loop(bot: 'SmartMoneyBot'):
    """Фоновый трейлинг-стоп: проверка каждые 10 секунд.
    При профите > TRAILING_ACTIVATE_PCT — двигает SL в безубыток и далее за ценой.
    """
    while bot.is_running:
        try:
            for pid, pos in list(bot.positions.items()):
                try:
                    ticker = await bot.exchange.fetch_ticker(pos.symbol)
                    current_price = ticker['last']

                    # Расчет изменения цены в % (без плеча)
                    if pos.side == 'SHORT':
                        price_change_pct = ((pos.entry_price - current_price) / pos.entry_price) * 100
                    else:
                        price_change_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100

                    # Активация трейлинга при профите > 1%
                    if price_change_pct >= config.TRAILING_ACTIVATE_PCT:
                        if not pos.trailing_active:
                            pos.trailing_active = True
                            pos.trailing_peak = price_change_pct
                            logger.info(f"Трейлинг активирован {pos.symbol}: {price_change_pct:+.2f}%")

                        # Обновление пика
                        if price_change_pct > pos.trailing_peak:
                            pos.trailing_peak = price_change_pct

                        # Расчет нового SL: на расстоянии TRAILING_DISTANCE_PCT от пика
                        if pos.side == 'SHORT':
                            # Для SHORT: SL двигается вниз (цена растёт → SL растёт)
                            new_sl_price = pos.entry_price * (1 - (pos.trailing_peak - config.TRAILING_DISTANCE_PCT) / 100)
                        else:
                            # Для LONG: SL двигается вверх (цена растёт → SL растёт)
                            new_sl_price = pos.entry_price * (1 + (pos.trailing_peak - config.TRAILING_DISTANCE_PCT) / 100)

                        # Проверяем что новый SL лучше текущего
                        should_update = False
                        if pos.side == 'SHORT':
                            # Для SHORT: SL должен быть ниже текущего (двигаться вниз за ценой)
                            if new_sl_price < pos.stop_loss:
                                should_update = True
                        else:
                            # Для LONG: SL должен быть выше текущего (двигаться вверх за ценой)
                            if new_sl_price > pos.stop_loss:
                                should_update = True

                        # Минимальный SL — безубыток + 0.2%
                        if pos.side == 'SHORT':
                            min_sl = pos.entry_price * (1 + config.TRAILING_BREAKEVEN_PCT / 100)
                            if new_sl_price > min_sl:
                                new_sl_price = min_sl
                        else:
                            min_sl = pos.entry_price * (1 - config.TRAILING_BREAKEVEN_PCT / 100)
                            if new_sl_price < min_sl:
                                new_sl_price = min_sl

                        if should_update:
                            new_sl_price = float(bot.exchange.price_to_precision(pos.symbol, new_sl_price))
                            old_sl = pos.stop_loss
                            pos.stop_loss = new_sl_price
                            logger.info(
                                f"Трейлинг SL {pos.symbol}: {old_sl:.4f} → {new_sl_price:.4f} "
                                f"(пик: {pos.trailing_peak:+.2f}%)"
                            )

                            # Отправляем новый SL на биржу
                            try:
                                qty_rounded = float(bot.exchange.amount_to_precision(pos.symbol, pos.remaining_quantity))
                                if qty_rounded <= 0:
                                    continue
                                # Удаляем старый SL и ставим новый
                                await bot.exchange.cancel_all_orders(pos.symbol)
                                
                                if pos.side == 'SHORT':
                                    actual_tp = float(bot.exchange.price_to_precision(pos.symbol, pos.entry_price * (1 - config.TP3_PCT / 100)))
                                    await bot.exchange.create_order(
                                        pos.symbol, 'STOP_MARKET', 'BUY', qty_rounded,
                                        params={'stopPrice': new_sl_price, 'reduceOnly': True}
                                    )
                                    await bot.exchange.create_order(
                                        pos.symbol, 'TAKE_PROFIT_MARKET', 'BUY', qty_rounded,
                                        params={'stopPrice': actual_tp, 'reduceOnly': True}
                                    )
                                else:
                                    actual_tp = float(bot.exchange.price_to_precision(pos.symbol, pos.entry_price * (1 + config.TP3_PCT / 100)))
                                    await bot.exchange.create_order(
                                        pos.symbol, 'STOP_MARKET', 'SELL', qty_rounded,
                                        params={'stopPrice': new_sl_price, 'reduceOnly': True}
                                    )
                                    await bot.exchange.create_order(
                                        pos.symbol, 'TAKE_PROFIT_MARKET', 'SELL', qty_rounded,
                                        params={'stopPrice': actual_tp, 'reduceOnly': True}
                                    )
                            except Exception as e:
                                logger.warning(f"Не удалось обновить SL/TP на бирже для {pos.symbol}: {e}")

                except Exception as e:
                    logger.error(f"Ошибка трейлинга для {pos.symbol}: {e}")

        except Exception as e:
            logger.error(f"Ошибка в trailing_stop_loop: {e}")

        await asyncio.sleep(10)


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
            
            if pdi + mdi > 0:
                dx_val = abs(pdi - mdi) / (pdi + mdi) * 100
            else:
                dx_val = 0
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
        """Обнаружение Fair Value Gap.
        Возвращает: 'BULLISH', 'BEARISH', или '' (пусто).
        """
        if len(ohlcv) < 3:
            return ''
        
        bullish = False
        bearish = False
        
        for i in range(len(ohlcv) - 2):
            c1, c2, c3 = ohlcv[i], ohlcv[i + 1], ohlcv[i + 2]
            high1, low1 = c1[2], c1[3]
            high2, low2 = c2[2], c2[3]
            high3, low3 = c3[2], c3[3]
            body = high2 - low2
            
            # Бычий FVG: low3 > high1
            if low3 > high1 and (low3 - high1) > body * 0.3:
                bullish = True
            
            # Медвежий FVG: high3 < low1
            if high3 < low1 and (low1 - high3) > body * 0.3:
                bearish = True
        
        if bullish:
            return 'BULLISH'
        if bearish:
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
                                    # ═══ 7. Всплеск объема (нейтральный) ═══
            vol_sma = self.calculate_sma(volumes_5m, 20)
            if vol_sma and vol_sma[-1] > 0:
                vol_ratio = volumes_5m[-1] / vol_sma[-1]
                if vol_ratio > 1.3:
                    long_score += 1
                    short_score += 1
                    result['volume_ok'] = True
                    long_ind['volume_spike'] = True
                    short_ind['volume_spike'] = True

            # Определяем базовое направление по EMA50
            potential_dir = 'LONG' if (ema50 and current_price > ema50[-1]) else 'SHORT'
            if not ema50:
                result['signal'] = False
                result['score'] = "NO_EMA"
                return result

            # 🔹 ПАТТЕРН 1: По тренду с имбалансом
            p1_trend = (potential_dir == 'LONG' and current_price > ema50[-1]) or \
                       (potential_dir == 'SHORT' and current_price < ema50[-1])
            p1_fvg = (fvg == 'BULLISH' and potential_dir == 'LONG') or \
                     (fvg == 'BEARISH' and potential_dir == 'SHORT')
            p1_adx = adx and adx[-1] > 20

            # 🔹 ПАТТЕРН 2: Смена структуры + Объем
            p2_bos = (bos in ['BOS_UP', 'CHoCH_BULLISH'] and potential_dir == 'LONG') or \
                     (bos in ['BOS_DOWN', 'CHoCH_BEARISH'] and potential_dir == 'SHORT')
            p2_macd = (macd['histogram'] > 0 and macd['macd'] > macd['signal'] and potential_dir == 'LONG') or \
                      (macd['histogram'] < 0 and macd['macd'] < macd['signal'] and potential_dir == 'SHORT')
            p2_vol = vol_sma and vol_ratio > 1.3

            # Проверка срабатывания
            if p1_trend and p1_fvg and p1_adx:
                result['signal'] = True
                result['direction'] = potential_dir
                result['score'] = "PATTERN_1_TREND_FVG"
                result['indicators'] = {'ema_trend': True, 'fvg': bool(fvg), 'adx': True}
            elif p2_bos and p2_macd and p2_vol:
                result['signal'] = True
                result['direction'] = potential_dir
                result['score'] = "PATTERN_2_BOS_VOL"
                result['indicators'] = {'bos': True, 'macd': True, 'volume': True}
            else:
                result['signal'] = False
                result['direction'] = potential_dir
                result['score'] = "NO_MATCH"
                result['indicators'] = {}


            
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
        self.symbols_to_scan: List[str] = []


        
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

    async def update_top_symbols(self):
        """Загружает Топ-80 USDT-фьючерсов по объему за 24ч"""
        try:
            markets = await self.exchange.load_markets()
            tickers = await self.exchange.fetch_tickers()
            usdt_perps = []
            for symbol, market in markets.items():
                if symbol.endswith('/USDT') and market.get('type') == 'swap':
                    vol = tickers[symbol].get('quoteVolume', 0.0) if symbol in tickers else 0.0
                    usdt_perps.append((symbol, vol))
            usdt_perps.sort(key=lambda x: x[1], reverse=True)
            self.symbols_to_scan = [pair[0] for pair in usdt_perps[:80]]
            logger.info(f"🔄 Топ-80 пар обновлен. Доступно фьючерсов: {len(markets)}")
        except Exception as e:
            logger.error(f"Ошибка обновления топа пар: {e}")
            if not self.symbols_to_scan:
                self.symbols_to_scan = ['BTC/USDT', 'ETH/USDT', 'BNB/USDT', 'SOL/USDT']





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
        """
        Расчёт размера позиции с динамическим числом слотов и взвешиванием по score.

        Алгоритм:
          1. virtual_equity = DEPOSIT + закрытый PnL из БД
          2. locked_margin  = сумма маржи открытых позиций (amount_usdt = margin)
          3. free_equity    = virtual_equity - locked_margin
          4. optimal_slots  = compute_optimal_slots(free_equity)  # от СВОБОДНЫХ средств!
          5. base_slot      = free_equity / optimal_slots
          6. weight         = score / 5.0
          7. entry_amount   = min(base_slot * weight, free_equity)  # не больше свободных!

        Возвращает: (quantity, margin, notional)
        """
        try:
            stats = self.db.get_all_statistics()
            total_pnl = float(stats.get('total_pnl') or 0.0)
            virtual_equity = max(config.DEPOSIT + total_pnl, 0.0)

            # locked_margin — это МАРЖА (не номинал), т.к. amount_usdt хранит margin
            locked_margin = sum(p.amount_usdt for p in self.positions.values())

            free_equity = virtual_equity - locked_margin

            if free_equity < config.MIN_SLOT_USDT:
                logger.warning(
                    f"Свободных средств ${free_equity:.2f} < минимума ${config.MIN_SLOT_USDT:.0f}. "
                    f"Ждём закрытия позиций."
                )
                return 0, 0, 0

            # Слоты считаем от СВОБОДНЫХ средств, а не от полного баланса
            optimal_slots = self.compute_optimal_slots(free_equity)

            logger.info(
                f"💰 Баланс: виртуальный=${virtual_equity:.2f} "
                f"(депозит=${config.DEPOSIT} + PnL=${total_pnl:.2f}) | "
                f"слотов={optimal_slots} | "
                f"заморожено=${locked_margin:.2f} | свободно=${free_equity:.2f}"
            )

            base_slot = free_equity / optimal_slots

            # Взвешивание по score: score 5 = x1.0 (базовый), score 7 = x1.4, score 4 = x0.8
            weight = max(score, config.MIN_INDICATORS_SCORE) / 5.0
            amount_usdt = base_slot * weight

            # Ограничиваем: маржа не может быть больше свободного капитала
            amount_usdt = min(amount_usdt, free_equity)

            min_slot = config.MIN_SLOT_USDT
            if amount_usdt < min_slot:
                if free_equity >= min_slot:
                    amount_usdt = min_slot
                else:
                    logger.warning(f"Сумма слота ${amount_usdt:.2f} < минимума ${min_slot:.0f} и нет свободных средств. Пропускаем.")
                    return 0, 0, 0

            leverage = config.LEVERAGE
            margin = amount_usdt
            notional = margin * leverage
            quantity = notional / entry_price

            logger.info(
                f"Расчет позиции: score={score}/7, weight={weight:.2f}, "
                f"маржа=${margin:.2f}, плечо=x{leverage}, "
                f"номинал=${notional:.2f}, qty={quantity:.6f}"
            )

            return quantity, margin, notional
        except Exception as e:
            logger.error(f"Ошибка расчёта размера позиции: {e}")
            return 0, 0, 0
    
    async def send_telegram_message(self, message: str, parse_mode: str = None):
        """Отправка сообщения в все активные чаты"""
        try:
            for chat_id in self.active_chat_ids:
                try:
                    await self._bot.send_message(
                        chat_id=chat_id,
                        text=message,
                        parse_mode=parse_mode
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки в {chat_id}: {e}")
        except Exception as e:
            logger.error(f"Ошибка send_telegram_message: {e}")
    
    def calculate_stop_loss(self, entry_price: float, side: str = 'LONG') -> float:
        """Расчет стоп-лосса"""
        if side == 'SHORT':
            return entry_price * (1 + config.STOP_LOSS_PCT / 100)
        return entry_price * (1 - config.STOP_LOSS_PCT / 100)
        
    def calculate_take_profit_max(self, entry_price: float, side: str = 'LONG') -> float:
        """Расчет максимального тейк-профита (TP3)"""
        if side == 'SHORT':
            return entry_price * (1 - config.TP3_PCT / 100)
        return entry_price * (1 + config.TP3_PCT / 100)
    
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
            quantity, margin, actual_amount = await self.calculate_position_size(entry_price, score=5)


         
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
            
            logger.info(f"Пытаюсь закрыть позицию {symbol}, количество: {qty_close}, тип: {position.side}")
            
            # Очистка оставшихся ордеров (SL/TP) BEFORE MARKET ORDER
            try:
                await self.exchange.cancel_all_orders(symbol)
                logger.info(f"Отменены все ордера для {symbol} перед закрытием")
            except Exception as cancel_e:
                logger.warning(f"Не удалось отменить ордера для {symbol} перед закрытием: {cancel_e}")
            
            # Закрытие: для LONG продаём, для SHORT покупаем
            try:
                if position.side == 'SHORT':
                    order = await self.exchange.create_market_buy_order(
                        symbol, qty_close, params={'reduceOnly': True}
                    )
                else:
                    order = await self.exchange.create_market_sell_order(
                        symbol, qty_close, params={'reduceOnly': True}
                    )
                
                logger.info(f"Ордер на закрытие размещен: {order.get('id', 'N/A')}, статус: {order.get('status', 'N/A')}")
            except Exception as order_e:
                logger.error(f"Ошибка при создании ордера закрытия {symbol}: {order_e}")
                # Пробуем получить актуальные цены и закрыть принудительно
                try:
                    ticker = await self.exchange.fetch_ticker(symbol)
                    current_price = ticker['last']
                    logger.info(f"Текущая цена {symbol}: {current_price}")
                except Exception as cleanup_e:
                    logger.error(f"Ошибка при получении цены {symbol}: {cleanup_e}")
                
                raise order_e
            
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
            error_msg = f"❌ Ошибка закрытия позиции {position_id}: {str(e)}"
            await self.send_telegram_message(error_msg)
            return False
    
    async def close_all_positions(self, emergency: bool = False):
        """Закрытие всех позиций"""
        position_ids = list(self.positions.keys())
        logger.info(f"Начинаю закрытие {len(position_ids)} позиций, emergency={emergency}")
        
        closed_count = 0
        for i, pid in enumerate(position_ids):
            logger.info(f"Закрываю позицию {i+1}/{len(position_ids)}: {pid}")
            success = await self.close_position(pid, emergency)
            if success:
                closed_count += 1
            # Небольшая задержка между закрытиями, чтобы избежать флуда в API
            await asyncio.sleep(0.5)
        
        logger.info(f"Завершено закрытие всех позиций: {closed_count}/{len(position_ids)} успешно")
        await self.send_telegram_message(f"✅ Закрытие всех позиций завершено: {closed_count}/{len(position_ids)} успешно")

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

    async def apply_dynamic_sl(self, position: Position, price_change_pct: float, current_price: float):
        """Динамический стоп-лосс — двигаем вверх/вниз по мере роста прибыли.
        
        ВАЖНО: price_change_pct — это процент изменения ЦЕНЫ в нашу пользу (без плеча),
        НЕ ROE (pnl_pct с плечом). Пороги 5/10/20% — это движение цены.
        """
        new_sl_price = None
        new_level = position.dynamic_sl_level

        # Пороги — по движению ЦЕНЫ в нашу пользу (без плеча)
        if price_change_pct >= 20.0 and position.dynamic_sl_level < 3:
            if position.side == 'SHORT':
                new_sl_price = position.entry_price * 0.90   # SL на +10% прибыли (цена ниже)
            else:
                new_sl_price = position.entry_price * 1.10   # SL на +10% прибыли
            new_level = 3
        elif price_change_pct >= 10.0 and position.dynamic_sl_level < 2:
            if position.side == 'SHORT':
                new_sl_price = position.entry_price * 0.95   # SL на +5% прибыли
            else:
                new_sl_price = position.entry_price * 1.05   # SL на +5% прибыли
            new_level = 2
        elif price_change_pct >= 5.0 and position.dynamic_sl_level < 1:
            if position.side == 'SHORT':
                new_sl_price = position.entry_price * 0.995  # Безубыток
            else:
                new_sl_price = position.entry_price * 1.005  # Безубыток
            new_level = 1

        is_better_sl = False
        if new_sl_price:
            if position.side == 'SHORT':
                is_better_sl = new_sl_price < position.stop_loss
            else:
                is_better_sl = new_sl_price > position.stop_loss

        if new_sl_price and is_better_sl:
            # ⚠️ Критическая проверка: стоп-лосс должен быть далеко от текущей цены
            too_close = False
            if position.side == 'SHORT':
                min_allowed_sl = current_price * 1.005
                too_close = new_sl_price <= min_allowed_sl
            else:
                max_allowed_sl = current_price * 0.995
                too_close = new_sl_price >= max_allowed_sl

            if too_close:
                # Логируем только один раз для этого уровня, чтобы не спамить
                warn_key = f"{position.symbol}_{new_level}"
                if not hasattr(position, '_sl_warn_logged'):
                    position._sl_warn_logged = set()
                if warn_key not in position._sl_warn_logged:
                    position._sl_warn_logged.add(warn_key)
                    logger.warning(
                        f"⚠️ Динамический SL для {position.symbol} ({new_sl_price:.5f}) "
                        f"слишком близко к цене ({current_price:.5f}). Пропускаем."
                    )
                return

            try:
                new_sl_price = float(self.exchange.price_to_precision(position.symbol, new_sl_price))
                qty_rounded = float(self.exchange.amount_to_precision(position.symbol, position.remaining_quantity))

                if qty_rounded <= 0:
                    return
                await self.exchange.cancel_all_orders(position.symbol)
                
                close_side = 'BUY' if position.side == 'SHORT' else 'SELL'
                if position.side == 'SHORT':
                    actual_tp = float(self.exchange.price_to_precision(position.symbol, position.entry_price * (1 - config.TP3_PCT / 100)))
                else:
                    actual_tp = float(self.exchange.price_to_precision(position.symbol, position.entry_price * (1 + config.TP3_PCT / 100)))

                await self.exchange.create_order(
                    symbol=position.symbol, type='STOP_MARKET', side=close_side,
                    amount=qty_rounded,
                    params={'stopPrice': new_sl_price, 'reduceOnly': True}
                )
                await self.exchange.create_order(
                    symbol=position.symbol, type='TAKE_PROFIT_MARKET', side=close_side,
                    amount=qty_rounded,
                    params={'stopPrice': actual_tp, 'reduceOnly': True}
                )
                position.stop_loss = new_sl_price
                position.dynamic_sl_level = new_level
                labels = {1: 'БЕЗУБЫТОК', 2: '+5%', 3: '+10%'}
                pair = position.symbol.replace('/USDT', '')
                await self.send_telegram_message(
                    f"🔒 ДИНАМИЧЕСКИЙ SL | {pair}\n"
                    f"SL перенесён в {labels[new_level]}\n"
                    f"Новый SL: {new_sl_price:.5f}\n"
                    f"Текущий PnL цены: +{price_change_pct:.1f}%"
                )
            except Exception as e:
                logger.error(f"Ошибка переноса SL для {position.symbol}: {e}")

    async def monitor_positions(self):
        """Мониторинг позиций — Трейлинг, Частичные TP и Динамический SL"""
        for position_id, position in list(self.positions.items()):
            try:
                # 1. Получение текущей цены
                ticker = await self.exchange.fetch_ticker(position.symbol)
                current_price = ticker['last']
                
                # 2. Расчет PnL (универсальный для LONG и SHORT)
                if position.side == 'SHORT':
                    price_change_pct = ((position.entry_price - current_price) / position.entry_price) * 100
                else:
                    price_change_pct = ((current_price - position.entry_price) / position.entry_price) * 100
                pnl_pct = price_change_pct * position.leverage
                if position.side == 'SHORT':
                    pnl_usd = position.realized_pnl_usd + (position.entry_price - current_price) * position.remaining_quantity
                else:
                    pnl_usd = position.realized_pnl_usd + (current_price - position.entry_price) * position.remaining_quantity
                
                # 3. Обновление пика (peak_pnl)
                if pnl_pct > position.peak_pnl:
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
                            f"Закрыто на: {pnl_pct:+.1f}%\n"
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
                            f"Закрыто на: {pnl_pct:+.1f}%\n"
                            f"💰 Вложено: ${position.amount_usdt:.2f}\n"
                            f"Текущий PnL: {'+' if pnl_usd >= 0 else ''}${pnl_usd:.2f}"
                        )
                        await self.send_telegram_message(message)
                        await self.close_position(position_id)
                        continue



                # 7. ЧАСТИЧНАЯ ФИКСАЦИЯ
                if pnl_pct >= config.PARTIAL_TP1_PCT and not position.partial_tp1_done:
                    position.partial_tp1_done = True
                    qty_to_close = position.quantity * 0.40  # ЗАКРЫВАЕМ 40%
                    await self.close_partial_position(position, qty_to_close, current_price)
                    # СРАЗУ переводим SL в безубыток
                    position.dynamic_sl_level = 1
                    await self.apply_dynamic_sl(position, price_change_pct, current_price)
                    await self.send_telegram_message(
                        f"💰 ЧАСТИЧНАЯ ФИКСАЦИЯ | {pair}\n"
                        f"TP1 +{config.PARTIAL_TP1_PCT:.0f}% ROE достигнут\n"
                        f"Закрыто: 40% позиции | SL в безубыток"
                    )

                if pnl_pct >= config.PARTIAL_TP2_PCT and not position.partial_tp2_done:
                    position.partial_tp2_done = True
                    qty_to_close = position.quantity * 0.30  # ЗАКРЫВАЕМ ЕЩЕ 30%
                    await self.close_partial_position(position, qty_to_close, current_price)
                    await self.send_telegram_message(
                        f"🚀 TP2 ДОСТИГНУТ | {pair}\n"
                        f"Уровень +{config.PARTIAL_TP2_PCT:.0f}% ROE пройден\n"
                        f"Закрыто еще 30% (всего 70%)"
                    )

                if pnl_pct >= config.PARTIAL_TP3_PCT and not position.partial_tp3_done:
                    position.partial_tp3_done = True
                    await self.send_telegram_message(
                        f"💎 TP3 +{config.PARTIAL_TP3_PCT:.0f}% ROE | {pair}\n"
                        f"Полная фиксация остатка!"
                    )
                    await self.close_position(position_id)
                    continue


                # 7. ДИНАМИЧЕСКИЙ SL (передаём price_change_pct — без плеча!)
                await self.apply_dynamic_sl(position, price_change_pct, current_price)

                # 8. Проверка времени позиции
                await self.check_position_timeout(position)

            except Exception as e:
                logger.error(f"Ошибка мониторинга {position_id}: {e}")

    
    async def check_position_timeout(self, position: Position):
        """Проверка времени позиции"""
        now = datetime.now(timezone.utc)
        duration = now - position.timestamp
        duration_minutes = duration.total_seconds() / 60
        
        # Для убыточных позиций — закрываем через 30 минут если нет признаков разворота
        if position.peak_pnl < 2.0 and duration_minutes > 30:
            logger.info(f"Закрытие убыточной позиции {position.symbol} по таймауту ({duration_minutes:.0f} мин)")
            await self.send_telegram_message(
                f"⏱ ТАЙМАУТ УБЫТОЧНОЙ ПОЗИЦИИ | {position.symbol.replace('/USDT', '')}\n"
                f"Позиция в убытке {duration_minutes:.0f} минут\n"
                f"Пик PnL: +{position.peak_pnl:.1f}%\n"
                f"Закрытие для минимизации убытка."
            )
            await self.close_position(position.id)
            return
        
        # Общий таймаут для всех позиций
        if duration >= timedelta(hours=config.POSITION_TIMEOUT_HOURS):
            logger.info(f"Закрытие позиции {position.symbol} по таймауту ({config.POSITION_TIMEOUT_HOURS}ч)")
            await self.send_telegram_message(
                f"⏱ Истекло {config.POSITION_TIMEOUT_HOURS} ч для {position.symbol}. Автоматическое закрытие по правилам бота."
            )
            await self.close_position(position.id)
    
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
            await asyncio.sleep(1)  # Защита от лимитов Binance
            if self.signals_today >= self.max_signals_per_day:
                break
            
            # Пропуск если уже есть позиция по этому символу



            
            # Пропуск если уже есть позиция по этому символу
            if any(p.symbol == symbol for p in self.positions.values()):
                continue
            
            # Анализ
            smc_result = await self.smc_analyzer.analyze_symbol(symbol)
            
            # Проверяем, чтобы score был >= MIN_INDICATORS_SCORE (от 5/7)
            if smc_result['signal']:


                logger.info(f"СИГНАЛ найден: {symbol} (score: {smc_result['score']}/{config.TOTAL_INDICATORS})")
                
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
        last_update = datetime.now(timezone.utc)
        while self.is_running:
            try:
                # Обновляем топ каждый час
                if (datetime.now(timezone.utc) - last_update).total_seconds() > 3600:
                    await self.update_top_symbols()
                    last_update = datetime.now(timezone.utc)
                await self.scan_market()
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
        """Цикл отправки часовых отчётов"""
        while self.is_running:
            try:
                await asyncio.sleep(3600)
                if self.is_running:
                    await self.send_hourly_report()
            except Exception as e:
                logger.error(f"Ошибка в цикле часовых отчетов: {e}")
                await asyncio.sleep(60)
    
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
        logger.info(f"Получена команда /balance от {update.effective_chat.id}")
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
            logger.error(f"Ошибка в cmd_balance: {e}")

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /positions"""
        logger.info(f"Получена команда /positions от {update.effective_chat.id}")
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
        logger.info(f"Получена команда /signals от {update.effective_chat.id}")
        # Упрощенная реализация - последние 10 сигналов
        message = "📡 ПОСЛЕДНИЕ СИГНАЛЫ\n\n"
        message += f"Сегодня: {self.signals_today}/{self.max_signals_per_day}\n"
        message += f"Последнее сканирование: {self.last_scan_time or 'Не было'}"
        await update.message.reply_text(message)
    
    async def cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /close"""
        logger.info(f"Получена команда /close от {update.effective_chat.id}")
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
                logger.info(f"Найдена позиция {pid} для символа {symbol}")
                break
        
        if not found:
            await update.message.reply_text(f"Позиция по {pair} не найдена")
            logger.info(f"Позиция для символа {symbol} не найдена")
            return
        
        await update.message.reply_text(f"Закрываю позицию по {symbol}...")
        await self.close_position(found)
    
    async def cmd_close_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /close_all"""
        logger.info(f"Получена команда /close_all от {update.effective_chat.id}")
        if not self.positions:
            await update.message.reply_text("Нет открытых позиций")
            logger.info("Нет открытых позиций для закрытия")
            return
        
        logger.info(f"Пользователь инициировал закрытие {len(self.positions)} позиций")
        await update.message.reply_text(f"Закрываю {len(self.positions)} позиций...")
        await self.close_all_positions()
    
    async def cmd_emergency(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /emergency"""
        logger.info(f"Получена команда /emergency от {update.effective_chat.id}")
        await update.message.reply_text("🚨 ЭКСТРЕННОЕ ЗАКРЫТИЕ ВСЕХ ПОЗИЦИЙ!")
        await self.close_all_positions(emergency=True)
    
    async def cmd_daily_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /daily_report"""
        logger.info(f"Получена команда /daily_report от {update.effective_chat.id}")
        await self.send_daily_report()

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /stats — полная статистика: баланс, PnL, winrate, открытые позиции"""
        logger.info(f"Получена команда /stats от {update.effective_chat.id}")
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
        """Команда /stop_trading"""
        logger.info(f"Получена команда /stop_trading от {update.effective_chat.id}")
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
        """Команда /start_bot"""
        logger.info(f"Получена команда /start_bot от {update.effective_chat.id}")
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
        """Команда /stop_bot"""
        logger.info(f"Получена команда /stop_bot от {update.effective_chat.id}")
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

    def setup_telegram_handlers(self):
        """Настройка обработчиков Telegram команд"""
        from telegram.ext import MessageHandler, filters
        
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
        
        # Обработчик текстовых кнопок
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))

    async def run_telegram_bot(self):
        """Запуск Telegram бота с авто-перезапуском"""
        import threading
        from telegram.ext import MessageHandler, filters

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

                # Регистрация обработчиков
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

                # Меню команд
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

                # Запуск бота
                await app.initialize()
                await app.start()

                # Запуск опроса
                polling_error = [None]

                def polling_thread():
                    try:
                        app.updater.start_polling(
                            bootstrap_retries=3,
                            drop_pending_updates=True,
                            allowed_updates=Update.ALL_TYPES
                        )
                    except Exception as e:
                        polling_error[0] = e

                thread = threading.Thread(target=polling_thread)
                thread.start()

                # Ждем завершения опроса
                thread.join()

                if polling_error[0]:
                    raise polling_error[0]

            except Exception as e:
                logger.error(f"Ошибка при запуске Telegram бота: {e}")

    async def start_telegram_bot(self):
        """Старт Telegram бота с правильным управлением событийным циклом"""
        try:
            # Используем глобальный event loop
            loop = asyncio.get_event_loop()
            
            # Создаем Application с указанием event loop
            self.app = Application.builder().token(self.telegram_token).loop(loop).build()

            # Добавляем обработчики команд
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
            self.app.add_handler(CommandHandler("stop_trading", self.cmd_stop_trading))
            self.app.add_handler(CommandHandler("stats", self.cmd_stats))
            from telegram.ext import MessageHandler, filters
            self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))

            # Включаем логирование для отладки
            self.app.add_error_handler(self.error_handler)

            logger.info("Telegram bot initialized successfully")

            # Initialize and start the application properly
            await self.app.initialize()
            await self.app.start()

            # Start the updater separately
            if not self.app.updater._running:
                await self.app.updater.start_polling(
                    bootstrap_retries=3,
                    drop_pending_updates=True,
                    allowed_updates=Update.ALL_TYPES
                )

            logger.info("Telegram polling started")

            # Ждем пока бот работает
            while self.is_running:
                await asyncio.sleep(1)
                
            if self.app.updater and self.app.updater.running:
                await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()

        except Exception as e:
            logger.error(f"Failed to start Telegram bot: {e}")
            raise

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик ошибок"""
        logger.error(f"Error in Telegram handler: {context.error}")
        if update:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id if update.effective_chat else next(iter(self.active_chat_ids), None),
                    text="❌ Произошла ошибка при обработке вашего запроса. Пожалуйста, попробуйте позже."
                )
            except Exception as e:
                logger.error(f"Error sending error message to user: {e}")



    async def send_error_message(self, user_id: int, message: str) -> None:
        try:
            await self.bot.send_message(user_id, message)
        except Exception as e:
            logger.error(f"Error sending error message to user: {e}")

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
        
        #         # Запуск задач с логированием
        await self.update_top_symbols()
        
        tasks = [
            asyncio.create_task(task_with_log("scanner", self.run_scanner_loop())),
            asyncio.create_task(task_with_log("monitoring", self.run_monitoring_loop())),
            asyncio.create_task(task_with_log("daily_report", self.run_daily_report_loop())),
            asyncio.create_task(task_with_log("hourly_report", self.run_hourly_report_loop())),
            asyncio.create_task(task_with_log("telegram", self.start_telegram_bot())),
            asyncio.create_task(task_with_log("fear_greed", check_fear_greed_index(self))),
            asyncio.create_task(task_with_log("trailing_stop", trailing_stop_loop(self)))
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
        if hasattr(self, 'app') and self.app:
            try:
                if self.app.updater and self.app.updater.running:
                    await self.app.updater.stop()
                await self.app.stop()
                await self.app.shutdown()
            except Exception as e:
                logger.error(f"Ошибка при остановке Telegram: {e}")
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
