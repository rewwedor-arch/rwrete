---
title: Smart Money Trading Bot
emoji: 📈
colorFrom: green
colorTo: blue
sdk: docker
app_file: main.py
pinned: false
---

# Smart Money Aggressive Trading Bot

Автоматический торговый бот для Binance Futures с Telegram управлением.

## Настройка

1. Создайте копию этого Space (Duplicate)
2. В Settings → Repository secrets добавьте:
   - `BINANCE_API_KEY` — ваш API ключ Binance
   - `BINANCE_SECRET` — ваш секретный ключ Binance
   - `TELEGRAM_BOT_TOKEN` — токен Telegram бота от @BotFather
   - `TELEGRAM_CHAT_ID` — ваш Chat ID

## Команды бота

- `/start` — показать статус
- `/positions` — открытые позиции
- `/balance` — баланс
- `/daily_report` — дневной отчёт
- `/close_all` — закрыть все позиции
- `/emergency` — экстренное закрытие
