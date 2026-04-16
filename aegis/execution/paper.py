"""
Paper trading engine for Aegis-Quant-System
"""
import os
import json
import logging
import uuid
from datetime import datetime, timezone

journal_path = os.path.join("data", "processed", "trade_journal.csv")
os.makedirs(os.path.dirname(journal_path), exist_ok=True)

class PaperTradeEngine:
    def __init__(self, broker=None):
        self.broker = broker
        self.state_file = os.path.join("data", "artifacts", "paper_state.json")
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        
        self.balance = 10000.0
        self.position = None
        self.open_orders = []
        self.equity_peak = 10000.0
        self.equity_history = [10000.0] * 20 # Seed with initial capital
        self._load_state()

    def _load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    state = json.load(f)
                    self.balance = state.get("balance", 10000.0)
                    self.position = state.get("position", None)
                    self.open_orders = state.get("open_orders", [])
                    self.equity_peak = state.get("equity_peak", 10000.0)
                    self.equity_history = state.get("equity_history", [10000.0] * 20)
            except Exception as e:
                logging.error(f"Failed to load paper state: {str(e)}")

    def _save_state(self):
        state = {
            "balance": self.balance,
            "position": self.position,
            "open_orders": self.open_orders,
            "equity_peak": self.equity_peak,
            "equity_history": self.equity_history
        }
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=4)

    def _write_journal(self, trade_summary: dict):
        cols = [
            "trade_id", "entry_time", "exit_time",
            "side", "entry_price", "exit_price",
            "quantity", "pnl_usdt", "pnl_pct",
            "exit_reason",
            "sl_price", "tp1_price", "tp2_price",
            "tp1_hit",
            "entry_total_score", "entry_family_a", "entry_family_b",
            "s1_score", "s2_score", "s3_score", "s4_score",
            "s5_score", "s6_score", "s7_score",
            "s1_zscore", "s4_ls_ratio", "s6_cvd",
            "s7_taker_ratio", "s3_cluster_distance",
            "balance_before", "balance_after",
            "drawdown_at_entry", "drawdown_at_exit",
            "regime", "hour_of_day", "day_of_week"
        ]
        
        write_header = not os.path.exists(journal_path)
        with open(journal_path, "a", encoding="utf-8") as f:
            if write_header:
                f.write(",".join(cols) + "\n")
            
            row = [str(trade_summary.get(c, "")) for c in cols]
            f.write(",".join(row) + "\n")

    def open_position(self, side, entry_price, quantity, sl, tp1, tp2, tp3, r_unit, score, signal_snapshot) -> dict:
        quantity = round(quantity, 3)
        cost = quantity * entry_price
        
        self.position = {
            "side": side,
            "entry_price": entry_price,
            "quantity": quantity,
            "orig_quantity": quantity, # Store original for % calculations
            "sl_price": sl,
            "tp1_price": tp1,
            "tp2_price": tp2,
            "tp3_price": tp3,
            "r_unit": r_unit,
            "tp1_hit": False,
            "tp2_hit": False,
            "tp3_hit": False,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "trade_id": str(uuid.uuid4()),
            "entry_score": score,
            "entry_signals": signal_snapshot
        }
        
        self.balance -= cost
        self._save_state()
        return self.position

    def update(self,
               current_price: float,
               candle_high: float = None,
               candle_low: float = None) -> list[str]:
        events = []
        if not self.position:
            return events
            
        pos = self.position
        side = pos["side"]

        # Use wicks if available, else fallback to current price
        high = candle_high if candle_high is not None else current_price
        low  = candle_low  if candle_low  is not None else current_price

        # SL Check
        if side == "LONG" and low <= pos["sl_price"]:
            summary = self.close_position("SL", pos["sl_price"])
            events.append(f"Trade closed at SL (wick hit). PnL: {summary['pnl_usdt']:.4f}")
            return events
        elif side == "SHORT" and high >= pos["sl_price"]:
            summary = self.close_position("SL", pos["sl_price"])
            events.append(f"Trade closed at SL (wick hit). PnL: {summary['pnl_usdt']:.4f}")
            return events

        # TP1 Check (30% of Total and move SL to Entry)
        if not pos["tp1_hit"]:
            if (side == "LONG" and high >= pos["tp1_price"]) or \
               (side == "SHORT" and low <= pos["tp1_price"]):
                # Close 30% of original position
                self.partial_close(0.30, "TP1", pos["tp1_price"])
                events.append("TP1 hit (wick). Closed 30%. SL moved to Break-Even.")
                pos["tp1_hit"] = True
                pos["sl_price"] = pos["entry_price"]

        # TP2 Check (Another 30% of Total and move SL to Entry + 1R)
        if pos["tp1_hit"] and not pos["tp2_hit"]:
            if (side == "LONG" and high >= pos["tp2_price"]) or \
               (side == "SHORT" and low <= pos["tp2_price"]):
                # To close 30% of TOTAL when 70% is left: close 30/70 ≈ 0.4285 fraction of current
                fraction = round(0.30 / (pos["quantity"] / pos["orig_quantity"]), 4)
                self.partial_close(fraction, "TP2", pos["tp2_price"])
                events.append("TP2 hit (wick). Closed another 30%. SL moved to lock in 1R.")
                pos["tp2_hit"] = True
                
                # Move SL to Entry + 1R
                if side == "LONG":
                    pos["sl_price"] = pos["entry_price"] + pos["r_unit"]
                else:
                    pos["sl_price"] = pos["entry_price"] - pos["r_unit"]

        # TP3 Check (Final 40%)
        if pos["tp2_hit"]:
            if (side == "LONG" and high >= pos["tp3_price"]) or \
               (side == "SHORT" and low <= pos["tp3_price"]):
                summary = self.close_position("TP3", pos["tp3_price"])
                events.append(f"Trade closed at TP3 (wick). PnL: {summary['pnl_usdt']:.4f}")
                return events

        self._save_state()
        return events

    def partial_close(self, fraction: float, reason: str, exit_price: float) -> dict:
        if not self.position:
            return {}
            
        pos = self.position
        close_qty = round(pos["quantity"] * fraction, 3)
        remain_qty = round(pos["quantity"] - close_qty, 3)
        
        if pos["side"] == "LONG":
            pnl = (exit_price - pos["entry_price"]) * close_qty
        else:
            pnl = (pos["entry_price"] - exit_price) * close_qty

        cost = close_qty * pos["entry_price"]
        self.balance += (cost + pnl)
        
        # Track equity curve
        current_equity = self.get_total_equity()
        self.equity_history.append(current_equity)
        if len(self.equity_history) > 100:
            self.equity_history.pop(0)
        self.equity_peak = max(self.equity_peak, current_equity)

        pos["quantity"] = remain_qty
        self._save_state()

        # Execute real market order if broker is attached
        if self.broker:
            # Internal SL/TP management — NOT SAFE FOR LIVE MONEY
            # For TP order side is opposite to position side
            side = "SELL" if pos["side"] == "LONG" else "BUY"
            try:
                self.broker.place_market_order(side, close_qty)
            except Exception as e:
                logging.error(f"[PaperEngine] Failed to execute real partial exit: {e}")

        return {"reason": reason, "close_qty": close_qty, "pnl": pnl}

    def close_position(self, reason: str, exit_price: float) -> dict:
        if not self.position:
            return {}
            
        pos = self.position
        
        if pos["side"] == "LONG":
            pnl = (exit_price - pos["entry_price"]) * pos["quantity"]
            pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100
        else:
            pnl = (pos["entry_price"] - exit_price) * pos["quantity"]
            pnl_pct = (pos["entry_price"] - exit_price) / pos["entry_price"] * 100

        cost = pos["quantity"] * pos["entry_price"]
        balance_before = self.balance + cost
        
        entry_equity = self.balance + cost
        dd_at_entry = (self.equity_peak - entry_equity) / self.equity_peak * 100 if self.equity_peak > 0 else 0.0

        self.balance += (cost + pnl)

        # Track equity curve
        current_equity = self.get_total_equity()
        self.equity_history.append(current_equity)
        if len(self.equity_history) > 100:
            self.equity_history.pop(0)
        self.equity_peak = max(self.equity_peak, current_equity)

        # Execute real market order if broker is attached
        if self.broker:
            # Internal SL/TP management — NOT SAFE FOR LIVE MONEY
            side = "SELL" if pos["side"] == "LONG" else "BUY"
            try:
                self.broker.place_market_order(side, round(pos["quantity"], 3))
            except Exception as e:
                logging.error(f"[PaperEngine] Failed to execute real full exit: {e}")

        if self.balance > self.equity_peak:
            self.equity_peak = self.balance

        dd_at_exit = self.get_drawdown()
        sig = pos["entry_signals"]
        
        summary = {
            "trade_id": pos["trade_id"],
            "entry_time": pos["entry_time"],
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "side": pos["side"],
            "entry_price": round(pos["entry_price"], 2),
            "exit_price": round(exit_price, 2),
            "quantity": round(pos["quantity"], 3),
            "pnl_usdt": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 4),
            "exit_reason": reason,
            "sl_price": round(pos["sl_price"], 2),
            "tp1_price": round(pos["tp1_price"], 2),
            "tp2_price": round(pos["tp2_price"], 2),
            "tp1_hit": pos["tp1_hit"],
            "entry_total_score": sig.get("total_score", 0),
            "entry_family_a": sig.get("family_a_score", 0),
            "entry_family_b": sig.get("family_b_score", 0),
            "s1_score": sig.get("s1_score", 0),
            "s2_score": sig.get("s2_score", 0),
            "s3_score": sig.get("s3_score", 0),
            "s4_score": sig.get("s4_score", 0),
            "s5_score": sig.get("s5_score", 0),
            "s6_score": sig.get("s6_score", 0),
            "s7_score": sig.get("s7_score", 0),
            "s1_zscore": sig.get("s1_zscore", 0.0),
            "s4_ls_ratio": sig.get("s4_ls_ratio", 0.0),
            "s6_cvd": sig.get("s6_cvd", 0.0),
            "s7_taker_ratio": sig.get("s7_taker_ratio", 0.0),
            "s3_cluster_distance": sig.get("s3_cluster_distance", 0.0),
            "balance_before": round(balance_before, 2),
            "balance_after": round(self.balance, 2),
            "drawdown_at_entry": round(dd_at_entry, 2), 
            "drawdown_at_exit": round(dd_at_exit, 2),
            "regime": sig.get("regime", 0),
            "hour_of_day": sig.get("hour_of_day", 0),
            "day_of_week": sig.get("day_of_week", 0),
        }

        self._write_journal(summary)
        self.position = None
        self._save_state()
        return summary

    def get_balance(self) -> float:
        total_equity = self.balance
        if self.position:
            total_equity += (self.position["quantity"] * self.position["entry_price"])
        return total_equity

    def get_position(self) -> dict | None:
        return self.position

    def get_drawdown(self) -> float:
        current_equity = self.get_balance()
        if self.equity_peak <= 0:
            return 0.0
        dd = (self.equity_peak - current_equity) / self.equity_peak * 100
        return max(0.0, dd)

    def get_total_equity(self) -> float:
        equity = self.balance
        if self.position:
            # Full valuation including unrealized pnl
            current_price = self.broker.get_current_price() if self.broker else self.position["entry_price"]
            pos = self.position
            pnl = (current_price - pos["entry_price"]) * pos["quantity"] if pos["side"] == "LONG" else (pos["entry_price"] - current_price) * pos["quantity"]
            equity += (pos["quantity"] * pos["entry_price"] + pnl)
        return equity

    def get_risk_stats(self) -> dict:
        """Computes rolling performance metrics for advanced sizing."""
        current_equity = self.get_total_equity()
        
        # Drawdown Fraction
        self.equity_peak = max(self.equity_peak, current_equity)
        drawdown_fraction = (self.equity_peak - current_equity) / self.equity_peak if self.equity_peak > 0 else 0
        
        # Equity EMA(20) approximation
        if len(self.equity_history) >= 5:
            ema_20 = sum(self.equity_history[-20:]) / len(self.equity_history[-20:])
        else:
            ema_20 = current_equity

        # Default stats for new bots
        win_rate = 0.50
        payoff_ratio = 1.0
        
        if os.path.exists(journal_path):
            try:
                import pandas as pd
                df = pd.read_csv(journal_path)
                if len(df) >= 3:
                    recent = df.tail(30)
                    wins = recent[recent['pnl_usdt'] > 0]
                    losses = recent[recent['pnl_usdt'] <= 0]
                    
                    if len(recent) > 0:
                        win_rate = len(wins) / len(recent)
                        
                    avg_win = wins['pnl_usdt'].mean() if not wins.empty else 0
                    avg_loss = abs(losses['pnl_usdt'].mean()) if not losses.empty else 0
                    
                    if avg_loss > 0:
                        payoff_ratio = avg_win / avg_loss
            except Exception:
                pass 

        return {
            "max_drawdown_fraction": drawdown_fraction,
            "equity_ema_20": ema_20,
            "current_equity": current_equity,
            "win_rate": win_rate,
            "payoff_ratio": payoff_ratio
        }
