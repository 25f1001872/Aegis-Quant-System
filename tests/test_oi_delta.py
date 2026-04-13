"""
Unit tests for OI Delta Signal (Signal #2)
Run with: pytest tests/test_oi_delta.py -v
"""
import pytest
import pandas as pd
import numpy as np
from aegis.signals.s2_oi_delta import S2OIDeltaSignal, get_oi_delta_columns


@pytest.fixture
def sample_data():
    """Create sample test data"""
    dates = pd.date_range('2024-01-01', periods=100, freq='4H')
    
    df = pd.DataFrame({
        'timestamp': dates,
        'close': np.random.uniform(90000, 100000, 100),
        'oi_btc': np.random.uniform(100000, 150000, 100)
    })
    
    return df


def test_signal_initialization():
    """Test signal can be initialized with default parameters"""
    signal = S2OIDeltaSignal()
    
    assert signal.name == 'oi_delta'
    assert signal.family == 'A'
    assert signal.timeframe == '4h'
    assert signal.weight == 1.0


def test_required_columns():
    """Test signal reports correct required columns"""
    signal = S2OIDeltaSignal()
    required = signal.get_required_columns()
    
    assert 'timestamp' in required
    assert 'close' in required
    assert 'oi_btc' in required


def test_calculate_adds_columns(sample_data):
    """Test calculate() adds expected columns"""
    signal = S2OIDeltaSignal()
    df_result = signal.calculate(sample_data)
    
    expected_cols = get_oi_delta_columns()
    
    for col in expected_cols:
        assert col in df_result.columns, f"Missing column: {col}"


def test_signal_values(sample_data):
    """Test signal values are in expected range"""
    signal = S2OIDeltaSignal()
    df_result = signal.calculate(sample_data)
    
    # Scores should only be -1, 0, or 1
    unique_scores = df_result['oi_delta_score'].dropna().unique()
    assert all(score in [-1, 0, 1] for score in unique_scores)
    
    # Signals should be valid classifications
    valid_signals = ['BULLISH', 'BEARISH', 'WEAK_BULLISH', 'CAPITULATION', 'NEUTRAL']
    unique_signals = df_result['oi_delta_signal'].dropna().unique()
    assert all(sig in valid_signals for sig in unique_signals)


def test_missing_columns_raises_error():
    """Test that missing required columns raises error"""
    signal = S2OIDeltaSignal()
    
    # DataFrame missing 'oi_btc'
    df_incomplete = pd.DataFrame({
        'timestamp': pd.date_range('2024-01-01', periods=10, freq='4H'),
        'close': np.random.uniform(90000, 100000, 10)
    })
    
    with pytest.raises(ValueError, match="missing required columns"):
        signal.calculate(df_incomplete)


def test_signal_interpretation():
    """Test signal interpretation strings exist for all signals"""
    signal = S2OIDeltaSignal()
    
    valid_signals = ['BULLISH', 'BEARISH', 'WEAK_BULLISH', 'CAPITULATION', 'NEUTRAL']
    
    for sig in valid_signals:
        interpretation = signal.get_signal_interpretation(sig)
        assert isinstance(interpretation, str)
        assert len(interpretation) > 0


def test_get_signal_stats(sample_data):
    """Test signal statistics generation"""
    signal = S2OIDeltaSignal()
    df_result = signal.calculate(sample_data)
    
    stats = signal.get_signal_stats(df_result)
    
    assert 'total_signals' in stats
    assert 'distribution' in stats
    assert stats['total_signals'] == len(sample_data)


def test_zscore_vs_raw():
    """Test Z-score vs raw percentage gives different results"""
    dates = pd.date_range('2024-01-01', periods=500, freq='4H')
    df = pd.DataFrame({
        'timestamp': dates,
        'close': np.random.uniform(90000, 100000, 500),
        'oi_btc': np.random.uniform(100000, 150000, 500)
    })
    
    # With Z-score
    signal_zscore = S2OIDeltaSignal(use_zscore=True)
    df_zscore = signal_zscore.calculate(df.copy())
    
    # Without Z-score
    signal_raw = S2OIDeltaSignal(use_zscore=False)
    df_raw = signal_raw.calculate(df.copy())
    
    # They should produce different results
    assert not df_zscore['oi_delta_signal'].equals(df_raw['oi_delta_signal'])


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
