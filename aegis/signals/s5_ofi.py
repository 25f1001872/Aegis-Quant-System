"""
AEGIS-QUANT-SYSTEM
==================
Signal 5 — Order Flow Imbalance (OFI)
Family    : B — Microstructure
Role      : ENTRY TRIGGER
Timeframe : 15M rolling window, Z-score normalized
Source    : Binance Futures WebSocket
            wss://fstream.binance.com/ws/btcusdt@aggTrade

─────────────────────────────────────────────────────────────────────────
WHY Z-SCORE NORMALIZATION (not fixed dollar thresholds)
─────────────────────────────────────────────────────────────────────────
  A fixed threshold like "$2M" is statistically meaningless because:

    • During high-volume sessions BTC can see $20M+ OFI in 15 min.
      A "$2M" threshold fires constantly and becomes noise.
    • During low-volume / Asia sessions OFI rarely exceeds $500K.
      A "$2M" threshold never fires — you miss every real signal.
    • As BTC price changes ($50K vs $100K), notional dollar volume
      scales with it, making the same threshold 2x too sensitive or
      2x too loose over a price cycle.
    • A fixed threshold cannot be compared to other signals on a
      common scale. Z-scores are dimensionless — all signals can be
      combined, ranked, and weighted consistently.

  Solution: normalize each new 15M OFI reading against the rolling
  history of past completed 15M windows.

  OFI_z = (current_15M_OFI  −  mean(past_N_windows)) /
                                std(past_N_windows)

  A Z-score of +2.0 means the current OFI is more extreme than ~97.5%
  of all recent 15M windows — regardless of whether that's $500K or
  $5M in absolute terms. The signal fires on statistical extremes, not
  arbitrary dollar amounts.

─────────────────────────────────────────────────────────────────────────
NORMALIZATION MECHANICS
─────────────────────────────────────────────────────────────────────────
  Current window   : trades in the last 15 minutes (live, always building)
  History buffer   : the OFI value of each COMPLETED 15M window,
                     kept for the last HISTORY_WINDOWS periods
                     (default: 96 windows = 24 hours of history)

  At each 15M candle close:
    1. Seal the current window → store its OFI in the history deque
    2. Start a fresh current window
    3. Compute Z-score of the sealed window against the history

  Between candle closes:
    • The live (partial) OFI is available for display / monitoring
    • The Z-score is always computed against the last sealed window
      so it is always based on a complete 15M period

  Minimum history required before Z-score is meaningful: MIN_HISTORY
  (default: 12 windows = 3 hours). Before that, score = 0 (WARMING_UP).

─────────────────────────────────────────────────────────────────────────
THRESHOLDS
─────────────────────────────────────────────────────────────────────────
  Z ≥ +Z_STRONG  (default +2.0)  → BULLISH  score = +1
  Z ≤ −Z_STRONG  (default −2.0)  → BEARISH  score = −1
  |Z| < Z_STRONG                 → NEUTRAL  score =  0

  These match Signal 1 (Funding Z-Score) exactly — both signals now
  speak the same language and can be combined without rescaling.

─────────────────────────────────────────────────────────────────────────
MERGE INTERFACE
─────────────────────────────────────────────────────────────────────────
  from signal5_ofi import OFICollector

  collector = OFICollector()
  collector.start()
  # Allow MIN_HISTORY windows (3H by default) before first meaningful read
  result = collector.get_signal5_score(allowed_direction="LONG")

  result["trigger"]      # "ENTER" | "WAIT" | "WARMING_UP"
  result["score"]        # +1 | 0 | -1
  result["ofi_z"]        # Z-score of last completed 15M window
  result["ofi_raw"]      # raw dollar OFI of last completed window
  result["ofi_live"]     # live partial OFI of the current (open) window
  result["confirmed"]    # True when trigger == "ENTER"
"""

import json
import math
import time
import threading
from collections import deque
from datetime import datetime, timezone

import websocket    # pip install websocket-client


# ──────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────
SYMBOL           = "btcusdt"
WS_URL           = f"wss://fstream.binance.com/ws/{SYMBOL}@aggTrade"

# ── DEV TOGGLE ────────────────────────────────────────────────────
TEST_MODE      = False
WARMUP_SECONDS = 900    # 15 minutes — one full 15M candle window
                        # must complete before OFI signal is valid
WINDOW_SECONDS = WARMUP_SECONDS              # keeps internals from breaking

