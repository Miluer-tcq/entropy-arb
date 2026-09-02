# entropy-arb

**[中文文档 / Chinese documentation → README.zh-CN.md](README.zh-CN.md)**

Open-source two-venue perp arbitrage bot. One leg is always **Entropy**
(the `io` builder dex on Hyperliquid); the other leg — the hedge — is one of:

| `--hedge` | venue | quote | taker fee | protocol |
|---|---|---|---|---|
| `lighter` | Lighter mainnet | USDC | 0 bps | zkLighter ws (diff books, async settle) |
| `lighter-rh` | Lighter Robinhood chain | **USDG** | 0 bps | zkLighter ws |
| `tradexyz` | Hyperliquid trade.xyz dex | USDC | ~1 bps | HL l2Book, sync IOC settle |

> **Referral links** — signing up through these supports this project:
> - Entropy — Tier 4 referral, 100% rebates: <https://entropy.io/?r=miluer>
> - Lighter: <https://app.lighter.xyz/?referral=MILUER&source=none>

## What this fork adds

- **Single-instance lock** — a second live start exits cleanly; stale lock
  files from crashes are detected and recovered (Windows-safe).
- **`--preflight` go/no-go startup check** — keys, market status, margin vs
  caps (incl. Hyperliquid **unifiedAccount**: free spot USDC counts),
  auto-discovered taker fees, recorder freshness and midline drift.
- **Session-aware thresholds** — `by_session` with an optional per-session
  `midline_bps` (e.g. wider band + lower center during US hours), weekends
  automatically fall back to the global band.
- **Auto fees & funding** — taker fee and funding rate are pulled from each
  exchange at runtime (config value only as fallback), shown in the TUI.
- **TUI dashboard** — exact terminal fill at any height, CJK-safe log
  wrapping, session panel with live trading-session tag and 1h midline
  drift (English / 中文 via `--cn`).
- **Data-driven thresholds** — `auto_midline` / `auto_band` re-anchor the
  center and band edges from the stable-regime window each minute; a slow
  drift freezes the tuners, arms a drift-lock (no opens against travel) and,
  after `auto_frozen_fallback_min`, re-anchors from the last 60 min instead
  of trading a stale seed.
- **Execution safety gates** — a `min_net_edge_bps` floor (never open below
  post-fee expected edge, closes exempt), a `max_top_premium_bps` ceiling
  (big "spreads" that only fill one leg are skipped) and `maker_enabled`:
  ceiling rejects rest a passive order on the lagging venue and only take
  the hedge after it prints — capturing fat spreads with maker economics or
  cancelling at zero cost.
- **Risk & exit** — realized/unrealized PnL ledger (fees and hedge slippage
  land in `realized` the moment they are paid), a UTC-day loss breaker that
  flattens and halts, and band-decoupled exits (`reversion_close_bps`,
  `timeout_close_min`) so a drifted anchor can never strand inventory.
- **Resilience** — Hyperliquid `/info` pacing+cache (429-safe), a shared
  nonce allocator, venue-outage pause/probe, reconcile that never overwrites
  fresh fills, and a Lighter settle that waits for the account stream to be
  ready before it can be false-"unresolved".
- **Data tooling** — 1-minute recorder, session-split analyzer
  (`tools/analyze.py`), band backtester with live-mirroring gate simulation
  (`tools/backtest.py`), a funding-vs-basis check (`tools/funding_check.py`),
  drift monitor module — all tested (119 pytest cases).
- **Windows wrapper** — `run-live.ps1`: preflight-gated start + auto-restart
  with clean exit-code handling.

When the same symbol trades rich on one venue and cheap on the other, the bot
simultaneously sells the rich book and buys the cheap book — by default with
taker orders, or with a passive rest when the gap is too wide to be real on a
cross — carrying a delta-neutral position until the premium reverts and the
opposite crossing unwinds it. Every price it acts on is the **actual order
book of the exchange that will fill the order** — Hyperliquid books come from
the official websocket (`wss://api.hyperliquid.xyz/ws`), Lighter books from
Lighter's official websocket.

While it runs — even with no credentials and no strategy — it records both
books to **1-minute CSV bars**, and the bundled analyzer turns that data into
the three numbers that define the whole strategy.

## The signal

The band is three numbers in `config.yaml`, derived by you from recorded
data:

