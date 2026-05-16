"""
Telegram Manager для агрессивного SMART MONEY бота
РУЧНОЕ УПРАВЛЕНИЕ - БЕЗ ВОПРОСОВ, ТОЛЬКО ВЫПОЛНЕНИЕ
"""

import asyncio
import logging
import sys
from datetime import datetime
from typing import Optional, Dict, Any

# Fix Unicode encoding for Windows console (cp1251 -> utf-8)
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
elif sys.stdout:
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer)

from telegram import Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters
)
import os
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('telegram_manager.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class TelegramManager:
    """
    Менеджер Telegram для ручного управления ботом
    Агрессивный стиль: выполнение команд без вопросов
    """
    
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.application = None
        self.trading_engine = None  # Будет установлен извне
        self.is_running = False
        
        # Статистика для отчетов
        self.daily_stats = {
            'trades_count': 0,
            'profitable_trades': 0,
            'daily_pnl': 0.0,
            'balance': 140.0,
            'deposit': 140.0
        }
        
    def set_trading_engine(self, engine):
        """Установка ссылки на торговый движок"""
        self.trading_engine = engine
        logger.info("Trading engine connected to Telegram Manager")
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start - приветствие и статус"""
        status = "🟢 РАБОТАЕТ" if self.is_running else "🔴 ОСТАНОВЛЕН"
        
        message = f"""
🤖 SMART MONEY AGGRESSIVE BOT

Статус: {status}
Режим: LONG ONLY
Депозит: ${self.daily_stats['deposit']:.2f}
Баланс: ${self.daily_stats['balance']:.2f}

Сегодня:
• Сделок: {self.daily_stats['trades_count']}
• PnL: ${self.daily_stats['daily_pnl']:+.2f}

Используй /help для списка команд
        """.strip()
        
        await update.message.reply_text(message)
        logger.info(f"Command /start executed by {update.effective_user.id}")
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /help - список команд"""
        message = """
📋 КОМАНДЫ УПРАВЛЕНИЯ:

/close {PAIR} - Закрыть позицию (например: /close VANA)
/close_all - Закрыть ВСЕ позиции
/emergency - СРОЧНО закрыть всё и остановить бота

/status - Текущий статус бота
/balance - Баланс и PnL
/positions - Открытые позиции
/signals - Последние сигналы
/daily_report - Отчет за день

/help - Эта справка

⚠️ ВАЖНО: Команды выполняются НЕМЕДЛЕННО без подтверждений!
        """.strip()
        
        await update.message.reply_text(message)
        logger.info(f"Command /help executed by {update.effective_user.id}")
    
    async def balance_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /balance - баланс и PnL"""
        if not self.trading_engine:
            await update.message.reply_text("❌ Trading engine не подключен")
            return
        
        try:
            balance_info = await self.trading_engine.get_balance_info()
            
            message = f"""
💰 БАЛАНС

Доступно: ${balance_info.get('available', 0):.2f} USDT
В позициях: ${balance_info.get('in_positions', 0):.2f} USDT
Общий: ${balance_info.get('total', 0):.2f} USDT

PnL сегодня: ${balance_info.get('daily_pnl', 0):+.2f} ({balance_info.get('daily_pnl_percent', 0):+.2f}%)
Общий PnL: ${balance_info.get('total_pnl', 0):+.2f}

Депозит: ${balance_info.get('deposit', 140):.2f}
Рост: {(balance_info.get('total', 0) / balance_info.get('deposit', 140) * 100 - 100):+.1f}%
            """.strip()
            
            await update.message.reply_text(message)
            logger.info(f"Command /balance executed by {update.effective_user.id}")
            
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка получения баланса: {str(e)}")
            logger.error(f"Error in /balance: {e}")
    
    async def positions_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /positions - открытые позиции"""
        if not self.trading_engine:
            await update.message.reply_text("❌ Trading engine не подключен")
            return
        
        try:
            positions = await self.trading_engine.get_open_positions()
            
            if not positions:
                await update.message.reply_text("📭 Нет открытых позиций")
                return
            
            message = "📊 ОТКРЫТЫЕ ПОЗИЦИИ:\n\n"
            
            for pos in positions:
                pnl_pct = pos.get('pnl_percent', 0)
                pnl_usd = pos.get('pnl_usd', 0)
                emoji = "✅" if pnl_usd >= 0 else "❌"
                
                message += f"""
{pos.get('symbol', 'UNKNOWN')} {pos.get('side', 'LONG')}
├─ Вход: {pos.get('entry_price', 0):.4f}
├─ Сейчас: {pos.get('current_price', 0):.4f}
├─ PnL: ${pnl_usd:+.2f} ({pnl_pct:+.2f}%) {emoji}
├─ Стоп: {pos.get('stop_loss', 0):.4f} (-3.5%)
└─ Плечо: x{pos.get('leverage', 75)}

"""
            
            message += "\nДля закрытия: /close {PAIR}"
            
            await update.message.reply_text(message)
            logger.info(f"Command /positions executed by {update.effective_user.id}")
            
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка получения позиций: {str(e)}")
            logger.error(f"Error in /positions: {e}")
    
    async def close_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Команда /close {PAIR} - закрыть конкретную позицию
        ВЫПОЛНЯЕТСЯ НЕМЕДЛЕННО БЕЗ ВОПРОСОВ
        """
        if not context.args or len(context.args) == 0:
            await update.message.reply_text(
                "❌ Укажите пару: /close VANA\nПример: /close BTC"
            )
            return
        
        pair = context.args[0].upper()
        
        if not self.trading_engine:
            await update.message.reply_text("❌ Trading engine не подключен")
            return
        
        try:
            await update.message.reply_text(f"⏳ Закрываю {pair}...")
            
            # Немедленное закрытие позиции
            result = await self.trading_engine.close_position(pair)
            
            if result.get('success'):
                entry = result.get('entry_price', 0)
                exit_price = result.get('exit_price', 0)
                pnl_usd = result.get('pnl_usd', 0)
                pnl_pct = result.get('pnl_percent', 0)
                duration = result.get('duration', 'N/A')
                
                message = f"""
✅ ЗАКРЫТО {pair}

Вход: {entry:.4f}
Выход: {exit_price:.4f}
PnL: ${pnl_usd:+.2f} ({pnl_pct:+.2f}%)
Время в рынке: {duration}
                """.strip()
                
                await update.message.reply_text(message)
                logger.info(f"Position {pair} closed manually: PnL ${pnl_usd:+.2f}")
            else:
                await update.message.reply_text(f"❌ Ошибка: {result.get('error', 'Неизвестная ошибка')}")
                
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка закрытия: {str(e)}")
            logger.error(f"Error in /close {pair}: {e}")
    
    async def close_all_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Команда /close_all - закрыть все позиции
        ВЫПОЛНЯЕТСЯ НЕМЕДЛЕННО БЕЗ ВОПРОСОВ
        """
        if not self.trading_engine:
            await update.message.reply_text("❌ Trading engine не подключен")
            return
        
        try:
            await update.message.reply_text("⏳ Закрываю ВСЕ позиции...")
            
            # Закрытие всех позиций
            results = await self.trading_engine.close_all_positions()
            
            total_pnl = sum(r.get('pnl_usd', 0) for r in results if r.get('success'))
            closed_count = sum(1 for r in results if r.get('success'))
            
            message = f"""
✅ ВСЕ ПОЗИЦИИ ЗАКРЫТЫ

Закрыто позиций: {closed_count}
Общий PnL: ${total_pnl:+.2f}
            """.strip()
            
            await update.message.reply_text(message)
            logger.info(f"All positions closed manually: {closed_count} positions, PnL ${total_pnl:+.2f}")
            
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка закрытия всех позиций: {str(e)}")
            logger.error(f"Error in /close_all: {e}")
    
    async def emergency_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Команда /emergency - СРОЧНЫЙ выход из всех позиций
        1. Закрывает ВСЕ позиции по рынку НЕМЕДЛЕННО
        2. Отменяет все ордера
        3. Останавливает бота
        """
        if not self.trading_engine:
            await update.message.reply_text("❌ Trading engine не подключен")
            return
        
        try:
            await update.message.reply_text("🚨 ЭКСТРЕННЫЙ ВЫХОД! Закрываю всё...")
            
            # Закрытие всех позиций
            results = await self.trading_engine.close_all_positions(emergency=True)
            
            # Отмена всех ордеров
            cancelled = await self.trading_engine.cancel_all_orders()
            
            # Остановка торгового цикла
            self.trading_engine.stop_trading()
            self.is_running = False
            
            total_pnl = sum(r.get('pnl_usd', 0) for r in results if r.get('success'))
            closed_count = sum(1 for r in results if r.get('success'))
            
            message = f"""
🚨 ЭКСТРЕННЫЙ ВЫХОД

Закрыто позиций: {closed_count}
Отменено ордеров: {cancelled}
Общий PnL: ${total_pnl:+.2f}

Бот остановлен. Требуются новые команды для запуска.
            """.strip()
            
            await update.message.reply_text(message)
            logger.critical(f"EMERGENCY STOP executed: {closed_count} positions closed, PnL ${total_pnl:+.2f}")
            
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка экстренного выхода: {str(e)}")
            logger.error(f"Error in /emergency: {e}")
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /status - текущий статус бота"""
        status = "🟢 РАБОТАЕТ" if self.is_running else "🔴 ОСТАНОВЛЕН"
        
        if self.trading_engine:
            market_status = "🟢 Рынок открыт" if self.trading_engine.is_market_open else "🔴 Рынок закрыт"
        else:
            market_status = "⚪ Не подключен"
        
        message = f"""
📊 СТАТУС БОТА

Статус: {status}
{market_status}

Режим: LONG ONLY
Макс позиций: 3
Плечо: x75
Стоп лосс: 3.5%

Депозит: ${self.daily_stats['deposit']:.2f}
Баланс: ${self.daily_stats['balance']:.2f}
Сегодня: ${self.daily_stats['daily_pnl']:+.2f}
        """.strip()
        
        await update.message.reply_text(message)
        logger.info(f"Command /status executed by {update.effective_user.id}")
    
    async def daily_report_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /daily_report - отчет за день"""
        if not self.trading_engine:
            await update.message.reply_text("❌ Trading engine не подключен")
            return
        
        try:
            report = await self.trading_engine.get_daily_report()
            today = datetime.now().strftime("%d.%m.%Y")
            
            profitable = report.get('profitable_trades', 0)
            total = report.get('total_trades', 0)
            win_rate = (profitable / total * 100) if total > 0 else 0
            
            message = f"""
📊 ОТЧЕТ ЗА {today}

Сделок: {total}
Прибыльных: {profitable}
Убыточных: {total - profitable}
Win Rate: {win_rate:.1f}%

PnL: ${report.get('daily_pnl', 0):+.2f} ({report.get('daily_pnl_percent', 0):+.2f}%)

Баланс: ${report.get('balance', 0):.2f}
Депозит: ${report.get('deposit', 140):.2f}
Рост: +{(report.get('balance', 0) / report.get('deposit', 140) * 100 - 100):.0f}%

Средний % в день: +{report.get('avg_daily_percent', 0):.1f}%
            """.strip()
            
            await update.message.reply_text(message)
            logger.info(f"Daily report sent to {update.effective_user.id}")
            
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка получения отчета: {str(e)}")
            logger.error(f"Error in /daily_report: {e}")
    
    async def signals_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /signals - последние сигналы"""
        if not self.trading_engine:
            await update.message.reply_text("❌ Trading engine не подключен")
            return
        
        try:
            signals = await self.trading_engine.get_recent_signals(limit=5)
            
            if not signals:
                await update.message.reply_text("📭 Нет недавних сигналов")
                return
            
            message = "📡 ПОСЛЕДНИЕ СИГНАЛЫ:\n\n"
            
            for sig in signals:
                timestamp = sig.get('timestamp', '')
                pair = sig.get('pair', 'UNKNOWN')
                side = sig.get('side', 'LONG')
                entry = sig.get('entry_price', 0)
                sl = sig.get('stop_loss', 0)
                score = sig.get('smc_score', 0)
                status = sig.get('status', 'ACTIVE')
                pnl = sig.get('pnl_percent', 0)
                
                emoji = "🟢" if side == "LONG" else "🔴"
                status_emoji = "✅" if pnl > 0 else "❌" if pnl < 0 else "⏳"
                
                message += f"""
{timestamp}
{emoji} {side} {pair}
Вход: {entry:.4f} | Стоп: {sl:.4f}
SMC: {score}/7 | Статус: {status} {status_emoji}
PnL: {pnl:+.2f}%

"""
            
            await update.message.reply_text(message)
            logger.info(f"Signals list sent to {update.effective_user.id}")
            
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка получения сигналов: {str(e)}")
            logger.error(f"Error in /signals: {e}")
    
    # ==================== МЕТОДЫ ОТПРАВКИ СООБЩЕНИЙ ====================
    
    async def send_signal(self, signal_data: Dict[str, Any]):
        """Отправка сигнала о новом входе"""
        pair = signal_data.get('pair', 'UNKNOWN')
        entry = signal_data.get('entry_price', 0)
        sl = signal_data.get('stop_loss', 0)
        leverage = signal_data.get('leverage', 75)
        amount = signal_data.get('amount', 140)
        smc_score = signal_data.get('smc_score', 0)
        structure = signal_data.get('structure', 'BOS')
        fvg = signal_data.get('fvg', False)
        rsi = signal_data.get('rsi', 0)
        adx = signal_data.get('adx', 0)
        
        structure_emoji = "✅" if structure in ['BOS', 'CHoCH'] else "❌"
        fvg_emoji = "✅" if fvg else "❌"
        
        message = f"""
🟢 ЗАХОДИМ LONG
#{pair}/USDT
Вход: {entry:.4f}
Стоп: {sl:.4f} (-3.5%)
Плечо: x{leverage}
Сумма: ${amount}

SMC: {smc_score}/7
Структура: {structure} {structure_emoji}
FVG: {fvg_emoji}
RSI: {rsi:.0f} ADX: {adx:.0f}

Без TP. Выход по команде.
        """.strip()
        
        await self.send_message(message)
        logger.info(f"Signal sent for {pair} at {entry}")
    
    async def send_profit_alert(self, pair: str, price: float, pnl_usd: float, pnl_percent: float):
        """Отправка алерта о прибыли"""
        message = f"""
💰 +{pnl_percent:.1f}% по {pair}
Цена: {price:.4f}
PnL: +${pnl_usd:.2f}

Закрываем? Жду команду.
        """.strip()
        
        await self.send_message(message)
        logger.info(f"Profit alert sent for {pair}: +{pnl_percent:.1f}%")
    
    async def send_urgent_alert(self, pair: str, current_price: float, peak_price: float, pnl_usd: float, pnl_percent: float):
        """Отправка срочного алерта об откате"""
        message = f"""
🚨 {pair} откат 5% от пика!
Сейчас: {current_price:.4f} (было {peak_price:.4f})
PnL: ${pnl_usd:+.2f} (+{pnl_percent:+.1f}%)

Закрываем СЕЙЧАС?
        """.strip()
        
        await self.send_message(message)
        logger.warning(f"Urgent alert sent for {pair}: -5% from peak")
    
    async def send_daily_report(self, report_data: Dict[str, Any]):
        """Отправка дневного отчета"""
        today = datetime.now().strftime("%d.%m.%Y")
        
        profitable = report_data.get('profitable_trades', 0)
        total = report_data.get('total_trades', 0)
        pnl = report_data.get('daily_pnl', 0)
        pnl_percent = report_data.get('daily_pnl_percent', 0)
        balance = report_data.get('balance', 0)
        deposit = report_data.get('deposit', 140)
        growth = (balance / deposit * 100 - 100)
        avg_daily = report_data.get('avg_daily_percent', 0)
        
        message = f"""
📊 ОТЧЕТ ЗА {today}

Сделок: {total}
Прибыльных: {profitable}
PnL: ${pnl:+.2f} (+{pnl_percent:+.1f}%)

Баланс: ${balance:.2f}
Депозит: ${deposit:.2f}
Рост: +{growth:.0f}%

Средний % в день: +{avg_daily:.1f}%
        """.strip()
        
        await self.send_message(message)
        logger.info(f"Daily report sent: PnL ${pnl:+.2f}")
    
    async def send_position_closed(self, pair: str, entry: float, exit: float, pnl_usd: float, pnl_percent: float, duration: str):
        """Отправка подтверждения о закрытии позиции"""
        emoji = "✅" if pnl_usd >= 0 else "❌"
        
        message = f"""
{emoji} ЗАКРЫТО {pair}
Вход: {entry:.4f}
Выход: {exit:.4f}
PnL: ${pnl_usd:+.2f} ({pnl_percent:+.2f}%)
Время: {duration}
        """.strip()
        
        await self.send_message(message)
        logger.info(f"Position closed notification sent for {pair}")
    
    async def send_stop_loss_triggered(self, pair: str, entry: float, sl: float, loss_usd: float):
        """Отправка уведомления о срабатывании стоп-лосса"""
        message = f"""
❌ STOP LOSS {pair}
Вход: {entry:.4f}
Выход: {sl:.4f}
PnL: -${loss_usd:.2f} (-3.5%)
        """.strip()
        
        await self.send_message(message)
        logger.warning(f"Stop loss triggered for {pair}: -${loss_usd:.2f}")
    
    async def send_message(self, text: str):
        """Отправка сообщения в Telegram"""
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram token or chat_id not set, skipping message")
            return
        
        try:
            bot = Bot(token=self.bot_token)
            await bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode='HTML'
            )
            logger.debug(f"Message sent to chat {self.chat_id}")
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
    
    def setup_handlers(self):
        """Настройка обработчиков команд"""
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("balance", self.balance_command))
        self.application.add_handler(CommandHandler("positions", self.positions_command))
        self.application.add_handler(CommandHandler("close", self.close_command))
        self.application.add_handler(CommandHandler("close_all", self.close_all_command))
        self.application.add_handler(CommandHandler("emergency", self.emergency_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler("daily_report", self.daily_report_command))
        self.application.add_handler(CommandHandler("signals", self.signals_command))
        
        logger.info("Telegram command handlers registered")
    
    async def run(self):
        """Запуск Telegram бота"""
        if not self.bot_token:
            logger.error("Telegram bot token not provided!")
            return
        
        try:
            # Создание приложения
            self.application = Application.builder().token(self.bot_token).build()
            
            # Настройка обработчиков
            self.setup_handlers()
            
            # Запуск
            self.is_running = True
            logger.info("Telegram bot starting...")
            
            await self.application.initialize()
            await self.application.start()
            await self.application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            
            logger.info("Telegram bot is running")
            
            # Держим бота запущенным
            while self.is_running:
                await asyncio.sleep(1)
                
        except Exception as e:
            logger.error(f"Telegram bot error: {e}")
            raise
        finally:
            if self.application:
                await self.application.updater.stop()
                await self.application.stop()
                await self.application.shutdown()
            self.is_running = False
            logger.info("Telegram bot stopped")
    
    def stop(self):
        """Остановка бота"""
        self.is_running = False
        logger.info("Telegram bot stop requested")


async def main():
    """Точка входа для standalone запуска"""
    # Загрузка конфигурации
    bot_token = os.getenv('TELEGRAM_BOT_TOKEN', '')
    chat_id = os.getenv('TELEGRAM_CHAT_ID', '')
    
    if not bot_token:
        print("❌ TELEGRAM_BOT_TOKEN not set in .env")
        return
    
    if not chat_id:
        print("❌ TELEGRAM_CHAT_ID not set in .env")
        return
    
    # Создание менеджера
    manager = TelegramManager(bot_token, chat_id)
    
    print("🤖 SMART MONEY Telegram Manager")
    print("Запуск... Нажмите Ctrl+C для остановки")
    
    try:
        await manager.run()
    except KeyboardInterrupt:
        print("\nОстановка по команде пользователя")
    except Exception as e:
        print(f"❌ Ошибка: {e}")


if __name__ == "__main__":
    asyncio.run(main())
