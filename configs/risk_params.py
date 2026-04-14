"""
AEGIS Risk Parameters
=====================
Single source of truth for all risk management decisions.
All thresholds, limits, and sizing rules are defined here.
Change a number here → it propagates everywhere.

Decisions rationale documented inline.
"""

# ─────────────────────────────────────────────
# CAPITAL
# ─────────────────────────────────────────────

STARTING_CAPITAL_USD = 10_000  # Paper trading start. Scale only after 30+ validated live trades.

# ─────────────────────────────────────────────
# RISK PER TRADE
# ─────────────────────────────────────────────
# Classic 1% rule. At $10k → $100 max loss per trade.
# SL distance determines actual position size, not a fixed lot.
# Formula: position_size_btc = (capital * RISK_PER_TRADE) / sl_distance_usd

RISK_PER_TRADE = 0.01          # 1% of current equity per trade
MAX_RISK_PER_TRADE_USD = 200   # Hard cap in USD even if 1% exceeds this (safety net)

# ─────────────────────────────────────────────
# LEVERAGE
# ─────────────────────────────────────────────
# BTC perps allow up to 125x. We cap at 3x during validation phase.
# Rationale: With liquidation cluster signals you don't need extreme leverage.
# The edge comes from precision, not size.

MAX_LEVERAGE = 3               # Hard ceiling. Paper trading: start at 1x.
DEFAULT_LEVERAGE = 1           # Used until system is validated over 30+ trades.

# ─────────────────────────────────────────────
# SCORE-BASED POSITION SIZING
# Score 0-7 from signal aggregator. Maps to % of MAX_POSITION_SIZE.
# ─────────────────────────────────────────────

SCORE_SIZE_MAP = {
    0: 0.00,   # No trade
    1: 0.00,   # No trade
    2: 0.00,   # No trade — minimum is 3 signals
    3: 0.25,   # Quarter size — weak confirmation only
    4: 0.50,   # Half size — Family A partial + Family B partial
    5: 0.75,   # Three-quarter size — strong setup
    6: 1.00,   # Full size — near-perfect alignment
    7: 1.00,   # Full size — 7/7 alignment (no extra leverage until live validated)
}

# ─────────────────────────────────────────────
# POSITION LIMITS
# ─────────────────────────────────────────────

MAX_POSITION_SIZE_PCT = 0.20   # Never more than 20% of equity in one position
MAX_OPEN_POSITIONS = 1         # One trade at a time during validation phase
MAX_POSITION_BTC = 0.5         # Hard BTC cap regardless of account size

# ─────────────────────────────────────────────
# STOP LOSS RULES
# ─────────────────────────────────────────────
# SL is placed beyond the last 4H swing high (for shorts) or swing low (for longs).
# These bounds prevent overly tight (noise) or overly wide (too much risk) SLs.

MIN_SL_PCT = 0.005             # 0.5% — tighter than this = noise
MAX_SL_PCT = 0.03              # 3.0% — wider than this = skip the trade (position size too small)
ATR_MULTIPLIER_SL = 1.5        # Fallback: SL = 1.5x ATR if swing level not computable

# ─────────────────────────────────────────────
# TAKE PROFIT RULES
# ─────────────────────────────────────────────
# TP is placed JUST BEFORE the nearest liquidation cluster in trade direction.
# "Just before" = cluster_price ∓ CLUSTER_BUFFER_PCT

CLUSTER_BUFFER_PCT = 0.002     # 0.2% before cluster (don't sit in the impact zone)
MIN_REWARD_TO_RISK = 1.5       # If TP gives < 1.5:1 R:R → skip the trade
TARGET_REWARD_TO_RISK = 2.0    # Ideal R:R target

# Partial exit: take 70% off at TP1, trail remaining 30% to TP2 (next cluster)
TP1_SIZE_PCT = 0.70
TP2_SIZE_PCT = 0.30

# ─────────────────────────────────────────────
# THESIS INVALIDATION (Early Exit Triggers)
# These override the SL — exit immediately regardless of price.
# ─────────────────────────────────────────────