HISTORY_WINDOWS  = 96           # how many completed 15M windows to keep in history
                                 # 96 × 15min = 24 hours — enough to capture intraday
                                 # volume regimes without bleeding into old conditions

MIN_HISTORY      = 1           # minimum completed windows before Z-score is valid
                                 # 12 × 15min = 3 hours of burn-in

Z_STRONG         = 2.0          # |Z| ≥ 2.0 → signal fires (~97.5th percentile)
                                 # matches the Z threshold used by Signal 1 (Funding)
                                 # so both signals are on the same scale

# No API key needed — Binance public WebSocket streams are unauthenticated.


# ──────────────────────────────────────────────────────
# WELFORD ONLINE VARIANCE  (numerically stable, O(1))
# ──────────────────────────────────────────────────────
# Rather than storing all values and computing std() from scratch each
# time, we use Welford's online algorithm. It updates mean and variance
# incrementally with each new value added or removed.
# This is critical for a rolling window: when an old value leaves the
# deque, we "un-add" it without recomputing from scratch.

class WelfordWindow:
    """
    Numerically stable rolling mean and population std-dev.
    Supports add() and remove() for a sliding window.
    Uses the compensated two-pass (West 1979) approach for removes.
    """

    def __init__(self):
        self.n     = 0       # count
        self.mean  = 0.0     # running mean
        self.M2    = 0.0     # running sum of squared deviations

    def add(self, x: float) -> None:
        self.n    += 1
        delta      = x - self.mean
        self.mean += delta / self.n
        delta2     = x - self.mean
        self.M2   += delta * delta2

    def remove(self, x: float) -> None:
        """
        Remove a previously added value from the running stats.
        Safe as long as n > 1. Call only for values that were add()-ed.
        """
        if self.n <= 1:
            self.n    = 0
            self.mean = 0.0
            self.M2   = 0.0
            return
        self.n    -= 1
        delta      = x - self.mean
        self.mean -= delta / self.n
        delta2     = x - self.mean
        self.M2   -= delta * delta2
        self.M2    = max(self.M2, 0.0)    # guard float precision drift

    @property
    def std(self) -> float:
        """Population standard deviation. Returns 0.0 if n < 2."""
        if self.n < 2:
            return 0.0
        return math.sqrt(self.M2 / self.n)

    def zscore(self, x: float) -> float | None:
        """
        Z-score of x against the current window distribution.
        Returns None if std == 0 (all past values were identical) or n < 2.
        """
        s = self.std
        if s == 0.0 or self.n < 2:
            return None
        return (x - self.mean) / s


# ──────────────────────────────────────────────────────
# INTERNAL TRADE MODEL
# ──────────────────────────────────────────────────────

class _Trade:
    """One aggTrade event in the live current window."""
    __slots__ = ("ts_ms", "qty_usd", "is_buyer_maker")

    def __init__(self, ts_ms: int, qty_usd: float, is_buyer_maker: bool):
        self.ts_ms          = ts_ms
        self.qty_usd        = qty_usd
        self.is_buyer_maker = is_buyer_maker
        # is_buyer_maker = True  → taker SOLD  (hit the bid) → negative OFI contribution
        # is_buyer_maker = False → taker BOUGHT (hit the ask) → positive OFI contribution


# ──────────────────────────────────────────────────────
# OFI COLLECTOR
# ──────────────────────────────────────────────────────

