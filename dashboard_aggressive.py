"""
SMART MONEY CLONE - АГРЕССИВНАЯ ВЕРСИЯ
Веб-интерфейс для торгового бота
"""

import asyncio
import logging
import sqlite3
import os
import sys

# Fix Unicode encoding for Windows console (cp1251 -> utf-8)
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
elif sys.stdout:
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer)

from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / '.env')

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
import ccxt.async_support as ccxt

# === КОНФИГУРАЦИЯ ===
app = Flask(__name__)
app.config['SECRET_KEY'] = 'smart_money_aggressive_key_2026'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Параметры стратегии (СИНХРОНИЗИРОВАНО С БОТОМ)
CONFIG = {
    'deposit': 140.0,
    'entry_amount': 140.0,
    'max_positions': 15,
    'stop_loss_pct': 3.5,
    'leverage': 75,
    'daily_target_min': 10,
    'daily_target_max': 15,
    'direction': 'LONG_ONLY',
    'reinvest_profits': True,
    'entry_percent': 100.0,
    'drawdown_alert': 12.0
}

try:
    CONFIG['deposit'] = float(os.getenv('DEPOSIT', str(CONFIG['deposit'])))
    CONFIG['entry_amount'] = float(os.getenv('ENTRY_AMOUNT', str(CONFIG['entry_amount'])))
except (TypeError, ValueError):
    pass


def _deposit_reference() -> float:
    """Тот же ориентир %, что и у бота (DEPOSIT в .env или CONFIG)."""
    try:
        return float(os.getenv('DEPOSIT', str(CONFIG['deposit'])) or CONFIG['deposit'])
    except (TypeError, ValueError):
        return float(CONFIG['deposit'])


# Глобальное состояние
bot_state = {
    'status': 'running',
    'balance': 0,
    'today_pnl': 0,
    'today_pnl_pct': 0,
    'last_update': datetime.now(timezone.utc).isoformat()
}

# Используем ту же БД, что и торговый бот
DB_PATH = Path(__file__).parent / 'smart_money.db'

# === БАЗА ДАННЫХ ===
def init_db():
    """Инициализация базы данных (СИНХРОНИЗИРОВАНО С БОТОМ)"""
    conn = sqlite3.connect(DB_PATH)
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
    try:
        ref = float(os.getenv('DEPOSIT', '140') or '140')
        if ref > 0:
            cursor.execute(
                'UPDATE statistics SET total_pnl_pct = (total_pnl * 100.0 / ?) WHERE ABS(total_pnl) > 1e-9 OR total_trades > 0',
                (ref,),
            )
            conn.commit()
    except Exception:
        pass
    conn.close()
    print("Database initialized (Dashboard)")

