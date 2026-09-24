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

A one-month backtest on real prices takes two commands:

```bash
python -m scalper fetch                 # last 45 days of M5 candles into data/
python -m scalper backtest --days 30    # trade the last 30 days; the 15 days before warm up the indicators
```

`fetch` can pull from three places:

| command                                  | source                                                     |
|------------------------------------------|------------------------------------------------------------|
| `python -m scalper fetch`                | Yahoo (default). Free, but Yahoo sometimes refuses with HTTP 429, especially from servers and VPSs. |
| `python -m scalper fetch --provider dukascopy` | Dukascopy's free historical data, no key. One file per pair per day; Dukascopy throttles fast downloads, so a year takes a few minutes per pair. Finished days are cached in `data/.cache`, so if some days fail, running the same command again only fetches those. Prices are bid, not mid. |
| `python -m scalper fetch --provider oanda`     | OANDA, needs `OANDA_API_TOKEN` (free practice account). |

Useful variations:

```bash
python -m scalper backtest --days 30 --no-costs     # zero costs, like the TradingView results above
python -m scalper backtest --days 30 --trades-out trades.csv
python -m scalper backtest --days 30 --log-level INFO   # print every signal, fill and exit
```

Without `--days` the backtest uses every bar in `data/`. Yahoo keeps about 59 days of 5-minute history, so `fetch --days 59` is the most it can give; Dukascopy and OANDA go back further.

#### Stop-size filter

On real data the costs decide the result: stops in the quiet Sydney hours are only a few pips, so a 1.5-3 pip spread eats a big share of each one. The per-pair table shows this in its `Stop` and `Spread/stop` columns.

`risk.min_stop_spread_ratio` skips any signal whose stop is smaller than that many spreads. It is off by default, which keeps the bot identical to the Pine script. Compare settings in one run:

```bash
python -m scalper backtest --days 360 --min-stop-spread 0 3 4 5 6 8
```

Pick a value from a range of settings that all do well, not the single best row. Then check it on data it was not chosen on: choose on the older half with `--until`, and confirm on the recent half.

```bash
python -m scalper backtest --until 2026-03-24 --days 180 --min-stop-spread 0 3 4 5 6 8   # choose here
python -m scalper backtest --days 180 --min-stop-spread 5                                 # then confirm here
```

Every single backtest also splits the results by direction and by signal time (30-minute slots, New York time). Watch the 17:00 rollover: spreads jump to 10-30 pips for a few minutes, and in bid-only data such as Dukascopy's that looks like a sharp dip the strategy loves to buy. Profits piled up in LONG trades around 17:00 are that artifact, not an edge. To test without it:

```bash
python -m scalper backtest --days 360 --min-stop-spread 0 3 4 5 6 --no-entry 1645-1730
```

The shipped `config/paper.yaml` turns both on (`min_stop_spread_ratio: 4`, `no_entry_windows: ["1630-1800"]`), based on a year of Dukascopy data: with them the backtest made PF 1.5 over 158 trades instead of an inflated PF 1.9 that got half its profit from the rollover. The last six months were much weaker than the six before, so treat the paper results as the real test. Set them to `null` and `[]` to trade exactly like the Pine script.

The `--min-stop-spread` and `--no-entry` flags override them for a single backtest (`--min-stop-spread 0` and `--no-entry none` switch them off). A `--no-costs` backtest still applies the filter with the configured spreads, so it takes the same trades as the run with costs.

#### Using data exported from TradingView

TradingView can export the candles on a chart as CSV, and the bot reads that format directly. For each of USDCHF, CHFJPY, AUDCAD and GBPAUD, plus USDJPY, USDCAD and AUDUSD (used to convert profits to USD):

1. Open the pair on a 5-minute chart, for example `FX:USDCHF` (FXCM, the feed the README results used).
2. Scroll left until at least 45 days are loaded.
3. Open the layout menu (the arrow next to the layout name, top right) and pick "Export chart data...". Keep the default time format.
4. Save the file into the `data` folder. The default name, such as `FX_USDCHF, 5.csv`, is fine.

Then run `python -m scalper backtest --days 30`. Export availability depends on your TradingView plan.

Any other CSV with time/open/high/low/close columns also works (MT4/MT5 exports, Dukascopy downloads, ISO timestamps). Name it after the pair, for example `data/USDCHF_M5.csv`. Without the three USD pairs the bot falls back to the fixed rates in `fx_fallback_rates`.

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
| session `1600-1900` in exchange time                     | `session: "1600-1900"`, `session_timezone: America/New_York` (TradingView's timezone for FXCM/OANDA forex). Monday to Friday only, because Pine v4 sessions default to weekdays; this skips the Sunday weekly open. |
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

If you remove a pair from `symbols` while the account still has a trade open in it, the bot refuses to start and tells you. Add the pair back until the trade closes, or reset the account.

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
