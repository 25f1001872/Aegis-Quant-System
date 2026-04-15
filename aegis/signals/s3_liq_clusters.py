r"""
import asyncio
import httpx
from signal3 import load_history, collect_liquidations, get_signal3

async def main():
    # Step 1: Load history ONCE at startup
    load_history()

    # Step 2: Start WebSocket collector as background task
    asyncio.create_task(collect_liquidations())

    # Step 3: Get your live BTC price (example)
    async with httpx.AsyncClient() as client:
        r = await client.get("https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT")
        current_price = float(r.json()["price"])

    # Step 4: Get signal (call this every 4 hours)
    result = get_signal3(current_price)

    signal   = result["signal"]    # "BUY" | "SELL" | "HOLD"
    strength = result["strength"]  # "STRONG" | "MEDIUM" | "WEAK"
    reason   = result["reason"]
    features = result["features"]

    print(f"Signal:   {signal}")
    print(f"Strength: {strength}")
    print(f"Reason:   {reason}")
    print(f"Features: {features}")

asyncio.run(main())
C:\Users\HP\Desktop\HighPrep\Aegis-Quant-System\Aegis-Quant-System\signals\signal3.py
"""

"""
{
    "signal":   "BUY" | "SELL" | "HOLD",
    "strength": "STRONG" | "MEDIUM" | "WEAK",
    "reason":   "No setup. Score=0 Dist=0.00% Size=$0 Imbal=0.00",  # example
    "features": {
        "s3_nearest_distance": 0.45,   # % distance to nearest cluster
        "s3_nearest_size":     1200000.0,  # $ size of nearest cluster
        "s3_nearest_side":     +1,     # +1 = above, -1 = below
        "s3_cluster_score":    2666.0, # size/distance score
        "s3_imbalance":        0.35,   # above/below imbalance (-1 to +1)
        # s3_above_total and s3_below_total are OFF in FEATURES_TO_USE
    }
}
"""


import asyncio
import websockets
import json
import os
import csv
from collections import defaultdict
from datetime import datetime, timedelta

# ============================================================
# SIGNAL 3 — Liquidation Clusters (Family A | 4H)
# ============================================================

# ── Thresholds ──────────────────────────────────────────────
DISTANCE_THRESHOLD_PCT  = 1.0
SIZE_THRESHOLD_MIN      = 500_000.0
SCORE_THRESHOLD_STRONG  = 5000.0
SCORE_THRESHOLD_MEDIUM  = 1000.0
ABOVE_TOTAL_MIN         = 1_000_000.0
BELOW_TOTAL_MIN         = 1_000_000.0
IMBALANCE_THRESHOLD     = 0.3

# ── Config ───────────────────────────────────────────────────
# ── DEV TOGGLE ────────────────────────────────────────────────────
TEST_MODE      = False
WARMUP_SECONDS = 600    # 10 minutes — enough to build initial
                        # liquidation cluster map from WebSocket
                        # stream before first signal read
BUCKET_SIZE  = 200
WINDOW_DAYS  = 7
SYMBOL       = "BTCUSDT"

# ── Paths ────────────────────────────────────────────────────
BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "history")
HISTORY_FILE = os.path.join(BASE_DIR, "signal3_liquidation_history.csv")

# ── ML Feature Selection ─────────────────────────────────────
FEATURES_TO_USE = {
    "s3_nearest_distance": True,
    "s3_nearest_size":     True,
    "s3_nearest_side":     True,
    "s3_cluster_score":    True,
    "s3_above_total":      False,
    "s3_below_total":      False,
    "s3_imbalance":        True,
}

# ── Shared State ─────────────────────────────────────────────
liquidation_history: dict = defaultdict(list)


# ============================================================
# PERSISTENCE — call load once at startup from main.py
# ============================================================

def load_history():
    """Load last 7 days of liquidation history from CSV into memory."""
    if not os.path.exists(HISTORY_FILE):
        print("[Signal 3] No history file found — starting fresh")
        return

    cutoff = datetime.now() - timedelta(days=WINDOW_DAYS)
    loaded = 0
    with open(HISTORY_FILE, "r", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)  # skip header
        for row in reader:
            try:
                ts       = datetime.fromisoformat(row[0])
                bucket   = int(row[1])
                notional = float(row[2])
                side     = row[3]
                if ts < cutoff:
                    continue
                liquidation_history[bucket].append((ts, notional, side))
                loaded += 1
            except Exception:
                continue

    print(f"[Signal 3] Loaded {loaded} events from history")


def _init_history_file():
    """Create history folder and CSV with header if not exists."""
    os.makedirs(BASE_DIR, exist_ok=True)
    if not os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "w", newline="") as f:
            csv.writer(f).writerow(["timestamp", "bucket", "notional", "side"])
        print(f"[Signal 3] Created {HISTORY_FILE}")


