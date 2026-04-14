"""
AEGIS-QUANT-SYSTEM
==================
Signal 4 — Long/Short Ratio (LSR)
Family    : A — Derivatives
Role      : DIRECTIONAL GATE
Timeframe : 4H  — polled once per 4H candle close (or on session open)
Source    : Binance Futures REST
            GET /futures/data/globalLongShortAccountRatio

─────────────────────────────────────────────────────────────────────────
ROLE IN THE TWO-STAGE SYSTEM
─────────────────────────────────────────────────────────────────────────
  LSR is STAGE 1.  It answers: "Which direction are we allowed to trade?"

  The aggregator reads result["direction"] before it ever looks at OFI.
  If LSR says NO_TRADE → the session is blocked entirely.
  If LSR says LONG     → only long OFI confirmations are valid.
  If LSR says SHORT    → only short OFI confirmations are valid.

  OFI (Signal 5) is STAGE 2.  It answers: "Is the move happening NOW?"
  OFI is only evaluated after LSR has given a directional bias.

─────────────────────────────────────────────────────────────────────────
DECISION TABLE
─────────────────────────────────────────────────────────────────────────
  long_pct < 35%   →  LONG  (+1)
      Extreme fear, crowded short, short-squeeze fuel is loaded.
      Bias: look for LONG entries on the 15M chart via OFI.

  long_pct > 70%   →  SHORT (-1)
      Retail overwhelmingly long, trapped, fading the crowd.
      Bias: look for SHORT entries on the 15M chart via OFI.

  35% ≤ long_pct ≤ 70%  →  NO_TRADE (0)
      Balanced positioning — no contrarian edge.
      Block all trades. Do not evaluate OFI.

─────────────────────────────────────────────────────────────────────────
MERGE INTERFACE
─────────────────────────────────────────────────────────────────────────
  from signal4_long_short_ratio import get_signal4_score

  lsr = get_signal4_score()
  lsr["direction"]   # "LONG" | "SHORT" | "NO_TRADE"
  lsr["score"]       # +1 | 0 | -1
  lsr["long_pct"]    # e.g. 72.3
  lsr["timestamp"]   # ISO-8601 UTC
"""

import requests
from datetime import datetime, timezone

# ──────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────
BINANCE_BASE    = "https://fapi.binance.com"
SYMBOL          = "BTCUSDT"
PERIOD          = "4h"       # Binance accepts: 5m 15m 30m 1h 2h 4h 6h 12h 1d

LONG_THRESHOLD  = 35.0       # long_pct BELOW this  → LONG bias
SHORT_THRESHOLD = 70.0       # long_pct ABOVE this  → SHORT bias
# Between 35%–70% → NO_TRADE — no contrarian edge, don't force a direction


# ──────────────────────────────────────────────────────
# DATA FETCH  (no API key needed — public endpoint)
# ──────────────────────────────────────────────────────

def fetch_lsr(symbol: str = SYMBOL, period: str = PERIOD) -> dict:
    """
    Pull the single latest globalLongShortAccountRatio record from Binance.

    Binance response fields:
        symbol         e.g. "BTCUSDT"
        longShortRatio ratio of long accounts to short accounts (string)
        longAccount    fraction of accounts that are net long  (string, 0–1)
        shortAccount   fraction of accounts that are net short (string, 0–1)
        timestamp      epoch milliseconds
    """
    url = f"{BINANCE_BASE}/futures/data/globalLongShortAccountRatio"
    try:
        resp = requests.get(
            url,
            params={"symbol": symbol, "period": period, "limit": 1},
            timeout=10,
        )
        resp.raise_for_status()
        records = resp.json()
        if not records:
            raise ValueError("[Signal4-LSR] Binance returned an empty list.")
        return records[-1]    # newest record
    except requests.RequestException as exc:
        raise RuntimeError(f"[Signal4-LSR] API request failed: {exc}") from exc


# ──────────────────────────────────────────────────────
# SIGNAL COMPUTATION
# ──────────────────────────────────────────────────────

