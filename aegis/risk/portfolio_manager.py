"""
Portfolio Manager
=================
Tracks account state and enforces all portfolio-level guards.
This is the gatekeeper — every trade request must pass through here
before touching the position sizer or execution layer.

Guards enforced:
  - Daily loss limit
  - Weekly loss limit
  - Consecutive loss streak
  - Max open positions
  - Max trades per day
  - Minimum bars between trades
  - Macro event no-trade window
"""

from __future__ import annotations
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from configs.risk_params import (
    STARTING_CAPITAL_USD,
    DAILY_LOSS_LIMIT_PCT,
    WEEKLY_LOSS_LIMIT_PCT,
    MAX_CONSECUTIVE_LOSSES,
    MAX_OPEN_POSITIONS,
    MAX_TRADES_PER_DAY,
    MIN_BARS_BETWEEN_TRADES,
    NO_TRADE_WINDOW_MINS,
    TIMEFRAME_FAMILY_B,
)

logger = logging.getLogger(__name__)

# 15M bars → each bar = 15 minutes
BAR_DURATION_MINS = 15


@dataclass
class TradeRecord:
    trade_id: str
    direction: str
    entry_time: datetime
    entry_price: float
    position_btc: float
    dollar_risk: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    status: str = "open"    # "open" | "closed_tp" | "closed_sl" | "closed_thesis" | "closed_manual"


@dataclass
class PortfolioState:
    equity: float
    peak_equity: float
    open_positions: int
    trades_today: int
    daily_pnl: float
    weekly_pnl: float
    consecutive_losses: int
    last_trade_time: Optional[datetime]
    halted: bool
    halt_reason: str


