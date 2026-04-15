import os
import requests
import time
import hmac
import hashlib
from dotenv import load_dotenv

load_dotenv()

API_KEY    = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
BASE_URL   = "https://testnet.binancefuture.com"

# Test 1 — public endpoint
price = requests.get(
    f"{BASE_URL}/fapi/v1/ticker/price",
    params={"symbol": "BTCUSDT"}
).json()
print(f"BTC Price on testnet : {price['price']}")

# Test 2 — authenticated endpoint
ts        = int(time.time() * 1000)
params    = f"timestamp={ts}"
signature = hmac.new(
    API_SECRET.encode(),
    params.encode(),
    hashlib.sha256
).hexdigest()

headers = {"X-MBX-APIKEY": API_KEY}
url     = f"{BASE_URL}/fapi/v2/account?{params}&signature={signature}"
res     = requests.get(url, headers=headers)
data    = res.json()

if "totalWalletBalance" in data:
    print(f"Testnet balance      : {data['totalWalletBalance']} USDT")
    print("Connection           : OK")
else:
    print(f"Error                : {data}")