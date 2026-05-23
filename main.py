import asyncio
import logging
import os
from aiohttp import web

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def handle_ping(request):
    return web.Response(text="OK")


async def start_aiohttp_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/ping", handle_ping)
    app.router.add_get("/health", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 7860))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"aiohttp server started on port {port}")
    return runner


def load_env_from_docker_secrets():
    secrets_dir = "/run/secrets"
    if os.path.isdir(secrets_dir):
        for secret_name in os.listdir(secrets_dir):
            secret_path = os.path.join(secrets_dir, secret_name)
            if os.path.isfile(secret_path):
                with open(secret_path, "r") as f:
                    value = f.read().strip()
                os.environ[secret_name] = value
                logger.info(f"Secret '{secret_name}' loaded from /run/secrets/")


async def run_bot_with_restart():
    """Запуск торгового бота с автоматическим перезапуском"""
    from smart_money_aggressive import SmartMoneyBot
    from dotenv import load_dotenv
    from pathlib import Path
    
    load_dotenv(Path(__file__).resolve().parent / '.env')
    
    while True:
        try:
            if os.path.exists('.bot_stopped'):
                logger.info("Bot is paused by user. Waiting...")
                await asyncio.sleep(10)
                continue

            bot = SmartMoneyBot(
                api_key=os.getenv('BINANCE_API_KEY', ''),
                api_secret=os.getenv('BINANCE_SECRET', '') or os.getenv('BINANCE_API_SECRET', ''),
                telegram_token=os.getenv('TELEGRAM_BOT_TOKEN', ''),
                telegram_chat_id=os.getenv('TELEGRAM_CHAT_ID', ''),
                user_chat_id=os.getenv('USER_CHAT_ID', ''),
                testnet=os.getenv('BINANCE_TESTNET', 'False').lower() == 'true'
            )
            started = await bot.start()
            if not started:
                if os.path.exists('.bot_stopped'):
                    logger.info("Bot stopped by user command.")
                    continue
                logger.error("Bot failed to start. Retrying in 60 sec...")
                await asyncio.sleep(60)
                continue
        except KeyboardInterrupt:
            logger.info("Bot stopped by Ctrl+C")
            break
        except Exception as e:
            logger.error(f"Bot crashed: {e}. Restarting in 60 sec...")
            await asyncio.sleep(60)


async def main():
    load_env_from_docker_secrets()

    runner = await start_aiohttp_server()

    bot_task = asyncio.create_task(run_bot_with_restart())

    try:
        await bot_task
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
