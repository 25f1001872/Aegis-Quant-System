"""
Portfolio manager for Aegis-Quant-System
"""
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

    def can_trade(self, feature_row: dict) -> bool:
        if self.paper_engine.get_position() is not None:
            return False
            
        if self.paper_engine.get_drawdown() > 8.0:
            return False
            
        hour = feature_row.get("hour_of_day", 0)
        if hour in {0, 1, 2, 3, 4}:
            return False
            
        if feature_row.get("regime", 0) == 2:
            return False
            
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
            cluster_price = s3_nearest_cluster_usd if (s3_dominant_side == "short" and s3_nearest_cluster_usd > current_price) else 0.0
            entry = current_price
            
            if cluster_price > 0:
                tp1 = cluster_price * (1 - 0.002)
                tp2 = cluster_price * 1.005
            else:
                tp1 = entry * 1.01
                tp2 = entry * 1.02

            try:
                swing = self.broker.get_last_4h_swing("LONG")
            except Exception:
                return None
                
            sl = swing * (1 - 0.001)

        elif direction == "SHORT":
            cluster_price = s3_nearest_cluster_usd if (s3_dominant_side == "long" and s3_nearest_cluster_usd > 0 and s3_nearest_cluster_usd < current_price) else 0.0
            entry = current_price
            
            if cluster_price > 0:
                tp1 = cluster_price * (1 + 0.002)
                tp2 = cluster_price * 0.995
            else:
                tp1 = entry * 0.99
                tp2 = entry * 0.98

            try:
                swing = self.broker.get_last_4h_swing("SHORT")
            except Exception:
                return None
                
            sl = swing * (1 + 0.001)
        else:
            return None

        sl_pct = abs(entry - sl) / entry * 100
        if sl_pct < 0.5 or sl_pct > 3.0:
            return None

        return {
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tp1": round(tp1, 2),
            "tp2": round(tp2, 2)
        }

    def compute_size(self, direction: str, entry: float, feature_row: dict) -> float:
        family_b = abs(feature_row.get("family_b_score", 0))
        total_score = abs(feature_row.get("total_score", 0))

        if family_b == 0:
            size_pct = 0.0
        elif family_b == 1 and total_score == 1:
            size_pct = 0.15
        elif family_b >= 1 and total_score == 2:
            size_pct = 0.25
        elif family_b >= 2 and total_score == 3:
            size_pct = 0.50
        elif family_b >= 2 and total_score == 4:
            size_pct = 0.75
        elif family_b == 3 and total_score >= 5:
            size_pct = 1.00
        else:
            size_pct = 0.15

        if size_pct == 0:
            return 0.0

        balance = self.paper_engine.get_balance()
        max_position = balance * self.max_position_pct
        position_usdt = max_position * size_pct
        quantity = round(position_usdt / entry, 3)
        
        if quantity < 0.001:
            return 0.0
            
        return quantity

    def process(self, feature_row: dict) -> dict:
        action = "SKIP"
        reason = "Neutral"
        direction = "NONE"
        entry = None
        quantity = None
        sl = None
        tp1 = None
        
        # Step 7 — Update open position
        try:
            current_price = self.broker.get_current_price()
            events = self.paper_engine.update(current_price)
            for event in events:
                portfolio_logger.info(event)
        except Exception as e:
            portfolio_logger.error(f"Failed to fetch price for update: {e}")
            current_price = 0.0

        # Step 8 — Thesis invalidation check
        pos = self.paper_engine.get_position()
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
                    levels = self.compute_levels(direction, feature_row)
                    if not levels:
                        reason = "SL out of bounds or levels uncomputable"
                    else:
                        entry = levels["entry"]
                        sl = levels["sl"]
                        tp1 = levels["tp1"]
                        tp2 = levels["tp2"]
                        
                        quantity = self.compute_size(direction, entry, feature_row)
                        if quantity == 0:
                            reason = "Position size calculated as 0 or < 0.001"
                        else:
                            # Step 6 — Execute
                            self.paper_engine.open_position(
                                side=direction,
                                entry_price=entry,
                                quantity=quantity,
                                sl=sl,
                                tp1=tp1,
                                tp2=tp2,
                                score=feature_row.get("total_score", 0),
                                signal_snapshot=feature_row
                            )
                            
                            opposite_side = "SELL" if direction == "LONG" else "BUY"
                            
                            is_paper = True
                            try:
                                from aegis.execution.broker import PAPER_MODE
                                is_paper = PAPER_MODE
                            except ImportError:
                                pass
                                
                            if not is_paper:
                                self.broker.place_market_order(direction, quantity)
                                self.broker.place_stop_order(opposite_side, quantity, sl)
                                self.broker.place_limit_order(opposite_side, quantity*0.7, tp1)

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
            "reason": reason
        }
