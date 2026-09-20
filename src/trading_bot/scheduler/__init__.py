"""When the bot thinks: one job per timeframe, fired at each real candle close (spec 016).

The package turns the market calendar into a schedule and each fire into exactly one
``SignalEngine.run`` for the candle that has just closed. It decides **when** and **how often**,
and nothing else: which tickers a run evaluates, whether a candle is closed and whether a signal
may be sent twice belong to the engine, the calendar and the persistence layer.

The modules, in dependency order:

- ``policy.py``: ``SchedulerPolicy``, the validated numbers every other module reads;
- ``trigger.py``: ``next_fire``, the pure schedule arithmetic, and the APScheduler trigger built
  on it;
- ``slots.py``: the predicate that skips the ``1h`` slots the provider never publishes;
- ``state.py``: the two short units of work that make "exactly once per close" durable;
- ``runner.py``: one fire of one timeframe, its retry window and its error boundary;
- ``service.py``: the ``AsyncIOScheduler`` and the job set.

It re-exports nothing, like ``data/`` and ``engine/``: import from the submodules. ``domain/``
is never imported the other way round, and nothing here is wired into the running application
yet (#16 does that).
"""
