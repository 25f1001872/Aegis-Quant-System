# AEGIS — Quant Trading System

End-to-end quantitative trading system for BTC-USDT perpetual futures. AEGIS combines derivatives positioning signals with live order-flow microstructure to identify crowded, fragile setups and execute with full risk discipline.

```
Asset BTC-USDT Perpetual
Venue Binance Futures
Timeframes 15M tactical · 4H strategic
Data Cost $0 (Binance public REST + WebSocket)
```

-----

## Thesis

The edge is not prediction — it is position awareness. Price-derived indicators (RSI, MACD, EMAs) are circular and broadly arbitraged. AEGIS reads the derivatives state directly: where leveraged participants are crowded, where forced liquidations will cascade, and which side of the book is currently aggressive. Entries fire at the moment a crowded, fragile structure meets confirming real-time aggression — setups with measurable tripwires rather than inferred trends.

-----

## The Seven Signals

**Family A — Derivatives (4H, strategic regime)**

1. **Funding Rate Z-Score** — rolling z-score with asymmetric thresholds calibrated to BTC’s right-skewed funding distribution, plus a persistence check that filters one-off spikes.
1. **Open Interest Delta** — change in OI paired with price direction separates conviction flows from short-covering and long capitulation.
1. **Liquidation Clusters** — price-binned forced-order map built from the live liquidation stream. Flags cascade targets and post-sweep reversal zones.
1. **Long/Short Ratio** — account-level crowding filter. Used as a size modifier and second confirmation alongside funding.

**Family B — Microstructure (15M, tactical trigger)**

1. **Order Flow Imbalance** — rolling net of taker-buy minus taker-sell volume. Measures genuine aggression rather than passive absorption.
1. **Cumulative Volume Delta** — long-run OFI accumulator. Divergence against price exposes smart-money distribution or seller exhaustion.
1. **Taker Ratio** — normalised share of aggressive volume. Filters false OFI reads during volume spikes.

Family A sets the battlefield. Family B pulls the trigger. Both are required.

-----

## Architecture

```
Ingest → Signals → Aggregate → Risk → Execute
REST 7 modules Score engine Portfolio Signed broker
WS unified + regime manager + paper engine
schema classifier + sizer
```

Key modules under `aegis/`:

|Module |Responsibility |
|---------------------------|----------------------------------------------------------------|
|`signals/` |Seven signal implementations, each with standardised output keys|
|`alpha/aggregator.py` |Master loop: fetches, caches, combines, scores, writes features |
|`portfolio/constructor.py` |Directional bias, SL/TP ladder, composite sizing |
|`risk/position_sizer.py` |SL-first sizing: `position = (equity × risk%) ÷ stop distance` |
|`risk/portfolio_manager.py`|Daily/weekly loss limits, drawdown throttle, trade-rate caps |
|`risk/metrics.py` |Sharpe, Sortino, Calmar, max drawdown |
|`execution/broker.py` |HMAC-signed Binance Futures REST client |
|`execution/paper.py` |Paper-trade engine that mirrors live execution semantics |
|`configs/risk_params.py` |Single source of truth for every risk parameter |

-----

## Dataset

Historical OHLCV used for research is hosted outside the repo:
👉 <https://drive.google.com/drive/folders/1z5gRYZGtlJCsPr_PR6H-fiXatbBdKYNf?usp=sharing>

Open-interest samples are included under `aegis/dataset/`. All live signal data is pulled directly from Binance public endpoints at runtime — no paid data provider required.

-----

## Getting Started

### 1. Requirements

- Python 3.10 or newer
- A Binance Futures **testnet** account (free): <https://testnet.binancefuture.com>
- Get testnet keys at <https://testnet.binancefuture.com> → *API Key* tab. These are free, carry no real money, and are the default target of the broker (`execution/broker.py` points at `testnet.binancefuture.com`).
- Stable internet connection (three WebSocket streams run continuously)

### 2. Clone and install

