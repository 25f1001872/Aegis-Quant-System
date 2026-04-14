"""
Tests for the risk management module.
Run with: pytest tests/test_risk/ -v
"""

import pytest
from datetime import datetime, timezone, timedelta

from configs.risk_params import STARTING_CAPITAL_USD
from aegis.risk.position_sizer import PositionSizer
from aegis.risk.portfolio_manager import PortfolioManager
from aegis.risk.exit_manager import ExitManager, ExitReason, PositionContext


# ─────────────────────────────────────────────────────────────────────
# POSITION SIZER TESTS
# ─────────────────────────────────────────────────────────────────────

class TestPositionSizer:

    def setup_method(self):
        self.sizer = PositionSizer(equity=10_000, leverage=1)

    def test_valid_long_setup(self):
        setup = self.sizer.compute(
            direction="long",
            score=6,
            entry_price=95_000,
            sl_price=93_500,       # 1.58% SL — within 0.5%-3% range
            cluster_tp1=98_000,
            cluster_tp2=101_000,
        )
        assert setup.valid
        assert setup.direction == "long"
        assert setup.position_btc > 0
        assert setup.dollar_risk <= 100 * 1.0   # 1% of 10k = $100, ×1.0 score mult
        assert setup.rr_ratio >= 1.5

    def test_valid_short_setup(self):
        setup = self.sizer.compute(
            direction="short",
            score=7,
            entry_price=95_000,
            sl_price=96_500,       # SL above entry for short
            cluster_tp1=92_000,
            cluster_tp2=89_000,
        )
        assert setup.valid
        assert setup.direction == "short"
        assert setup.sl_price > setup.entry_price

    def test_score_too_low_rejected(self):
        setup = self.sizer.compute(
            direction="long", score=2,
            entry_price=95_000, sl_price=93_500,
            cluster_tp1=98_000,
        )
        assert not setup.valid
        assert "score" in setup.rejection_reason.lower()

    def test_sl_too_tight_rejected(self):
        setup = self.sizer.compute(
            direction="long", score=6,
            entry_price=95_000,
            sl_price=94_960,    # 0.04% — way too tight
            cluster_tp1=98_000,
        )
        assert not setup.valid
        assert "tight" in setup.rejection_reason.lower()

    def test_sl_too_wide_rejected(self):
        setup = self.sizer.compute(
            direction="long", score=6,
            entry_price=95_000,
            sl_price=90_000,    # 5.26% — too wide
            cluster_tp1=100_000,
        )
        assert not setup.valid
        assert "wide" in setup.rejection_reason.lower()

    def test_rr_too_low_rejected(self):
        setup = self.sizer.compute(
            direction="long", score=6,
            entry_price=95_000,
            sl_price=93_500,    # SL: $1500 away
            cluster_tp1=96_000, # TP1: $1000 away → R:R = 0.67 < 1.5
        )
        assert not setup.valid
        assert "r:r" in setup.rejection_reason.lower()

    def test_sl_wrong_side_rejected(self):
        setup = self.sizer.compute(
            direction="long", score=6,
            entry_price=95_000,
            sl_price=96_000,    # SL above entry = wrong for long
            cluster_tp1=98_000,
        )
        assert not setup.valid

    def test_tp1_tp2_split(self):
        setup = self.sizer.compute(
            direction="long", score=6,
            entry_price=95_000, sl_price=93_500,
            cluster_tp1=98_000, cluster_tp2=101_000,
        )
        assert setup.valid
        assert abs(setup.tp1_btc / setup.position_btc - 0.70) < 0.01
        assert abs(setup.tp2_btc / setup.position_btc - 0.30) < 0.01

    def test_score_3_quarter_size(self):
        setup_score3 = self.sizer.compute(
            direction="long", score=3,
            entry_price=95_000, sl_price=93_500, cluster_tp1=98_000,
        )
        setup_score6 = self.sizer.compute(
            direction="long", score=6,
            entry_price=95_000, sl_price=93_500, cluster_tp1=98_000,
        )
        assert setup_score3.valid and setup_score6.valid
        # Score 3 should be ~25% the risk of score 6
        assert abs(setup_score3.dollar_risk / setup_score6.dollar_risk - 0.25) < 0.01


# ─────────────────────────────────────────────────────────────────────
# PORTFOLIO MANAGER TESTS
# ─────────────────────────────────────────────────────────────────────