# If OI drops this much while in a trade, conviction is gone → exit
OI_COLLAPSE_THRESHOLD = 0.12   # 12% OI drop triggers early exit

# If funding Z-score crosses zero from opposing direction mid-trade → regime change → exit
FUNDING_REGIME_FLIP = True

# If 4H score drops to this or below on next check → reduce or exit
SCORE_REDUCTION_THRESHOLD = 2  # Score ≤ 2 → exit

# ─────────────────────────────────────────────
# CIRCUIT BREAKERS (Daily / Weekly Loss Caps)
# ─────────────────────────────────────────────

DAILY_LOSS_LIMIT_PCT  = 0.02   # -2% in a single day → halt all trading for the day
WEEKLY_LOSS_LIMIT_PCT = 0.05   # -5% in a week → halt all trading for the week
MAX_CONSECUTIVE_LOSSES = 3     # 3 losses in a row → pause and review thesis

# ─────────────────────────────────────────────
# TRADE FREQUENCY CONTROLS
# ─────────────────────────────────────────────

MIN_BARS_BETWEEN_TRADES = 4    # Minimum 4 × 15M bars (1 hour) between entries
NO_TRADE_WINDOW_MINS = 30      # No entry within 30 min of macro event
MAX_TRADES_PER_DAY = 6         # Hard cap on daily trade count

# ─────────────────────────────────────────────
# SIGNAL THRESHOLDS (mirrors AEGIS doc)
# ─────────────────────────────────────────────

# Family A
FUNDING_ZSCORE_BULLISH = -2.0
FUNDING_ZSCORE_BEARISH = +2.0
FUNDING_ZSCORE_NEUTRAL_LOW  = -1.0
FUNDING_ZSCORE_NEUTRAL_HIGH = +1.0

OI_DELTA_LOOKBACK_BARS = 1     # Compare current 4H bar to previous

LS_RATIO_BULLISH  = 0.35       # < 35% long = extreme fear = bullish
LS_RATIO_BEARISH  = 0.70       # > 70% long = crowded = bearish
LS_RATIO_NEUTRAL_LOW  = 0.40
LS_RATIO_NEUTRAL_HIGH = 0.60

LIQ_CLUSTER_WINDOW_DAYS  = 7   # Rolling 7-day liquidation history
LIQ_CLUSTER_BUCKET_USD   = 200 # $200 price bins for cluster map
LIQ_CLUSTER_MIN_SIZE_USD = 1_000_000  # Only count clusters ≥ $1M

# Family B
OFI_WINDOW_MINS   = 15
OFI_STRONG_THRESHOLD_USD = 2_000_000  # ±$2M OFI = strong signal

CVD_DIVERGENCE_LOOKBACK  = 3   # 3 candles to detect divergence
CVD_DIVERGENCE_THRESHOLD = 0.05  # 5% divergence ratio

TAKER_RATIO_BULLISH = 0.60     # > 60% taker buys = bullish
TAKER_RATIO_BEARISH = 0.40     # < 40% taker buys = bearish
TAKER_RATIO_NEUTRAL_LOW  = 0.40
TAKER_RATIO_NEUTRAL_HIGH = 0.60

# Minimum Family B signals required before entry
MIN_FAMILY_B_SIGNALS = 2

# ─────────────────────────────────────────────
# FEES & COST MODEL (Binance Futures)
# ─────────────────────────────────────────────

MAKER_FEE = 0.0002             # 0.02% maker
TAKER_FEE = 0.0004             # 0.04% taker (using market orders = taker)
ESTIMATED_SLIPPAGE = 0.0001    # 0.01% slippage estimate for BTC at normal volume
FUNDING_RATE_PERIOD_HOURS = 8  # Funding settles every 8 hours

# ─────────────────────────────────────────────
# EXCHANGE
# ─────────────────────────────────────────────

EXCHANGE = "binance"
SYMBOL   = "BTCUSDT"
ASSET    = "BTC"
QUOTE    = "USDT"
TIMEFRAME_FAMILY_A = "4h"
TIMEFRAME_FAMILY_B = "15m"