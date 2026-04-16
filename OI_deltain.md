# Open Interest Delta Signal - Integration Guide

**Signal:** #2 (Family A - Derivatives)  
**Status:** ✅ Ready for Integration

---

## Quick Start

### Basic Usage

```python
from aegis.signals.oi_delta import OIDeltaSignal
import pandas as pd

# Load your data
df = pd.read_parquet('data/btc_4h_complete.parquet')

# Initialize signal
oi_signal = OIDeltaSignal(
    threshold_up=2.0,
    threshold_down=-2.0,
    use_zscore=True
)

# Calculate signals
df_with_signals = oi_signal.calculate(df)

# Use the signals
print(df_with_signals[['timestamp', 'oi_delta_signal', 'oi_delta_score']])
```

### Even Simpler (One-Liner)

```python
from aegis.signals.oi_delta import load_and_calculate_oi_delta

df = load_and_calculate_oi_delta('data/btc_4h_complete.parquet')
```

---

## Integration Points

### For Scoring Engine Team

**What you need:**
```python
# After your teammate calculates OI Delta
oi_score = df['oi_delta_score']  # Values: -1, 0, or 1

# Combine with other Family A signals
family_a_score = (
    df['funding_zscore_score'] +
    df['oi_delta_score'] +
    df['liq_cluster_score'] +
    df['ls_ratio_score']
) / 4  # Average of 4 Family A signals
```

### For Backtesting Team

**What you need:**
```python
# Get signal for entry logic
bullish_setup = df['oi_delta_signal'] == 'BULLISH'
bearish_setup = df['oi_delta_signal'] == 'BEARISH'

# Or use score directly
long_signal = df['oi_delta_score'] == 1
short_signal = df['oi_delta_score'] == -1
```

### For Dashboard Team

**What you need:**
```python
from aegis.signals.oi_delta import OIDeltaSignal

signal = OIDeltaSignal()
df = signal.calculate(data)

# Get statistics for display
stats = signal.get_signal_stats(df)
print(stats)
# Output:
# {
#   'total_signals': 6570,
#   'distribution': {
#     'BULLISH': {'count': 1234, 'percentage': 18.78},
#     'BEARISH': {'count': 1156, 'percentage': 17.60},
#     ...
#   }
# }

# Get interpretation for tooltip/help text
interpretation = signal.get_signal_interpretation('BULLISH')
print(interpretation)
```

---

## Input Requirements

### Required Columns
Your data must have:
- `timestamp` (datetime)
- `close` (float) - closing price
- `oi_btc` (float) - open interest in BTC

### Optional Columns
Everything else is computed internally.

### Data Format Example
