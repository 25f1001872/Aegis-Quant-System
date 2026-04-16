import time
import logging
import pandas as pd
import numpy as np
import requests
import os
import sys
from datetime import datetime, timezone

# Add the project root to sys.path so we can import 'aegis' when running as a script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Adjusting imports to use the correct file names from the existing aegis/signals module
from aegis.signals.s1_funding_zscore import get_signal as get_s1
from aegis.signals.s2_oi_delta import get_signal as get_s2
from aegis.signals.s3_liq_clusters import get_signal as get_s3, start_s3_stream, stop_s3_stream
from aegis.signals.s4_long_short_ratio import get_signal as get_s4
from aegis.signals.s5_ofi import get_signal as get_s5, start_ofi_stream, stop_ofi_stream
from aegis.signals.s6_cvd import get_signal as get_s6, start_cvd_stream, stop_cvd_stream
from aegis.signals.s7_taker_ratio import get_signal as get_s7

# Setup local file logging
log_dir = os.path.join("logs")
if not os.path.exists(log_dir):
    os.makedirs(log_dir)
log_file = os.path.join(log_dir, "aggregator_errors.log")

file_handler = logging.FileHandler(log_file)
stream_handler = logging.StreamHandler()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", 
                    handlers=[file_handler, stream_handler])
logger = logging.getLogger("aggregator")

