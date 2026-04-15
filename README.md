# Aegis-Quant-System — Complete Runtime Walkthrough
> From `python aegis/alpha/aggregator.py` → To Trade Journal

---

## PHASE 1 — STARTUP (T+0:00 to T+0:10)

You run:
```bash
python aegis/alpha/aggregator.py
```

What happens immediately:

```
─────────────────────────────────────────────────────────────────────
1. Three WebSocket streams open simultaneously:

   S3 Liquidation stream → connects to Binance
      wss://fstream.binance.com/ws/!forceOrder@arr
      Starts receiving every forced liquidation in real time
      Building the cluster map in memory

   S5 OFI stream → connects to Binance
      wss://fstream.binance.com/ws/btcusdt@aggTrade
      Starts receiving every single trade
      Tracking buy volume vs sell volume

   S6 CVD stream → connects to Binance
      wss://fstream.binance.com/ws/btcusdt@aggTrade
      Same stream — different calculator
      Building 5-minute candles, tracking cumulative delta

2. REST signals (S1 S2 S4 S7) are ready immediately
   No warmup needed — they fetch on demand

3. BinanceBroker initializes
   Connects to https://testnet.binancefuture.com
   Sets leverage to 1x on BTCUSDT
   Loads your $5000 USDT paper balance

4. PaperTradeEngine loads
   Checks data/artifacts/paper_state.json
   If file exists → resumes from last known state
   If file does not exist → starts fresh with $5000

5. PortfolioManager initializes
   Loads risk config from configs/risk.yaml
   max_position_pct = 0.20 (20% of equity per trade)
   Ready to evaluate feature rows

Terminal prints:
   Starting Streaming Signals (S3, S5, S6)...
   [OFI] Connected → wss://...
   [CVD WS] Connected — streaming aggTrade...
   Warming up WebSocket streams — 50 minutes...
   S3 / S5 / S6 collecting data in background...
   REST signals S1 S2 S4 S7 need no warmup — ready immediately
```

---

## PHASE 2 — WARMUP (T+0:10 to T+50:00)

All 3 WebSocket streams are running in background threads.
Nothing is evaluated. No trades. No signals read. Just data collection.

```
─────────────────────────────────────────────────────────────────────
S3 — Every liquidation that hits the market gets recorded
     Price level, size, side (long or short liquidated)
     After 10 minutes (600s) → cluster map is ready
     You now know where the largest liquidation clusters are
     for the last 7 days binned into price buckets

S5 — Every trade is classified as taker buy or taker sell
     OFI accumulates in 15-minute windows
     After 15 minutes (900s) → first complete window sealed
     Z-score can now be computed against history
     The signal starts being meaningful

S6 — Every trade updates the 5-minute CVD candles
     After 50 minutes (3000s) → 10 complete 5M candles closed
     Both sides of the divergence lookback window are populated
     Multi-timeframe divergence detection becomes valid

Terminal prints every minute:
   Warmup: 1 min done — 49 min remaining...
   Warmup: 2 min done — 48 min remaining...
   ...
   Warmup: 50 min done — 0 min remaining...

   Warmup complete — starting collection loop
```

---

## PHASE 3 — FIRST EVALUATION (T+50:10)

`aggregator.aggregate()` is called for the first time.