```
premium_bps = (Entropy price / hedge price − 1) × 10 000

                          ┌──────────────  SELL entropy + BUY hedge
midline + upper  ───────────────────────────────────────────────────
                                       ▲
midline          ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┼ ─ ─   the premium's usual level
                                       ▼
midline − lower  ───────────────────────────────────────────────────
                          └──────────────  BUY entropy + SELL hedge
```

- `midline_bps` — where the premium normally sits. Cross-venue premiums are
  rarely centered at zero (different oracles, different quote assets, listing
  premia), so a zero-centered band would fire one direction only, cap out and
  never unwind. Measure where the premium actually sits and type it in.
- `upper_bps` / `lower_bps` — the entry bands on each side of the midline.

Both hurdles are applied to **executable** prices (entropy bid vs hedge ask,
and vice versa) and are **net of both venues' taker fees** — the engine adds
fees on top before a slice qualifies. A full round trip therefore nets
**≥ upper + lower bps after fees by construction**.

One consequence worth understanding: with `midline_bps: 5`, the buy-entropy
hurdle is `lower − midline`, which can be **negative**. That is intentional —
if entropy is persistently 5 bps rich, buying it at a 0 bps premium is 5 bps
cheap versus its own equilibrium, and that trade is the profitable unwind of
an earlier sell at `midline + upper`. It also means a **wrong midline loses
money**: if you type `midline_bps: 5` while the true premium sits at 0, the
bot happily buys entropy at fair value all day. Measure first, then trade —
that is what the recorder and analyzer are for.

## Quick start

```bash
git clone https://github.com/Miluer-tcq/entropy-arb.git && cd entropy-arb
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # data collection needs only this

cp config.example.yaml config.yaml       # the strategy (thresholds, sizing, risk)
cp .env.example .env                     # credentials — required to trade
```

The markets are **not** in the config file — you state them explicitly on
every start: `--symbol` (traded on both venues) and `--hedge` (one of
`lighter`, `lighter-rh`, `tradexyz`; Entropy is always the
other leg).

There is **no paper mode** — the bot either collects data (`--record-only`)
or trades live. Validate with recorded data and tiny position caps, not with
simulated fills.

**1. Collect data first** (no credentials needed):

```bash
python3 main.py --record-only --symbol SNDK --hedge lighter-rh
```

Let it run for at least a few hours (a day is better — premiums have
intraday regimes). It writes `logs/minutes.csv`.

**2. Analyze and set your thresholds:**

```bash
python3 tools/analyze.py
```

It prints the premium distribution, how often each candidate band would have
fired, and a ready-to-paste `thresholds:` block for `config.yaml`.

**3. Go live** — fill in `.env`, install the signing SDKs, and start with
the smallest position caps that clear the venue minimums:

```bash
pip install -r requirements-live.txt
python3 main.py --symbol SNDK --hedge lighter-rh
```

Running without `--record-only` sends real orders immediately once both
feeds are fresh and the band is crossed.

**Dashboard.** On a terminal the bot shows a live Rich dashboard: both
books with age/spread, positions and caps, equity and session PnL, the
executable premium of each direction against its full hurdle (fees and
inventory surcharge included, ● = armed), recorder progress, the last
executions, and a tail of the log (the full log goes to `logging.file`,
default `logs/engine.log`). It works in `--record-only` too. Add `--cn` to
display the dashboard in Chinese. Use `--no-dashboard` for plain console
logs (nohup/systemd — off-terminal runs fall back automatically), or set
`logging.dashboard: false`.

## Data collection & analysis

The recorder runs automatically in every mode (`recorder.enabled: true`).
Once per second it samples both live books; once per minute it writes a row:

| column | meaning |
|---|---|
| `minute_ts`, `time_utc` | minute start (epoch seconds, ISO UTC) |
| `entropy_bid/ask`, `hedge_bid/ask` | last fresh top-of-book of the minute |
| `premium_open/high/low/close/mean/std_bps` | mid-to-mid premium of Entropy over the hedge |
| `sell_edge_mean/max_bps` | executable premium for SELL entropy (entropy bid / hedge ask − 1) |
| `buy_edge_mean/max_bps` | executable premium for BUY entropy (hedge bid / entropy ask − 1) |
| `samples` | how many of the ~60 seconds both books were fresh |

Recorded edges are pre-fee; the analyzer subtracts `--fees-bps` (pass the
**sum** of both venues' taker fees — default 0.0 for the zero-fee venues,
~1.0 with a `tradexyz` hedge) before counting firings, so its table and
suggestions translate directly into config values. `--hours 24` restricts to
recent data; premiums drift, so re-run it regularly and update
`config.yaml`.

