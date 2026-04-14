"""
Open Interest Delta Signal - Signal #2 (Family A)

Author: [Your Name]
Component: OI Delta Calculator
Integration: Standalone module for AEGIS system

Description:
Measures change in Open Interest over 4H periods and combines with price
direction to identify:
- New conviction entering (BULLISH/BEARISH)
- Weak moves from covering (WEAK_BULLISH)
- Capitulation (CAPITULATION)

Integration Points:
- Input: DataFrame with ['timestamp', 'close', 'oi_btc']
- Output: DataFrame with oi_delta_* columns
- Used by: Scoring Engine (Family A aggregation)
"""

import pandas as pd
import numpy as np
from typing import Dict, Any, Optional, Tuple
import warnings

from aegis.signals.base import BaseSignal

warnings.filterwarnings('ignore')


class S2OIDeltaSignal(BaseSignal):
    """
    Open Interest Delta Signal Calculator
    
    Signal Logic (from AEGIS research doc):
    ┌─────────────┬──────────┬────────────────────────────────────────┐
    │ Price Dir   │ OI Change│ Signal         │ Interpretation        │
    ├─────────────┼──────────┼────────────────┼───────────────────────┤
    │ UP          │ UP       │ BULLISH        │ New longs entering    │
    │ DOWN        │ UP       │ BEARISH        │ New shorts entering   │
    │ UP          │ DOWN     │ WEAK_BULLISH   │ Short covering only   │
    │ DOWN        │ DOWN     │ CAPITULATION   │ Long closing, reversal│
    │ Any         │ FLAT     │ NEUTRAL        │ No conviction         │
    └─────────────┴──────────┴────────────────┴───────────────────────┘
    
    Scoring:
    - BULLISH → +1
    - BEARISH → -1
    - WEAK_BULLISH, CAPITULATION, NEUTRAL → 0
    """
    
    def __init__(self,
                 threshold_up: float = 2.0,
                 threshold_down: float = -2.0,
                 use_zscore: bool = True,
                 zscore_window: int = 360,
                 weight: float = 1.0):
        """
        Initialize OI Delta Signal
        
        Args:
            threshold_up: Percentage threshold for significant OI increase (default 2%)
            threshold_down: Percentage threshold for significant OI decrease (default -2%)
            use_zscore: If True, use Z-score normalization (recommended)
            zscore_window: Rolling window for Z-score (default 360 = 60 days * 6 candles)
            weight: Signal weight in scoring system (default 1.0 = 1/7)
        """
        super().__init__(
            name='oi_delta',
            family='A',
            timeframe='4h',
            weight=weight
        )
        
        self.threshold_up = threshold_up
        self.threshold_down = threshold_down
        self.use_zscore = use_zscore
        self.zscore_window = zscore_window
        
        # Config for teammates to inspect
        self.config = {
            'threshold_up': threshold_up,
            'threshold_down': threshold_down,
            'use_zscore': use_zscore,
            'zscore_window': zscore_window,
            'weight': weight
        }
    
    def get_required_columns(self) -> list:
        """Required input columns for calculation"""
        return ['timestamp', 'close', 'oi_btc']
    
    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Calculate OI Delta signal
        
        Integration Note:
        - This method is the MAIN entry point for your teammates
        - Takes OHLCV + OI data, returns same DataFrame with added columns
        - Can be chained with other signal calculations
        
        Args:
            df: DataFrame with columns ['timestamp', 'close', 'oi_btc']
        
        Returns:
            DataFrame with added columns:
            - oi_delta_pct: Percentage change in OI
            - oi_delta_abs: Absolute change in OI
            - oi_delta_zscore: Z-score normalized delta (if use_zscore=True)
            - oi_delta_value: The value used for thresholding
            - oi_delta_signal: Signal classification
            - oi_delta_score: Numeric score (-1, 0, 1)
        
        Example:
            >>> signal = S2OIDeltaSignal()
            >>> df = pd.read_parquet('data.parquet')
            >>> df_with_signals = signal.calculate(df)
            >>> print(df_with_signals[['timestamp', 'oi_delta_signal', 'oi_delta_score']])
        """
        # Validate input
        self.validate_input(df)
        
        # Create copy to avoid modifying original
        df_out = df.copy()
        
        # Step 1: Calculate raw OI changes
        df_out = self._calculate_oi_changes(df_out)
        
        # Step 2: Calculate price direction
        df_out = self._calculate_price_direction(df_out)
        
        # Step 3: Determine which metric to use for thresholding
        if self.use_zscore:
            df_out = self._calculate_zscore(df_out)
            df_out['oi_delta_value'] = df_out['oi_delta_zscore']
        else:
            df_out['oi_delta_value'] = df_out['oi_delta_pct']
        
        # Step 4: Classify signals
        df_out = self._classify_signals(df_out)
        
        # Step 5: Generate scores
        df_out = self._generate_scores(df_out)
        
        self._is_fitted = True
        
        return df_out
    
    def _calculate_oi_changes(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate OI percentage and absolute changes"""
        df['oi_delta_abs'] = df['oi_btc'].diff()
        df['oi_delta_pct'] = df['oi_btc'].pct_change() * 100
        return df
    
    def _calculate_price_direction(self, df: pd.DataFrame) -> pd.DataFrame:
        """Determine if price moved UP or DOWN"""
        df['price_change'] = df['close'].diff()
        df['price_direction'] = np.where(df['price_change'] > 0, 'UP', 'DOWN')
        return df
    
    def _calculate_zscore(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate rolling Z-score for OI delta"""
        rolling_mean = df['oi_delta_pct'].rolling(
            window=self.zscore_window,
            min_periods=self.zscore_window
        ).mean()
        
        rolling_std = df['oi_delta_pct'].rolling(
            window=self.zscore_window,
            min_periods=self.zscore_window
        ).std()
        
        df['oi_delta_zscore'] = (df['oi_delta_pct'] - rolling_mean) / rolling_std
        
        return df
    
    def _classify_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Classify each row into signal categories
        
        This is the core logic from AEGIS research document
        """
        def classify_row(row):
            price_dir = row['price_direction']
            oi_value = row['oi_delta_value']
            
            # Handle NaN
            if pd.isna(oi_value) or pd.isna(price_dir):
                return 'NEUTRAL'
            
            # Determine thresholds based on normalization method
            if self.use_zscore:
                thresh_up = self.threshold_up
                thresh_down = self.threshold_down
            else:
                thresh_up = self.threshold_up
                thresh_down = self.threshold_down
            
            # Classification logic
            if price_dir == 'UP':
                if oi_value > thresh_up:
                    return 'BULLISH'  # New longs entering
                elif oi_value < thresh_down:
                    return 'WEAK_BULLISH'  # Short covering
                else:
                    return 'NEUTRAL'
            else:  # DOWN
                if oi_value > thresh_up:
                    return 'BEARISH'  # New shorts entering
                elif oi_value < thresh_down:
                    return 'CAPITULATION'  # Longs closing
                else:
                    return 'NEUTRAL'
        
        df['oi_delta_signal'] = df.apply(classify_row, axis=1)
        
        return df
    
    def _generate_scores(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Convert signal to numeric score for integration with scoring engine
        
        Scoring (for Family A aggregation):
        +1 = Bullish signal (contributes to long thesis)
        -1 = Bearish signal (contributes to short thesis)
         0 = Neutral/Weak/Ambiguous (no contribution)
        """
        score_map = {
            'BULLISH': 1,
            'BEARISH': -1,
            'WEAK_BULLISH': 0,
            'CAPITULATION': 0,
            'NEUTRAL': 0
        }
        
        df['oi_delta_score'] = df['oi_delta_signal'].map(score_map)
        
        return df
    
    def get_signal_stats(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Get statistics about signal distribution
        
        Useful for teammates doing backtesting/reporting
        
        Args:
            df: DataFrame with calculated signals
        
        Returns:
            Dictionary with signal statistics
        """
        if 'oi_delta_signal' not in df.columns:
            raise ValueError("Signals not calculated yet. Run calculate() first.")
        
        signal_counts = df['oi_delta_signal'].value_counts()
        total = len(df)
        
        stats = {
            'total_signals': total,
            'distribution': {},
            'bullish_ratio': 0,
            'bearish_ratio': 0,
            'neutral_ratio': 0
        }
        
        for signal in ['BULLISH', 'BEARISH', 'WEAK_BULLISH', 'CAPITULATION', 'NEUTRAL']:
            count = signal_counts.get(signal, 0)
            pct = (count / total * 100) if total > 0 else 0
            stats['distribution'][signal] = {
                'count': int(count),
                'percentage': round(pct, 2)
            }
        
        # Summary ratios
        stats['bullish_ratio'] = stats['distribution']['BULLISH']['percentage']
        stats['bearish_ratio'] = stats['distribution']['BEARISH']['percentage']
        stats['neutral_ratio'] = sum([
            stats['distribution']['WEAK_BULLISH']['percentage'],
            stats['distribution']['CAPITULATION']['percentage'],
            stats['distribution']['NEUTRAL']['percentage']
        ])
        
        return stats
    
    def get_signal_interpretation(self, signal: str) -> str:
        """
        Get human-readable interpretation of signal
        
        Useful for dashboard/reporting module
        
        Args:
            signal: Signal classification
        
        Returns:
            Interpretation string
        """
        interpretations = {
            'BULLISH': (
                "📈 NEW LONGS ENTERING | Price rising + OI increasing = "
                "fresh capital flowing in with conviction. Trend continuation likely."
            ),
            'BEARISH': (
                "📉 NEW SHORTS ENTERING | Price falling + OI increasing = "
                "bears committed, downtrend has fuel. Expect continued selling."
            ),
            'WEAK_BULLISH': (
                "⚠️ HOLLOW MOVE | Price rising + OI decreasing = "
                "short covering only, no real demand. Move likely to fade soon."
            ),
            'CAPITULATION': (
                "🔄 POTENTIAL EXHAUSTION | Price falling + OI decreasing = "
                "longs capitulating and closing. Watch for reversal if Family B confirms."
            ),
            'NEUTRAL': (
                "➖ NO EDGE | OI change below threshold. "
                "No strong positioning signal - wait for clearer setup."
            )
        }
        
        return interpretations.get(signal, "Unknown signal")


# ==================== INTEGRATION HELPER FUNCTIONS ====================

def load_and_calculate_oi_delta(
    data_path: str = 'dataset/open_interest.csv',
    threshold_up: float = 2.0,
    threshold_down: float = -2.0,
    use_zscore: bool = True
) -> pd.DataFrame:
    """
    Convenience function for quick integration
    
    For teammates who just want to add OI Delta to their pipeline:
    
    Example:
        >>> from aegis.signals.oi_delta import load_and_calculate_oi_delta
        >>> df = load_and_calculate_oi_delta()
        >>> print(df[['timestamp', 'oi_delta_signal', 'oi_delta_score']])
    
    Args:
        data_path: Path to data file (CSV or Parquet), defaults to 'dataset/open_interest.csv'
        threshold_up: OI increase threshold
        threshold_down: OI decrease threshold
        use_zscore: Use Z-score normalization
    
    Returns:
        DataFrame with OI Delta signals
    """
    # Load data
    if data_path.endswith('.parquet'):
        df = pd.read_parquet(data_path)
    else:
        df = pd.read_csv(data_path, parse_dates=['timestamp'])

    # --- Data Preparation ---
    # Calculate 'close' price
    df['close'] = df['sum_open_interest_value'] / df['sum_open_interest']
    
    # Rename 'sum_open_interest' to 'oi_btc'
    df.rename(columns={'sum_open_interest': 'oi_btc'}, inplace=True)
    
    # Calculate signal
    signal = S2OIDeltaSignal(
        threshold_up=threshold_up,
        threshold_down=threshold_down,
        use_zscore=use_zscore
    )
    
    df = signal.calculate(df)
    
    return df


def get_oi_delta_columns() -> list:
    """
    Return list of columns added by OI Delta signal
    
    Useful for teammates doing feature selection or column filtering
    
    Returns:
        List of column names
    """
    return [
        'oi_delta_abs',
        'oi_delta_pct',
        'oi_delta_zscore',
        'price_change',
        'price_direction',
        'oi_delta_value',
        'oi_delta_signal',
        'oi_delta_score'
    ]