```
─────────────────────────────────────────────────────────────────────
STEP A — Fetch all 7 signals

   S1 Funding Z-Score (Cache COLD — first fetch)
      Calls Binance REST → fetches last 500 funding rates
      Computes rolling Z-score
      Example output:
         s1_zscore      = -1.19
         s1_score       = 0  (not extreme enough, threshold is -1.6)
         s1_watch_state = 0

   S2 OI Delta (Cache COLD — first fetch)
      Calls Binance REST → fetches OI history
      Computes change in open interest
      Example output:
         s2_oi_delta  = +4930 USD
         s2_score     = 0  (small change, no conviction signal)

   S3 Liquidation Clusters (from live WebSocket data)
      Reads current cluster map built over last 50 minutes
      Finds nearest significant cluster to current price
      Example output:
         s3_cluster_distance    = +136 USD  (cluster is above price)
         s3_nearest_cluster_usd = 74,000
         s3_cluster_size_usd    = 429,770
         s3_dominant_side       = "short"   (shorts would be liquidated)
         s3_score               = 0

   S4 Long/Short Ratio (Cache COLD — first fetch)
      Calls Binance REST → fetches global LSR
      Example output:
         s4_ls_ratio  = 0.395  (39.5% of accounts are long)
         s4_long_pct  = 39.5
         s4_short_pct = 60.5
         s4_score     = 0  (not extreme, threshold is < 35%)

   S5 OFI (from live WebSocket data)
      Reads last sealed 15M window OFI
      Computes Z-score against history
      Example output:
         s5_ofi_raw   = -42,663,324 USD  (net selling in last 15M)
         s5_buy_vol   = 180,000,000
         s5_sell_vol  = 222,000,000
         s5_ofi_norm  = -0.105
         s5_score     = 0  (Z not extreme enough)

   S6 CVD (from live WebSocket data)
      Reads multi-timeframe divergence result
      Example output:
         s6_cvd             = -89,898,716 USD  (session sellers dominating)
         s6_divergence_str  = 0.0
         s6_divergence_type = "neutral"
         s6_score           = 0

   S7 Taker Ratio (Cache COLD — first fetch)
      Calls Binance REST → fetches taker buy/sell ratio
      Example output:
         s7_taker_ratio = 0.409  (40.9% of volume is aggressive buying)
         s7_buy_ratio   = 0.409
         s7_sell_ratio  = 0.590
         s7_score       = 0  (neutral zone, threshold is < 0.40)

STEP B — Compute OHLCV features (Cache COLD — first fetch)
   Fetches last 100 15M candles from Binance
   Computes:
      volatility_15m   = 0.358  (% std dev of last 20 candles)
      volume_15m       = 230,962,816 USD
      atr_15m          = 350.39 USD  (average true range)
      realized_vol_1h  = 46.98%  (annualized)
      trend_strength   = 0.000782
      adx_15m          = 26.07  (trending — ADX > 25)
      price_15m_return = -0.51%
      price_1h_return  = -1.12%
      regime           = 1  (trending — ADX ≥ 25)

STEP C — Compute aggregated features
   family_a_score = 0+0+0+0 = 0
   family_b_score = 0+0+0   = 0
   total_score    = 0

   funding_x_ls_ratio = -1.19 × 0.395 = -0.47
   ofi_x_taker_ratio  = -0.105 × 0.409 = -0.043

   hour_of_day = 18
   day_of_week = 1  (Monday)

STEP D — Write row to CSV
   One complete row appended to data/processed/aegis_features.csv
   label column = ""  (unknown — filled later by labeling script)
```

---

## PHASE 4 — PORTFOLIO MANAGER EVALUATION (T+50:10)

`manager.process(feature_row)` is called with the feature row above.

```
─────────────────────────────────────────────────────────────────────
STEP 1 — Can we trade? (can_trade check)

   Check 1: Is there already an open position?
      paper_engine.get_position() → None
      ✅ No open position — continue

   Check 2: Is drawdown above 8%?
      paper_engine.get_drawdown() → 0%  (no trades yet)
      ✅ Drawdown fine — continue

   Check 3: Is it a dead hour? (00:00–04:00 UTC)
      hour_of_day = 18
      ✅ Not dead hours — continue

   Check 4: Is regime volatile?
      regime = 1  (trending)
      ✅ Not volatile — continue

   Check 5: Does Family B have any trigger?
      family_b_score = 0
      ❌ NO TRIGGER — STOP HERE

Result: can_trade = False
Reason: "no trigger signal — family_b_score = 0"

Terminal prints:
   Action:SKIP  Dir:None  Score:+0  B:0  Bulls:2 Bears:3
   → no trigger signal — family_b_score = 0
```

