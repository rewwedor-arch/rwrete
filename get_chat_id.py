"""
Скрипт для получения вашего TELEGRAM_CHAT_ID.
1. Напишите ЛЮБОЕ сообщение своему боту в Telegram
2. Запустите этот скрипт: py get_chat_id.py
3. Скопируйте Chat ID и вставьте в .env
"""
import os
import sys

if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import requests
from dotenv import load_dotenv

load_dotenv()

token = os.getenv('TELEGRAM_BOT_TOKEN', '')
if not token:
    print("❌ TELEGRAM_BOT_TOKEN не найден в .env")
    sys.exit(1)

print(f"🔍 Проверяю обновления бота...")
print(f"⚠️  Убедитесь, что вы уже написали ЛЮБОЕ сообщение боту в Telegram!\n")

url = f"https://api.telegram.org/bot{token}/getUpdates"
resp = requests.get(url).json()

if not resp.get('ok'):
    print(f"❌ Ошибка API: {resp}")
    sys.exit(1)

updates = resp.get('result', [])

if not updates:
    print("❌ Нет сообщений! Напишите что-нибудь своему боту в Telegram и запустите скрипт снова.")
    sys.exit(1)

chat_ids_found = set()
for update in updates:
    msg = update.get('message', {})
    chat = msg.get('chat', {})
    if chat.get('id'):
        chat_ids_found.add(chat['id'])
        print(f"✅ Найден Chat ID: {chat['id']}")
        print(f"   Имя: {chat.get('first_name', '')} {chat.get('last_name', '')}")
        print(f"   Username: @{chat.get('username', 'N/A')}")
        print()

if chat_ids_found:
    chat_id = list(chat_ids_found)[0]
    print(f"{'='*50}")
    print(f"📋 Ваш TELEGRAM_CHAT_ID = {chat_id}")
    print(f"{'='*50}")
    print(f"\nОбновите .env файл: TELEGRAM_CHAT_ID={chat_id}")
    print(f"И замените в run_all.py: \"TELEGRAM_CHAT_ID\": \"{chat_id}\"")