class OFICollector:
    """
    Maintains:
      • A live rolling current window (last 15 minutes of trades)
      • A history deque of completed 15M window OFI values
      • Welford stats over the history for O(1) Z-score computation

    Thread-safe via a single lock covering all shared state.
    Auto-reconnects the WebSocket on disconnect.
    """

    def __init__(
        self,
        ws_url:          str = WS_URL,
        window_seconds:  int = WINDOW_SECONDS,
        history_windows: int = HISTORY_WINDOWS,
        min_history:     int = MIN_HISTORY,
        z_strong:       float = Z_STRONG,
    ):
        self._ws_url          = ws_url
        self._window_ms       = window_seconds * 1000
        self._history_windows = history_windows
        self._min_history     = min_history
        self._z_strong        = z_strong

        # ── Live current window ───────────────────────────────────────
        self._live_trades    : deque[_Trade] = deque()
        self._live_ofi       : float         = 0.0   # incremental, O(1) update
        self._live_buy_vol       : float = 0.0
        self._live_sell_vol      : float = 0.0
        self._last_sealed_buy_vol : float = 0.0
        self._last_sealed_sell_vol: float = 0.0
        self._window_start_ms: int           = 0     # set on first trade

        # ── Completed-window history ──────────────────────────────────
        self._history        : deque[float]  = deque(maxlen=history_windows)
        self._stats          : WelfordWindow = WelfordWindow()

        # ── Last sealed window (what Z-score is computed from) ────────
        self._last_sealed_ofi: float | None  = None
        self._last_sealed_ts : str   | None  = None

        # ── Threading ─────────────────────────────────────────────────
        self._lock    = threading.Lock()
        self._ws_app  : websocket.WebSocketApp | None = None
        self._thread  : threading.Thread       | None = None
        self._running : bool = False

    # ── WebSocket callbacks ──────────────────────────────────────────

    def _on_open(self, ws):
        print(f"[OFI] Connected → {self._ws_url}")

    def _on_message(self, ws, raw: str):
        try:
            evt = json.loads(raw)
            if evt.get("e") != "aggTrade":
                return

            price          = float(evt["p"])
            qty            = float(evt["q"])
            qty_usd        = price * qty
            is_buyer_maker = bool(evt["m"])
            ts_ms          = int(evt["T"])

            with self._lock:
                # Initialise window start on very first trade
                if self._window_start_ms == 0:
                    self._window_start_ms = ts_ms

                # ── Seal completed 15M windows ────────────────────────
                # A trade may arrive after one or more full windows have
                # elapsed. Seal each one before adding the new trade.
                while ts_ms >= self._window_start_ms + self._window_ms:
                    self._seal_window(self._window_start_ms + self._window_ms)

                # ── Add trade to live current window ──────────────────
                if is_buyer_maker:
                    self._live_sell_vol += qty_usd
                    self._live_ofi      -= qty_usd
                else:
                    self._live_buy_vol  += qty_usd
                    self._live_ofi      += qty_usd
                self._live_trades.append(_Trade(ts_ms, qty_usd, is_buyer_maker))

                # Evict trades that fell outside the current 15M window
                # (handles edge cases where trades arrive out of order)
                cutoff = self._window_start_ms
                while self._live_trades and self._live_trades[0].ts_ms < cutoff:
                    old        = self._live_trades.popleft()
                    old_signed = -old.qty_usd if old.is_buyer_maker else +old.qty_usd
                    self._live_ofi -= old_signed

        except Exception as exc:
            print(f"[OFI] Message parse error: {exc}")

    def _seal_window(self, new_start_ms: int) -> None:
        """
        Called under lock.
        Stores the current live OFI as a completed window, updates Welford
        stats, and resets the live window for the next period.
        """
        completed_ofi = self._live_ofi
        ts_utc        = datetime.fromtimestamp(
            self._window_start_ms / 1000, tz=timezone.utc
        ).isoformat()

        # Store sealed value + update rolling stats
        if len(self._history) == self._history.maxlen:
            # Window is full — evict oldest value from Welford stats
            oldest = self._history[0]
            self._stats.remove(oldest)
        self._history.append(completed_ofi)
        self._stats.add(completed_ofi)

        self._last_sealed_ofi = completed_ofi
        self._last_sealed_ts  = ts_utc

        # Reset live window
        self._last_sealed_buy_vol  = self._live_buy_vol
        self._last_sealed_sell_vol = self._live_sell_vol
        self._live_ofi        = 0.0
        self._live_buy_vol  = 0.0
        self._live_sell_vol = 0.0
        self._live_trades.clear()
        self._window_start_ms = new_start_ms

    def _on_error(self, ws, error):
        print(f"[OFI] WebSocket error: {error}")

    def _on_close(self, ws, code, msg):
        print(f"[OFI] Closed — code={code} msg={msg}")
        if self._running:
            print("[OFI] Reconnecting in 5 s …")
            time.sleep(5)
            self._connect()

    # ── Lifecycle ────────────────────────────────────────────────────

    def _connect(self):
        self._ws_app = websocket.WebSocketApp(
            self._ws_url,
            on_open    = self._on_open,
            on_message = self._on_message,
            on_error   = self._on_error,
            on_close   = self._on_close,
        )
        self._ws_app.run_forever(ping_interval=20, ping_timeout=10)

    def start(self):
        """Start background WebSocket thread (non-blocking)."""
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._connect, daemon=True)
        self._thread.start()
        mins = (self._min_history * WINDOW_SECONDS) // 60
        print(f"[OFI] Collector started. Needs {mins} min ({self._min_history} windows) "
              f"before Z-score is meaningful.")

    def stop(self):
        self._running = False
        if self._ws_app:
            self._ws_app.close()
        print("[OFI] Collector stopped.")

    # ── Raw accessors (for monitoring / other signals) ───────────────

    def get_live_ofi(self) -> tuple[float, int]:
        """Current partial OFI and trade count in the open 15M window."""
        with self._lock:
            return self._live_ofi, len(self._live_trades)

    def get_history_stats(self) -> tuple[int, float, float]:
        """(n_completed_windows, mean_ofi, std_ofi) from history."""
        with self._lock:
            return self._stats.n, self._stats.mean, self._stats.std

    def get_last_sealed(self) -> tuple[float | None, float | None, str | None]:
        """
        (last_sealed_ofi, z_score, timestamp_iso) of the most recently
        completed 15M window.  z_score is None if history is insufficient.
        """
        with self._lock:
            ofi = self._last_sealed_ofi
            ts  = self._last_sealed_ts
            if ofi is None or self._stats.n < self._min_history:
                return ofi, None, ts
            z = self._stats.zscore(ofi)
            return ofi, z, ts

    # ── Signal computation ───────────────────────────────────────────

    def _classify_z(self, z: float | None) -> tuple[int, str, str]:
        """
        Convert a Z-score to (score, condition, reason).
        z == None means history is insufficient (warming up).
        """
        if z is None:
            n_have = self._stats.n
            n_need = self._min_history
            return (0, "WARMING_UP",
                    f"Only {n_have}/{n_need} completed 15M windows in history. "
                    f"Z-score not yet reliable. Wait {n_need - n_have} more windows "
                    f"({(n_need - n_have) * WINDOW_SECONDS // 60} min).")

        if z >= self._z_strong:
            return (+1, "BULLISH",
                    f"OFI Z-score = +{z:.2f} — current 15M buying aggression is more "
                    f"extreme than ~{_z_to_pct(z):.0f}% of recent history. "
                    f"Real demand, not noise.")

        if z <= -self._z_strong:
            return (-1, "BEARISH",
                    f"OFI Z-score = {z:.2f} — current 15M selling aggression is more "
                    f"extreme than ~{_z_to_pct(-z):.0f}% of recent history. "
                    f"Real pressure, not noise.")

        return (0, "NEUTRAL",
                f"OFI Z-score = {z:+.2f} — within normal range (threshold ±{self._z_strong}). "
                f"Neither side is committing abnormally. Stay flat.")

    def get_signal5_score(self, allowed_direction: str = "ANY") -> dict:
        """
        STAGE 2 entry trigger. Called by aggregator after LSR sets direction.

        Args:
            allowed_direction : "LONG" | "SHORT" | "NO_TRADE" | "ANY"
                Passed in from Signal 4 (LSR).
                "LONG"     → only Z ≥ +Z_STRONG fires ENTER
                "SHORT"    → only Z ≤ −Z_STRONG fires ENTER
                "NO_TRADE" → always returns WAIT (session blocked upstream)
                "ANY"      → fires on any strong Z (standalone / testing)

        Returns dict with:
            trigger          "ENTER" | "WAIT" | "WARMING_UP"
            confirmed        bool
            score            +1 | 0 | -1
            ofi_z            Z-score of last sealed 15M window (or None)
            ofi_raw          raw dollar OFI of last sealed window (or None)
            ofi_live         live partial OFI of current open window
            history_n        number of completed windows in history
            history_mean     mean OFI across history windows
            history_std      std  OFI across history windows
        """
        sealed_ofi, z, sealed_ts = self.get_last_sealed()
        live_ofi, live_count     = self.get_live_ofi()
        n, mean, std             = self.get_history_stats()
        now_utc                  = datetime.now(tz=timezone.utc).isoformat()

        raw_score, condition, reason = self._classify_z(z)

        # ── Apply LSR direction gate ──────────────────────────────────
        if condition == "WARMING_UP":
            trigger   = "WARMING_UP"
            confirmed = False
            gate_note = reason   # warming-up message is self-explanatory

        elif allowed_direction == "NO_TRADE":
            trigger   = "WAIT"
            confirmed = False
            gate_note = "LSR blocked this session (neutral zone). OFI not evaluated."

        elif allowed_direction == "LONG":
            if raw_score == +1:
                trigger   = "ENTER"
                confirmed = True
                gate_note = (f"LSR bias = LONG ✅  +  OFI Z = +{z:.2f} ✅  "
                             f"Both stages confirmed. ENTER LONG.")
            else:
                trigger   = "WAIT"
                confirmed = False
                gate_note = (f"LSR bias = LONG but OFI Z = {_fmt_z(z)} ({condition}). "
                             f"Aggression not confirmed yet. Keep watching.")

        elif allowed_direction == "SHORT":
            if raw_score == -1:
                trigger   = "ENTER"
                confirmed = True
                gate_note = (f"LSR bias = SHORT ✅  +  OFI Z = {z:.2f} ✅  "
                             f"Both stages confirmed. ENTER SHORT.")
            else:
                trigger   = "WAIT"
                confirmed = False
                gate_note = (f"LSR bias = SHORT but OFI Z = {_fmt_z(z)} ({condition}). "
                             f"Aggression not confirmed yet. Keep watching.")

        else:   # "ANY" — standalone / testing mode
            trigger   = "ENTER" if raw_score != 0 else "WAIT"
            confirmed = raw_score != 0
            gate_note = "Direction gate bypassed (allowed_direction='ANY')."

        return {
            # ── Primary aggregator fields ─────────────────────────────
            "signal_id"        : 5,
            "signal_name"      : "Order Flow Imbalance — Z-Score",
            "family"           : "B",
            "timeframe"        : "15M",
            "role"             : "ENTRY_TRIGGER",
            "allowed_direction": allowed_direction,
            "trigger"          : trigger,       # "ENTER" | "WAIT" | "WARMING_UP"
            "confirmed"        : confirmed,
            "score"            : raw_score,     # +1 | 0 | -1
            "condition"        : condition,
            # ── OFI detail ────────────────────────────────────────────
            "ofi_z"            : round(z, 4)    if z           is not None else None,
            "ofi_raw"          : round(sealed_ofi, 2) if sealed_ofi is not None else None,
            "ofi_live"         : round(live_ofi, 2),
            "sealed_at"        : sealed_ts,
            # ── History stats (useful for other signals / logging) ────
            "history_n"        : n,
            "history_mean"     : round(mean, 2),
            "history_std"      : round(std, 2),
            "z_threshold"      : self._z_strong,
            # ── Reasoning ─────────────────────────────────────────────
            "reason"           : reason,
            "gate_note"        : gate_note,
            "timestamp"        : now_utc,
        }