---

## PHASE 5 — WHAT NEEDS TO HAPPEN FOR A TRADE TO OPEN

```
For portfolio manager to proceed past can_trade():
   family_b_score must be ≠ 0

This requires at least one of:
   S5 OFI Z-score to breach ±2.0  → buying/selling extremely aggressive
   S6 CVD to show confirmed divergence on both 25M and 50M windows
   S7 taker ratio to go outside 0.40–0.60 range

Example scenario that triggers evaluation:
─────────────────────────────────────────────────────────────────────
BTC drops $500 in 15 minutes

   S5: aggressive selling floods in
       s5_ofi_raw   = -180,000,000 USD
       Z-score      = -2.4  (more extreme than 99% of history)
       s5_score     = -1  ← BEARISH TRIGGER

   S6: price falling + CVD falling = confirmed bearish
       s6_divergence_type = "confirmed_bearish"
       s6_divergence_str  = 0.85
       s6_score           = -1  ← BEARISH CONFIRMATION

   S7: 72% of volume is aggressive selling
       s7_taker_ratio = 0.28
       s7_score       = -1  ← BEARISH CONFIRMATION

   family_b_score = -3  ← maximum bearish trigger
   can_trade()    = True  ← proceeds to direction check
```

---

## PHASE 6 — DIRECTION DETERMINATION

`get_direction(feature_row)` runs the raw value vote system:

```
─────────────────────────────────────────────────────────────────────
bull_votes:
   s1_zscore < -0.8?        -1.19 < -0.8   → YES → bull_votes = 1
   s2_score == +1?           0              → NO
   s3_dominant_side=short
   AND cluster above?        short + +136   → YES → bull_votes = 2
   s4_ls_ratio < 0.45?       0.395 < 0.45  → YES → bull_votes = 3
   s6_cvd > 0?              -89M           → NO
   s7_taker_ratio > 0.52?    0.28          → NO

   Total bull_votes = 3

bear_votes:
   s1_zscore > 0.8?         -1.19          → NO
   s2_score == -1?           0             → NO
   s3_dominant_side=long
   AND cluster below?        "short"       → NO
   s4_ls_ratio > 0.55?       0.395         → NO
   s6_cvd < 0?              -89M           → YES → bear_votes = 1
   s7_taker_ratio < 0.48?    0.28 < 0.48  → YES → bear_votes = 2

   Total bear_votes = 2

Direction rule:
   bull_votes ≥ 4 AND family_b_score > 0 → LONG
   bear_votes ≥ 4 AND family_b_score < 0 → SHORT
   otherwise                             → NONE

Result: bear_votes = 2, not enough (need 4)
        direction = NONE → SKIP

For SHORT to fire in this example we need 2 more bear signals:
   s2_score = -1 (OI rising as price drops)
   s4_ls_ratio > 0.55 (crowd getting more long as it dumps)
   THEN bear_votes = 4 AND family_b_score = -3 → SHORT confirmed
```

---

## PHASE 7 — WHEN DIRECTION IS CONFIRMED (Full SHORT Example)