class AegisAggregator:
    def __init__(self, mode="LIVE"):
        self.mode = mode
        self.csv_path = os.path.join("data", "processed", "aegis_features.csv")

        # ── TTL Cache ──────────────────────────────────────────────────
        # Signals are cached for exactly as long as their underlying
        # Binance data source actually updates. Fetching more often
        # than the source updates returns identical data — wasted call.
        #
        # S1 Funding     → 28,800s (8H)  — Binance settles every 8H
        # S2 OI Delta    → 300s    (5M)  — Binance OI hist updates every 5M
        # S3 Clusters    → 1,800s  (30M) — cluster map changes slowly
        # S4 LSR         → 300s    (5M)  — Binance LSR updates every 5M
        # S7 Taker Ratio → 300s    (5M)  — Binance taker ratio every 5M
        # OHLCV          → 300s    (5M)  — new candle closes every 5M
        # S5 OFI         → no cache — live WebSocket trigger signal
        # S6 CVD         → no cache — live WebSocket trigger signal

        self._cache = {}

        self._ttl = {
            "s1"   : 28_800,
            "s2"   : 300,
            "s3"   : 1_800,
            "s4"   : 300,
            "s7"   : 300,
            "ohlcv": 300,
        }
        if self.mode == "COLLECT":
            os.makedirs(os.path.dirname(self.csv_path), exist_ok=True)
            self._ensure_csv_header()

    def _ensure_csv_header(self):
        if not os.path.exists(self.csv_path):
            with open(self.csv_path, "w") as f:
                f.write(','.join(self._get_columns()) + '\n')

    def _get_columns(self):
        return [
            "row_timestamp", "data_valid",
            "s1_score", "s2_score", "s3_score", "s4_score",
            "s5_score", "s6_score", "s7_score",
            "s1_zscore", "s1_funding_raw", "s1_zscore_pctile",
            "s1_persistence", "s1_watch_state", "s1_z_momentum",
            "s2_oi_delta", "s2_oi_current", "s2_price_up", "s2_oi_up",
            "s3_cluster_distance", "s3_nearest_cluster_usd",
            "s3_cluster_size_usd", "s3_dominant_side",
            "s4_ls_ratio", "s4_long_pct", "s4_short_pct", "s4_ls_extreme",
            "s5_ofi_raw", "s5_buy_vol", "s5_sell_vol", "s5_ofi_norm",
            "s6_cvd", "s6_divergence_str", "s6_divergence_type",
            "s6_warmup_done", "s6_candles_live",
            "s7_taker_ratio", "s7_buy_ratio", "s7_sell_ratio", "s7_ratio_pctile",
            "family_a_score", "family_b_score", "total_score",
            "funding_x_ls_ratio", "ofi_x_taker_ratio",
            "volatility_15m", "volume_15m", "atr_15m", "realized_vol_1h",
            "trend_strength", "adx_15m", "price_15m_return", "price_1h_return",
            "regime", "hour_of_day", "day_of_week",
            "label"
        ]

    def _get_neutral_fallback(self, signal_id):
        fallbacks = {
            1: {"score": 0, "s1_score": 0, "s1_zscore": 0.0, "s1_funding_raw": 0.0, "s1_zscore_pctile": 0.0, "s1_persistence": 0, "s1_watch_state": 0, "s1_z_momentum": 0.0},
            2: {"score": 0, "s2_score": 0, "s2_oi_delta": 0.0, "s2_oi_current": 0.0, "s2_price_up": 0, "s2_oi_up": 0},
            3: {"score": 0, "s3_score": 0, "s3_cluster_distance": 0.0, "s3_nearest_cluster_usd": 0.0, "s3_cluster_size_usd": 0.0, "s3_dominant_side": ""},
            4: {"score": 0, "s4_score": 0, "s4_ls_ratio": 0.5, "s4_long_pct": 50.0, "s4_short_pct": 50.0, "s4_ls_extreme": 0},
            5: {"score": 0, "s5_score": 0, "s5_ofi_raw": 0.0, "s5_buy_vol": 0.0, "s5_sell_vol": 0.0, "s5_ofi_norm": 0.0},
            6: {"score": 0, "s6_score": 0, "s6_cvd": 0.0, "s6_divergence_str": 0.0, "s6_divergence_type": "neutral", "s6_warmup_done": 0, "s6_candles_live": 0},
            7: {"score": 0, "s7_score": 0, "s7_taker_ratio": 0.5, "s7_buy_ratio": 0.5, "s7_sell_ratio": 0.5, "s7_ratio_pctile": 0.0},
        }
        return fallbacks[signal_id]

    def _safe_call(self, func, signal_id):
        try:
            return func()
        except Exception as e:
            logger.error(f"Signal {signal_id} failed: {str(e)}")
            return self._get_neutral_fallback(signal_id)

    def _fetch_with_cache(self, key: str, func, signal_id: int) -> dict:
        """
        Return cached result if TTL has not expired.
        Otherwise fetch fresh data, cache it, and return it.

        Args:
            key       : cache key — "s1","s2","s3","s4","s7","ohlcv"
            func      : callable to fetch fresh data — pass None for ohlcv
            signal_id : int signal id for _safe_call (pass 0 for ohlcv)

        Returns:
            signal dict or ohlcv feature dict
        """
        now = time.time()
        ttl = self._ttl.get(key, 0)

        if key in self._cache:
            age = now - self._cache[key]["ts"]
            if age < ttl:
                logger.info(
                    f"Cache HIT  [{key}] — age {age:.0f}s / ttl {ttl}s"
                )
                return self._cache[key]["data"]
            logger.info(
                f"Cache MISS [{key}] — age {age:.0f}s exceeded ttl {ttl}s"
                f" — fetching fresh"
            )
        else:
            logger.info(f"Cache COLD [{key}] — first fetch")

        if signal_id == 0:
            fresh = self._compute_ohlcv_features()
        else:
            fresh = self._safe_call(func, signal_id)

        self._cache[key] = {"data": fresh, "ts": now}
        return fresh

    def _compute_ohlcv_features(self):
        try:
            url = "https://fapi.binance.com/fapi/v1/klines"
            params = {"symbol": "BTCUSDT", "interval": "15m", "limit": 100}
            res = requests.get(url, params=params, timeout=10)
            res.raise_for_status()
            
            data = res.json()
            if len(data) < 25:
                raise ValueError("Not enough klines data")

            # DataFrame from klines
            df = pd.DataFrame(data, columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_volume", "count", "taker_buy_volume", 
                "taker_buy_quote_volume", "ignore"
            ])
            df = df[["open", "high", "low", "close", "quote_volume"]].astype(float)

            # Volatility 15m (std of close-to-close % returns over last 20 periods)
            df['returns'] = df['close'].pct_change() * 100
            volatility_15m = float(df['returns'].rolling(20).std().iloc[-2]) if len(df) > 20 else 0.0

            # Volume 15m (last completed candle)
            volume_15m = float(df['quote_volume'].iloc[-2])

            # ATR 14
            high = df['high']
            low = df['low']
            close = df['close']
            tr1 = high - low
            tr2 = (high - close.shift()).abs()
            tr3 = (low - close.shift()).abs()
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr_15m = float(tr.rolling(14).mean().iloc[-2]) if len(df) > 15 else 0.0

            # Realized Vol 1H
            returns_15m = df['close'].pct_change()
            realized_vol_series = returns_15m.rolling(4).std() * np.sqrt(365 * 24 * 4) * 100
            realized_vol_1h = float(realized_vol_series.iloc[-2]) if len(df) > 5 else 0.0
            
            # Regime calculation dependencies
            realized_vol_mean_20 = float(realized_vol_series.rolling(20).mean().iloc[-2]) if len(df) > 25 else 0.0

            # Trend strength
            closes = df['close'].iloc[-21:-1].values
            if len(closes) == 20:
                x = np.arange(20)
                slope, _ = np.polyfit(x, closes, 1)
                trend_strength = abs(slope) / closes[-1]
            else:
                trend_strength = 0.0

            # ADX 14
            up = df['high'] - df['high'].shift(1)
            down = df['low'].shift(1) - df['low']
            pos_dm = np.where((up > down) & (up > 0), up, 0.0)
            neg_dm = np.where((down > up) & (down > 0), down, 0.0)
            
            tr_series = pd.Series(tr)
            pos_dm_series = pd.Series(pos_dm)
            neg_dm_series = pd.Series(neg_dm)
            
            atr = tr_series.rolling(14).mean()
            pos_di = 100 * (pos_dm_series.rolling(14).mean() / atr)
            neg_di = 100 * (neg_dm_series.rolling(14).mean() / atr)
            dx = 100 * abs(pos_di - neg_di) / (pos_di + neg_di).replace(0, np.nan)
            adx_15m = float(dx.rolling(14).mean().iloc[-2]) if len(df) > 28 else 0.0

            # Price Returns
            # -1 is live, -2 is last completed.
            price_15m_return = float((df['close'].iloc[-1] - df['close'].iloc[-2]) / df['close'].iloc[-2] * 100)
            price_1h_return = float((df['close'].iloc[-1] - df['close'].iloc[-5]) / df['close'].iloc[-5] * 100) if len(df) > 5 else 0.0

            # Last closed candle wicks for SL/TP internal detection
            candle_high = float(df["high"].iloc[-2])
            candle_low  = float(df["low"].iloc[-2])

            # Regime
            if realized_vol_1h > 1.5 * realized_vol_mean_20 and realized_vol_mean_20 > 0:
                regime = 2
            elif adx_15m >= 25:
                regime = 1
            elif adx_15m < 20:
                regime = 0
            else:
                regime = 0 # Fallback for 20-25

            return {
                "volatility_15m": volatility_15m,
                "volume_15m": volume_15m,
                "atr_15m": atr_15m,
                "realized_vol_1h": realized_vol_1h,
                "trend_strength": trend_strength,
                "adx_15m": adx_15m,
                "price_15m_return": price_15m_return,
                "price_1h_return": price_1h_return,
                "candle_high": candle_high,
                "candle_low": candle_low,
                "regime": regime
            }

        except Exception as e:
            logger.error(f"OHLCV Feature error: {str(e)}")
            return {
                "volatility_15m": 0.0, "volume_15m": 0.0, "atr_15m": 0.0, "realized_vol_1h": 0.0,
                "trend_strength": 0.0, "adx_15m": 0.0, "price_15m_return": 0.0, "price_1h_return": 0.0,
                "candle_high": 0.0, "candle_low": 0.0,
                "regime": 0
            }

    def aggregate(self):
        import concurrent.futures

        # ── Cached REST signals ────────────────────────────────────────────
        # Cache hits are instant — only fetches when TTL has expired
        s1    = self._fetch_with_cache("s1",    get_s1, 1)
        s2    = self._fetch_with_cache("s2",    get_s2, 2)
        s4    = self._fetch_with_cache("s4",    get_s4, 4)
        s7    = self._fetch_with_cache("s7",    get_s7, 7)
        ohlcv = self._fetch_with_cache("ohlcv", None,   0)

        # ── Live signals — always fresh, run in parallel ───────────────────
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            fut_s3 = executor.submit(self._fetch_with_cache, "s3", get_s3, 3)
            fut_s5 = executor.submit(self._safe_call, get_s5, 5)
            fut_s6 = executor.submit(self._safe_call, get_s6, 6)

            s3 = fut_s3.result()
            s5 = fut_s5.result()
            s6 = fut_s6.result()

        # Build feature row dictionary
        now = datetime.now(timezone.utc)
        
        row_timestamp = now.isoformat()
        data_valid = 1 # Could validate if any critical failure occurred

        family_a_score = s1["s1_score"] + s2["s2_score"] + s3["s3_score"] + s4["s4_score"]
        family_b_score = s5["s5_score"] + s6["s6_score"] + s7["s7_score"]
        total_score = family_a_score + family_b_score

        funding_x_ls_ratio = s1["s1_zscore"] * s4["s4_ls_ratio"]
        ofi_x_taker_ratio = s5["s5_ofi_norm"] * s7["s7_taker_ratio"]

        hour_of_day = now.hour
        day_of_week = now.weekday()

        row = {
            "row_timestamp": row_timestamp, "data_valid": data_valid,
            "s1_score": s1["s1_score"], "s2_score": s2["s2_score"], "s3_score": s3["s3_score"], "s4_score": s4["s4_score"],
            "s5_score": s5["s5_score"], "s6_score": s6["s6_score"], "s7_score": s7["s7_score"],
            
            "s1_zscore": s1["s1_zscore"], "s1_funding_raw": s1["s1_funding_raw"], "s1_zscore_pctile": s1["s1_zscore_pctile"],
            "s1_persistence": s1["s1_persistence"], "s1_watch_state": s1["s1_watch_state"], "s1_z_momentum": s1["s1_z_momentum"],
            
            "s2_oi_delta": s2["s2_oi_delta"], "s2_oi_current": s2["s2_oi_current"], "s2_price_up": s2["s2_price_up"], "s2_oi_up": s2["s2_oi_up"],
            
            "s3_cluster_distance": s3["s3_cluster_distance"], "s3_nearest_cluster_usd": s3["s3_nearest_cluster_usd"],
            "s3_cluster_size_usd": s3["s3_cluster_size_usd"], "s3_dominant_side": s3["s3_dominant_side"],
            
            "s4_ls_ratio": s4["s4_ls_ratio"], "s4_long_pct": s4["s4_long_pct"], "s4_short_pct": s4["s4_short_pct"], "s4_ls_extreme": s4["s4_ls_extreme"],
            
            "s5_ofi_raw": s5["s5_ofi_raw"], "s5_buy_vol": s5["s5_buy_vol"], "s5_sell_vol": s5["s5_sell_vol"], "s5_ofi_norm": s5["s5_ofi_norm"],
            
            "s6_cvd": s6["s6_cvd"], "s6_divergence_str": s6["s6_divergence_str"], "s6_divergence_type": s6["s6_divergence_type"],
            "s6_warmup_done": s6["s6_warmup_done"], "s6_candles_live": s6["s6_candles_live"],
            
            "s7_taker_ratio": s7["s7_taker_ratio"], "s7_buy_ratio": s7["s7_buy_ratio"], "s7_sell_ratio": s7["s7_sell_ratio"], "s7_ratio_pctile": s7["s7_ratio_pctile"],
            
            "family_a_score": family_a_score, "family_b_score": family_b_score, "total_score": total_score,
            
            "funding_x_ls_ratio": funding_x_ls_ratio, "ofi_x_taker_ratio": ofi_x_taker_ratio,
            
            "volatility_15m": ohlcv["volatility_15m"], "volume_15m": ohlcv["volume_15m"], "atr_15m": ohlcv["atr_15m"], "realized_vol_1h": ohlcv["realized_vol_1h"],
            "trend_strength": ohlcv["trend_strength"], "adx_15m": ohlcv["adx_15m"], "price_15m_return": ohlcv["price_15m_return"], "price_1h_return": ohlcv["price_1h_return"],
            "regime": ohlcv["regime"], "hour_of_day": hour_of_day, "day_of_week": day_of_week,
            
            "label": ""
        }

        # Format floats internally just for safety (round 6 as requested)
        for k, v in row.items():
            if isinstance(v, float):
                row[k] = round(v, 6)
        
        if self.mode == "COLLECT":
            ordered_vals = [str(row.get(col, "")) for col in self._get_columns()]
            with open(self.csv_path, "a") as f:
                f.write(','.join(ordered_vals) + '\n')
            
            print(f"[{row_timestamp}] | Total:{total_score:+d} | A:{family_a_score:+d} B:{family_b_score:+d} | "
                  f"S1:{s1['s1_score']:+d} S2:{s2['s2_score']:+d} S3:{s3['s3_score']:+d} "
                  f"S4:{s4['s4_score']:+d} S5:{s5['s5_score']:+d} S6:{s6['s6_score']:+d} S7:{s7['s7_score']:+d} | regime:{ohlcv['regime']}")

        return row

