"""The command line, run with ``python -m trading_bot.cli`` (spec 013, §9; spec 014, §10).

One module per group — ``tickers``, ``rules``, ``assignments``, ``config``, ``signals`` and
``state`` — over the repositories of ``trading_bot.persistence``. The package re-exports nothing
and wires nothing into the application: it is a process of its own and it never migrates the
database (decision D95). It writes configuration only; the signal history and the bot state are
read-only here (decision D122). The bot still never places orders.
"""
