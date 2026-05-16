import asyncio
import logging
import os
from aiohttp import web

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def health_check(request):
    """Health check endpoint для Hugging Face Spaces"""
    return web.Response(text="OK")


async def start_aiohttp_server():
    """Запуск aiohttp сервера на порту 7860"""
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 7860)
    await site.start()
    logger.info("✅ aiohttp сервер запущен на порту 7860")
    return runner


def load_env_from_docker_secrets():
    """
    Hugging Face Spaces передаёт secrets через /run/secrets/ файлы.
    Читаем их и устанавливаем как os.environ.
    """
    secrets_dir = "/run/secrets"
    if os.path.isdir(secrets_dir):
        for secret_name in os.listdir(secrets_dir):
            secret_path = os.path.join(secrets_dir, secret_name)
            if os.path.isfile(secret_path):
                with open(secret_path, "r") as f:
                    value = f.read().strip()
                os.environ[secret_name] = value
                logger.info(f"✅ Secret '{secret_name}' загружен из /run/secrets/")


async def run_bot():
    """Запуск торгового бота с обработкой ошибок"""
    from smart_money_aggressive import main as bot_main
    try:
        await bot_main()
    except KeyboardInterrupt:
        logger.info("Остановка бота по Ctrl+C")
    except Exception as e:
        logger.error(f"Ошибка бота: {e}. Перезапуск через 60 сек...")
        await asyncio.sleep(60)
        # Рекурсивный перезапуск
        await run_bot()


async def main():
    """Точка входа — запуск aiohttp + торгового бота"""
    # Загружаем secrets из Docker/Hugging Face
    load_env_from_docker_secrets()

    # Запускаем aiohttp сервер
    runner = await start_aiohttp_server()

    # Запускаем торгового бота в фоне
    bot_task = asyncio.create_task(run_bot())

    # Держим сервер живым
    try:
        await bot_task
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
