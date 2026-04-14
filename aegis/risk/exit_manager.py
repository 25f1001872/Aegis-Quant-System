"""
Exit Manager
============
Monitors open positions every 15M bar and decides when to exit.

Three exit types, in priority order:
  1. THESIS INVALIDATION — immediate exit, doesn't wait for price
     - OI collapses while in trade
     - Funding rate regime flips
     - Score drops to ≤ 2 on next 4H check
  2. STOP LOSS — price hits SL level
  3. TAKE PROFIT — price hits TP1 (partial) then TP2 (remainder)

Never override a thesis invalidation with "but the price looks fine."
The thesis IS the edge. When it's gone, the trade has no reason to exist.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from config.risk_params import (
    OI_COLLAPSE_THRESHOLD,
    FUNDING_REGIME_FLIP,
    SCORE_REDUCTION_THRESHOLD,
    FUNDING_ZSCORE_NEUTRAL_LOW,
    FUNDING_ZSCORE_NEUTRAL_HIGH,
)

logger = logging.getLogger(__name__)


class ExitReason(Enum):
    NONE           = "none"
    STOP_LOSS      = "stop_loss"
    TAKE_PROFIT_1  = "take_profit_1"
    TAKE_PROFIT_2  = "take_profit_2"
    OI_COLLAPSE    = "thesis_oi_collapse"
    FUNDING_FLIP   = "thesis_funding_regime_flip"
    SCORE_DROP     = "thesis_score_drop"
    MANUAL         = "manual"


@dataclass
class ExitDecision:
    should_exit: bool
    reason: ExitReason
    exit_fraction: float        # 0.0 to 1.0 — fraction of position to close
    urgency: str                # "immediate" | "next_bar" | "limit"
    message: str = ""

    @property
    def is_full_exit(self) -> bool:
        return self.exit_fraction >= 1.0

    @property
    def is_thesis_break(self) -> bool:
        return self.reason in (
            ExitReason.OI_COLLAPSE,
            ExitReason.FUNDING_FLIP,
            ExitReason.SCORE_DROP,
        )


@dataclass
class PositionContext:
    """Current state of an open position. Pass this to ExitManager each bar."""
    trade_id: str
    direction: str              # "long" | "short"

    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: Optional[float]

    current_price: float
    current_high: float         # current bar high
    current_low: float          # current bar low

    # Trade state
    tp1_hit: bool = False       # True once TP1 has been executed
    position_btc_remaining: float = 0.0

    # Live signal readings (updated each 4H / each bar)
    current_score: int = 7
    entry_oi: float = 0.0       # OI at entry (in USD)
    current_oi: float = 0.0     # current OI (in USD)
    entry_funding_zscore: float = 0.0
    current_funding_zscore: float = 0.0


class ExitManager:
    """
    Evaluates exit conditions for open positions.

    Usage:
        em = ExitManager()
        decision = em.evaluate(position_ctx)

        if decision.should_exit:
            if decision.is_thesis_break:
                # Market order, close immediately
                execute_market_order(decision.exit_fraction)
            else:
                execute_limit_or_market(decision)
    """

    def evaluate(self, ctx: PositionContext) -> ExitDecision:
        """
        Evaluate all exit conditions for a position.
        Called every 15M bar.
        Returns the highest-priority exit decision.
        """

        # ── Priority 1: Thesis invalidation ──────────────────────────
        thesis_exit = self._check_thesis_invalidation(ctx)
        if thesis_exit.should_exit:
            return thesis_exit

        # ── Priority 2: Stop loss ─────────────────────────────────────
        sl_exit = self._check_stop_loss(ctx)
        if sl_exit.should_exit:
            return sl_exit

        # ── Priority 3: Take profit ───────────────────────────────────
        tp_exit = self._check_take_profit(ctx)
        if tp_exit.should_exit:
            return tp_exit

        return ExitDecision(
            should_exit=False,
            reason=ExitReason.NONE,
            exit_fraction=0.0,
            urgency="none",
            message="Hold — no exit condition triggered",
        )

    # ─────────────────────────────────────────────────────────────────
    # THESIS INVALIDATION CHECKS
    # ─────────────────────────────────────────────────────────────────

    def _check_thesis_invalidation(self, ctx: PositionContext) -> ExitDecision:

        # 1. OI Collapse
        if ctx.entry_oi > 0 and ctx.current_oi > 0:
            oi_change = (ctx.current_oi - ctx.entry_oi) / ctx.entry_oi
            if oi_change <= -OI_COLLAPSE_THRESHOLD:
                msg = (
                    f"OI collapsed {oi_change*100:.1f}% since entry "
                    f"(from ${ctx.entry_oi/1e6:.1f}M to ${ctx.current_oi/1e6:.1f}M). "
                    f"Conviction gone — exit immediately."
                )
                logger.warning(f"[ExitManager] THESIS BREAK (OI): {msg}")
                return ExitDecision(
                    should_exit=True,
                    reason=ExitReason.OI_COLLAPSE,
                    exit_fraction=1.0,
                    urgency="immediate",
                    message=msg,
                )

        # 2. Funding rate regime flip
        if FUNDING_REGIME_FLIP:
            entry_z  = ctx.entry_funding_zscore
            current_z = ctx.current_funding_zscore

            regime_flipped = False
            if ctx.direction == "long":
                # We entered on negative funding (short squeeze setup)
                # Flip = funding gone positive (longs now paying, trade reversal)
                if entry_z < FUNDING_ZSCORE_NEUTRAL_LOW and current_z > FUNDING_ZSCORE_NEUTRAL_HIGH:
                    regime_flipped = True
            else:
                # We entered on positive funding (fade longs setup)
                # Flip = funding gone negative (shorts now paying)
                if entry_z > FUNDING_ZSCORE_NEUTRAL_HIGH and current_z < FUNDING_ZSCORE_NEUTRAL_LOW:
                    regime_flipped = True

            if regime_flipped:
                msg = (
                    f"Funding Z-score regime flip: {entry_z:.2f} → {current_z:.2f}. "
                    f"The crowd positioning that justified this trade no longer exists."
                )
                logger.warning(f"[ExitManager] THESIS BREAK (Funding): {msg}")
                return ExitDecision(
                    should_exit=True,
                    reason=ExitReason.FUNDING_FLIP,
                    exit_fraction=1.0,
                    urgency="immediate",
                    message=msg,
                )

        # 3. Score drop (checked on 4H update)
        if ctx.current_score <= SCORE_REDUCTION_THRESHOLD:
            msg = (
                f"Signal score dropped to {ctx.current_score}/7. "
                f"Family A thesis no longer supported — exit."
            )
            logger.warning(f"[ExitManager] THESIS BREAK (Score): {msg}")
            return ExitDecision(
                should_exit=True,
                reason=ExitReason.SCORE_DROP,
                exit_fraction=1.0,
                urgency="next_bar",
                message=msg,
            )

        return ExitDecision(
            should_exit=False, reason=ExitReason.NONE,
            exit_fraction=0.0, urgency="none"
        )

    # ─────────────────────────────────────────────────────────────────
    # STOP LOSS CHECK
    # ─────────────────────────────────────────────────────────────────

    def _check_stop_loss(self, ctx: PositionContext) -> ExitDecision:
        sl_hit = False
        if ctx.direction == "long" and ctx.current_low <= ctx.sl_price:
            sl_hit = True
        elif ctx.direction == "short" and ctx.current_high >= ctx.sl_price:
            sl_hit = True

        if sl_hit:
            msg = (
                f"Stop loss hit at ${ctx.sl_price:,.2f}. "
                f"Price {'low' if ctx.direction == 'long' else 'high'}: "
                f"${ctx.current_low if ctx.direction == 'long' else ctx.current_high:,.2f}"
            )
            logger.info(f"[ExitManager] SL: {msg}")
            return ExitDecision(
                should_exit=True,
                reason=ExitReason.STOP_LOSS,
                exit_fraction=1.0,
                urgency="immediate",
                message=msg,
            )

        return ExitDecision(
            should_exit=False, reason=ExitReason.NONE,
            exit_fraction=0.0, urgency="none"
        )

    # ─────────────────────────────────────────────────────────────────
    # TAKE PROFIT CHECKS
    # ─────────────────────────────────────────────────────────────────

    def _check_take_profit(self, ctx: PositionContext) -> ExitDecision:

        # TP1 — 70% of position
        if not ctx.tp1_hit:
            tp1_hit = False
            if ctx.direction == "long" and ctx.current_high >= ctx.tp1_price:
                tp1_hit = True
            elif ctx.direction == "short" and ctx.current_low <= ctx.tp1_price:
                tp1_hit = True

            if tp1_hit:
                msg = (
                    f"TP1 reached at ${ctx.tp1_price:,.2f}. "
                    f"Closing 70% of position. Moving SL to entry."
                )
                logger.info(f"[ExitManager] TP1: {msg}")
                return ExitDecision(
                    should_exit=True,
                    reason=ExitReason.TAKE_PROFIT_1,
                    exit_fraction=0.70,
                    urgency="limit",
                    message=msg,
                )

        # TP2 — remaining 30% (only after TP1 has been hit)
        if ctx.tp1_hit and ctx.tp2_price is not None:
            tp2_hit = False
            if ctx.direction == "long" and ctx.current_high >= ctx.tp2_price:
                tp2_hit = True
            elif ctx.direction == "short" and ctx.current_low <= ctx.tp2_price:
                tp2_hit = True

            if tp2_hit:
                msg = (
                    f"TP2 reached at ${ctx.tp2_price:,.2f}. "
                    f"Closing remaining 30% of position."
                )
                logger.info(f"[ExitManager] TP2: {msg}")
                return ExitDecision(
                    should_exit=True,
                    reason=ExitReason.TAKE_PROFIT_2,
                    exit_fraction=1.0,     # 100% of remaining
                    urgency="limit",
                    message=msg,
                )

        return ExitDecision(
            should_exit=False, reason=ExitReason.NONE,
            exit_fraction=0.0, urgency="none"
        )

    # ─────────────────────────────────────────────────────────────────
    # TRAILING STOP (post-TP1)
    # ─────────────────────────────────────────────────────────────────

    def compute_trailing_sl(
        self,
        ctx: PositionContext,
        trail_pct: float = 0.008,   # 0.8% trail after TP1
    ) -> float:
        """
        After TP1 is hit, trail the SL behind price.
        Returns the new SL price. Caller is responsible for updating position.
        """
        if ctx.direction == "long":
            new_sl = ctx.current_price * (1 - trail_pct)
            return max(new_sl, ctx.entry_price)     # Never trail below entry (free ride)
        else:
            new_sl = ctx.current_price * (1 + trail_pct)
            return min(new_sl, ctx.entry_price)     # Never trail above entry