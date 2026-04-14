"""
Position Sizer
==============
Converts signal score + stop-loss distance → position size in BTC.

Core formula:
    dollar_risk   = equity × RISK_PER_TRADE × score_multiplier
    position_btc  = dollar_risk / sl_distance_usd

This means your SL placement determines your size — not the other way around.
Never size first and then find a SL to fit it.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Optional

from config.risk_params import (
    RISK_PER_TRADE,
    MAX_RISK_PER_TRADE_USD,
    SCORE_SIZE_MAP,
    MAX_POSITION_SIZE_PCT,
    MAX_POSITION_BTC,
    MAX_LEVERAGE,
    DEFAULT_LEVERAGE,
    MIN_SL_PCT,
    MAX_SL_PCT,
    ATR_MULTIPLIER_SL,
    MIN_REWARD_TO_RISK,
    CLUSTER_BUFFER_PCT,
    TP1_SIZE_PCT,
    TP2_SIZE_PCT,
    TAKER_FEE,
    ESTIMATED_SLIPPAGE,
)

logger = logging.getLogger(__name__)


@dataclass
class TradeSetup:
    """Output of the position sizer. Everything needed to place and manage a trade."""
    direction: str              # "long" or "short"
    score: int                  # 0-7 signal score
    score_multiplier: float     # fraction of max risk used

    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: Optional[float]  # second cluster target (may be None)

    sl_pct: float               # SL distance as % from entry
    rr_ratio: float             # reward-to-risk ratio

    position_btc: float         # position size in BTC
    dollar_risk: float          # max $ loss if SL hit
    leverage: float             # leverage applied

    tp1_btc: float              # BTC to close at TP1
    tp2_btc: float              # BTC to close at TP2 (or trail)

    valid: bool                 # False = trade rejected, check rejection_reason
    rejection_reason: str = ""

    def summary(self) -> str:
        if not self.valid:
            return f"REJECTED: {self.rejection_reason}"
        return (
            f"{'LONG' if self.direction == 'long' else 'SHORT'} | "
            f"Score {self.score}/7 | "
            f"Entry ${self.entry_price:,.2f} | "
            f"SL ${self.sl_price:,.2f} ({self.sl_pct*100:.2f}%) | "
            f"TP1 ${self.tp1_price:,.2f} | "
            f"Size {self.position_btc:.4f} BTC | "
            f"Risk ${self.dollar_risk:.2f} | "
            f"R:R {self.rr_ratio:.2f}"
        )


class PositionSizer:
    """
    Computes position size for a given trade setup.

    Usage:
        sizer = PositionSizer(equity=10_000)
        setup = sizer.compute(
            direction="long",
            score=6,
            entry_price=95_000,
            sl_price=93_500,           # below last 4H swing low
            cluster_tp1=98_000,        # nearest liq cluster above
            cluster_tp2=101_000,       # next cluster (optional)
            current_atr=800,           # used as fallback SL validator
        )
        print(setup.summary())
    """

    def __init__(self, equity: float, leverage: float = DEFAULT_LEVERAGE):
        self.equity = equity
        self.leverage = min(leverage, MAX_LEVERAGE)

    def update_equity(self, new_equity: float) -> None:
        """Call after each trade closes. Equity compounds (or shrinks)."""
        self.equity = new_equity
        logger.info(f"Equity updated: ${new_equity:,.2f}")

    def compute(
        self,
        direction: str,
        score: int,
        entry_price: float,
        sl_price: float,
        cluster_tp1: float,
        cluster_tp2: Optional[float] = None,
        current_atr: Optional[float] = None,
    ) -> TradeSetup:
        """
        Main computation. Returns a TradeSetup — check .valid before placing.

        Parameters
        ----------
        direction    : "long" or "short"
        score        : aggregator score 0-7
        entry_price  : expected fill price (use current mid-price)
        sl_price     : stop-loss price (beyond last 4H swing)
        cluster_tp1  : first liquidation cluster price (just before it)
        cluster_tp2  : second cluster price for trailing 30% (optional)
        current_atr  : current 4H ATR value for fallback SL validation
        """
        assert direction in ("long", "short"), "direction must be 'long' or 'short'"

        # ── 1. Score check ─────────────────────────────────────────────
        score_mult = SCORE_SIZE_MAP.get(score, 0.0)
        if score_mult == 0.0:
            return self._reject(score, "Score too low — minimum score is 3")

        # ── 2. SL validation ───────────────────────────────────────────
        sl_distance_usd = abs(entry_price - sl_price)
        sl_pct = sl_distance_usd / entry_price

        if sl_pct < MIN_SL_PCT:
            return self._reject(score, f"SL too tight ({sl_pct*100:.3f}% < {MIN_SL_PCT*100:.1f}%)")

        if sl_pct > MAX_SL_PCT:
            return self._reject(score, f"SL too wide ({sl_pct*100:.2f}% > {MAX_SL_PCT*100:.1f}%) — position size would be too small")

        # ATR sanity check (optional)
        if current_atr is not None:
            atr_sl = current_atr * ATR_MULTIPLIER_SL
            if sl_distance_usd < atr_sl * 0.5:
                logger.warning(f"SL distance ${sl_distance_usd:.0f} is much tighter than ATR-based SL ${atr_sl:.0f}")

        # ── 3. Direction consistency check ─────────────────────────────
        if direction == "long" and sl_price >= entry_price:
            return self._reject(score, "Long SL must be below entry price")
        if direction == "short" and sl_price <= entry_price:
            return self._reject(score, "Short SL must be above entry price")

        # ── 4. TP with cluster buffer ──────────────────────────────────
        if direction == "long":
            if cluster_tp1 <= entry_price:
                return self._reject(score, "TP1 cluster must be above entry for longs")
            tp1 = cluster_tp1 * (1 - CLUSTER_BUFFER_PCT)   # just before cluster
            tp2 = (cluster_tp2 * (1 - CLUSTER_BUFFER_PCT)) if cluster_tp2 else None
        else:
            if cluster_tp1 >= entry_price:
                return self._reject(score, "TP1 cluster must be below entry for shorts")
            tp1 = cluster_tp1 * (1 + CLUSTER_BUFFER_PCT)   # just before cluster
            tp2 = (cluster_tp2 * (1 + CLUSTER_BUFFER_PCT)) if cluster_tp2 else None

        # ── 5. R:R check ───────────────────────────────────────────────
        tp1_distance = abs(tp1 - entry_price)
        rr = tp1_distance / sl_distance_usd

        if rr < MIN_REWARD_TO_RISK:
            return self._reject(score, f"R:R {rr:.2f} < minimum {MIN_REWARD_TO_RISK}")

        # ── 6. Dollar risk calculation ─────────────────────────────────
        base_risk = self.equity * RISK_PER_TRADE
        scaled_risk = base_risk * score_mult
        dollar_risk = min(scaled_risk, MAX_RISK_PER_TRADE_USD)

        # ── 7. Position size ───────────────────────────────────────────
        position_btc = dollar_risk / sl_distance_usd

        # Apply leverage: buying power = equity × leverage
        buying_power = self.equity * self.leverage
        max_position_from_buying_power = buying_power / entry_price
        max_position_from_pct_cap = (self.equity * MAX_POSITION_SIZE_PCT) / entry_price

        position_btc = min(
            position_btc,
            max_position_from_buying_power,
            max_position_from_pct_cap,
            MAX_POSITION_BTC,
        )

        if position_btc <= 0:
            return self._reject(score, "Computed position size is zero or negative")

        # ── 8. Fee-adjusted minimum size check ─────────────────────────
        # Fees should not exceed 20% of expected dollar risk
        round_trip_fee = position_btc * entry_price * (TAKER_FEE + ESTIMATED_SLIPPAGE) * 2
        if round_trip_fee > dollar_risk * 0.20:
            logger.warning(
                f"Round-trip costs ${round_trip_fee:.2f} are {round_trip_fee/dollar_risk*100:.0f}% "
                f"of dollar risk ${dollar_risk:.2f}. Consider larger position or skip."
            )

        # ── 9. Split into TP1 / TP2 tranches ──────────────────────────
        tp1_btc = round(position_btc * TP1_SIZE_PCT, 6)
        tp2_btc = round(position_btc * TP2_SIZE_PCT, 6)

        logger.info(
            f"[PositionSizer] {direction.upper()} | Score {score}/7 | "
            f"Entry ${entry_price:,.0f} | SL ${sl_price:,.0f} | "
            f"TP1 ${tp1:,.0f} | {position_btc:.4f} BTC | Risk ${dollar_risk:.2f} | R:R {rr:.2f}"
        )

        return TradeSetup(
            direction=direction,
            score=score,
            score_multiplier=score_mult,
            entry_price=entry_price,
            sl_price=sl_price,
            tp1_price=tp1,
            tp2_price=tp2,
            sl_pct=sl_pct,
            rr_ratio=rr,
            position_btc=round(position_btc, 6),
            dollar_risk=round(dollar_risk, 2),
            leverage=self.leverage,
            tp1_btc=tp1_btc,
            tp2_btc=tp2_btc,
            valid=True,
        )

    def _reject(self, score: int, reason: str) -> TradeSetup:
        logger.warning(f"[PositionSizer] Trade rejected: {reason}")
        return TradeSetup(
            direction="", score=score, score_multiplier=0, entry_price=0,
            sl_price=0, tp1_price=0, tp2_price=None, sl_pct=0, rr_ratio=0,
            position_btc=0, dollar_risk=0, leverage=self.leverage,
            tp1_btc=0, tp2_btc=0, valid=False, rejection_reason=reason,
        )