# ──────────────────────────────────────────────────────
# SMALL UTILITIES
# ──────────────────────────────────────────────────────

def _z_to_pct(z: float) -> float:
    """Rough percentile from positive Z (one-tailed), good enough for log messages."""
    # Using the error function approximation
    import math
    return 100 * 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _fmt_z(z: float | None) -> str:
    return f"{z:+.2f}" if z is not None else "N/A"


# ──────────────────────────────────────────────────────
# MODULE-LEVEL SINGLETON (matches S6 pattern)
# ──────────────────────────────────────────────────────

_collector: OFICollector | None = None


def start_ofi_stream() -> OFICollector:
    """Start OFI WebSocket stream. Call ONCE at system startup."""
    global _collector
    _collector = OFICollector()
    _collector.start()
    return _collector


def get_signal() -> dict:
    global _collector
    if _collector is None:
        raise RuntimeError(
            "OFI stream not started. Call start_ofi_stream() first."
        )

    result  = _collector.get_signal5_score(allowed_direction="ANY")
    score   = result["score"]
    ofi_raw = result["ofi_raw"] if result["ofi_raw"] is not None else 0.0

    with _collector._lock:
        buy_vol  = _collector._last_sealed_buy_vol
        sell_vol = _collector._last_sealed_sell_vol

    total_vol = buy_vol + sell_vol
    ofi_norm  = round(ofi_raw / total_vol, 6) if total_vol > 0 else 0.0

    return {
        "signal_id"  : 5,
        "score"      : score,
        "timestamp"  : result["timestamp"],
        "reason"     : result["reason"],
        "s5_score"   : score,
        "s5_ofi_raw" : round(ofi_raw, 2),
        "s5_buy_vol" : round(buy_vol, 2),
        "s5_sell_vol": round(sell_vol, 2),
        "s5_ofi_norm": ofi_norm,
    }


def stop_ofi_stream() -> None:
    """Stop WebSocket stream on system shutdown."""
    global _collector
    if _collector is not None:
        _collector.stop()




