"""
import asyncio
import httpx
from signal7 import fetch_candles, get_signal7

async def main():
    async with httpx.AsyncClient() as client:
        # Step 1: Fetch candles
        candles = await fetch_candles(client)

        # Step 2: Get signal
        result = get_signal7(candles)

        # Step 3: Extract what you need
        signal   = result["signal"]    # "BUY" | "SELL" | "HOLD"
        strength = result["strength"]  # "STRONG" | "MEDIUM" | "WEAK"
        reason   = result["reason"]    # human-readable string
        features = result["features"]  # dict of ML features

        print(f"Signal:   {signal}")
        print(f"Strength: {strength}")
        print(f"Reason:   {reason}")
        print(f"Features: {features}")

asyncio.run(main())
"""




import asyncio
import httpx
from datetime import datetime

# ============================================================
# SIGNAL 7 — Taker Buy/Sell Volume Ratio (Family B | 15M)
# ============================================================

# ── Thresholds ──────────────────────────────────────────────
BULL_THRESHOLD      = 0.55
BEAR_THRESHOLD      = 0.30
CHANGE_THRESHOLD    = 0.02
MOMENTUM_THRESHOLD  = 0.01
ZSCORE_THRESHOLD    = 1.0

# ── Config ───────────────────────────────────────────────────
BASE_URL     = "https://fapi.binance.com"
SYMBOL       = "BTCUSDT"
PERIOD       = "15m"
FETCH_LIMIT  = 100

# ── ML Feature Selection ─────────────────────────────────────
FEATURES_TO_USE = {
    "s7_ratio":         True,
    "s7_ratio_change":  True,
    "s7_momentum":      True,
    "s7_extreme_flag":  True,
    "s7_zscore":        True,
}


# ============================================================
# DATA FETCHING — call from main.py every 15 minutes
# ============================================================

async def fetch_candles(client: httpx.AsyncClient, limit: int = FETCH_LIMIT) -> list:
    """
    Fetch latest taker buy/sell ratio candles from Binance.
    Returns list of dicts sorted oldest → newest.

    Args:
        client : shared httpx.AsyncClient from main.py
        limit  : number of candles to fetch

    Call this every 15 minutes from main.py.
    """
    r = await client.get(
        f"{BASE_URL}/futures/data/takerlongshortRatio",
        params={"symbol": SYMBOL, "period": PERIOD, "limit": limit},
        timeout=10,
    )
    r.raise_for_status()

    candles = []
    for item in r.json():
        buy_vol  = float(item["buyVol"])
        sell_vol = float(item["sellVol"])
        total    = buy_vol + sell_vol
        candles.append({
            "timestamp":   int(item["timestamp"]),
            "taker_ratio": round(buy_vol / total, 6) if total > 0 else 0.5,
        })

    candles.sort(key=lambda x: x["timestamp"])
    return candles


# ============================================================
# INTERNALS
# ============================================================

def _get_features(candles: list) -> dict:
    empty = {
        "s7_ratio": 0.5, "s7_ratio_change": 0.0,
        "s7_momentum": 0.0, "s7_extreme_flag": 0, "s7_zscore": 0.0,
    }
    if len(candles) < 3:
        return empty

    ratios  = [c["taker_ratio"] for c in candles]
    current = ratios[-1]

    ratio_change = round(current - ratios[-2], 6)
    momentum     = round(sum(ratios[i] - ratios[i-1] for i in range(-3, 0)) / 3, 6)
    extreme_flag = +1 if current >= BULL_THRESHOLD else -1 if current <= BEAR_THRESHOLD else 0

    window   = ratios[-50:]
    mean     = sum(window) / len(window)
    std      = (sum((x - mean) ** 2 for x in window) / len(window)) ** 0.5
    zscore   = round((current - mean) / std, 4) if std > 0 else 0.0

    return {
        "s7_ratio":        round(current, 6),
        "s7_ratio_change": ratio_change,
        "s7_momentum":     momentum,
        "s7_extreme_flag": extreme_flag,
        "s7_zscore":       zscore,
    }


# ============================================================
# PUBLIC API — only this is called from main.py
# ============================================================

def get_signal7(candles: list) -> dict:
    """
    Returns Signal 7 result.

    Args:
        candles : list from fetch_candles()

    Returns:
        {
            "signal":   "BUY" | "SELL" | "HOLD",
            "strength": "STRONG" | "MEDIUM" | "WEAK",
            "reason":   str,
            "features": dict   ← ML feature vector (FEATURES_TO_USE filtered)
        }
    """
    f = _get_features(candles)

    ratio  = f["s7_ratio"]
    change = f["s7_ratio_change"]
    mom    = f["s7_momentum"]
    flag   = f["s7_extreme_flag"]
    zscore = f["s7_zscore"]

    strength = (
        "STRONG" if abs(zscore) >= ZSCORE_THRESHOLD * 1.5 else
        "MEDIUM" if abs(zscore) >= ZSCORE_THRESHOLD else
        "WEAK"
    )

    if (flag   == +1
            and ratio  >= BULL_THRESHOLD
            and change >= CHANGE_THRESHOLD
            and mom    >= MOMENTUM_THRESHOLD
            and zscore >= ZSCORE_THRESHOLD):
        signal = "BUY"
        reason = (f"Taker ratio {ratio:.3f} buyers dominating. "
                  f"Change={change:+.3f} Mom={mom:+.3f} Z={zscore:+.2f}")

    elif (flag   == -1
            and ratio  <= BEAR_THRESHOLD
            and change <= -CHANGE_THRESHOLD
            and mom    <= -MOMENTUM_THRESHOLD
            and zscore <= -ZSCORE_THRESHOLD):
        signal = "SELL"
        reason = (f"Taker ratio {ratio:.3f} sellers dominating. "
                  f"Change={change:+.3f} Mom={mom:+.3f} Z={zscore:+.2f}")

    else:
        signal, strength = "HOLD", "WEAK"
        reason = (f"Neutral. Ratio={ratio:.3f} Change={change:+.3f} "
                  f"Mom={mom:+.3f} Z={zscore:+.2f}")

    return {
        "signal":   signal,
        "strength": strength,
        "reason":   reason,
        "features": {k: v for k, v in f.items() if FEATURES_TO_USE.get(k)},
    }