def main():
    print("Starting Streaming Signals (S3, S5, S6)...")
    start_s3_stream()
    start_ofi_stream()
    start_cvd_stream()

    # ── Warmup ────────────────────────────────────────────────────────────
    # Wait for the longest WebSocket signal warmup to complete.
    # S6 CVD needs 3000s (50 min) — this is the bottleneck.
    # S3 needs 600s and S5 needs 900s — both finish inside S6 warmup.
    # No point collecting rows before all 3 streams are ready.
    WARMUP = 3000   # seconds — matches S6 WARMUP_SECONDS exactly

    print(f"Warming up WebSocket streams — {WARMUP // 60} minutes...")
    print("S3 / S5 / S6 collecting data in background...")
    print("REST signals S1 S2 S4 S7 need no warmup — ready immediately\n")

    for minute in range(WARMUP // 60):
        time.sleep(60)
        done      = minute + 1
        remaining = (WARMUP // 60) - done
        print(f"  Warmup: {done} min done — {remaining} min remaining...")

    print("\nWarmup complete — starting collection loop\n")

    agg = AegisAggregator(mode="COLLECT")
    print(f"Saving to: {agg.csv_path}")

    import yaml
    config = {}
    try:
        with open("configs/risk.yaml", "r") as f:
            config = yaml.safe_load(f) or {}
    except Exception:
        pass

    from aegis.execution.broker import BinanceBroker, SYMBOL, LEVERAGE
    from aegis.execution.paper import PaperTradeEngine
    from aegis.portfolio.constructor import PortfolioManager

    broker  = BinanceBroker()
    paper   = PaperTradeEngine(broker=broker)
    manager = PortfolioManager(broker, paper, config=config)
    broker.set_leverage(SYMBOL, LEVERAGE)

    # ── Master loop interval ───────────────────────────────────────────────
    # 5 minutes matches the fastest REST signal update frequency
    # (S2 OI, S4 LSR, S7 Taker Ratio, OHLCV all update every 5M on Binance)
    #
    # Latency buffer: sleep 310s not 300s
    # Exact 300s risks landing on the same candle boundary as Binance
    # which can cause the REST call to fetch the previous candle value
    # instead of the newly closed one (off-by-one candle bug).
    # 10 second buffer ensures Binance has fully settled the new candle
    # before we fetch it. Small cost — large reliability gain.
    INTERVAL = 310  # 5 minutes + 10 second latency buffer

    print(f"Collection interval: {INTERVAL}s (5min + 10s latency buffer)")
    print("Ctrl+C to stop\n")

    try:
        while True:
            feature_row = agg.aggregate()
            result      = manager.process(feature_row)
            print(
                f"  {result['action']:<6} | "
                f"Dir:{str(result['direction']):<5} | "
                f"Score:{result['total_score']:+d} B:{result['family_b_score']:+d} | "
                f"Risk:{result.get('risk_factor', 1.0):.2f}x | "
                f"{result['reason']}"
            )
            time.sleep(INTERVAL)

    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        stop_s3_stream()
        stop_ofi_stream()
        stop_cvd_stream()
        print("Done.")

if __name__ == "__main__":
    main()
