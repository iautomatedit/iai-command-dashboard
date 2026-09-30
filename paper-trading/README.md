# Paper Trading Bot (research only, never trades)

Observes real BTC prices and real Polymarket data, generates signals, and logs
hypothetical trades to a local SQLite file. It holds no keys, signs nothing,
and its HTTP layer can only send GET requests to a fixed list of public
market-data hosts (`ptbot/http.py`). A test fails the build if order or
key-handling code ever shows up in the source.

Python 3.9+ standard library only. Nothing to `pip install`.

## What it tests

| Part | Logic | Where |
|---|---|---|
| A1 Temporal arbitrage | Model P(Up) for the current Polymarket "BTC Up or Down 15m" market from live spot vs. window-open price and realized volatility. Signal when the all-in ask (fee included) is at least `min_edge` below that probability. | `strategy.fair_prob_up`, `engine.check_temporal` |
| A2 Complete sets | Legged: holding one side, buy the other when combined cost < $1 minus margin (locks profit). Instant: Up ask + Down ask < $1 right now. | `engine.check_complete_set` |
| A3 Kelly sizing | `f* = (p*b - q) / b`, quarter Kelly, capped at 5% of paper bankroll, capped again by the real size on the best ask. | `strategy.kelly_fraction` |
| B Wallet consensus | Only wallets whose own public trade history shows activity in the last 24h and 10+ trades in 7 days. Signal only when 2+ (configurable to 3) distinct wallets BUY the same outcome of the same market within 15 minutes. One wallet is never enough. | `consensus.py` |
| C Paper ledger | Entry at the real best ask when the signal fires, held to Polymarket's official resolution, P&L from the actual payout. | `ledger.py` |
| Backtest | Replays A1-A3 over past markets with no look-ahead. | `backtest.py` |
| Momentum | Daily time-series momentum on BTC/ETH vs. buy-and-hold. | `momentum.py` |
| Jev challenge | Jev's P(Up) scored vs. Polymarket's price. Measurement only. | `jev.py` |
| Dashboard | Local web view of everything above. | `dashboard.py`, `static/dashboard.html` |
| D Report | Signal counts per source, win rate, Kelly-sized P&L, per-share P&L after fees, and a verdict that says "no edge" when the data says so. | `report.py` |

Consensus signals size at **$0** until 20 of them have settled, because there
is no honest prior that copying wallets beats the market. They are still
logged and scored per share, so the edge (or lack of it) gets measured.

## Backtest first (hours, not weeks)

`python3 -m ptbot backtest --days 7`

Replays Part A over past BTC 15-minute markets using Polymarket's public price
history and Coinbase 1-minute candles. First run downloads roughly 700 markets
per week of history and takes several minutes; after that it is cached in
`bt_cache/`.

What it prints:
- **Calibration:** Brier score of our model vs. Polymarket's own price at every
  minute. If the market is as accurate as our model, there is no information
  edge, no matter what the P&L line says.
- **Per strategy:** win rate, P&L, and first half vs. second half. An edge
  that shows up in only one half is noise.
- **Verdict:** same rules as the live report.

Limits: Polymarket does not publish old order books, so the ask is modeled as
last price + `half_spread` (default 1 cent) and stakes are capped at
`max_stake_usd`. Real fills would likely be worse. Treat a backtest as the
best case. Wallet consensus is not backtested for the same reason.

If the backtest says no edge, don't bother paper trading that rule. If it
says maybe, the live paper run is the real test.

## Dashboard

`python3 -m ptbot dashboard`

Opens http://127.0.0.1:8765 in your browser. Runs on your machine only and
reads the paper ledger read-only, so leave it open while the bot runs. Ctrl+C
in Terminal stops it (the bot keeps running).

- **Scorecard:** every strategy, how it was tested, the evidence, and a verdict.
  Backtest rows fill in from your last `backtest` run.
- **Momentum:** growth of $1 vs. buy-and-hold for each lookback, the full
  metrics grid, and today's HOLD / CASH signal for BTC and ETH.
- **Live market:** our model's odds vs. Polymarket's price for the current
  15-minute market.
- **Wallet consensus:** tracked wallets' latest buys and any live consensus.
- **Paper trades and bankroll.** Trades staked at $0 are scored per share.
- **Research lab:** XP, levels and badges. They come only from research work:
  backtests run (logged to `results/lab_log.jsonl`), stress tests, strategies
  killed or surviving, days of live data, signals observed, trades settled.
  Nothing rewards trading more or winning. Profit earns zero XP on purpose.
- **Wallet radar:** tracked wallets around the edge, beams to UP or DOWN for
  buys in the last 15 minutes. A pole glows at consensus.
- **Buy-price ridge:** how cheap the cheaper side got in each recent market,
  plus the hindsight pair cost (cheapest Up + cheapest Down). Hindsight only.
- **Ticker tape and alerts:** live prices scroll across the top; wallet buys
  and consensus signals pop up bottom right.

Terminal dark theme by default, light theme via the toggle top right.

## Jev challenge (TypeSafe)

