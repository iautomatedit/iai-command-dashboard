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
| D Report | Signal counts per source, win rate, Kelly-sized P&L, per-share P&L after fees, and a verdict that says "no edge" when the data says so. | `report.py` |

Consensus signals size at **$0** until 20 of them have settled, because there
is no honest prior that copying wallets beats the market. They are still
logged and scored per share, so the edge (or lack of it) gets measured.

## Run it (Mac, beginner steps)

1. Open Terminal and go to this folder:
   `cd path/to/iai-command-dashboard/paper-trading`
2. Create your config: `cp config.example.json config.json`
3. Step 0 precheck. Every line must say `[OK]`:
   `python3 -m ptbot check`
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