```bash
git clone https://github.com/25f1001872/Aegis-Quant-System.git
cd Aegis-Quant-System

python -m venv venv
source venv/bin/activate # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

### 3. Configure credentials

Copy the template and fill in your **testnet** API keys:

Edit `.env`:

```
BINANCE_API_KEY=your_testnet_api_key
BINANCE_API_SECRET=your_testnet_api_secret
```
then run this command on terminal 

```bash
cp .env.example .env
```





### 4. Verify connectivity

```bash
python test_connection.py
```

You should see a successful testnet handshake and account balance readout.

### 5. Tune risk parameters (optional)

All risk settings live in one place — edit `configs/risk_params.py`:

```python
STARTING_CAPITAL_USD = 10_000 # testnet paper capital
RISK_PER_TRADE = 0.01 # 1% of equity per trade
MAX_LEVERAGE = 3 # hard ceiling
DEFAULT_LEVERAGE = 1 # used until 30+ trades validated
DAILY_LOSS_LIMIT_PCT = 0.03 # halt new entries after 3% daily loss
WEEKLY_LOSS_LIMIT_PCT = 0.08 # halt new entries after 8% weekly loss
MAX_CONSECUTIVE_LOSSES = 3 # circuit breaker
```

Change a value here and it propagates everywhere.

-----

## Running AEGIS on Testnet

Launch the full system:

```bash
python -m aegis.alpha.aggregator
```

### What happens on startup

1. **Streams boot** — S3 (liquidations), S5 (OFI), S6 (CVD) connect to Binance WebSocket endpoints.
1. **Warm-up** — the system waits ~50 minutes for S6’s CVD accumulator to build enough history. S3 and S5 finish earlier and wait.
1. **Collection loop begins** — every 5 minutes (plus a 10-second latency buffer), AEGIS:
- pulls fresh REST data for S1, S2, S4, S7, OHLCV (cached by each source’s actual update frequency),
- reads the current WebSocket state for S3, S5, S6,
- computes a total score across both families,
- decides entry / exit / hold,
- routes any trade through the paper engine against the testnet broker.

Every cycle prints a one-line status:

```
ENTER | Dir:LONG | Score:+5 B:+2 | Risk:1.00x | Funding crowded short, OFI aggressive long
```

All feature rows are appended to `data/processed/aegis_features.csv` for later analysis.

### Stopping cleanly

Press `Ctrl+C`. AEGIS shuts down all three WebSocket streams in order before exiting.

-----

## Logs and Output

|File |Contents |
|-----------------------------------|---------------------------------------------|
|`logs/aggregator_errors.log` |Per-signal fetch errors and fallback events |
|`logs/broker.log` |Every broker request and response |
|`logs/portfolio.log` |Trade decisions, sizing, rejections |
|`data/processed/aegis_features.csv`|One row per 5-minute cycle — full feature set|

-----

## Risk Controls at a Glance

**Pre-trade** — minimum score threshold, Family B confirmation required, regime filter, macro-event no-trade window, SL distance bounded to 0.5–3.0%.

**Sizing** — SL-first sizing, quarter-Kelly scaled by live win rate and payoff ratio, equity-EMA filter (halves size when equity is below its 20-bar EMA), volatility scaling via current-vs-mean ATR, leverage cap.

**Portfolio guards** — daily and weekly loss limits, linear drawdown throttle beyond 5% DD, consecutive-loss streak breaker, daily trade-rate cap, minimum bars between entries, laddered 1.2R / 2.0R / 3.5R take-profit exits.

-----

## Repository Layout

```
Aegis-Quant-System/
├── aegis/
│ ├── signals/ # 7 signal implementations
│ ├── alpha/ # Aggregator + signal combiner
│ ├── portfolio/ # Portfolio construction and sizing
│ ├── risk/ # Metrics, limits, portfolio manager, exit manager
│ ├── execution/ # Broker, OMS, paper-trade engine
│ ├── features/ # Technical and microstructure features
│ ├── models/ # ML signal models
│ ├── backtesting/ # Backtest engine, cost model, metrics
│ ├── reporting/ # Dashboard and tearsheet generators
│ ├── dataset/ # Sample derivatives data
│ └── history/ # Rolling liquidation history
├── configs/ # Risk parameters and strategy configs
├── research/ # Notebooks, reports, alpha research
├── tests/ # Unit tests
├── requirements.txt
├── test_connection.py # API connectivity check
└── README.md
```

-----

## Safety

- **Testnet only by default.** The broker is configured to target `testnet.binancefuture.com`. Testnet accounts carry no real money.
- **Never commit your `.env` file.** It is listed in `.gitignore` — keep it that way.
- **Paper-trade first.** Validate behaviour over at least 30 full cycles before considering any migration to live capital.
- **Keep leverage low.** The edge in AEGIS comes from precision, not size.

-----

## License

MIT — see `LICENSE`.

-----

*AEGIS Quant System · v1.0 · Built for BTC-USDT perpetual futures on Binance.*