Every minute, the bot asks TypeSafe's Jev model one question: will the current
15-minute BTC market resolve Up? Jev sees only BTC facts (price to beat, spot,
recent one-minute closes), never Polymarket's price, so it can't just copy the
market. After Polymarket settles, each answer is scored against Polymarket's own
price and our volatility model.

It is measurement only and never opens a paper trade. The verdict needs 200+
settled windows and is tested per window (answers inside one window are not
independent). Jev only earns a trading role if it beats the market price.

Setup:
1. Get an API key from TypeSafe (https://typesafe.ai).
2. Add it to your shell once, never to `config.json`:
   `echo 'export TYPESAFE_API_KEY=your_key_here' >> ~/.zshrc && source ~/.zshrc`
3. Check it works: `python3 -m ptbot jev-test`
4. Turn it on in `config.json`: `"jev": {"enabled": true, "model": "jev-latest", "every_seconds": 60, "timeout_s": 2.0}`
   then pin the version: `jev-test` prints which version answered; put that
   exact name in `"model"` so a TypeSafe release can't split the test in two.
   Every prediction also records the version that answered, and only the
   latest version is scored.
5. Restart the bot. Results appear in `python3 -m ptbot report` and on the dashboard board.

Keep results private. TypeSafe's customer agreement reportedly bans publishing
benchmark or performance results (section 2.3(f)) and training other models on
Jev's answers (2.3(b)). Read the agreement when you sign up, and don't post these
numbers publicly (PULSE, X, anywhere).

Cost: one small request per minute (about 1,440 a day). Check TypeSafe's current
pricing; at their published per-token rate this is cents per day.

## Momentum backtest (daily BTC / ETH)

`python3 -m ptbot momentum`

Time-series momentum (Moskowitz, Ooi & Pedersen 2012): hold the coin when its
trailing return is positive, go flat when negative, size by volatility,
rebalance weekly, 0.5% cost per unit traded (Coinbase retail taker range).
Uses Coinbase daily closes since mid-2016, cached after the first run.

How to read it:
- **Buy & hold row first.** Momentum has to beat just holding the coin on
  Sharpe (return per unit of risk) or it is not worth running.
- **Every lookback is shown** (30, 90, 180, 365 days). If only one setting
  looks good, that is luck. A real effect shows up across most of them.
- **Split test:** must beat buy-and-hold in both halves.
- **Verdict** also checks whether the Sharpe is more than 2 standard errors
  from zero.

Settings live under `"momentum"` in `config.json` (`allow_short`,
`max_leverage`, `cost`, `lookbacks`). Leave leverage at 1.0.

## Live strategies on/off

`"strategies"` in `config.json`. Temporal and complete sets are **off** by
default: the stress-tested backtest (60s delay, 60s price freshness, 2 cent
spread) showed a negative edge. Wallet consensus is on.

Wallet selection now also requires `min_directional` (default 0.8): the
wallet must buy only one outcome in at least 80% of the markets it trades.
Two-sided wallets are market makers, and their buys are not signals.

## Run it (Mac, beginner steps)

1. Open Terminal and go to this folder:
   `cd path/to/iai-command-dashboard/paper-trading`
2. Create your config: `cp config.example.json config.json`
3. Step 0 precheck. Every line must say `[OK]`:
   `python3 -m ptbot check`
   Then run the backtest above before anything else.
4. Find genuinely active wallets from real trade data and save the top 5:
   `python3 -m ptbot discover --write`
   Look at the table it prints. Only rows marked `YES` get saved.
5. Start the bot and leave it running (it keeps running when the terminal is
   closed, and the Mac must stay awake; `caffeinate` handles that):
   `nohup caffeinate -i python3 -m ptbot run >> bot.log 2>&1 &`
6. Check on it any time: `tail -f bot.log` (Ctrl+C to stop watching)
7. Get the report: `python3 -m ptbot report`
8. Stop it: `pkill -f "ptbot run"`

For a real multi-week run, a laptop that sleeps will leave gaps. A $5/month
VPS (DigitalOcean, Hetzner) running the same command is the reliable option.
The ledger picks up where it left off after any restart.

## Known limitations (read before trusting any number)

- **Resolution source mismatch.** Polymarket settles these markets on the
  Chainlink BTC/USD stream. The bot models with Coinbase. Close, not identical.
  Settlement always uses Polymarket's official result, so P&L is real; only
  the model's probability carries this error.
- **Fees.** `taker_fee_rate` defaults to 2% of notional, which is deliberately
  conservative. Check Polymarket's current fee schedule for 15-minute crypto
  markets and set the real number in `config.json`.
- **Latency.** Polling every 10 seconds over REST. Real temporal arbitrage on
  these markets is contested by bots reacting in milliseconds. If the edge
  exists at all, a 10-second poller sees what is left over. That is part of
  what this test measures.
- **Fills.** Assumes you get the best ask up to its displayed size. No queue,
  no partial fills beyond that cap.
- **Market slug.** Assumes the `btc-updown-15m-<unix start>` slug format. If
  Polymarket changes it, `check` fails loudly; update `market_slug_prefix`.
- **Not yet run against live data.** Built and tested offline with mocked
  responses because the build environment's network blocked these APIs.
  Step 3 above is the first real validation.

## Tests

`python3 -m unittest discover -s tests -v`