class TestPortfolioManager:

    def setup_method(self):
        self.pm = PortfolioManager(starting_capital=10_000)
        self.now = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)

    def test_initial_state_allows_trading(self):
        allowed, reason = self.pm.can_trade(self.now)
        assert allowed, f"Should be allowed but got: {reason}"

    def test_daily_loss_limit_triggers_halt(self):
        # Simulate a -2.5% day
        self.pm.equity = 10_000 * 0.975   # -2.5%
        self.pm._day_start_equity = 10_000
        allowed, reason = self.pm.can_trade(self.now)
        assert not allowed
        assert "daily" in reason.lower()

    def test_consecutive_losses_trigger_halt(self):
        self.pm._consecutive_losses = 3
        allowed, reason = self.pm.can_trade(self.now)
        assert not allowed
        assert "consecutive" in reason.lower()

    def test_max_open_positions_blocks_entry(self):
        # Open one position (max is 1)
        self.pm.record_trade_open(
            "T001", "long", 95000, 0.01, 100,
            self.now, bar_index=100
        )
        allowed, reason = self.pm.can_trade(self.now)
        assert not allowed
        assert "positions" in reason.lower()

    def test_min_bars_between_trades(self):
        self.pm._last_trade_bar = 100
        self.pm._current_bar = 102   # only 2 bars since last trade (need 4)
        allowed, reason = self.pm.can_trade(self.now)
        assert not allowed
        assert "soon" in reason.lower()

    def test_trade_open_close_updates_equity(self):
        initial_equity = self.pm.equity
        self.pm.record_trade_open("T001", "long", 95000, 0.01, 100, self.now, 100)
        pnl = self.pm.record_trade_close("T001", 97000, self.now + timedelta(hours=2), "closed_tp")
        expected_pnl = (97000 - 95000) * 0.01
        assert abs(pnl - expected_pnl) < 0.01
        assert abs(self.pm.equity - (initial_equity + expected_pnl)) < 0.01

    def test_winning_trade_resets_loss_streak(self):
        self.pm._consecutive_losses = 2
        self.pm.record_trade_open("T001", "long", 95000, 0.01, 100, self.now, 100)
        self.pm.record_trade_close("T001", 97000, self.now, "closed_tp")
        assert self.pm._consecutive_losses == 0

    def test_macro_event_blocks_trading(self):
        event = self.now + timedelta(minutes=15)
        self.pm.add_macro_event(event)
        # 10 minutes before event = inside window
        allowed, reason = self.pm.can_trade(self.now + timedelta(minutes=5))
        assert not allowed
        assert "macro" in reason.lower()

    def test_summary_returns_expected_keys(self):
        summary = self.pm.summary()
        assert "equity" in summary
        assert "win_rate" in summary
        assert "profit_factor" in summary
        assert "total_return_pct" in summary


# ─────────────────────────────────────────────────────────────────────
# EXIT MANAGER TESTS
# ─────────────────────────────────────────────────────────────────────

class TestExitManager:

    def setup_method(self):
        self.em = ExitManager()
        self.base_ctx = PositionContext(
            trade_id="T001",
            direction="long",
            entry_price=95_000,
            sl_price=93_500,
            tp1_price=97_800,    # cluster at 98k minus 0.2% buffer
            tp2_price=100_800,   # cluster at 101k minus 0.2% buffer
            current_price=96_000,
            current_high=96_200,
            current_low=95_800,
            tp1_hit=False,
            position_btc_remaining=0.01,
            current_score=6,
            entry_oi=1_000_000_000,
            current_oi=1_000_000_000,
            entry_funding_zscore=-2.2,
            current_funding_zscore=-2.2,
        )

    def test_no_exit_in_normal_conditions(self):
        decision = self.em.evaluate(self.base_ctx)
        assert not decision.should_exit

    def test_sl_hit_triggers_exit(self):
        ctx = self.base_ctx
        ctx.current_low = 93_400   # below SL
        decision = self.em.evaluate(ctx)
        assert decision.should_exit
        assert decision.reason == ExitReason.STOP_LOSS
        assert decision.exit_fraction == 1.0
        assert decision.urgency == "immediate"

    def test_tp1_hit_triggers_partial_exit(self):
        ctx = self.base_ctx
        ctx.current_high = 98_100  # above TP1
        decision = self.em.evaluate(ctx)
        assert decision.should_exit
        assert decision.reason == ExitReason.TAKE_PROFIT_1
        assert abs(decision.exit_fraction - 0.70) < 0.01

    def test_tp2_only_after_tp1(self):
        ctx = self.base_ctx
        ctx.tp1_hit = False
        ctx.current_high = 101_500  # above TP2
        decision = self.em.evaluate(ctx)
        # Should trigger TP1 first, not TP2
        assert decision.reason == ExitReason.TAKE_PROFIT_1

    def test_oi_collapse_triggers_thesis_exit(self):
        ctx = self.base_ctx
        ctx.current_oi = 800_000_000   # -20% collapse
        decision = self.em.evaluate(ctx)
        assert decision.should_exit
        assert decision.reason == ExitReason.OI_COLLAPSE
        assert decision.is_thesis_break

    def test_score_drop_triggers_thesis_exit(self):
        ctx = self.base_ctx
        ctx.current_score = 2
        decision = self.em.evaluate(ctx)
        assert decision.should_exit
        assert decision.reason == ExitReason.SCORE_DROP

    def test_funding_flip_triggers_thesis_exit(self):
        ctx = self.base_ctx
        # Entered on very negative funding (short squeeze setup)
        ctx.entry_funding_zscore = -2.5
        # Funding has now gone very positive (regime completely reversed)
        ctx.current_funding_zscore = 1.5
        decision = self.em.evaluate(ctx)
        assert decision.should_exit
        assert decision.reason == ExitReason.FUNDING_FLIP

    def test_thesis_break_priority_over_sl(self):
        """Thesis invalidation should be checked before SL."""
        ctx = self.base_ctx
        ctx.current_oi = 700_000_000   # OI collapse
        ctx.current_low = 93_000       # SL also hit
        decision = self.em.evaluate(ctx)
        # Thesis break should take priority
        assert decision.reason == ExitReason.OI_COLLAPSE

    def test_trailing_sl_never_below_entry(self):
        ctx = self.base_ctx
        ctx.current_price = 94_000   # price moved against us after TP1
        new_sl = self.em.compute_trailing_sl(ctx, trail_pct=0.008)
        assert new_sl >= ctx.entry_price   # never trail below entry

    def test_short_sl_above_entry(self):
        ctx = self.base_ctx
        ctx.direction = "short"
        ctx.sl_price = 96_500
        ctx.current_high = 96_600   # above SL for short
        decision = self.em.evaluate(ctx)
        assert decision.should_exit
        assert decision.reason == ExitReason.STOP_LOSS