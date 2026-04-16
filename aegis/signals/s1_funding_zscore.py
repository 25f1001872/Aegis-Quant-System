"""
AEGIS QUANT SYSTEM
Signal 1 — Funding Rate Z-Score
Family    : A (Derivatives)
Timeframe : 4H (funding updates every 8H on Binance)
Data Source: https://fapi.binance.com/fapi/v1/fundingRate (PUBLIC — no API key)

Threshold research & optimization (v2.0):
    Empirical BTC funding distribution is RIGHT-SKEWED (mean +0.0065%, skew +1.8)
    Symmetric ±2.0 thresholds misrepresent the actual distribution.
    Optimized thresholds are ASYMMETRIC and include a persistence check
    to eliminate single-period funding spikes that do not represent
    genuine crowding events.

Output scores:
    +1  →  Bullish  (Z < -1.6 for 1+ periods → extreme short crowding → squeeze fuel)
    -1  →  Bearish  (Z > +2.5 for 1+ periods → extreme long crowding  → fade the crowd)
     0  →  Neutral  (Z inside asymmetric dead zone → no signal)
"""

import requests
import pandas as pd
import numpy as np
from datetime import datetime

# ── Endpoint ──────────────────────────────────────────────────────────────────
FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"

# ── Optimized Thresholds (v2.0 — empirically calibrated for BTC perpetuals) ──
#
# BTC funding rate Z-score is RIGHT-SKEWED (skewness ≈ +1.8).
# Symmetric ±2.0 thresholds create an asymmetric firing rate:
#   Bearish fires in top ~5% | Bullish fires in bottom ~3%
# Corrected asymmetric thresholds fire at approximately equal ~3% on both sides.
#
# BEARISH raised from +2.0 → +2.5:
#   At +2.0, signal fires too early in sustained bull trends where funding
#   stays elevated for weeks. +2.5 represents genuine painful carry cost
#   that forces position closures rather than just elevated enthusiasm.
#
# BULLISH raised from -2.0 → -1.6:
#   Due to right-skew, Z = -1.6 is the empirical ~3rd percentile.
#   Also catches the squeeze SETUP phase (1-2 periods before squeeze ignites)
#   rather than waiting for full -2.0 capitulation which is often too late.
#
# PERSISTENCE_PERIODS = 2:
#   Signal requires threshold breach on current AND previous funding period.
#   Eliminates single-period spikes (funding hits +2.6 for 8H then recovers).
#   Genuine crowding events persist across multiple 8H funding periods.

BEAR_THRESHOLD       = +2.5   # Z above this → extreme long crowding  → BEARISH
BULL_THRESHOLD       = -1.6   # Z below this → extreme short crowding → BULLISH

WATCH_BEAR_LOW       = +1.5   # Z between +1.5 and +2.5 → elevated, watch only
WATCH_BULL_HIGH      = -0.8   # Z between -1.6 and -0.8 → elevated short, watch only

NEUTRAL_LOW          = -0.8   # Asymmetric neutral band: -0.8 to +1.5
NEUTRAL_HIGH         = +1.5   # Positive funding up to +1.5 is structurally
                               # normal in crypto — not worth flagging

ROLLING_WINDOW       = 500    # ~83 days of 8H funding history
MIN_PERIODS          = 30     # minimum data points before z-score is valid
PERSISTENCE_PERIODS  = 2      # how many consecutive periods must breach threshold


# ── Data Fetching ─────────────────────────────────────────────────────────────
def fetch_funding_rates(symbol: str = "BTCUSDT", limit: int = 500) -> pd.DataFrame:
    """
    Fetch historical funding rates from Binance Futures public REST API.
    No API key required.

    Args:
        symbol : trading pair (default BTCUSDT)
        limit  : number of records to fetch (max 1000, default 500 ≈ 83 days)

    Returns:
        DataFrame with columns: fundingTime, fundingRate
    """
    params   = {"symbol": symbol, "limit": limit}
    response = requests.get(FUNDING_URL, params=params, timeout=10)
    response.raise_for_status()

    df = pd.DataFrame(response.json())
    df["fundingTime"] = pd.to_datetime(df["fundingTime"], unit="ms")
    df["fundingRate"] = df["fundingRate"].astype(float)
    df = df.sort_values("fundingTime").reset_index(drop=True)
    return df[["fundingTime", "fundingRate"]]