def get_db_connection():
    """Получение соединения с БД"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# === API ЭНДПОИНТЫ ===

@app.route('/')
def index():
    """Главная страница дашборда"""
    return render_template('index.html')

@app.route('/signals')
def signals_page():
    """Страница сигналов"""
    return render_template('signals.html')

@app.route('/settings')
def settings_page():
    """Страница настроек"""
    return render_template('settings.html')


@app.route('/api/balance')
def api_balance():
    """Получить баланс и PnL - живые данные с биржи + БД"""
    import os
    
    # Получаем баланс с биржи (синхронно)
    live_balance = CONFIG['deposit']  # fallback
    try:
        import ccxt as ccxt_sync
        key = (os.getenv('BINANCE_API_KEY') or '').strip().strip('\ufeff')
        secret = (os.getenv('BINANCE_SECRET') or os.getenv('BINANCE_API_SECRET') or '').strip().strip('\ufeff')
        exchange = ccxt_sync.binanceusdm({
            'apiKey': key,
            'secret': secret,
            'enableRateLimit': True,
            'options': {'defaultType': 'future', 'recvWindow': 60000, 'adjustForTimeDifference': True}
        })
        is_testnet = str(os.getenv('BINANCE_TESTNET', 'False')).lower() in ('true', '1', 'yes')
        if is_testnet:
            demo_urls = {
                'fapiPublic': 'https://demo-fapi.binance.com/fapi/v1',
                'fapiPrivate': 'https://demo-fapi.binance.com/fapi/v1',
                'fapiPublicV2': 'https://demo-fapi.binance.com/fapi/v2',
                'fapiPrivateV2': 'https://demo-fapi.binance.com/fapi/v2',
                'fapiPublicV3': 'https://demo-fapi.binance.com/fapi/v3',
                'fapiPrivateV3': 'https://demo-fapi.binance.com/fapi/v3',
            }
            exchange.urls['api'].update(demo_urls)
            
        exchange.has['fetchCurrencies'] = False
        bal = exchange.fetch_balance()
        live_balance = float(bal.get('USDT', {}).get('total', 0))
    except Exception as e:
        print(f'Balance fetch error: {e}')
    
    # Статистика из БД бота
    conn = get_db_connection()
    
    # Дневной PnL из таблицы statistics (БД бота)
    today = datetime.utcnow().strftime('%Y-%m-%d')
    today_data = None
    try:
        today_data = conn.execute('SELECT * FROM statistics WHERE date = ?', (today,)).fetchone()
    except:
        pass
    
    today_pnl = today_data['total_pnl'] if today_data else 0
    dep_ref = _deposit_reference()
    today_pnl_pct = (today_pnl / dep_ref) * 100.0 if dep_ref > 0 else 0.0
    
    conn.close()
    
    bot_state['balance'] = live_balance
    bot_state['today_pnl'] = today_pnl
    bot_state['today_pnl_pct'] = today_pnl_pct
    bot_state['last_update'] = datetime.now(timezone.utc).isoformat()
    
    return jsonify({
        'balance': round(live_balance, 2),
        'today_pnl': round(today_pnl, 2),
        'today_pnl_pct': round(today_pnl_pct, 2),
        'status': bot_state['status'],
        'last_update': bot_state['last_update']
    })

@app.route('/api/positions')
def api_positions():
    """Получить открытые позиции"""
    conn = get_db_connection()
    
    positions = conn.execute('''
        SELECT * FROM positions 
        WHERE status = 'OPEN' 
        ORDER BY timestamp DESC
    ''').fetchall()
    
    conn.close()
    
    result = []
    for pos in positions:
        result.append({
            'id': pos['id'],
            'symbol': pos['symbol'],
            'side': pos['side'],
            'entry_price': pos['entry_price'],
            'amount': pos['amount_usdt'],
            'leverage': pos['leverage'],
            'stop_loss': pos['stop_loss'],
            'timestamp': pos['timestamp'],
            'current_price': None,
            'pnl': pos['pnl'] or 0,
            'pnl_pct': pos['pnl_pct'] or 0
        })
    
    return jsonify(result)

@app.route('/api/stats')
def api_stats():
    """Получить статистику из БД бота"""
    conn = get_db_connection()
    
    # Из таблицы positions (бота)
    try:
        row = conn.execute('''
            SELECT 
                COUNT(*) as total_trades,
                COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0) as total_profit,
                COALESCE(SUM(CASE WHEN pnl < 0 THEN ABS(pnl) ELSE 0 END), 0) as total_loss,
                COALESCE(SUM(pnl), 0) as net_profit,
                COALESCE(MAX(pnl), 0) as best_trade,
                COALESCE(MIN(pnl), 0) as worst_trade,
                CASE WHEN COUNT(*) > 0 
                    THEN ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1)
                    ELSE 0 END as win_rate
            FROM positions WHERE status = 'CLOSED'
        ''').fetchone()
    except:
        row = None
    
    # Дневная статистика: средний % дня = среднее (PnL / депозит * 100), а не AVG(накопленных total_pnl_pct из старых версий)
    dep_ref = _deposit_reference()
    try:
        days_row = conn.execute(
            '''
            SELECT 
                COUNT(*) as total_days,
                COALESCE(SUM(CASE WHEN total_pnl > 0 THEN 1 ELSE 0 END), 0) as profitable_days,
                COALESCE(SUM(CASE WHEN total_pnl < 0 THEN 1 ELSE 0 END), 0) as losing_days,
                COALESCE(AVG(CASE WHEN ? > 0 THEN total_pnl * 100.0 / ? END), 0) as avg_daily_pct
            FROM statistics
            ''',
            (dep_ref, dep_ref),
        ).fetchone()
    except:
        days_row = None
    
    conn.close()
    
    return jsonify({
        'total_profit': round(row['total_profit'], 2) if row else 0,
        'total_loss': round(row['total_loss'], 2) if row else 0,
        'net_profit': round(row['net_profit'], 2) if row else 0,
        'profitable_days': days_row['profitable_days'] if days_row else 0,
        'losing_days': days_row['losing_days'] if days_row else 0,
        'win_rate': round(row['win_rate'], 1) if row else 0,
        'avg_daily_pct': round(days_row['avg_daily_pct'], 1) if days_row else 0,
        'best_trade': round(row['best_trade'], 2) if row else 0,
        'worst_trade': round(row['worst_trade'], 2) if row else 0
    })

@app.route('/api/daily_pnl')
def api_daily_pnl():
    """Получить PnL по дням для календаря из БД бота"""
    conn = get_db_connection()
    
    # Из таблицы statistics (бота)
    try:
        daily_data = conn.execute('''
            SELECT date, total_pnl, total_pnl_pct, total_trades, profitable_trades
            FROM statistics 
            ORDER BY date ASC
        ''').fetchall()
    except:
        daily_data = []
    
    conn.close()
    
    dep_ref = _deposit_reference()
    result = {}
    for row in daily_data:
        day_pnl = float(row['total_pnl'] or 0)
        day_pct = (day_pnl / dep_ref) * 100.0 if dep_ref > 0 else 0.0
        result[row['date']] = {
            'pnl': round(day_pnl, 2),
            'pnl_pct': round(day_pct, 2),
            'trades': row['total_trades'],
            'profitable': row['profitable_trades']
        }
    
    return jsonify(result)

@app.route('/api/trades')
def api_trades():
    """Получить последние сделки из БД бота"""
    conn = get_db_connection()
    
    try:
        trades = conn.execute('''
            SELECT symbol, close_price, pnl, pnl_pct, close_timestamp, status
            FROM positions 
            WHERE status = 'CLOSED' 
            ORDER BY close_timestamp DESC 
            LIMIT 20
        ''').fetchall()
    except:
        trades = []
    
    conn.close()
    
    result = []
    for trade in trades:
        result.append({
            'symbol': trade['symbol'],
            'exit_price': trade['close_price'],
            'pnl': round(trade['pnl'], 2) if trade['pnl'] else 0,
            'pnl_pct': round(trade['pnl_pct'], 2) if trade['pnl_pct'] else 0,
            'timestamp': trade['close_timestamp'],
            'status': 'win' if (trade['pnl'] or 0) > 0 else 'loss'
        })
    
    return jsonify(result)

@app.route('/api/signals')
def api_signals():
    """Получить историю сигналов из БД бота"""
    conn = get_db_connection()
    
    try:
        signals = conn.execute('''
            SELECT * FROM signals 
            ORDER BY timestamp DESC 
            LIMIT 50
        ''').fetchall()
    except:
        signals = []
    
    conn.close()
    
    result = []
    for sig in signals:
        result.append({
            'id': sig['id'],
            'symbol': sig['symbol'],
            'direction': sig['signal_type'],
            'entry_price': sig['entry_price'],
            'stop_loss': 0,
            'smc_score': sig['smc_score'],
            'structure': '',
            'fvg': False,
            'rsi': 0,
            'adx': 0,
            'timestamp': sig['timestamp'],
            'status': 'executed' if sig['executed'] else 'active',
            'exit_price': None,
            'pnl': None,
            'pnl_pct': None
        })
    
    return jsonify(result)

@app.route('/api/config')
def api_config():
    """Получить текущую конфигурацию"""
    return jsonify(CONFIG)

@app.route('/api/config', methods=['POST'])
def api_update_config():
    """Обновить конфигурацию"""
    global CONFIG
    
    data = request.json
    if data:
        if 'deposit' in data:
            CONFIG['deposit'] = float(data['deposit'])
        if 'entry_amount' in data:
            CONFIG['entry_amount'] = float(data['entry_amount'])
        if 'max_positions' in data:
            CONFIG['max_positions'] = int(data['max_positions'])
        if 'stop_loss_pct' in data:
            CONFIG['stop_loss_pct'] = float(data['stop_loss_pct'])
        if 'leverage' in data:
            CONFIG['leverage'] = int(data['leverage'])
        if 'daily_target_min' in data:
            CONFIG['daily_target_min'] = int(data['daily_target_min'])
        if 'daily_target_max' in data:
            CONFIG['daily_target_max'] = int(data['daily_target_max'])
        if 'reinvest_profits' in data:
            CONFIG['reinvest_profits'] = bool(data['reinvest_profits'])
        if 'entry_percent' in data:
            CONFIG['entry_percent'] = float(data['entry_percent'])
        if 'drawdown_alert' in data:
            CONFIG['drawdown_alert'] = float(data['drawdown_alert'])
    
    return jsonify({'status': 'success', 'config': CONFIG})

@app.route('/api/close/<pair>', methods=['POST'])
def api_close_position(pair):
    """Закрыть позицию по паре"""
    # Здесь будет логика закрытия через бота
    # Пока просто эмуляция
    return jsonify({'status': 'success', 'message': f'Position {pair} closing...'})

@app.route('/api/close_all', methods=['POST'])
def api_close_all():
    """Закрыть все позиции"""
    return jsonify({'status': 'success', 'message': 'Closing all positions...'})

@app.route('/api/emergency', methods=['POST'])
def api_emergency():
    """Экстренный выход"""
    return jsonify({'status': 'success', 'message': 'EMERGENCY CLOSE INITIATED!'})

@app.route('/api/start_bot', methods=['POST'])
def api_start_bot():
    """Запустить бота"""
    global bot_state
    bot_state['status'] = 'running'
    socketio.emit('bot_status', {'status': 'running'})
    return jsonify({'status': 'success', 'message': 'Bot started'})

@app.route('/api/stop_bot', methods=['POST'])
def api_stop_bot():
    """Остановить бота"""
    global bot_state
    bot_state['status'] = 'stopped'
    socketio.emit('bot_status', {'status': 'stopped'})
    return jsonify({'status': 'success', 'message': 'Bot stopped'})

# === WEBSOCKET ===

@socketio.on('connect')
def handle_connect(auth=None):
    """Подключение клиента"""
    print('Client connected')
    emit('initial_data', {
        'balance': bot_state,
        'status': bot_state['status']
    })

@socketio.on('disconnect')
def handle_disconnect():
    """Отключение клиента"""
    print('Client disconnected')

# === ФОНОВЫЕ ЗАДАЧИ ===

async def update_prices():
    """Обновление цен в реальном времени"""
    exchange = ccxt.binanceusdm({
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })
    
    is_testnet = str(os.getenv('BINANCE_TESTNET', 'False')).lower() in ('true', '1', 'yes')
    if is_testnet:
        demo_urls = {
            'fapiPublic': 'https://demo-fapi.binance.com/fapi/v1',
            'fapiPrivate': 'https://demo-fapi.binance.com/fapi/v1',
            'fapiPublicV2': 'https://demo-fapi.binance.com/fapi/v2',
            'fapiPrivateV2': 'https://demo-fapi.binance.com/fapi/v2',
            'fapiPublicV3': 'https://demo-fapi.binance.com/fapi/v3',
            'fapiPrivateV3': 'https://demo-fapi.binance.com/fapi/v3',
        }
        exchange.urls['api'].update(demo_urls)
    
    while True:
        try:
            # Получаем открытые позиции
            conn = get_db_connection()
            positions = conn.execute('''
                SELECT symbol, entry_price, amount_usdt, leverage, stop_loss, id
                FROM positions WHERE status = 'OPEN'
            ''').fetchall()
            conn.close()  # Закрываем, чтобы не держать лок во время запросов
            
            # Собираем обновления
            updates = []
            
            for pos in positions:
                try:
                    # Получаем текущую цену
                    ticker = await exchange.fetch_ticker(pos['symbol'])
                    current_price = ticker['last']
                    
                    # Рассчитываем PnL
                    entry = pos['entry_price']
                    pnl_pct = ((current_price - entry) / entry) * 100 * pos['leverage']
                    pnl = (pos['amount_usdt'] / pos['leverage']) * (pnl_pct / 100)
                    
                    # Сохраняем для массового обновления
                    updates.append((pnl, pnl_pct, pos['id']))
                    
                    # Отправляем через WebSocket
                    socketio.emit('position_update', {
                        'id': pos['id'],
                        'symbol': pos['symbol'],
                        'current_price': current_price,
                        'pnl': round(pnl, 2),
                        'pnl_pct': round(pnl_pct, 2)
                    })
                    
                    # Проверка алертов
                    if pnl_pct >= 10:
                        socketio.emit('alert', {
                            'type': 'profit_10',
                            'symbol': pos['symbol'],
                            'pnl_pct': round(pnl_pct, 2)
                        })
                    if pnl_pct >= 15:
                        socketio.emit('alert', {
                            'type': 'profit_15',
                            'symbol': pos['symbol'],
                            'pnl_pct': round(pnl_pct, 2)
                        })
                    if pnl_pct >= 40:
                        socketio.emit('alert', {
                            'type': 'profit_40',
                            'symbol': pos['symbol'],
                            'pnl_pct': round(pnl_pct, 2)
                        })
                    
                except Exception as e:
                    print(f"Error updating {pos['symbol']}: {e}")
            
            if updates:
                conn = get_db_connection()
                conn.executemany('''
                    UPDATE positions 
                    SET pnl = ?, pnl_pct = ?
                    WHERE id = ?
                ''', updates)
                conn.commit()
                conn.close()
            
            await asyncio.sleep(5)  # Обновление каждые 5 секунд
            
        except Exception as e:
            print(f"Price update error: {e}")
            await asyncio.sleep(10)

def run_async_loop():
    """Запуск асинхронного цикла"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(update_prices())

# === ЗАПУСК ===

if __name__ == '__main__':
    # Инициализация БД
    init_db()
    
    # Запуск фонового потока для обновления цен
    import threading
    price_thread = threading.Thread(target=run_async_loop, daemon=True)
    price_thread.start()
    
    # Запуск сервера
    print("🚀 SMART MONEY CLONE Dashboard starting...")
    print("📊 Access at: http://localhost:5000")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
