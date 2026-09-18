"""The configuration command line, run with ``python -m trading_bot.cli`` (spec 013, §9).

One module per group — ``tickers``, ``rules``, ``assignments`` and ``config`` — over the
repositories of ``trading_bot.persistence``. The package re-exports nothing and wires nothing
into the application: it is a process of its own, it never migrates the database (decision
D95), and it can only read and write configuration. The bot still never places orders.
"""