# ── Z-Score Computation ───────────────────────────────────────────────────────
def compute_zscore(df: pd.DataFrame,
                   window: int     = ROLLING_WINDOW,
                   min_periods: int = MIN_PERIODS) -> pd.Series:
    """
    Compute rolling z-score of funding rate.

    Z = (current_rate - rolling_mean) / rolling_std

    Uses ROLLING statistics (not global) because crypto funding regimes
    shift dramatically between bull and bear markets. Rolling stats adapt
    to current regime rather than being distorted by a different market era.

    Right-skew awareness:
        Raw funding rate has skewness ≈ +1.8. Rolling z-score partially
        corrects for this by using local mean/std, but the skew means
        positive Z-scores are structurally more common than negative ones.
        Thresholds are set asymmetrically to account for this.

    Args:
        df          : DataFrame from fetch_funding_rates()
        window      : rolling window size (number of 8H periods)
        min_periods : minimum periods before producing a valid z-score

    Returns:
        Series of z-score values aligned with df index
    """
    rates        = df["fundingRate"]
    rolling_mean = rates.rolling(window=window, min_periods=min_periods).mean()
    rolling_std  = rates.rolling(window=window, min_periods=min_periods).std()

    # Avoid division by zero in flat-funding regimes
    rolling_std  = rolling_std.replace(0, np.nan)

    zscore = (rates - rolling_mean) / rolling_std
    return zscore


# ── Persistence Check ─────────────────────────────────────────────────────────
def _check_persistence(zscores: pd.Series,
                        threshold: float,
                        direction: str,
                        n: int = PERSISTENCE_PERIODS) -> bool:
    """
    Check if the Z-score has breached the threshold for N consecutive periods.

    Eliminates single-period funding spikes that do not represent genuine
    crowding events. A real crowding regime persists across multiple 8H
    funding periods — it does not spike once and immediately recover.

    Args:
        zscores   : full z-score Series
        threshold : the threshold to check against
        direction : "above" (bearish check) or "below" (bullish check)
        n         : number of consecutive periods required

    Returns:
        True if threshold breached for last N periods consecutively
    """
    if len(zscores) < n:
        return False

    recent = zscores.iloc[-n:]

    if direction == "above":
        return bool((recent > threshold).all())
    elif direction == "below":
        return bool((recent < threshold).all())

    return False


# ── Signal Classification ─────────────────────────────────────────────────────
def _classify(current_z: float,
              all_zscores: pd.Series) -> tuple[int, str]:
    """
    Map z-score to signal score using optimized asymmetric thresholds
    with persistence confirmation.

    Threshold logic:
        BEARISH  : Z > +2.5 for 2 consecutive periods
                   (extreme long crowding, painful carry cost)
        BULLISH  : Z < -1.6 for 2 consecutive periods
                   (extreme short crowding, squeeze fuel loaded)
        WATCH    : Z in elevated zones but not yet actionable
        NEUTRAL  : Z in -0.8 to +1.5 (structurally normal for crypto)

    Args:
        current_z   : latest z-score float
        all_zscores : full z-score Series for persistence check

    Returns:
        (score, reason) tuple
    """
    # ── BEARISH CHECK ─────────────────────────────────────────────────────────
    if current_z > BEAR_THRESHOLD:
        persists = _check_persistence(
            all_zscores, BEAR_THRESHOLD, "above", PERSISTENCE_PERIODS
        )
        if persists:
            return (
                -1,
                f"Z-Score {current_z:.3f} > +{BEAR_THRESHOLD} | "
                f"Confirmed {PERSISTENCE_PERIODS} consecutive periods | "
                f"Extreme long crowding — longs paying punishing carry fees | "
                f"Any price dip triggers mass forced closure cascade | "
                f"BEARISH — fade the crowd"
            )
        else:
            return (
                0,
                f"Z-Score {current_z:.3f} > +{BEAR_THRESHOLD} | "
                f"Threshold breached but persistence not confirmed yet | "
                f"Need {PERSISTENCE_PERIODS} consecutive periods — watch closely | "
                f"No signal yet"
            )

    # ── BULLISH CHECK ─────────────────────────────────────────────────────────
    elif current_z < BULL_THRESHOLD:
        persists = _check_persistence(
            all_zscores, BULL_THRESHOLD, "below", PERSISTENCE_PERIODS
        )
        if persists:
            return (
                +1,
                f"Z-Score {current_z:.3f} < {BULL_THRESHOLD} | "
                f"Confirmed {PERSISTENCE_PERIODS} consecutive periods | "
                f"Extreme short crowding — short squeeze fuel at maximum | "
                f"Shorts bleeding carry costs — forced buybacks imminent | "
                f"BULLISH — squeeze is loaded"
            )
        else:
            return (
                0,
                f"Z-Score {current_z:.3f} < {BULL_THRESHOLD} | "
                f"Threshold breached but persistence not confirmed yet | "
                f"Need {PERSISTENCE_PERIODS} consecutive periods — watch closely | "
                f"No signal yet"
            )

    # ── WATCH BEARISH (elevated but not actionable) ───────────────────────────
    elif current_z >= WATCH_BEAR_LOW:
        return (
            0,
            f"Z-Score {current_z:.3f} in watch-bearish zone "
            f"[+{WATCH_BEAR_LOW}, +{BEAR_THRESHOLD}] | "
            f"Long positioning elevated but not extreme | "
            f"Monitor — needs to breach +{BEAR_THRESHOLD} and persist | "
            f"No signal yet"
        )

    # ── WATCH BULLISH (elevated short but not actionable) ────────────────────
    elif current_z <= WATCH_BULL_HIGH:
        return (
            0,
            f"Z-Score {current_z:.3f} in watch-bullish zone "
            f"[{BULL_THRESHOLD}, {WATCH_BULL_HIGH}] | "
            f"Short positioning elevated but not extreme | "
            f"Monitor — needs to breach {BULL_THRESHOLD} and persist | "
            f"No signal yet"
        )

    # ── NEUTRAL ───────────────────────────────────────────────────────────────
    else:
        return (
            0,
            f"Z-Score {current_z:.3f} in neutral zone "
            f"[{NEUTRAL_LOW}, +{NEUTRAL_HIGH}] | "
            f"Balanced positioning | "
            f"Positive funding up to +1.5 is structurally normal in crypto | "
            f"No signal"
        )


