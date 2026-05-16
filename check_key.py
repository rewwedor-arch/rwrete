import os
from dotenv import load_dotenv

load_dotenv()
key = os.getenv('BINANCE_API_KEY')
secret = os.getenv('BINANCE_SECRET')

print(f"API Key length: {len(key)}")
print(f"Secret length: {len(secret)}")
print(f"API Key repr: {repr(key)}")
print(f"Secret repr: {repr(secret)}")

# Check for whitespace
if key != key.strip():
    print("⚠️ API Key has leading/trailing whitespace!")
if secret != secret.strip():
    print("⚠️ Secret has leading/trailing whitespace!")