def _append_to_history(ts: datetime, bucket: int, notional: float, side: str):
    """Append a single liquidation event to the CSV."""
    with open(HISTORY_FILE, "a", newline="") as f:
        csv.writer(f).writerow([ts.isoformat(), bucket, round(notional, 2), side])


# ============================================================
# BACKGROUND TASK — call once from main.py
# ============================================================

async def collect_liquidations():
    """
    Long-running WebSocket task.
    Call from main.py:  asyncio.create_task(collect_liquidations())
    """
    _init_history_file()
    url = "wss://fstream.binance.com/ws/!forceOrder@arr"
    while True:
        try:
            async with websockets.connect(url, ping_interval=180, ping_timeout=10) as ws:
                print("[Signal 3] WebSocket connected")
                async for raw in ws:
                    data = json.loads(raw)["o"]
                    if data["s"] != SYMBOL:
                        continue
                    price    = float(data["p"])
                    qty      = float(data["q"])
                    side     = data["S"]
                    ts       = datetime.now()
                    notional = qty * price
                    bucket   = round(price / BUCKET_SIZE) * BUCKET_SIZE

                    liquidation_history[bucket].append((ts, notional, side))
                    _append_to_history(ts, bucket, notional, side)

        except Exception as e:
            print(f"[Signal 3] WebSocket error: {e} — reconnecting in 5s")
            await asyncio.sleep(5)


# ============================================================
# INTERNALS
# ============================================================

def _clean_old_data():
    cutoff = datetime.now() - timedelta(days=WINDOW_DAYS)
    for bucket in list(liquidation_history.keys()):
        liquidation_history[bucket] = [
            (ts, n, s) for ts, n, s in liquidation_history[bucket] if ts > cutoff
        ]
        if not liquidation_history[bucket]:
            del liquidation_history[bucket]


def _get_features(current_price: float) -> dict:
    _clean_old_data()

    empty = {
        "s3_nearest_distance": 0.0, "s3_nearest_size": 0.0,
        "s3_nearest_side":     0,   "s3_cluster_score": 0.0,
        "s3_above_total":      0.0, "s3_below_total":   0.0,
        "s3_imbalance":        0.0,
    }

    cluster_map = {
        b: round(sum(n for _, n, _ in events), 2)
        for b, events in liquidation_history.items() if events
    }
    if not cluster_map:
        return empty

    above = {b: s for b, s in cluster_map.items() if b > current_price}
    below = {b: s for b, s in cluster_map.items() if b < current_price}

    nearest_above = min(above, key=lambda b: b - current_price) if above else None
    nearest_below = max(below, key=lambda b: current_price - b) if below else None
    dist_above    = (nearest_above - current_price) if nearest_above else float("inf")
    dist_below    = (current_price - nearest_below) if nearest_below else float("inf")

    if dist_above <= dist_below and nearest_above:
        nb, nd, ns = nearest_above, dist_above, +1
    elif nearest_below:
        nb, nd, ns = nearest_below, dist_below, -1
    else:
        return empty

    nearest_size  = cluster_map[nb]
    dist_pct      = nd / current_price * 100
    cluster_score = nearest_size / nd if nd > 0 else 0.0

    top_above   = sorted(above.items(), key=lambda x: x[1], reverse=True)[:3]
    top_below   = sorted(below.items(), key=lambda x: x[1], reverse=True)[:3]
    above_total = sum(s for _, s in top_above)
    below_total = sum(s for _, s in top_below)
    total       = above_total + below_total

    return {
        "s3_nearest_distance": round(dist_pct, 4),
        "s3_nearest_size":     round(nearest_size, 2),
        "s3_nearest_side":     ns,
        "s3_cluster_score":    round(cluster_score, 4),
        "s3_above_total":      round(above_total, 2),
        "s3_below_total":      round(below_total, 2),
        "s3_imbalance":        round((above_total - below_total) / total, 4) if total else 0.0,
    }


# ============================================================
# PUBLIC API — only this is called from main.py
# ============================================================

