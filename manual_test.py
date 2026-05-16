import hashlib
import hmac
import time
import requests
from urllib.parse import urlencode

API_KEY = 'jxAO4KPKf05CdM2QUVsvcOEbOaG4a7H5yE4Zy2besz32L3lfJWFwgzN0F40Fn5RS'
API_SECRET = 'vH7B0xFjtPk2aOOvH3ke637JHj5vVyvTBGheSH0TunxGSX8amZ6MlQDWSnFKhUmL'

def binance_signature(query_string):
    return hmac.new(
        API_SECRET.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

# Test 1: Ping (no auth needed)
print("Testing connectivity...")
try:
    r = requests.get('https://fapi.binance.com/fapi/v1/ping', timeout=5)
    print(f"Ping: {r.status_code} {r.text}")
except Exception as e:
    print(f"Ping failed: {e}")

# Test 2: Server time
print("\nTesting server time...")
try:
    r = requests.get('https://fapi.binance.com/fapi/v1/time', timeout=5)
    print(f"Server time: {r.status_code} {r.json()}")
except Exception as e:
    print(f"Server time failed: {e}")

# Test 3: Signed request (account info)
print("\nTesting signed request (account info)...")
try:
    timestamp = int(time.time() * 1000)
    params = {
        'timestamp': timestamp,
        'recvWindow': 60000
    }
    query_string = urlencode(params)
    signature = binance_signature(query_string)
    params['signature'] = signature
    
    headers = {
        'X-MBX-APIKEY': API_KEY
    }
    
    r = requests.get(
        'https://fapi.binance.com/fapi/v2/account',
        params=params,
        headers=headers,
        timeout=10
    )
    print(f"Account info: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"Can trade: {data.get('canTrade')}")
        print(f"Can withdraw: {data.get('canWithdraw')}")
        print(f"Can deposit: {data.get('canDeposit')}")
        # Find USDT balance
        for asset in data.get('assets', []):
            if asset['asset'] == 'USDT':
                print(f"USDT balance: {asset['walletBalance']}")
                break
    else:
        print(f"Error: {r.text}")
except Exception as e:
    print(f"Signed request failed: {e}")
    import traceback
    traceback.print_exc()