## Configuration

Strategy lives in `config.yaml` (validated — unknown keys are startup
errors), credentials in `.env`, and the markets on the command line
(`--symbol`, `--hedge`). Full commented reference:
[config.example.yaml](config.example.yaml). The essentials:

| key | meaning | default |
|---|---|---|
| `thresholds.midline_bps` | premium center (measure it!) | — |
| `thresholds.upper_bps` / `lower_bps` | entry bands (> 0) | — |
| `entropy.dex` | Entropy's dex name on Hyperliquid | `io` |
| `*.taker_fee_bps` | per-venue taker fee | 0.0 (tradexyz hedge: 1.0) |
| `*.max_position_usd` | per-venue position cap | 1000 |
| `*.max_orders_per_min` | per-venue send budget (sliding 60 s) | 120; lighter hedges 30 |
| `sizing.take_fraction` | fraction of crossable depth taken | 0.5 |
| `sizing.max_order_notional_usd` | per-slice cap | 500 |
| `inventory.scale_bps` / `floor_frac` | inventory ladder (extra bps past `floor_frac` of the cap) | 10 / 0.5 |
| `execution.premium_persist_sec` | edge must persist before firing | 0.3 |
| `execution.daily_max_loss_usd` | UTC-day realized+unrealized loss that flattens both legs and halts (0 = off) | 0 |
| `execution.reversion_close_bps` / `timeout_close_min` | band-decoupled exits: force-close inventory once premium is within `|premium − midline| ≤ bps`, or after N minutes held | 0 / 0 |
| `execution.min_cross_rounds` | refuse opens unless book depth crosses ≥ N venue minimums (closes exempt) | 0 |
| `execution.*` | slippage bounds, timeouts, reconcile cadence… | see file |
| `recorder.*` | minute-data recorder | on, `logs/minutes.csv` |
| `logging.dashboard` / `logging.file` | Rich dashboard on a tty; log file while it runs | on, `logs/engine.log` |

Data-driven threshold keys (all inside `thresholds:`, all opt-in):

| key | meaning | default |
|---|---|---|
| `auto_midline` | re-anchor `midline_bps` from the stable-window median each minute | false |
| `auto_midline_clamp_bps` | max distance from the manual anchor (the manual value stays the prior) | 3.0 |
| `auto_band` / `auto_band_trigger_pct` | size `upper/lower` so each side fires on ~this % of minutes | false / 10.0 |
| `auto_band_floor_bps` / `auto_band_ceiling_bps` | band size limits | 2.0 / 8.0 |
| `auto_frozen_fallback_min` | after the drift-freeze lasts this long, re-anchor from the last 60 min instead of trading a stale seed | 30 |
| `min_net_edge_bps` | hard floor on post-fee expected edge for **opens** (closes exempt) | 0 |
| `max_top_premium_bps` | sanity ceiling: "spreads" this wide are stale books, not opportunity | 0 |
| `maker_enabled` / `maker_wait_sec` | ceiling rejects rest a passive order on the lagging venue; hedge only if it prints | false / 3.0 |

## Credentials (`.env`, live only)

- **Entropy / tradexyz (Hyperliquid)** — create an API ("agent") wallet at
  <https://app.hyperliquid.xyz/API>. `HL_PRIVATE_KEY` is the **agent** key,
  `HL_ACCOUNT_ADDRESS` your main account address. With `--hedge tradexyz`
  both legs share this account by default (one nonce sequence is handled
  internally); set `HL_PRIVATE_KEY_XYZ` / `HL_ACCOUNT_ADDRESS_XYZ`
  to split them. Fund the dex-specific clearinghouses you trade.