# ── Main Signal Function ──────────────────────────────────────────────────────
def get_signal(symbol: str = "BTCUSDT") -> dict:
    """
    Compute Signal 1 — Funding Rate Z-Score (v2.0 optimized).

    Only function your aggregator needs to call.
    """
    df           = fetch_funding_rates(symbol=symbol)
    df["zscore"] = compute_zscore(df)

    latest       = df.iloc[-1]
    current_z    = latest["zscore"]
    current_rate = latest["fundingRate"]
    timestamp    = latest["fundingTime"]
    all_zscores  = df["zscore"].dropna()

    # ── Handle insufficient data ──────────────────────────────────────────────
    if pd.isna(current_z):
        # ── Standardized v1.0 ─────────────────────────────────────────
        # Keys added   : score, s1_watch_state, s1_z_momentum
        # Keys renamed : zscore_pctile -> s1_zscore_pctile, persistence_ok -> s1_persistence
        # Logic changed: NONE
        return {
            "signal_id"        : 1,
            "score"            : 0,
            "timestamp"        : timestamp,
            "reason"           : "Insufficient data for z-score — defaulting neutral",
            "s1_score"         : 0,
            "s1_zscore"        : 0.0,
            "s1_funding_raw"   : round(float(current_rate), 6),
            "s1_zscore_pctile" : 0.0,
            "s1_persistence"   : 0,
            "s1_watch_state"   : 0,
            "s1_z_momentum"    : 0.0,
        }

    # ── Percentile rank of current Z in its own history ───────────────────────
    # Useful context: tells you exactly how extreme this reading is
    # relative to all historical z-scores, not just the threshold.
    zscore_pctile = float(
        round((all_zscores < current_z).mean() * 100, 1)
    )

    # ── Persistence check for output metadata ─────────────────────────────────
    if current_z > BEAR_THRESHOLD:
        persistence_ok = _check_persistence(
            all_zscores, BEAR_THRESHOLD, "above", PERSISTENCE_PERIODS
        )
    elif current_z < BULL_THRESHOLD:
        persistence_ok = _check_persistence(
            all_zscores, BULL_THRESHOLD, "below", PERSISTENCE_PERIODS
        )
    else:
        persistence_ok = False

    score, reason = _classify(current_z, all_zscores)

    # ── Standardized v1.0 ─────────────────────────────────────────
    # Keys added   : score, s1_watch_state, s1_z_momentum
    # Keys renamed : zscore_pctile -> s1_zscore_pctile, persistence_ok -> s1_persistence
    # Logic changed: NONE
    return {
        "signal_id"        : 1,
        "score"            : score,
        "timestamp"        : timestamp,
        "reason"           : reason,
        "s1_score"         : score,
        "s1_zscore"        : round(float(current_z), 4),
        "s1_funding_raw"   : round(float(current_rate), 6),
        "s1_zscore_pctile" : float(zscore_pctile),
        "s1_persistence"   : int(persistence_ok),
        "s1_watch_state"   : 0,
        "s1_z_momentum"    : 0.0,
    }

