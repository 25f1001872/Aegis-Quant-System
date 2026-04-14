"""
Risk Metrics
============
Performance and risk metrics computed from trade history and equity curve.
All metrics are used in the dashboard and backtesting evaluation.
"""

from __future__ import annotations
import math
import logging
from typing import Optional
import numpy as np

logger = logging.getLogger(__name__)


def sharpe_ratio(
    returns: list[float],
    risk_free_rate: float = 0.0,
    annualize: bool = True,
    periods_per_year: int = 35040,   # 15M bars in a year (365 * 24 * 4)
) -> float:
    """
    Sharpe ratio on a series of per-bar returns.
    periods_per_year=35040 assumes 15M bars (Family B timeframe).
    """
    if len(returns) < 2:
        return 0.0
    r = np.array(returns)
    excess = r - (risk_free_rate / periods_per_year)
    std = np.std(excess, ddof=1)
    if std == 0:
        return 0.0
    mean = np.mean(excess)
    raw = mean / std
    return float(raw * math.sqrt(periods_per_year)) if annualize else float(raw)


def sortino_ratio(
    returns: list[float],
    risk_free_rate: float = 0.0,
    periods_per_year: int = 35040,
) -> float:
    """Like Sharpe but only penalizes downside volatility."""
    if len(returns) < 2:
        return 0.0
    r = np.array(returns)
    excess = r - (risk_free_rate / periods_per_year)
    downside = excess[excess < 0]
    if len(downside) == 0:
        return float("inf")
    downside_std = np.std(downside, ddof=1)
    if downside_std == 0:
        return 0.0
    return float((np.mean(excess) / downside_std) * math.sqrt(periods_per_year))


def max_drawdown(equity_curve: list[float]) -> tuple[float, int, int]:
    """
    Returns (max_drawdown_pct, peak_idx, trough_idx).
    max_drawdown_pct is a positive number (e.g. 0.15 = 15% drawdown).
    """
    if not equity_curve:
        return 0.0, 0, 0

    peak = equity_curve[0]
    peak_idx = 0
    max_dd = 0.0
    trough_idx = 0
    current_peak_idx = 0

    for i, val in enumerate(equity_curve):
        if val > peak:
            peak = val
            current_peak_idx = i
        dd = (peak - val) / peak
        if dd > max_dd:
            max_dd = dd
            peak_idx = current_peak_idx
            trough_idx = i

    return max_dd, peak_idx, trough_idx


def calmar_ratio(
    total_return_pct: float,
    max_dd_pct: float,
    years: float = 1.0,
) -> float:
    """Annual return / max drawdown. > 1 is acceptable, > 2 is good."""
    annualized_return = (1 + total_return_pct) ** (1 / years) - 1 if years > 0 else 0
    if max_dd_pct == 0:
        return float("inf")
    return annualized_return / max_dd_pct


def win_rate(pnls: list[float]) -> float:
    if not pnls:
        return 0.0
    return sum(1 for p in pnls if p > 0) / len(pnls)


def profit_factor(pnls: list[float]) -> float:
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss   = abs(sum(p for p in pnls if p < 0))
    if gross_loss == 0:
        return float("inf")
    return gross_profit / gross_loss


def average_rr(pnls: list[float], dollar_risks: list[float]) -> float:
    """Average realized R:R across all trades."""
    if not pnls or len(pnls) != len(dollar_risks):
        return 0.0
    rrs = [p / r for p, r in zip(pnls, dollar_risks) if r > 0]
    return float(np.mean(rrs)) if rrs else 0.0


def expectancy(pnls: list[float]) -> float:
    """Average PnL per trade. Positive = edge exists."""
    if not pnls:
        return 0.0
    return float(np.mean(pnls))


def var_95(returns: list[float], equity: float) -> float:
    """
    Value at Risk at 95% confidence.
    Returns the dollar loss expected to not be exceeded 95% of the time.
    """
    if len(returns) < 20:
        return 0.0
    r = np.array(returns)
    percentile_5 = np.percentile(r, 5)
    return abs(percentile_5 * equity)


def cvar_95(returns: list[float], equity: float) -> float:
    """
    Conditional VaR (Expected Shortfall) at 95%.
    Average loss in the worst 5% of cases.
    """
    if len(returns) < 20:
        return 0.0
    r = np.array(returns)
    cutoff = np.percentile(r, 5)
    tail = r[r <= cutoff]
    if len(tail) == 0:
        return 0.0
    return abs(np.mean(tail) * equity)


def full_report(
    equity_curve: list[float],
    pnls: list[float],
    dollar_risks: list[float],
    starting_capital: float,
    years: float = 1.0,
) -> dict:
    """
    Generate a complete performance report dict.
    Ready for dashboard rendering or logging.
    """
    returns = []
    for i in range(1, len(equity_curve)):
        if equity_curve[i - 1] != 0:
            returns.append((equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1])

    dd, peak_i, trough_i = max_drawdown(equity_curve)
    total_ret = (equity_curve[-1] - starting_capital) / starting_capital if equity_curve else 0

    return {
        "total_return_pct":    round(total_ret * 100, 2),
        "sharpe_ratio":        round(sharpe_ratio(returns), 3),
        "sortino_ratio":       round(sortino_ratio(returns), 3),
        "calmar_ratio":        round(calmar_ratio(total_ret, dd, years), 3),
        "max_drawdown_pct":    round(dd * 100, 2),
        "max_dd_peak_idx":     peak_i,
        "max_dd_trough_idx":   trough_i,
        "win_rate_pct":        round(win_rate(pnls) * 100, 1),
        "profit_factor":       round(profit_factor(pnls), 2),
        "avg_rr":              round(average_rr(pnls, dollar_risks), 2),
        "expectancy_usd":      round(expectancy(pnls), 2),
        "total_trades":        len(pnls),
        "var_95_usd":          round(var_95(returns, equity_curve[-1] if equity_curve else 0), 2),
        "cvar_95_usd":         round(cvar_95(returns, equity_curve[-1] if equity_curve else 0), 2),
    }