- **Lighter** — `LIGHTER_ACCOUNT_INDEX`, `LIGHTER_API_KEY_INDEX`,
  `LIGHTER_API_PRIVATE_KEY`, registered on the **same deployment** as your
  `--hedge` flag (mainnet and the Robinhood chain are separate accounts and
  keys — see [lighter-python](https://github.com/elliottech/lighter-python)).

## How execution works

- Both legs are normally **taker** orders sent concurrently: Lighter market
  orders with average-price protection settling on the authenticated account
  websocket; Hyperliquid IOC limits settling synchronously (with
  orderStatus polling for unknown outcomes).
- **Safety gates** between signal and send: a `min_net_edge_bps` floor (no
  open below post-fee expected edge — closes always exempt), a
  `max_top_premium_bps` ceiling (a gap too wide to survive the trip to the
  exchange is a stale book, not an opportunity), and `min_cross_rounds`
  (book too thin to hedge cleanly).
- **Maker ladder** (`maker_enabled`): instead of discarding a ceiling
  reject, the bot rests a **passive** order on the lagging venue at its
  stale price and only takes the hedge once that order prints. A real
  mispricing pays with maker economics; a fast move simply never fills it
  and it is cancelled at zero cost — and because the second leg is never
  touched before the first prints, there is no naked-leg window.
- A **persistence gate** (`premium_persist_sec`) arms each direction and only
  fires if the edge survives — one-tick phantoms are filtered.
- **Inventory ladder**: past `floor_frac` of a venue's cap, adding to the
  position requires linearly more edge, up to `scale_bps` extra at the cap.
- **Decoupled exits**: with `reversion_close_bps` / `timeout_close_min`,
  closing inventory does not wait for the entry band — once the premium is
  back at the midline or a hold times out, the unwind fires at a fee-only
  hurdle, so a drifted anchor can never strand a position.
- **Net-delta hedge**: if legs fill unevenly, the imbalance is immediately
  reduced (reduce-only, price-protected), and positions are reconciled
  against the chain every `reconcile_sec`.
- **Failure containment**: a rate-limited venue pauses briefly; an
  unreachable venue (e.g. exchange maintenance) pauses trading and is probed
  every `venue_probe_sec` until it recovers; `max_consecutive_errors`
  execution pathologies halt the engine entirely; `daily_max_loss_usd`
  flattens both legs and halts on a bad UTC day.
- **Live-only**: there is no simulated-fill mode. `--record-only` is the
  risk-free way to run it; anything else trades real money.

## Layout

```
main.py                  entry point (--record-only, or live by default)
entropy_arb/config.py    YAML + .env contract, validation
entropy_arb/book.py      order books + fee-aware crossing/sizing math
entropy_arb/feeds.py     official HL ws + zkLighter ws book feeds (+ silence watchdog)
entropy_arb/venue_hl.py  Hyperliquid dex adapter (Entropy, tradexyz) + maker/resting orders
entropy_arb/venue_lighter.py  zkLighter adapter (mainnet, Robinhood chain)
entropy_arb/engine.py    the two-venue strategy loop, gates, maker ladder, risk checks
entropy_arb/ledger.py    per-venue avg-cost PnL ledger (realized + fees)
entropy_arb/monitor.py   stable-window, drift report, auto midline/band targets
entropy_arb/preflight.py --preflight go/no-go checks
entropy_arb/dashboard.py Rich terminal dashboard
entropy_arb/recorder.py  1-minute orderbook bars
tools/analyze.py         minutes.csv -> suggested thresholds
tools/backtest.py        band replay with live-mirroring gate simulation
tools/funding_check.py   is the premium funding carry or structural basis?
tools/session_stats.py   per-session premium stats from the recorder CSV
tests/                   python3 -m pytest tests/
```

## Known risks

- **A wrong midline is a losing strategy.** The premium center drifts;
  re-measure regularly and keep `config.yaml` current (or run
  `auto_midline`/`auto_band` and read the drift-lock logs).
- **USDG basis** (`lighter-rh`): the hedge quotes in USDG. Part of any
  persistent premium is the stablecoin itself; your midline absorbs the
  level, but a USDG *move* is real PnL.
- **Funding**: two venues, two independent funding rates; carry is not
  modeled. Position caps bound it — keep them modest. (Measured on
  entropy-io:SNDK vs Lighter, funding contributes ~0.6 bps/day while the
  basis sits at −4 bps: this is a liquidity basis, not carry — run
  `tools/funding_check.py` before assuming otherwise on another market.)
- **Thin books**: Entropy depth can be tiny; `take_fraction` and notional
  caps keep clips small, but slippage on the hedge leg after a partial fill
  is real.
- **Market hours**: for equity perps (e.g. SNDK), off-hours oracle regimes
  differ per venue; consider wider bands or not trading them.
- **One-leg risk**: a leg can fail after the other filled. The bot hedges
  and reconciles automatically, but you should still watch it.

Use at your own risk. This is trading software operating with real money;
nothing here is investment advice. Start with tiny position caps.

## License

[MIT](LICENSE)