```
Assume all conditions met:
   family_b_score = -3
   bear_votes     = 4
   direction      = SHORT
   total_score    = -5
   current_price  = 74,000

─────────────────────────────────────────────────────────────────────
STEP 1 — Compute entry levels

   Entry price:
      current_price = 74,000  (market order — enter now)

   Cluster target:
      s3_nearest_cluster_usd = 73,000  (long cluster below)
      s3_dominant_side       = "long"  (longs get liquidated if price drops)

   TP1 = 73,000 × (1 + 0.002) = 73,146  (just above cluster, 0.2% buffer)
         Close 70% of position here
   TP2 = 73,000 × 0.995       = 72,635  (through the cluster)
         Close remaining 30% here or trail

   SL placement:
      broker.get_last_4h_swing("SHORT")
      Returns highest high of last 2 complete 4H candles
      Example: 4H high = 74,850

      sl = 74,850 × (1 + 0.001) = 74,925  (0.1% above 4H high)
      sl_pct = (74,925 - 74,000) / 74,000 × 100 = 1.25%

      Is 0.5% ≤ sl_pct ≤ 3.0%?
      1.25% is in range ✅ → proceed

STEP 2 — Compute position size

   balance          = $5,000
   max_position_pct = 0.20
   max_position     = $5,000 × 0.20 = $1,000

   Sizing table:
      family_b_score = -3 (all 3 B signals fire)
      total_score    = -5
      → 100% of max_position

   position_usdt = $1,000 × 1.00 = $1,000
   quantity      = $1,000 / 74,000 = 0.01351 BTC
   rounded       = 0.013 BTC  (3 decimal places)

   Risk/Reward:
      Risk  = (74,925 - 74,000) / 74,000 = 1.25%  = $12.50
      TP1   = (74,000 - 73,146) / 74,000 = 1.15%  = $11.11
      TP2   = (74,000 - 72,635) / 74,000 = 1.84%  = $17.70
      R:R   = TP2 / Risk = 1.84 / 1.25 = 1.47:1
      Blended R:R (70% TP1 + 30% TP2):
              (0.70 × 1.15 + 0.30 × 1.84) / 1.25 = 1.09:1

STEP 3 — Execute on testnet

   PAPER_MODE = True → simulated fill at current price

   paper_engine.open_position(
      side         = "SHORT"
      entry_price  = 74,000
      quantity     = 0.013
      sl           = 74,925
      tp1          = 73,146
      tp2          = 72,635
      score        = -5
      signal_snap  = {all 7 scores and raw values}
   )

   State saved to:
   data/artifacts/paper_state.json

Terminal prints:
   Action:OPEN  Dir:SHORT  Score:-5  B:-3  Bulls:1 Bears:5
   → SHORT opened at 74000 | SL:74925 | TP1:73146 | TP2:72635
   → Size: 0.013 BTC ($962) | Risk: $12.50 | R:R: 1.47
```

---

## PHASE 8 — TRADE MANAGEMENT (Every 310 Seconds)

Every time aggregator fires, `paper_engine.update(current_price)` runs:

```
─────────────────────────────────────────────────────────────────────
SCENARIO A — Nothing happened

   current_price = 73,800
   SL not hit    (74,925 not touched)
   TP1 not hit   (73,146 not touched)
   No events
   Terminal: Action:UPDATE Dir:SHORT → position live, price 73800

SCENARIO B — TP1 Hit

   current_price = 73,100
   TP1 = 73,146 → price passed through TP1

   paper_engine.partial_close(fraction=0.70, reason="TP1")
      Close 70% of 0.013 BTC = 0.009 BTC at 73,100
      PnL on partial = (74,000 - 73,100) × 0.009 = +$8.10
      Remaining position = 0.004 BTC

   After TP1:
      Move SL to breakeven (entry price = 74,000)
      Activate trailing stop at 73,100 × (1 + 0.008) = 73,685
      (0.8% above current price for short)
      Place TP2 limit at 72,635

   Journal records partial close row

SCENARIO C — Trailing Stop Hit After TP1

   Price bounces from 73,100 back to 73,700
   Trailing stop = 73,685 → hit

   paper_engine.close_position(reason="TRAIL", exit_price=73,685)
      Close remaining 0.004 BTC at 73,685
      PnL on trail = (74,000 - 73,685) × 0.004 = +$1.26

   Total trade PnL = +$8.10 + $1.26 = +$9.36
   New balance = $5,000 + $9.36 = $5,009.36

SCENARIO D — SL Hit (loss)

   Price moves up to 74,925 (SL level)

   paper_engine.close_position(reason="SL", exit_price=74,925)
      Close all 0.013 BTC at 74,925
      PnL = (74,000 - 74,925) × 0.013 = -$12.02
      New balance = $5,000 - $12.02 = $4,987.98

SCENARIO E — Thesis Invalidation (immediate exit)

   OI drops 12%+ while in trade
   OR family_a_score flips to ≥ +2 (regime turned bullish)
   OR funding z-score crosses -1.5 (funding flipped against short)

   paper_engine.close_position(reason="THESIS_BREAK", exit_price=current)
      Exit at whatever price is current — no waiting for SL/TP
      This overrides everything

   Terminal prints:
   THESIS BREAK: OI dropped 14% — conviction gone — exiting SHORT
```