class PortfolioManager:
    """
    Central account state manager. All trading decisions route through here.

    Usage:
        pm = PortfolioManager(starting_capital=10_000)

        # Before placing any trade:
        allowed, reason = pm.can_trade(current_time)
        if not allowed:
            print(f"Trade blocked: {reason}")

        # After trade closes:
        pm.record_trade_close(trade_id, exit_price, exit_time)
    """

    def __init__(
        self,
        starting_capital: float = STARTING_CAPITAL_USD,
        macro_events: Optional[list[datetime]] = None,
    ):
        self.equity = starting_capital
        self.peak_equity = starting_capital
        self.starting_capital = starting_capital

        self._open_trades: dict[str, TradeRecord] = {}
        self._closed_trades: list[TradeRecord] = []
        self._macro_events: list[datetime] = macro_events or []

        # Rolling windows
        self._day_start_equity: float = starting_capital
        self._week_start_equity: float = starting_capital
        self._day_start: datetime = self._today_utc()
        self._week_start: datetime = self._this_week_utc()

        self._consecutive_losses: int = 0
        self._last_trade_bar: int = -999   # bar index of last entry
        self._current_bar: int = 0

        self._halted: bool = False
        self._halt_reason: str = ""
        self._halt_until: Optional[datetime] = None

        logger.info(f"[PortfolioManager] Initialized. Capital: ${starting_capital:,.2f}")

    # ─────────────────────────────────────────────────────────────────
    # CORE GATE: can_trade()
    # Every entry attempt must pass this before reaching the sizer.
    # ─────────────────────────────────────────────────────────────────

    def can_trade(self, current_time: datetime) -> tuple[bool, str]:
        """
        Returns (True, "") if trading is allowed.
        Returns (False, reason) if blocked.
        """
        self._refresh_daily_weekly(current_time)

        # Hard halt check
        if self._halted:
            if self._halt_until and current_time < self._halt_until:
                remaining = self._halt_until - current_time
                return False, f"System halted: {self._halt_reason}. Resumes in {remaining}"
            else:
                self._lift_halt()

        # Max open positions
        if len(self._open_trades) >= MAX_OPEN_POSITIONS:
            return False, f"Max open positions ({MAX_OPEN_POSITIONS}) reached"

        # Max trades per day
        daily_count = self._count_trades_today(current_time)
        if daily_count >= MAX_TRADES_PER_DAY:
            return False, f"Max daily trades ({MAX_TRADES_PER_DAY}) reached"

        # Daily loss limit
        daily_pnl_pct = (self.equity - self._day_start_equity) / self._day_start_equity
        if daily_pnl_pct <= -DAILY_LOSS_LIMIT_PCT:
            reason = f"Daily loss limit hit ({daily_pnl_pct*100:.2f}%)"
            self._trigger_halt(reason, resume_hours=24)
            return False, reason

        # Weekly loss limit
        weekly_pnl_pct = (self.equity - self._week_start_equity) / self._week_start_equity
        if weekly_pnl_pct <= -WEEKLY_LOSS_LIMIT_PCT:
            reason = f"Weekly loss limit hit ({weekly_pnl_pct*100:.2f}%)"
            self._trigger_halt(reason, resume_hours=168)  # 1 week
            return False, reason

        # Consecutive loss streak
        if self._consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            reason = f"{self._consecutive_losses} consecutive losses — manual review required"
            self._trigger_halt(reason, resume_hours=None)  # manual lift only
            return False, reason

        # Minimum bars between trades
        bars_since_last = self._current_bar - self._last_trade_bar
        if bars_since_last < MIN_BARS_BETWEEN_TRADES:
            mins_remaining = (MIN_BARS_BETWEEN_TRADES - bars_since_last) * BAR_DURATION_MINS
            return False, f"Too soon after last trade — wait {mins_remaining} more minutes"

        # Macro event window
        for event_time in self._macro_events:
            window_start = event_time - timedelta(minutes=NO_TRADE_WINDOW_MINS)
            window_end   = event_time + timedelta(minutes=NO_TRADE_WINDOW_MINS)
            if window_start <= current_time <= window_end:
                return False, f"Inside macro event no-trade window: {event_time}"

        return True, ""

    # ─────────────────────────────────────────────────────────────────
    # TRADE LIFECYCLE
    # ─────────────────────────────────────────────────────────────────

    def record_trade_open(
        self,
        trade_id: str,
        direction: str,
        entry_price: float,
        position_btc: float,
        dollar_risk: float,
        entry_time: datetime,
        bar_index: int,
    ) -> None:
        self._open_trades[trade_id] = TradeRecord(
            trade_id=trade_id,
            direction=direction,
            entry_time=entry_time,
            entry_price=entry_price,
            position_btc=position_btc,
            dollar_risk=dollar_risk,
        )
        self._last_trade_bar = bar_index
        logger.info(f"[Portfolio] Trade opened: {trade_id} | {direction.upper()} | {position_btc:.4f} BTC @ ${entry_price:,.2f}")

    def record_trade_close(
        self,
        trade_id: str,
        exit_price: float,
        exit_time: datetime,
        status: str = "closed_manual",
    ) -> float:
        """
        Closes a trade, updates equity, and returns the PnL.
        status: "closed_tp" | "closed_sl" | "closed_thesis" | "closed_manual"
        """
        if trade_id not in self._open_trades:
            logger.error(f"[Portfolio] Trade {trade_id} not found in open trades")
            return 0.0

        trade = self._open_trades.pop(trade_id)
        trade.exit_price = exit_price
        trade.exit_time = exit_time
        trade.status = status

        # PnL calculation (excluding fees — fees handled in execution layer)
        if trade.direction == "long":
            pnl = (exit_price - trade.entry_price) * trade.position_btc
        else:
            pnl = (trade.entry_price - exit_price) * trade.position_btc

        trade.pnl = pnl
        self._closed_trades.append(trade)

        # Update equity
        self.equity += pnl
        self.peak_equity = max(self.peak_equity, self.equity)

        # Update streak
        if pnl < 0:
            self._consecutive_losses += 1
            logger.warning(f"[Portfolio] Loss #{self._consecutive_losses}: ${pnl:.2f}")
        else:
            self._consecutive_losses = 0

        logger.info(
            f"[Portfolio] Trade closed: {trade_id} | {status} | "
            f"PnL: ${pnl:+.2f} | Equity: ${self.equity:,.2f}"
        )
        return pnl

    def update_bar(self, bar_index: int) -> None:
        """Call at the start of each 15M bar."""
        self._current_bar = bar_index

    def add_macro_event(self, event_time: datetime) -> None:
        """Register an upcoming macro event (FOMC, CPI, etc.)."""
        self._macro_events.append(event_time)
        logger.info(f"[Portfolio] Macro event registered: {event_time}")

    # ─────────────────────────────────────────────────────────────────
    # STATE QUERIES
    # ─────────────────────────────────────────────────────────────────

    def get_state(self, current_time: Optional[datetime] = None) -> PortfolioState:
        now = current_time or datetime.now(timezone.utc)
        daily_pnl  = self.equity - self._day_start_equity
        weekly_pnl = self.equity - self._week_start_equity
        return PortfolioState(
            equity=self.equity,
            peak_equity=self.peak_equity,
            open_positions=len(self._open_trades),
            trades_today=self._count_trades_today(now),
            daily_pnl=daily_pnl,
            weekly_pnl=weekly_pnl,
            consecutive_losses=self._consecutive_losses,
            last_trade_time=self._last_trade_time(),
            halted=self._halted,
            halt_reason=self._halt_reason,
        )

    def current_drawdown_pct(self) -> float:
        """Current drawdown from peak as a positive percentage."""
        if self.peak_equity == 0:
            return 0.0
        return (self.peak_equity - self.equity) / self.peak_equity

    def total_return_pct(self) -> float:
        return (self.equity - self.starting_capital) / self.starting_capital

    def win_rate(self) -> float:
        closed = [t for t in self._closed_trades if t.pnl is not None]
        if not closed:
            return 0.0
        wins = sum(1 for t in closed if t.pnl > 0)
        return wins / len(closed)

    def profit_factor(self) -> float:
        closed = [t for t in self._closed_trades if t.pnl is not None]
        gross_profit = sum(t.pnl for t in closed if t.pnl > 0)
        gross_loss   = abs(sum(t.pnl for t in closed if t.pnl < 0))
        if gross_loss == 0:
            return float("inf")
        return gross_profit / gross_loss

    def summary(self) -> dict:
        return {
            "equity": round(self.equity, 2),
            "starting_capital": self.starting_capital,
            "total_return_pct": round(self.total_return_pct() * 100, 2),
            "peak_equity": round(self.peak_equity, 2),
            "current_drawdown_pct": round(self.current_drawdown_pct() * 100, 2),
            "open_positions": len(self._open_trades),
            "total_trades": len(self._closed_trades),
            "win_rate": round(self.win_rate() * 100, 1),
            "profit_factor": round(self.profit_factor(), 2),
            "consecutive_losses": self._consecutive_losses,
            "halted": self._halted,
            "halt_reason": self._halt_reason,
        }

    # ─────────────────────────────────────────────────────────────────
    # INTERNAL HELPERS
    # ─────────────────────────────────────────────────────────────────

    def _trigger_halt(self, reason: str, resume_hours: Optional[int]) -> None:
        self._halted = True
        self._halt_reason = reason
        if resume_hours:
            self._halt_until = datetime.now(timezone.utc) + timedelta(hours=resume_hours)
            logger.critical(f"[Portfolio] HALT triggered: {reason} | Auto-resumes in {resume_hours}h")
        else:
            self._halt_until = None
            logger.critical(f"[Portfolio] HALT triggered: {reason} | Manual lift required")

    def _lift_halt(self) -> None:
        logger.info(f"[Portfolio] Halt lifted (was: {self._halt_reason})")
        self._halted = False
        self._halt_reason = ""
        self._halt_until = None
        self._consecutive_losses = 0

    def manual_lift_halt(self) -> None:
        """Call this after manual review when consecutive losses triggered halt."""
        self._lift_halt()

    def _refresh_daily_weekly(self, current_time: datetime) -> None:
        today = self._today_utc(current_time)
        this_week = self._this_week_utc(current_time)

        if today > self._day_start:
            self._day_start = today
            self._day_start_equity = self.equity

        if this_week > self._week_start:
            self._week_start = this_week
            self._week_start_equity = self.equity

    def _count_trades_today(self, current_time: datetime) -> int:
        today = self._today_utc(current_time)
        return sum(
            1 for t in self._closed_trades
            if t.entry_time.date() >= today.date()
        ) + len(self._open_trades)

    def _last_trade_time(self) -> Optional[datetime]:
        all_times = [t.entry_time for t in self._closed_trades] + \
                    [t.entry_time for t in self._open_trades.values()]
        return max(all_times) if all_times else None

    @staticmethod
    def _today_utc(dt: Optional[datetime] = None) -> datetime:
        now = dt or datetime.now(timezone.utc)
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _this_week_utc(dt: Optional[datetime] = None) -> datetime:
        now = dt or datetime.now(timezone.utc)
        monday = now - timedelta(days=now.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)