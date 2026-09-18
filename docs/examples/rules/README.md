# Example rules

Three ready-to-import rules for the signal bot. **Not financial advice.** They are examples of
what the [rule schema](../../ARCHITECTURE.md#rule-model) can express, not a strategy: review,
adapt and back-check them before you let one notify you.

Every example ships **disabled**. Importing one changes nothing the bot evaluates until you
assign it to a ticker and enable it deliberately.

| File | Rule | Signal | Timeframe | Fires when |
|------|------|--------|-----------|------------|
| `rsi-oversold-in-uptrend.json` | RSI oversold in uptrend | `BUY` | `1d` | `rsi(14)` crosses below `30` **and** the close is above `sma(200)` |
| `macd-bearish-crossover.json` | MACD bearish crossover | `SELL` | `1d` | the MACD line crosses below its signal line **and** the close is below `ema(50)` |
| `bollinger-breakout-on-volume.json` | Bollinger breakout on volume | `BUY` | `1d` | the close crosses above the upper Bollinger band **and** volume is above `volume_sma(20)` |

The bot only notifies. It never places an order.

## The file format

Each file is a complete `config import` file: an envelope with a `version` and the optional
sections `tickers`, `rules` and `assignments`. A section that is **absent** is left alone, so
these rules-only files touch neither your tickers nor your assignments.

```json
{
  "version": 1,
  "rules": [
    {
      "enabled": false,
      "rule": {
        "name": "RSI oversold in uptrend",
        "signal": "BUY",
        "timeframe": "1d",
        "conditions": { "all": ["..."] },
        "cooldown_bars": 5
      }
    }
  ]
}
```

- `enabled` lives in the envelope because a rule document cannot carry it: the schema rejects
  unknown keys.
- A rule is addressed by its **name**, which is unique and compared exactly as written, so
  `daily` and `DAILY` are two different rules.
- Items carry no database identifier: the file is portable between databases.
- Import **merges and never deletes**. Removing a rule from the file does not remove it from the
  database; `rules remove` does.

## Importing an example

Check first with `--dry-run`, which does the whole unit of work and rolls it back:

```sh
docker compose --project-name trading-bot-prod exec -T app \
  python -m trading_bot.cli config import --dry-run - < docs/examples/rules/rsi-oversold-in-uptrend.json
docker compose --project-name trading-bot-prod exec -T app \
  python -m trading_bot.cli config import - < docs/examples/rules/rsi-oversold-in-uptrend.json
```

Re-running the same import writes nothing: an existing rule is skipped unless you ask for
`--on-conflict replace`.

## From imported to live

An imported example is inert until all four steps are done:

```sh
python -m trading_bot.cli config import - < docs/examples/rules/rsi-oversold-in-uptrend.json
python -m trading_bot.cli tickers add AAPL --timeframe 1d
python -m trading_bot.cli assignments add AAPL "RSI oversold in uptrend"
python -m trading_bot.cli rules enable "RSI oversold in uptrend"
```

A rule can only be assigned to a ticker of the **same timeframe**: these examples are `1d`, so
the ticker must be tracked on `1d`. `config export -` writes the whole configuration back as a
readable companion to the binary backup the deploy takes.

## Writing your own

Start from a copy of one of these files, change the name and the conditions, and import it. The
allowed indicators, parameters, operators and bounds are listed in
[Architecture](../../ARCHITECTURE.md#indicators): anything else is rejected with the path of the
offending field, and nothing is written. These three examples are imported by the test suite, so
a catalog change that invalidated one of them would fail the build instead of failing on the
Raspberry Pi.