---

## PHASE 9 — TRADE JOURNAL ENTRY

After every complete close, one row written to:
`data/processed/trade_journal.csv`

Example row for the SHORT trade above:

```
trade_id            : a3f8c2d1-...  (uuid)
entry_time          : 2026-04-14T20:00:00+00:00
exit_time           : 2026-04-14T21:30:00+00:00
side                : SHORT
entry_price         : 74000.00
exit_price          : 73685.00
quantity            : 0.013
pnl_usdt            : +9.3600
pnl_pct             : +0.9360
exit_reason         : TRAIL
sl_price            : 74925.00
tp1_price           : 73146.00
tp2_price           : 72635.00
tp1_hit             : True
entry_total_score   : -5
entry_family_a      : -2
entry_family_b      : -3
s1_score            : 0
s2_score            : -1
s3_score            : 0
s4_score            : -1
s5_score            : -1
s6_score            : -1
s7_score            : -1
s1_zscore           : 1.24
s4_ls_ratio         : 0.612
s6_cvd              : -89898716.52
s7_taker_ratio      : 0.281
s3_cluster_distance : -854.00
balance_before      : 5000.00
balance_after       : 5009.36
drawdown_at_entry   : 0.00
drawdown_at_exit    : 0.00
regime              : 1
hour_of_day         : 20
day_of_week         : 1
```

---

## COMPLETE FLOW SUMMARY

```
python aegis/alpha/aggregator.py
         │
         ▼
T+0:00  3 WebSocket streams start (S3 S5 S6)
         │
         ▼
T+50:00 Warmup complete — all signals ready
         │
         ▼
Every 310 seconds:
         │
         ├─→ aggregate() fetches all 7 signals + OHLCV
         │         REST signals use TTL cache (S1=8H, S2/S4/S7=5M)
         │         WebSocket signals always live (S3 30M cache, S5 S6 live)
         │
         ├─→ Writes feature row to aegis_features.csv (label empty)
         │
         ├─→ manager.process(feature_row)
         │         ↓
         │    can_trade()?
         │         ↓ NO → print SKIP + reason → wait 310s
         │         ↓ YES
         │    get_direction() — vote system (need ≥4 votes + B trigger)
         │         ↓ NONE → print SKIP → wait 310s
         │         ↓ LONG or SHORT
         │    compute_levels() — entry, SL, TP1, TP2 from clusters + 4H swing
         │         ↓ SL out of range → print SKIP → wait 310s
         │         ↓ levels valid
         │    compute_size() — Family B veto sizing table
         │         ↓
         │    open_position() → paper_engine → paper_state.json
         │         ↓
         │    print OPEN + all trade details
         │
         ├─→ If position open → update(current_price)
         │         Check SL / TP1 / TP2 / trailing stop / thesis break
         │         On close → write to trade_journal.csv
         │
         └─→ Wait 310 seconds → repeat
```





## 📂 Dataset

The historical OHLCV market data used in this project is not stored in this repository due to its large size.

You can download the dataset from the link below:

👉 https://drive.google.com/drive/folders/1z5gRYZGtlJCsPr_PR6H-fiXatbBdKYNf?usp=sharing