def compute_lsr(record: dict) -> dict:
    """
    Convert the raw Binance record into the AEGIS signal dict.

    The aggregator reads:
        result["direction"]  →  "LONG" | "SHORT" | "NO_TRADE"
        result["score"]      →  +1 / 0 / -1
    """
    long_fraction = float(record["longAccount"])        # 0.0–1.0
    long_pct      = round(long_fraction * 100, 2)       # 0.0–100.0 %
    short_pct     = round(100 - long_pct, 2)
    raw_ratio     = float(record["longShortRatio"])
    ts_ms         = int(record["timestamp"])
    ts_utc        = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()

    if long_pct < LONG_THRESHOLD:
        direction = "LONG"
        score     = +1
        condition = "LONG_BIAS"
        reason    = (
            f"{long_pct}% of accounts are long — extreme fear, crowded short. "
            f"Short-squeeze fuel is fully loaded. "
            f"4H BIAS → LONG only. Now drop to 15M and wait for OFI to confirm "
            f"aggressive buying before entering."
        )
    elif long_pct > SHORT_THRESHOLD:
        direction = "SHORT"
        score     = -1
        condition = "SHORT_BIAS"
        reason    = (
            f"{long_pct}% of accounts are long — retail dangerously overcrowded. "
            f"Fading the trapped crowd. "
            f"4H BIAS → SHORT only. Now drop to 15M and wait for OFI to confirm "
            f"aggressive selling before entering."
        )
    else:
        direction = "NO_TRADE"
        score     = 0
        condition = "NEUTRAL"
        reason    = (
            f"{long_pct}% long — balanced positioning ({LONG_THRESHOLD}%–{SHORT_THRESHOLD}% zone). "
            f"No contrarian edge available. SESSION BLOCKED — do not evaluate OFI. "
            f"Wait for the next 4H candle and re-check."
        )

    return {
        # ── Primary fields (read by aggregator) ─────────────────────
        "signal_id"  : 4,
        "signal_name": "Long/Short Ratio (LSR)",
        "family"     : "A",
        "timeframe"  : "4H",
        "role"       : "DIRECTIONAL_GATE",   # aggregator uses this to enforce stage logic
        "direction"  : direction,             # "LONG" | "SHORT" | "NO_TRADE"
        "score"      : score,                 # +1 | 0 | -1
        "condition"  : condition,
        # ── Detail ──────────────────────────────────────────────────
        "long_pct"   : long_pct,
        "short_pct"  : short_pct,
        "raw_ratio"  : raw_ratio,
        "reason"     : reason,
        "timestamp"  : ts_utc,
        "ts_ms"      : ts_ms,
    }


# ──────────────────────────────────────────────────────
# PUBLIC MERGE INTERFACE
# ──────────────────────────────────────────────────────

def get_signal4_score(symbol: str = SYMBOL, period: str = PERIOD) -> dict:
    """
    Called by the AEGIS aggregator once per 4H candle close.

    The aggregator uses result["direction"] to gate Signal 5 (OFI):
      - "LONG"     → only a positive OFI reading triggers a trade
      - "SHORT"    → only a negative OFI reading triggers a trade
      - "NO_TRADE" → OFI is not evaluated; session is blocked
    """
    record = fetch_lsr(symbol=symbol, period=period)
    return compute_lsr(record)


# ==================== LIVE SIGNAL WRAPPER ====================

def get_signal(symbol: str = SYMBOL, period: str = PERIOD) -> dict:
    """
    Contract-compliant wrapper around get_signal4_score().
    """
    result = get_signal4_score(symbol=symbol, period=period)

    score    = result["score"]
    long_pct = result["long_pct"]

    # ── Standardized v1.0 ─────────────────────────────────────────
    # Keys added   : s4_score, s4_ls_ratio, s4_long_pct, s4_short_pct, s4_ls_extreme
    # Keys renamed : long_pct -> s4_long_pct, short_pct -> s4_short_pct
    # Logic changed: NONE
    return {
        "signal_id"        : 4,
        "score"            : score,
        "timestamp"        : result["timestamp"],
        "reason"           : result["reason"],
        "s4_score"         : score,
        "s4_ls_ratio"      : round(long_pct / 100.0, 6),
        "s4_long_pct"      : long_pct,
        "s4_short_pct"     : result["short_pct"],
        "s4_ls_extreme"    : 1 if (long_pct < LONG_THRESHOLD or long_pct > SHORT_THRESHOLD) else 0,
    }


# ──────────────────────────────────────────────────────
# STANDALONE TEST / DEBUG
# ──────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 65)
    print("AEGIS — Signal 4 : Long/Short Ratio  [4H Directional Gate]")
    print("=" * 65)

    result = get_signal()

    print(f"  Timestamp  : {result['timestamp']}")
    print(f"  Long  %    : {result['s4_long_pct']}%")
    print(f"  Short %    : {result['s4_short_pct']}%")
    print(f"  LS Ratio   : {result['s4_ls_ratio']:.4f}")
    print(f"  Extreme    : {result['s4_ls_extreme']}")
    print(f"  Score      : {result['score']:+d}")
    print()
    print(f"  Reasoning:")
    print(f"  → {result['reason']}")
    print("=" * 65)

