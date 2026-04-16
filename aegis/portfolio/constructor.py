"""
Portfolio manager for Aegis-Quant-System
"""

# Production TP Ladder — R-multiple optimized
# 30/30/40 distribution
# Designed for fat-tail crypto expansion

# ── DEMO MODE ─────────────────────────────────────────────────────
# True  → forces a trade entry on raw value lean alone
#          used for demonstration only — bypasses score thresholds
#          set False for real paper trading
# False → full production logic — all gates must pass
DEMO_MODE = True

import os
import logging

log_dir = os.path.join("logs")
os.makedirs(log_dir, exist_ok=True)
portfolio_logger = logging.getLogger("portfolio")
portfolio_logger.setLevel(logging.INFO)
fh = logging.FileHandler(os.path.join(log_dir, "portfolio.log"))
formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
fh.setFormatter(formatter)
if not portfolio_logger.handlers:
    portfolio_logger.addHandler(fh)

class PortfolioManager:
    def __init__(self, broker, paper_engine, config: dict):
        self.broker = broker
        self.paper_engine = paper_engine
        self.config = config
        self.max_position_pct = float(self.config.get("max_position_pct", 0.20))
        self.last_bull_votes = 0
        self.last_bear_votes = 0
        self._last_direction   = None   # direction of last decided trade
        self._direction_count  = 0      # how many consecutive cycles same direction

    def can_trade(self, feature_row: dict) -> bool:
        if self.paper_engine.get_position() is not None:
            return False
            
        if self.paper_engine.get_drawdown() > 8.0:
            return False
            
            
        if not DEMO_MODE:
            if feature_row.get("family_b_score", 0) == 0:
                return False
            
        return True

    def get_direction(self, feature_row: dict) -> str:
        s1_zscore = feature_row.get("s1_zscore", 0.0)
        s2_score = feature_row.get("s2_score", 0)
        s3_dominant_side = feature_row.get("s3_dominant_side", "")
        s3_cluster_distance = feature_row.get("s3_cluster_distance", 0.0)
        s4_ls_ratio = feature_row.get("s4_ls_ratio", 0.5)
        s6_cvd = feature_row.get("s6_cvd", 0.0)
        s7_taker_ratio = feature_row.get("s7_taker_ratio", 0.5)
        family_b_score = feature_row.get("family_b_score", 0)

        bull_votes = 0
        bear_votes = 0

        # Bull votes
        if s1_zscore < -0.8: bull_votes += 1
        if s2_score == 1: bull_votes += 1
        if s3_dominant_side == "short" and s3_cluster_distance > 0: bull_votes += 1
        if s4_ls_ratio < 0.45: bull_votes += 1
        if s6_cvd > 0: bull_votes += 1
        if s7_taker_ratio > 0.52: bull_votes += 1

        # Bear votes
        if s1_zscore > 0.8: bear_votes += 1
        if s2_score == -1: bear_votes += 1
        if s3_dominant_side == "long" and s3_cluster_distance < 0: bear_votes += 1
        if s4_ls_ratio > 0.55: bear_votes += 1
        if s6_cvd < 0: bear_votes += 1
        if s7_taker_ratio < 0.48: bear_votes += 1

        self.last_bull_votes = bull_votes
        self.last_bear_votes = bear_votes

        if DEMO_MODE:
            # In demo mode use minimum vote threshold
            # and do not require family_b confirmation
            # Just go with whichever side has more raw votes
            # Minimum 3 votes required to avoid 50/50 coin flip
            if bull_votes >= 3 and bull_votes > bear_votes:
                return "LONG"
            if bear_votes >= 3 and bear_votes > bull_votes:
                return "SHORT"
            # If tied or neither reaches 3 votes
            # default to the direction CVD is pointing
            # CVD is the most reliable raw value signal
            if s6_cvd > 0:
                return "LONG"
            return "SHORT"
        else:
            if bull_votes >= 4 and family_b_score > 0:
                return "LONG"
            if bear_votes >= 4 and family_b_score < 0:
                return "SHORT"
            return "NONE"

    def compute_levels(self, direction: str, feature_row: dict) -> dict | None:
        try:
            current_price = self.broker.get_current_price()
        except Exception:
            return None

        s3_nearest_cluster_usd = feature_row.get("s3_nearest_cluster_usd", 0.0)
        s3_dominant_side = feature_row.get("s3_dominant_side", "")

        if direction == "LONG":
            entry = current_price
            try:
                swing = self.broker.get_last_4h_swing("LONG")
            except Exception:
                return None
            sl = swing * (1 - 0.001)
            
            # Compute R (Risk Unit)
            R = abs(entry - sl)
            
            # R-Multiple TP Ladder
            tp1 = entry + (1.2 * R)
            tp2 = entry + (2.0 * R)
            tp3 = entry + (3.5 * R)

        elif direction == "SHORT":
            entry = current_price
            try:
                swing = self.broker.get_last_4h_swing("SHORT")
            except Exception:
                return None
            sl = swing * (1 + 0.001)

            # Compute R (Risk Unit)
            R = abs(entry - sl)

            # R-Multiple TP Ladder
            tp1 = entry - (1.2 * R)
            tp2 = entry - (2.0 * R)
            tp3 = entry - (3.5 * R)
        else:
            return None

        sl_pct = abs(entry - sl) / entry * 100
        if sl_pct < 0.5 or sl_pct > 3.0:
            return None

        return {
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tp1": round(tp1, 2),
            "tp2": round(tp2, 2),
            "tp3": round(tp3, 2),
            "R": round(R, 2)
        }

    def compute_size(self, direction: str, entry: float, feature_row: dict) -> tuple[float, float]:
        stats = self.paper_engine.get_risk_stats()
        
        # Factor 1: Quarter-Kelly Sizing
        # formula: K% = W - (1-W)/R
        w = stats["win_rate"]
        r = stats["payoff_ratio"]
        kelly_full = max(0, w - (1 - w) / r) if r > 0 else 0
        kelly_factor = max(0.5, kelly_full * 0.25) # Floor at 0.5 to stay in game
        
        # Factor 2: Equity Curve Scaling (20-EMA Filter)
        equity_ema = stats["equity_ema_20"]
        current_equity = stats["current_equity"]
        equity_factor = 1.0 if current_equity >= equity_ema else 0.5
        
        # Factor 3: Drawdown Throttle (Threshold: 5%)
        # Linear reduction from 1.0 to 0.2 as DD goes 5% -> 20%
        dd = stats["max_drawdown_fraction"]
        if dd <= 0.05:
            dd_factor = 1.0
        else:
            dd_factor = max(0.2, 1.0 - ((dd - 0.05) / 0.15))

        # Factor 4: Volatility Scaling (ATR Adjusted)
        # Scale size inversely to relative volatility
        # BTC baseline ATR logic
        current_atr = feature_row.get("atr_15m", 0)
        avg_atr = feature_row.get("volatility_15m", current_atr) # Use volatility field as proxy for mean
        if current_atr > 0 and avg_atr > 0:
            vol_factor = avg_atr / current_atr
            vol_factor = max(0.4, min(1.3, vol_factor)) # Clamp to avoid extreme swings
        else:
            vol_factor = 1.0

        # Composite Risk Multiplier
        risk_multiplier = kelly_factor * equity_factor * dd_factor * vol_factor
        risk_multiplier = max(0.1, min(1.5, risk_multiplier)) # Safe Floor 10%
        
        # Base Sizing
        if DEMO_MODE:
            max_position = stats["current_equity"] * 0.20
            base_size_multiplier = 0.15
        else:
            family_b = abs(feature_row.get("family_b_score", 0))
            total_score = abs(feature_row.get("total_score", 0))
            
            if family_b >= 3 and total_score >= 5: base_size_multiplier = 1.0
            elif family_b >= 2 and total_score >= 4: base_size_multiplier = 0.75
            elif family_b >= 2 and total_score >= 3: base_size_multiplier = 0.50
            elif family_b >= 1 and total_score >= 2: base_size_multiplier = 0.25
            else: base_size_multiplier = 0.15
            
            max_position = stats["current_equity"] * self.max_position_pct

        final_pos_usdt = max_position * base_size_multiplier * risk_multiplier
        quantity = round(final_pos_usdt / entry, 3)
        
        if quantity < 0.001:
            return 0.0, risk_multiplier
            
        return quantity, risk_multiplier

    def process(self, feature_row: dict) -> dict:
        action = "SKIP"
        reason = "Neutral"
        direction = "NONE"
        entry = None
        quantity = None
        sl = None
        tp1 = None
        risk_factor = 1.0
        
        # Step 7 — Update open position
        try:
            current_price = self.broker.get_current_price()
            events = self.paper_engine.update(
                current_price=current_price,
                candle_high=feature_row.get("candle_high", current_price),
                candle_low=feature_row.get("candle_low", current_price)
            )
            for event in events:
                portfolio_logger.info(event)
        except Exception as e:
            portfolio_logger.error(f"Failed to fetch price for update: {e}")
            current_price = 0.0

        # Build shared state for UI/aggregator
        pos = self.paper_engine.get_position()
        if pos:
            pnl_pct = (current_price - pos['entry_price'])/pos['entry_price']*100 if pos['side']=='LONG' else (pos['entry_price']-current_price)/pos['entry_price']*100
            result = {
                "action": "HOLD",
                "direction": pos["side"],
                "total_score": feature_row.get("total_score", 0),
                "family_b_score": feature_row.get("family_b_score", 0),
                "bull_votes": feature_row.get("bull_votes", 0),
                "bear_votes": feature_row.get("bear_votes", 0),
                "reason": f"Holding {pos['side']} | Entry:{pos['entry_price']} | SL:{pos['sl_price']} | TP1:{pos['tp1_price']}({'✔' if pos['tp1_hit'] else ' '}) | TP2:{pos['tp2_price']}({'✔' if pos['tp2_hit'] else ' '}) | TP3:{pos['tp3_price']}({'✔' if pos['tp3_hit'] else ' '}) | Current:{current_price} | PnL:{pnl_pct:.3f}%",
                "tp1_price": pos["tp1_price"],
                "tp2_price": pos["tp2_price"],
                "tp3_price": pos["tp3_price"],
                "tp1_hit": pos["tp1_hit"],
                "tp2_hit": pos["tp2_hit"],
                "tp3_hit": pos["tp3_hit"],
                "sl_price": pos["sl_price"],
                "risk_factor": 1.0 # Holder
            }
        else:
            result = {
                "action": action,
                "direction": direction,
                "total_score": feature_row.get("total_score", 0),
                "family_b_score": feature_row.get("family_b_score", 0),
                "bull_votes": feature_row.get("bull_votes", 0),
                "bear_votes": feature_row.get("bear_votes", 0),
                "reason": reason,
                "tp1_hit": False,
                "tp2_hit": False,
                "tp3_hit": False,
                "risk_factor": 1.0
            }
        if pos and current_price > 0:
            s2_oi_current = feature_row.get("s2_oi_current", 0.0)
            s2_oi_delta = feature_row.get("s2_oi_delta", 0.0)
            family_a_score = feature_row.get("family_a_score", 0)
            s1_zscore = feature_row.get("s1_zscore", 0.0)
            
            invalidation_reason = None
            if s2_oi_current > 0 and (s2_oi_delta < -0.12 * s2_oi_current):
                invalidation_reason = "OI dropped 12% rapidly"
            elif family_a_score <= -2 and pos["side"] == "LONG":
                invalidation_reason = "Regime flipped bearish for LONG"
            elif family_a_score >= 2 and pos["side"] == "SHORT":
                invalidation_reason = "Regime flipped bullish for SHORT"
            elif s1_zscore > 1.5 and pos["side"] == "LONG":
                invalidation_reason = "Funding flipped against LONG"
            elif s1_zscore < -1.5 and pos["side"] == "SHORT":
                invalidation_reason = "Funding flipped against SHORT"

            if invalidation_reason:
                summary = self.paper_engine.close_position("THESIS_BREAK", current_price)
                portfolio_logger.info(f"Thesis invalidated: {invalidation_reason}. PnL: {summary.get('pnl_usdt', 0.0)}")
                action = "CLOSE"
                reason = invalidation_reason

        # Trade Entry Logic
        if not pos and action != "CLOSE":
            if not self.can_trade(feature_row):
                reason = "Blocked by pre-trade checks (DD, Hours, Regime, or No Signal)"
            else:
                direction = self.get_direction(feature_row)
                
                if direction == "NONE":
                    reason = "Not enough clear directional votes"
                else:
                    # ── Direction stability check ──────────────────────────────────────
                    # Require the same direction for 2 consecutive 310s cycles
                    # before opening a trade in DEMO_MODE
                    # Prevents opening on a single-cycle signal that immediately flips
                    if DEMO_MODE:
                        if direction == self._last_direction:
                            self._direction_count += 1
                        else:
                            self._last_direction  = direction
                            self._direction_count = 1
                            return {
                                "action"        : "SKIP",
                                "direction"     : direction,
                                "total_score"   : feature_row.get("total_score", 0),
                                "family_b_score": feature_row.get("family_b_score", 0),
                                "bull_votes"    : self.last_bull_votes,
                                "bear_votes"    : self.last_bear_votes,
                                "entry_price"   : None,
                                "sl_price"      : None,
                                "tp1_price"     : None,
                                "quantity"      : None,
                                "reason"        : f"DEMO: {direction} direction seen once — waiting for confirmation next cycle"
                            }
                        # Direction confirmed for 2 cycles — proceed to open
                        self._direction_count = 0
                        self._last_direction  = None

                    levels = self.compute_levels(direction, feature_row)
                    if not levels:
                        reason = "SL out of bounds or levels uncomputable"
                    else:
                        entry = levels["entry"]
                        sl = levels["sl"]
                        tp1 = levels["tp1"]
                        tp2 = levels["tp2"]
                        tp3 = levels["tp3"]
                        
                        quantity, risk_factor = self.compute_size(direction, entry, feature_row)
                        if quantity == 0:
                            reason = f"Position size calculated as 0 (Risk Factor: {risk_factor:.2x})"
                        else:
                            # Step 6 — Execute
                            self.paper_engine.open_position(
                                side=direction,
                                entry_price=entry,
                                quantity=quantity,
                                sl=sl,
                                tp1=tp1,
                                tp2=tp2,
                                tp3=tp3,
                                r_unit=levels["R"],
                                score=feature_row.get("total_score", 0),
                                signal_snapshot=feature_row
                            )
                            
                            side = "BUY" if direction == "LONG" else "SELL"
                            opposite_side = "SELL" if direction == "LONG" else "BUY"
                            
                            is_paper = True
                            try:
                                from aegis.execution.broker import PAPER_MODE
                                is_paper = PAPER_MODE
                            except ImportError:
                                pass
                                
                            if not is_paper:
                                # Entry order only — Testnet does not support STOP orders
                                # SL and TP will be handled internally by PaperTradeEngine.update().
                                # Internal SL/TP management — NOT SAFE FOR LIVE MONEY
                                self.broker.place_market_order(side, quantity)

                            action = "OPEN"
                            reason = f"Entered {direction} at {entry}"

        if action == "SKIP" and pos:
            action = "UPDATE"
            reason = f"Holding {pos['side']} at {pos['entry_price']}"

        return {
            "action": action,
            "direction": direction if direction != "NONE" else None,
            "total_score": feature_row.get("total_score", 0),
            "family_b_score": feature_row.get("family_b_score", 0),
            "bull_votes": self.last_bull_votes,
            "bear_votes": self.last_bear_votes,
            "entry_price": entry,
            "sl_price": sl,
            "tp1_price": tp1,
            "quantity": quantity,
            "risk_factor": risk_factor,
            "reason": reason
        }
