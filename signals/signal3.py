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
BUCKET_SIZE  = 200
WINDOW_DAYS  = 7
SYMBOL       = "BTCUSDT"

# ── Paths ────────────────────────────────────────────────────
BASE_DIR     = r"C:\Users\HP\Desktop\HighPrep\final_files\history"
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