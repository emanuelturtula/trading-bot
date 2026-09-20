"""Signal engine: one run of the pipeline per timeframe and scheduled candle close (spec 015).

``SignalEngine.run(timeframe, now)`` is the single entry point the scheduler (#15) calls, and
the only place where the data layer, ``domain/``, the repositories and the notifier meet: it
reads one configuration snapshot, fetches the closed candles of every enabled ticker, evaluates
its enabled rules, suppresses what the cooldown and the signal history say must not be sent
again, records what fires and notifies it exactly once.

- ``unit_of_work``: ``EngineRepositories`` and the ``UnitOfWork`` port the engine writes through.
- ``sql_unit_of_work``: the SQLAlchemy factory (#16 wires it); the only module of the package
  that names a ``Sql*`` repository or the ``Database`` handle.
- ``planning``: the frozen ``RunPlan``/``TickerPlan`` snapshot, ``plan_lookback`` and
  ``read_plan``.
- ``cooldown``: ``cooldown_decision`` and ``cooldown_decisions``, counted by candle position and
  never by a duration division.
- ``results``: ``RunReport``, ``TickerOutcome``, ``SignalOutcome`` and their enumerations.
- ``signal_engine``: ``SignalEngine`` itself.

No module here reads a clock: ``now`` is the scheduled candle close the caller passes, and every
stored instant comes from the repositories' injected clock. The package re-exports nothing.
"""
