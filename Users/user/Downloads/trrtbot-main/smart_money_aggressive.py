
    async def start_telegram_bot(self):
        """Старт Telegram бота с правильным управлением событийным циклом"""
        try:
            # Используем глобальный event loop
            loop = asyncio.get_event_loop()
            
            # Создаем Application с указанием event loop
            self.app = Application.builder().token(self.telegram_token).loop(loop).build()

            # Добавляем обработчики команд
            self.app.add_handler(CommandHandler("start", self.handle_start))
            self.app.add_handler(CommandHandler("stats", self.handle_stats))
            self.app.add_handler(CommandHandler("help", self.handle_help))

            # Включаем логирование для отладки
            self.app.add_error_handler(self.error_handler)

            logger.info("Telegram bot initialized successfully")

            # Proper initialization sequence
            await self.app.initialize()
            await self.app.start()

            # Start the updater separately with proper configuration
            if not self.app.updater._running:
                await self.app.updater.start_polling(
                    bootstrap_retries=3,
                    drop_pending_updates=True,
                    allowed_updates=Update.ALL_TYPES
                )

            logger.info("Telegram polling started")

            # Main loop to keep the bot running
            while self.is_running:
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Failed to start Telegram bot: {e}")
            raise

    async def handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик /start"""
        logger.info("Received /start command")  # 添加日志
        chat_id = update.effective_chat.id
        if str(chat_id) in self.active_chat_ids:
            await context.bot.send_message(
                chat_id=chat_id,
                text="👋 Привет! Я — Smart Money Aggressive Trading Bot.\n\n"
                     "Используйте команды:\n"
                     "/stats — статистика торговли\n"
                     "/help — помощь"
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Нет доступа к этому боту."
            )

    async def handle_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик /stats"""
        logger.info("Received /stats command")  # 添加日志
        chat_id = update.effective_chat.id
        stats = self.db.get_all_statistics()
        
        if not stats:
            await context.bot.send_message(chat_id=chat_id, text="📊 Статистика не найдена.")
            return
        
        msg = (
            f"📊 Статистика торговли:\n"
            f"📈 Общее число сделок: {stats['total_trades']}\n"
            f"✅ Прибыльных: {stats['profitable']}\n"
            f"❌ Убыточных: {stats['losing']}\n"
            f"💰 Общий PnL: ${stats['total_pnl']:.2f} ({stats['avg_pnl_pct']:+.2f}%)\n"
            f"🏆 Лучшая сделка: ${stats['best_trade']:.2f}\n"
            f"📉 Худшая сделка: ${stats['worst_trade']:.2f}"
        )
        await context.bot.send_message(chat_id=chat_id, text=msg)

    async def handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик /help"""
        logger.info("Received /help command")  # 添加日志
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="📋 Команды:\n"
                 "/start — приветствие\n"
                 "/stats — статистика торговли\n"
                 "/help — помощь"
        )

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик ошибок"""
        logger.error(f"Error in Telegram handler: {context.error}")
        if update:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Произошла ошибка при обработке вашего запроса. Пожалуйста, попробуйте позже."
            )

    async def send_telegram_message(self, message: str):
        """Отправка сообщения в Telegram"""
        try:
            await self._bot.send_message(
                chat_id=self.telegram_chat_id,
                text=message,
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