def get_signal3(current_price: float) -> dict:
    """
    Returns Signal 3 result.

    Args:
        current_price : live BTC price

    Returns:
        {
            "signal":   "BUY" | "SELL" | "HOLD",
            "strength": "STRONG" | "MEDIUM" | "WEAK",
            "reason":   str,
            "features": dict   ← ML feature vector (FEATURES_TO_USE filtered)
        }
    """
    f = _get_features(current_price)

    score = f["s3_cluster_score"]
    side  = f["s3_nearest_side"]
    dist  = f["s3_nearest_distance"]
    size  = f["s3_nearest_size"]
    above = f["s3_above_total"]
    below = f["s3_below_total"]
    imbal = f["s3_imbalance"]

    strength = (
        "STRONG" if score >= SCORE_THRESHOLD_STRONG else
        "MEDIUM" if score >= SCORE_THRESHOLD_MEDIUM else
        "WEAK"
    )

    if (side == -1
            and dist  <= DISTANCE_THRESHOLD_PCT
            and size  >= SIZE_THRESHOLD_MIN
            and score >= SCORE_THRESHOLD_MEDIUM
            and below >= BELOW_TOTAL_MIN
            and imbal <= -IMBALANCE_THRESHOLD):
        signal = "SELL"
        reason = (f"Cluster BELOW (${size:,.0f}) {dist:.2f}% away. "
                  f"Below=${below:,.0f} Score={score:.0f} Imbal={imbal:.2f}")

    elif (side == +1
            and dist  <= DISTANCE_THRESHOLD_PCT
            and size  >= SIZE_THRESHOLD_MIN
            and score >= SCORE_THRESHOLD_MEDIUM
            and above >= ABOVE_TOTAL_MIN
            and imbal >= IMBALANCE_THRESHOLD):
        signal = "BUY"
        reason = (f"Cluster ABOVE (${size:,.0f}) {dist:.2f}% away. "
                  f"Above=${above:,.0f} Score={score:.0f} Imbal={imbal:.2f}")

    else:
        signal, strength = "HOLD", "WEAK"
        reason = (f"No setup. Score={score:.0f} Dist={dist:.2f}% "
                  f"Size=${size:,.0f} Imbal={imbal:.2f}")

    return {
        "signal":   signal,
        "strength": strength,
        "reason":   reason,
        "features": {k: v for k, v in f.items() if FEATURES_TO_USE.get(k)},
    }


# ==================== LIVE SIGNAL WRAPPER ====================

def get_signal() -> dict:
    import requests
    from datetime import datetime, timezone

    # ── Standardized v1.0 ─────────────────────────────────────────
    # Keys added   : signal_id, score, timestamp, reason, s3_score, s3_cluster_distance, s3_nearest_cluster_usd, s3_cluster_size_usd, s3_dominant_side
    # Keys renamed : None (no original get_signal existed)
    # Logic changed: NONE

    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/ticker/price",
            params={"symbol": "BTCUSDT"}, timeout=10
        )
        r.raise_for_status()
        current_price = float(r.json()["price"])

        # Call existing functions — logic untouched
        f = _get_features(current_price)
        result = get_signal3(current_price)

        # Map BUY/SELL/HOLD → +1/-1/0
        signal_map = {"BUY": +1, "SELL": -1, "HOLD": 0}
        score = signal_map.get(result["signal"], 0)

        # Derive USD distance with sign from % distance
        dist_pct = f["s3_nearest_distance"]
        side     = f["s3_nearest_side"]
        dist_usd = dist_pct * current_price / 100.0

        # Contract: NEGATIVE = cluster below price, POSITIVE = above
        if side == -1:
            s3_cluster_distance = -dist_usd
            s3_nearest_cluster_usd = current_price - dist_usd
        elif side == +1:
            s3_cluster_distance = dist_usd
            s3_nearest_cluster_usd = current_price + dist_usd
        else:
            s3_cluster_distance = 0.0
            s3_nearest_cluster_usd = 0.0

        # Cluster below price → long liquidations, above → short liquidations
        if side == -1:
            s3_dominant_side = "long"
        elif side == +1:
            s3_dominant_side = "short"
        else:
            s3_dominant_side = ""

    except Exception:
        score = 0
        result = {"reason": "Signal error — no liquidation data available"}
        s3_cluster_distance = 0.0
        s3_nearest_cluster_usd = 0.0
        f = {"s3_nearest_size": 0.0}
        s3_dominant_side = ""

    return {
        "signal_id"              : 3,
        "score"                  : score,
        "timestamp"              : datetime.now(timezone.utc),
        "reason"                 : result["reason"],
        "s3_score"               : score,
        "s3_cluster_distance"    : round(s3_cluster_distance, 2),
        "s3_nearest_cluster_usd" : round(s3_nearest_cluster_usd, 2),
        "s3_cluster_size_usd"    : round(f["s3_nearest_size"], 2),
        "s3_dominant_side"       : s3_dominant_side,
    }


# ==================== MODULE SINGLETON ====================
import threading

_s3_thread: threading.Thread | None = None
_s3_loop: asyncio.AbstractEventLoop | None = None

def _run_s3_loop():
    global _s3_loop
    _s3_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_s3_loop)
    load_history()
    _s3_loop.run_until_complete(collect_liquidations())

def start_s3_stream():
    """Start S3 liquidation history loader and websocket in a background thread."""
    global _s3_thread
    if _s3_thread is None:
        _s3_thread = threading.Thread(target=_run_s3_loop, daemon=True)
        _s3_thread.start()

def stop_s3_stream():
    """Stop the S3 websocket thread gracefully if possible."""
    global _s3_loop
    if _s3_loop and _s3_loop.is_running():
        _s3_loop.call_soon_threadsafe(_s3_loop.stop)