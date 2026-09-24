# Reverse RSI Forex Strategy
This is a scalping forex strategy which run during the quiet hour of sydney session. Works best on **USDCHF**, **CHFJPY**, **AUDCAD**, and **GBPAUD** forex pairs with **5-minute timeframe**.

## Performance From 2020-01-01 until 2022-09-25
Testing is done using __Deep Backtesting__ feature on the Tradingview platform.

### USDCHF M5
![](./USDCHF.png)

### CHFJPY M5
![](./CHFJPY.png)

### AUDCAD M5
![](./AUDCAD.png)

### GBPAUD M5
![](./GBPAUD.png)

---

## Python bot (paper trading)

The `scalper` package is a Python port of `Strategy - Reverse RSI.pine`. It runs the same rules on live 5-minute candles and fills orders with a simulated broker. **It only runs in PAPER mode.** No code path sends an order to a real broker, and the config loader rejects any mode other than `PAPER`.

### Quick start

Python 3.10 or newer.

```bash
pip install -r requirements.txt

python -m scalper paper      # start paper trading (free Yahoo data, no API key)
python -m scalper status     # balance, open positions, recent trades
```

On Windows you can double-click `run_paper.bat` instead.

The bot loads 500 bars of history per pair to warm up the indicators. After that it wakes 15 seconds after every 5-minute close, pulls the new candles, and trades them. It only enters inside the 16:00-19:00 New York window, so expect long quiet stretches. Stop it with Ctrl+C. The paper account is saved after every bar, and the next start picks up where it stopped.

### Try it offline first

```bash
python -m scalper paper --provider synthetic   # replays 30 days of made-up prices through the paper loop
python -m scalper backtest --synthetic         # same engine, straight backtest
```

Synthetic prices are random walks. They exercise the plumbing, and their P&L tells you nothing about the strategy.

### Backtesting on real data

Put one CSV per pair in `data/`, named after the pair (`data/USDCHF_M5.csv`, `data/CHFJPY.csv`, and so on). TradingView exports, MT4/MT5 exports, Dukascopy downloads and ISO-timestamp files all load. Then:

```bash
python -m scalper backtest                 # with the spreads from config/paper.yaml
python -m scalper backtest --no-costs      # zero costs, like the TradingView results above
python -m scalper backtest --trades-out trades.csv
```

For the cross pairs, also add the USD pair that prices the quote currency: `USDJPY` for CHFJPY, `USDCAD` for AUDCAD, `AUDUSD` for GBPAUD. Without those files the bot falls back to the fixed rates in `fx_fallback_rates`.

`python -m scalper fetch` downloads recent candles from the live feed into `data/`. Yahoo keeps about 60 days of 5-minute history, and OANDA returns up to 5000 bars per request.

### Data feeds

Set `feed.provider` in `config/paper.yaml`:

| provider    | what it is                                                                  |
|-------------|------------------------------------------------------------------------------|
| `yahoo`     | Default. Free Yahoo Finance chart API, no key. Unofficial, so it can lag or change without notice. |
| `oanda`     | OANDA v20 candles. Put your token in the `OANDA_API_TOKEN` environment variable (a free practice account is enough). The bot only reads candles from OANDA. |
| `csv`       | Replays the CSVs in `feed.data_dir` through the paper loop on a simulated clock. |
| `synthetic` | Replays generated prices on a simulated clock.                                |

Replays (`csv`, `synthetic`) write to `state/replay-<provider>/`, so they never touch the real paper account.

### How the Pine script maps to the bot

| Pine script                                              | Bot                                                   |
|----------------------------------------------------------|-------------------------------------------------------|
| `rsi` from `rma` of up/down moves, length 20; `sma(rsi, 5)` | `indicators.RSI`, `indicators.SMA` (Wilder smoothing seeded with an SMA, like Pine) |
| long: `crossunder(rsi_ma, 49)`; short: `crossover(rsi_ma, 55)` | `strategy.ReverseRSIStrategy`                  |
| EMA ribbon 4/10/15/19/25, fully stacked                  | same; long needs `close < ema4 < ... < ema25`         |
| session `1600-1900` in exchange time                     | `session: "1600-1900"`, `session_timezone: America/New_York` (TradingView's timezone for FXCM/OANDA forex) |
| `posSize = capital * 1% / (ATR*2*SLmult) / quoteUSD`     | `engine.Engine._on_signal`, rounded like Pine's `round`. Pine takes the quote currency's USD rate from the previous daily close; the bot uses the latest 5-minute close. |
| SL/TP from the signal bar's close, TP = 1.5x stop        | same                                                  |
| one position per pair (`position_size == 0`)             | same                                                  |
| CHF pairs skip 1-18 Jan 2015                             | same                                                  |

The indicator code is checked against StockCharts' published Wilder RSI table and against an independent reimplementation of the Pine rules (`tests/`).

### Paper broker fill rules

These follow TradingView's broker emulator so a paper session and a TradingView backtest line up:

* A signal on a bar's close becomes a market order that fills at the next bar's open.
* Stop loss and take profit are live from the fill bar on. Inside a bar the broker assumes price went open, high, low, close when the high is closer to the open than the low, and open, low, high, close otherwise.
* A bar that opens past a level fills at the open.
* Costs (TradingView's defaults have none): each fill pays half the configured spread. Market entries and stop exits also pay `slippage_pips`. Optional commission per 100k units.

### Files

| path                        | contents                                       |
|-----------------------------|------------------------------------------------|
| `config/paper.yaml`         | all settings, commented                        |
| `state/paper_state.json`    | balance, open positions, last processed bar    |
| `state/paper_trades.csv`    | every closed trade                             |
| `logs/paper.log`            | signals, fills, exits, hourly status (rotated) |

`python -m scalper reset --yes` starts a fresh account and moves the old files into a backup folder.

### Restarts and downtime

On restart the bot replays the bars it missed. Stops and targets that were hit while it was off get booked where they would have filled, since resting orders at a broker would have filled too. It does not open new trades on signals older than two bars.

To run it from a scheduler instead of leaving it open, call `python -m scalper paper --once` every 5 minutes. Each call processes the new bars and exits.

### Things to know before trusting the numbers

* The TradingView results above include no spread or commission. The session window spans the 17:00 New York rollover, when spreads on these pairs widen a lot, and a strategy with 1-2 ATR stops on M5 is sensitive to that. Compare `backtest` with and without `--no-costs` on real data before reading much into either.
* Yahoo and OANDA candles are mid prices. The broker adds half the spread to each fill instead of using real bid/ask quotes.
* Position size comes from the starting capital, as in the Pine script. Set `account.sizing_basis: equity` to compound instead.
* `risk.max_open_positions` and `risk.max_daily_loss_pct` are optional guard rails that the Pine script does not have.

### Tests

```bash
pip install pytest
python -m pytest -q
```
