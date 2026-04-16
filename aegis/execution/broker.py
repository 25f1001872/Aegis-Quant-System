"""
Broker wrapper for Binance Futures REST API
"""
import os
import time
import hmac
import hashlib
import logging
import requests
from dotenv import load_dotenv

load_dotenv()

PAPER_MODE   = False    # True = paper trade, False = live
LEVERAGE     = 1        # always 1x during paper phase
BASE_URL_LIVE = "https://testnet.binancefuture.com"
BASE_URL_TEST = "https://testnet.binancefuture.com"
SYMBOL       = "BTCUSDT"

BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

log_dir = os.path.join("logs")
os.makedirs(log_dir, exist_ok=True)
broker_logger = logging.getLogger("broker")
broker_logger.setLevel(logging.INFO)
fh = logging.FileHandler(os.path.join(log_dir, "broker.log"))
formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
fh.setFormatter(formatter)
if not broker_logger.handlers:
    broker_logger.addHandler(fh)

class BrokerError(Exception):
    pass

class BinanceBroker:
    def __init__(self):
        self.base_url = BASE_URL_LIVE if not PAPER_MODE else BASE_URL_TEST
        self.api_key = BINANCE_API_KEY
        self.api_secret = BINANCE_API_SECRET

    def _sign_request(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    def _request(self, method: str, endpoint: str, auth=True, **kwargs) -> dict:
        url = self.base_url + endpoint
        headers = {}
        params = kwargs.get("params", {})
        
        if auth:
            headers["X-MBX-APIKEY"] = self.api_key
            params = self._sign_request(params)
            
        kwargs["params"] = params
        kwargs["headers"] = headers
        
        try:
            if method == "GET":
                response = requests.get(url, **kwargs)
            elif method == "POST":
                response = requests.post(url, **kwargs)
            elif method == "DELETE":
                response = requests.delete(url, **kwargs)
            else:
                raise ValueError(f"Unsupported method {method}")
            
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            err_msg = str(e)
            if hasattr(e, 'response') and e.response is not None:
                err_msg += f" | {e.response.text}"
            broker_logger.error(f"API Error: {err_msg}")
            raise BrokerError(err_msg)

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        if PAPER_MODE:
            broker_logger.info(f"PAPER: set_leverage {leverage}x for {symbol}")
            return {"symbol": symbol, "leverage": leverage}
        
        broker_logger.info(f"Setting leverage {leverage}x for {symbol}")
        return self._request("POST", "/fapi/v1/leverage", params={"symbol": symbol, "leverage": leverage})

    def place_market_order(self, side: str, quantity: float) -> dict:
        quantity = round(quantity, 3)
        if PAPER_MODE:
            try:
                price = self.get_current_price()
            except Exception:
                price = 0.0
            sim_order = {
                "orderId": int(time.time() * 1000),
                "symbol": SYMBOL,
                "status": "FILLED",
                "clientOrderId": "paper_" + str(int(time.time() * 1000)),
                "price": "0",
                "avgPrice": str(price),
                "origQty": str(quantity),
                "executedQty": str(quantity),
                "side": side,
                "type": "MARKET"
            }
            broker_logger.info(f"PAPER: place_market_order {side} {quantity} at ~{price}")
            return sim_order
        
        params = {
            "symbol": SYMBOL,
            "side": side,
            "type": "MARKET",
            "quantity": quantity
        }
        broker_logger.info(f"Placing MARKET {side} for {quantity}")
        return self._request("POST", "/fapi/v1/order", params=params)

    def place_limit_order(self, side: str, quantity: float, price: float) -> dict:
        quantity = round(quantity, 3)
        price = round(price, 2)
        if PAPER_MODE:
            sim_order = {
                "orderId": int(time.time() * 1000) + 1,
                "symbol": SYMBOL,
                "status": "NEW",
                "clientOrderId": "paper_limit_" + str(int(time.time() * 1000)),
                "price": str(price),
                "origQty": str(quantity),
                "side": side,
                "type": "LIMIT",
                "timeInForce": "GTC"
            }
            broker_logger.info(f"PAPER: place_limit_order {side} {quantity} at {price}")
            return sim_order
            
        params = {
            "symbol": SYMBOL,
            "side": side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": quantity,
            "price": price
        }
        broker_logger.info(f"Placing LIMIT {side} for {quantity} at {price}")
        return self._request("POST", "/fapi/v1/order", params=params)

    def place_stop_order(self, side: str, quantity: float, stop_price: float) -> dict:
        quantity = round(quantity, 3)
        stop_price = round(stop_price, 2)
        if PAPER_MODE:
            sim_order = {
                "orderId": int(time.time() * 1000) + 2,
                "symbol": SYMBOL,
                "status": "NEW",
                "clientOrderId": "paper_stop_" + str(int(time.time() * 1000)),
                "stopPrice": str(stop_price),
                "origQty": str(quantity),
                "side": side,
                "type": "STOP_MARKET"
            }
            broker_logger.info(f"PAPER: place_stop_order {side} {quantity} at SL {stop_price}")
            return sim_order

        params = {
            "symbol": SYMBOL,
            "side": side,
            "type": "STOP_MARKET",
            "quantity": quantity,
            "stopPrice": stop_price
        }
        broker_logger.info(f"Placing STOP_MARKET {side} for {quantity} at {stop_price}")
        return self._request("POST", "/fapi/v1/order", params=params)

    def cancel_order(self, order_id: int) -> dict:
        if PAPER_MODE:
            broker_logger.info(f"PAPER: cancel_order {order_id}")
            return {"status": "CANCELED"}
            
        params = {"symbol": SYMBOL, "orderId": order_id}
        broker_logger.info(f"Canceling order {order_id}")
        return self._request("DELETE", "/fapi/v1/order", params=params)

    def get_position(self) -> dict | None:
        if PAPER_MODE:
            return None
            
        params = {"symbol": SYMBOL}
        positions = self._request("GET", "/fapi/v2/positionRisk", params=params)
        for pos in positions:
            if pos["symbol"] == SYMBOL:
                amt = float(pos["positionAmt"])
                if amt != 0:
                    return pos
        return None

    def get_account_balance(self) -> float:
        if PAPER_MODE:
            return 0.0

        res = self._request("GET", "/fapi/v2/account")
        for asset in res.get("assets", []):
            if asset["asset"] == "USDT":
                return float(asset["availableBalance"])
        return 0.0

    def get_current_price(self) -> float:
        res = self._request("GET", "/fapi/v1/ticker/price", auth=False, params={"symbol": SYMBOL})
        return float(res["price"])

    def get_last_4h_swing(self, side: str) -> float:
        res = self._request("GET", "/fapi/v1/klines", auth=False, params={
            "symbol": SYMBOL,
            "interval": "4h",
            "limit": 10
        })
        if len(res) < 3:
            raise BrokerError("Not enough 4h candles")
            
        c1 = res[-3]
        c2 = res[-2]
        
        low1, low2 = float(c1[3]), float(c2[3])
        high1, high2 = float(c1[2]), float(c2[2])
        
        if side == "LONG":
            return min(low1, low2)
        elif side == "SHORT":
            return max(high1, high2)
        else:
            raise ValueError(f"Invalid side {side}")
